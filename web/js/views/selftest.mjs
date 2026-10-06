import { legacy, v2, raw, balance, orderbook } from '../api.mjs';
import { esc, cut, num } from '../format.mjs';
import { loadSession, on, off } from '../store.mjs';
import * as sse from '../sse.mjs';
import { fetchFx, FX_PATH, defaultPriceFor } from '../fx.mjs';

// Selftest view (#/selftest?auto=1): in-page contract assertions against the
// connected daemon (mock or real). PASS rows are binding contract; INFO rows
// document provisional shapes (their bodies are how provisional mock shapes
// get pinned against a real daemon); WARN rows are degraded but not fatal.
// Not in the nav on purpose — a developer tool reachable by URL only.

const rows = [];

export function render(root) {
  const query = new URLSearchParams(location.hash.split('?').slice(1).join('?'));
  root.innerHTML = `
    <div class="page">
      <div class="panel-head">
        <h2>selftest</h2>
        <button id="t-run" class="btn small">run all</button>
      </div>
      <div id="t-sum" class="muted">idle — PASS = binding · INFO = shape record · WARN = degraded</div>
      <div id="t-out"></div>
    </div>`;
  document.getElementById('t-run').addEventListener('click', run);
  if (query.get('auto') === '1') run();
}

function record(name, status, detail, rawBody) {
  rows.push({ name, status, detail: detail || '', rawBody: truncate(rawBody) });
  paint();
}

const truncate = (v) => {
  if (v === undefined || v === null || v === '') return '';
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s.length > 400 ? s.slice(0, 400) + ' …' : s;
};

function paint() {
  const out = document.getElementById('t-out');
  const sum = document.getElementById('t-sum');
  if (!out) return;
  const n = { PASS: 0, FAIL: 0, WARN: 0, INFO: 0 };
  rows.forEach((r) => { n[r.status] += 1; });
  if (sum) {
    sum.innerHTML = `<span class="pass-line">PASS ${n.PASS}</span> · FAIL ${n.FAIL} · ` +
      `WARN ${n.WARN} · INFO ${n.INFO}` +
      (n.FAIL ? ' — <span class="err-text">contract broken</span>' : ' — contract holds');
  }
  out.innerHTML = rows.map((r) => `
    <details class="st-row st-${r.status.toLowerCase()}">
      <summary><span class="tag st-tag">${r.status}</span> ${esc(r.name)}${r.detail ? ` <span class="muted">— ${esc(r.detail)}</span>` : ''}</summary>
      ${r.rawBody ? `<pre class="st-raw">${esc(r.rawBody)}</pre>` : ''}
    </details>`).join('');
}

const errLabel = (e) => ((e && e.rpcType) ? e.rpcType + ': ' : '') + ((e && e.message) || e);

/**
 * Wrap one step. The step function returns:
 *   true / {note, body}              -> PASS (body recorded)
 *   '…'                              -> WARN with that text
 *   false                            -> FAIL
 * and throws -> FAIL, unless expectFail, then PASS (mock-accepted -> WARN).
 */
async function step(name, fn, { expectFail = false, infoLevel = false } = {}) {
  try {
    const observed = await fn();
    if (observed === false) {
      record(name, 'FAIL', 'assertion failed');
      return;
    }
    if (typeof observed === 'string') {
      record(name, 'WARN', observed);
      return;
    }
    const isObj = observed && typeof observed === 'object';
    record(name, isObj || observed === true ? 'PASS' : 'INFO',
      isObj ? observed.note : '', isObj ? observed.body : observed);
  } catch (e) {
    if (expectFail) record(name, 'PASS', 'rejected as expected (' + errLabel(e) + ')', e);
    else record(name, infoLevel ? 'INFO' : 'FAIL', errLabel(e), e);
  }
}

function listTickers(coins) {
  return (coins || []).map((c) => c.ticker || c.coin);
}

async function run() {
  rows.length = 0;
  paint();
  const query = new URLSearchParams(location.hash.split('?').slice(1).join('?'));
  const session = loadSession();
  if (!session) {
    record('session', 'FAIL', 'no session — connect first (#/connect)');
    return;
  }
  record('daemon', 'INFO', session.url + ' (version ' + session.version + ')');

  // 1 reachability: public version, no password — a real kdf answers this.
  await step('version (public, no userpass)', async () => {
    const r = await raw(session.url, { method: 'version' });
    const res = r.result;
    return { note: typeof res === 'string' ? res : JSON.stringify(res), body: r };
  });

  // 2 auth: a real daemon rejects the wrong password; the mock accepts any —
  // accepted == WARN, rejected == PASS.
  await step('wrong password rejected', async () => {
    const r = await raw(session.url, { userpass: 'definitely-not-the-password', method: 'get_enabled_coins' });
    const list = r.result;
    if (Array.isArray(list) && list.length) return 'mock-style: wrong password returned data — WARN';
    return true;
  }, { expectFail: true, infoLevel: true });

  // 3 enabled coins
  let coins = [];
  await step('get_enabled_coins', async () => {
    const r = await legacy('get_enabled_coins');
    coins = listTickers(r.result || []);
    return Array.isArray(r.result) && r.result.length
      ? { note: coins.length + ' coins: ' + coins.join(', '), body: r.result } : false;
  });

  if (coins.length) {
    // 4 balance of the first coin
    await step('my_balance(' + coins[0] + ')', async () => {
      const r = await balance(coins[0]);
      const b = r.result;
      return b && b.balance !== undefined
        ? { note: numRaw(b.balance) + ' @ ' + cut(b.address || '', 6, 4), body: b } : false;
    });

    // 5 orderbook of the first pair (asks/bids arrays must EXIST; may be empty)
    const base = coins[0];
    const rel = coins[1] || coins[0];
    await step('orderbook ' + base + '/' + rel, async () => {
      const r = await orderbook(base, rel);
      const ob = r.result;
      return ob && Array.isArray(ob.asks) && Array.isArray(ob.bids)
        ? { note: ob.asks.length + ' asks / ' + ob.bids.length + ' bids', body: ob } : false;
    });
  }

  // 5b unified price estimate: electrs' rich-shape rates file via elek-web,
  // same origin (doc/elektron.md §13). Not a daemon call — 404 == WARN
  // (feature off by env, not broken); a 200 that fails the fetchFx trust gate
  // is a broken contract == FAIL; a valid snapshot is binding == PASS.
  await step(FX_PATH + ' (unified price estimate)', async () => {
    let resp;
    try {
      resp = await fetch(FX_PATH, { cache: 'no-cache' });
    } catch {
      return 'fetch failed — elek-web itself down?';
    }
    if (!resp.ok) return 'endpoint off — HTTP ' + resp.status + ' (no MM_WEB_FX_RATES), rate line hidden';
    const snap = await fetchFx();
    if (!snap) return false;   // 200 but untrusted: the gate rejects it
    const market = snap.market
      ? ' market=' + snap.market.pair
        + ' ask=' + (snap.market.bestAsk ?? '—') + ' bid=' + (snap.market.bestBid ?? '—')
      : '';
    return { note: 'usd=' + snap.usd + ' eur=' + snap.eur + ' src=' + snap.source + market, body: snap };
  }, { infoLevel: true });

  // 5c price-field default derivation (trade view): same precedence as
  // electrs itself — the real book beats the estimate; the estimate prices
  // BTC-family rels over usd_per_btc; unknown pairs and missing rates stay
  // empty (nothing invented). Pure function, so the shapes are synthetic —
  // the 5b snapshot above pins fetchFx's normalized shape they mirror.
  await step('price default derivation (defaultPriceFor)', async () => {
    const book = {
      usd: 200, eur: 180, usdPerBtc: 100000, time: 1, source: 'p2p_market',
      market: { pair: 'ELEK/TBTC', bestBid: 0.0009, bestAsk: 0.0011, mid: 0.00099, asks: 3, bids: 3 },
    };
    const est = { usd: 200, eur: 180, usdPerBtc: 100000, time: 1, source: 'registry', market: null };
    const checks = [
      ['book pair -> real mid', defaultPriceFor(book, { base: 'ELEK', rel: 'TBTC' }) === num(0.00099, 8)],
      ['book pair w/o mid -> empty', defaultPriceFor({ ...book, market: { ...book.market, mid: null } }, { base: 'ELEK', rel: 'TBTC' }) === ''],
      ['estimate -> btc-family cross', defaultPriceFor(est, { base: 'ELEK', rel: 'TBTC' }) === num(0.002, 8)],
      ['estimate -> non-btc rel -> empty', defaultPriceFor(est, { base: 'ELEK', rel: 'ETH' }) === ''],
      ['no snapshot -> empty', defaultPriceFor(null, { base: 'ELEK', rel: 'TBTC' }) === ''],
      ['zero usd -> empty', defaultPriceFor({ ...est, usd: null }, { base: 'ELEK', rel: 'TBTC' }) === ''],
    ];
    const bad = checks.filter(([, ok]) => !ok).map(([n]) => n);
    if (bad.length) throw new Error(bad.join('; '));
    return { note: checks.length + ' precedence checks', body: checks.map(([n]) => n) };
  });

  // 6 provisional shape: my_orders — the recorded body pins the real shape
  await step('my_orders (shape record)', async () => {
    const r = await legacy('my_orders');
    const res = r.result || {};
    const n = Array.isArray(res.orders) ? res.orders.length : '??';
    return { note: 'type=' + (res.type ?? '??') + ' orders=' + n + ' (provisional)', body: r };
  });

  // 7 v2 active_swaps
  await step('active_swaps (v2 include_status)', async () => {
    const res = await v2('active_swaps', { include_status: true });
    return res && Array.isArray(res.uuids)
      ? { note: res.uuids.length + ' active', body: res } : false;
  });

  // 8 v2 my_recent_swaps
  await step('my_recent_swaps (v2, limit 10)', async () => {
    const res = await v2('my_recent_swaps', { limit: 10, include_status: true });
    return res && Array.isArray(res.swaps)
      ? { note: res.swaps.length + ' swaps', body: res } : false;
  });

  // 9 v2 my_swap_status with a bogus uuid — how the daemon reacts is itself
  // the record (error form vs null result vs empty events)
  await step('my_swap_status(bogus uuid) (shape record)', async () => {
    const res = await v2('my_swap_status', { uuid: '00000000-0000-0000-0000-000000000000' });
    return { note: 'succeeded — ' + JSON.stringify(res).slice(0, 120), body: res };
  }, { infoLevel: true });

  // 10 withdraw error channel: an invalid target must be rejected and must
  // not produce a transaction. The recorded error form documents the channel.
  await step('withdraw invalid target (error channel)', async () => {
    if (!coins.length) return 'no coins — skipped';
    const res = await v2('withdraw', {
      coin: coins[0], to: 'not-an-address', amount: '0.0001', broadcast: false,
    });
    return 'withdraw accepted an invalid target — WARN';
  }, { expectFail: true, infoLevel: true });

  // 10b coins error channel: activating a ticker that cannot exist must be
  // rejected — an invalid charset can never be a config coin, so nothing
  // stateful happens on either the mock or the real daemon.
  await step('electrum invalid ticker (error channel)', async () => {
    await legacy('electrum', { coin: 'NoSuchCoin!!', servers: [{ url: '127.0.0.1:99999' }] });
    return 'a daemon accepted an invalid-ticker activation — WARN';
  }, { expectFail: true, infoLevel: true });

  // 10c disable_coin error channel: deactivating an unknown ticker must error
  await step('disable_coin unknown ticker (error channel)', async () => {
    await legacy('disable_coin', { coin: 'NoSuchCoin!!' });
    return 'disable_coin accepted an unknown ticker — WARN';
  }, { expectFail: true, infoLevel: true });

  // 11 SSE round-trip: enable a stream, count events for 3s, unsubscribe.
  // Needs event_streaming_configuration on a real daemon; without it the
  // endpoint 500s (recorded, not fatal). SKIPPED in auto mode (headless
  // verification): an open EventSource keeps one request pending forever and
  // dump-dom waits for network idle — observed twice, with and without the
  // gate on navigator.webdriver (which plain chromium headless never sets).
  // In a manual browser session the check runs; the wire format is also
  // proven at the curl layer (web/test/README.md).
  if (query.get('auto') === '1') {
    record('sse round-trip (orderbook stream)', 'WARN', 'skipped in auto mode — open transport blocks headless dump-dom; SSE proven via curl');
  } else {
    await step('sse round-trip (orderbook stream)', () => sseRoundTrip(coins), { infoLevel: true });
  }
}

async function sseRoundTrip(coins) {
  if (!coins.length) return { note: 'no coins — skipped', body: '' };
  const base = coins[0];
  const rel = coins[1] || coins[0];
  let got = 0;
  const counter = () => { got += 1; };
  on('sse:orderbook', counter);
  try {
    await sse.orderbook(base, rel);
    await new Promise((r) => setTimeout(r, 3000));
    if (got > 0) return { note: got + ' event(s) in 3s', body: 'events=' + got };
    return 'no event within 3s — streaming disabled or quiet book';
  } catch (e) {
    return 'enable/open failed: ' + errLabel(e);
  } finally {
    off('sse:orderbook', counter);
    await sse.unsubscribe(base, rel).catch(() => {});
    // A live EventSource keeps the page's network busy forever — headless
    // dump-dom waits for idle and would never fire. The selftest therefore
    // closes the transport after its probe (views keep it open on purpose).
    sse.closeTransport();
  }
}

function numRaw(x) {
  if (x === undefined || x === null || x === '') return '0';
  const n = Number(x);
  return Number.isFinite(n) ? String(n) : String(x);
}