import { enabledCoins, electrum, disableCoin, version } from '../api.mjs';
import { esc, cut } from '../format.mjs';
import { on, off, emit } from '../store.mjs';

// Coins view: activation is the LEGACY `electrum` envelope — the v2 method
// does not exist on this build (doc/elektron.md section 10) — and
// `disable_coin` is legacy top-level too. get_enabled_coins is the only
// truth: the first electrum call may return an empty 500 body on a cold
// daemon (scripts/elektron/testnet_rpc.py retries — so does this view), and
// the response body proves nothing either way. There is no available-coins
// RPC in this daemon, so the ticker list is a curated preset (mirroring
// testnet_rpc.py SERVERS + the deployed ELEK electrs of doc section 10)
// plus tab-local custom entries (ticker + comma-separated server urls).
// The view owns none of it permanently: the daemon restores its coin set
// from the instance database on restart.

const PRESETS = [
  { ticker: 'ELEK', servers: ['127.0.0.1:50002'] },       // machine-local electrs via loopback (any real host goes through the custom form)
  { ticker: 'rELEK', servers: ['127.0.0.1:50003'] },      // regtest (testnet_rpc.py)
  { ticker: 'rBTC', servers: ['127.0.0.1:50004'] },
  { ticker: 'tELEK', servers: ['127.0.0.1:50005'] },      // testnet
  { ticker: 'tBTC', servers: ['testnet.aranguren.org:51001'] },
];
const POLL_MS = 8000;
// real tickers are case-sensitive and lowercase-prefixed on the test chains
// (rELEK, rBTC, tBTC) — no forced uppercasing anywhere
const TICKER_RE = /^[A-Za-z0-9]{2,20}$/;
const SERVER_RE = /^[a-zA-Z0-9.\-]+:\d{1,5}$/;

// tab-local state — deliberately survives re-rendering of the view
let coinRows = [];        // get_enabled_coins structs (ticker/address)
let custom = [];          // {ticker, servers:[url]} added via the form
let activating = new Set();
let tick = null;
let listErr = null;
let onClick = null;

export function render(root) {
  root.innerHTML = `
    <div class="page">
      <div class="panel-head">
        <h2>coins</h2>
        <div class="pair-row">
          <span id="c-version" class="muted"></span>
          <button id="c-refresh" class="btn small ghost">refresh</button>
        </div>
      </div>
      <section class="panel">
        <div class="panel-head"><h3>activation <span id="c-count" class="muted"></span></h3>
          <span class="muted">electrum activation — takes a moment on a real daemon</span>
        </div>
        <div id="c-table"><div class="state loading">loading coins…</div></div>
      </section>
      <section class="panel">
        <div class="panel-head"><h3>add coin</h3>
          <span class="muted">not yet on the daemon — joins the tab's list</span>
        </div>
        <div class="form-row">
          <label class="field"><span>ticker <span class="muted">(2-20 letters/digits)</span></span>
            <input id="c-add-ticker" autocomplete="off" spellcheck="false" placeholder="KMD" /></label>
          <label class="field"><span>electrum servers <span class="muted">(host:port, comma separated)</span></span>
            <input id="c-add-servers" autocomplete="off" spellcheck="false" placeholder="electrum.cipig.net:10000" /></label>
        </div>
        <button id="c-add-btn" class="btn small">add + activate</button>
        <p id="c-add-err" class="form-err" hidden></p>
      </section>
    </div>`;

  document.getElementById('c-refresh').addEventListener('click', refresh);
  document.getElementById('c-add-btn').addEventListener('click', addAndActivate);
  onClick = (ev) => {
    const btn = ev.target.closest('button[data-act]');
    if (btn && btn.closest('#c-table')) {
      const ticker = btn.getAttribute('data-ticker');
      if (btn.getAttribute('data-act') === 'enable') activate(ticker);
      else disable(ticker);
    }
  };
  document.addEventListener('click', onClick);
  tick = setInterval(refresh, POLL_MS);
  on('daemon-state', onDaemonState);

  probeVersion();
  refresh();
  return cleanup;
}

function cleanup() {
  if (tick) clearInterval(tick);
  tick = null;
  document.removeEventListener('click', onClick);
  off('daemon-state', onDaemonState);
}

// daemon down mid-activation must not leave the buttons lying — re-paint on
// the LED transition (listErr state keeps the table honest either way)
function onDaemonState() { poll(); }

function probeVersion() {
  version().then((r) => {
    const el = document.getElementById('c-version');
    if (el) el.textContent = 'kdf ' + (r.result || '');
  }).catch(() => {
    const el = document.getElementById('c-version');
    if (el) el.textContent = 'version failed';
  });
}

async function refresh() {
  try {
    const r = await enabledCoins();
    coinRows = (Array.isArray(r.result) ? r.result : []).map((c) => ({
      ticker: c.ticker || c.coin, address: c.address || '',
    }));
    listErr = null;
  } catch (e) {
    listErr = e.message || String(e);
  }
  poll();
}

// ---- rendering ---------------------------------------------------------------

function poll() {
  const table = document.getElementById('c-table');
  if (!table) return;
  const el = document.getElementById('c-count');
  if (el) el.textContent = coinRows.length ? `${coinRows.length} enabled` : '';

  // the degraded state stays honest: the error paints — alone when we never
  // saw a list, above the last known table otherwise (stale beats blank, but
  // labelled — the header LED alone does not say THIS list is stale)
  if (listErr !== null) {
    const errHtml = `<div class="state error">get_enabled_coins failed: ${esc(listErr || '')}</div>`;
    if (coinRows.length === 0) {
      table.innerHTML = errHtml;
      return;
    }
    table.innerHTML = errHtml + staleTableHtml();
    return;
  }
  table.innerHTML = staleTableHtml();
}

// paints from the module state we have (last known list) — shared by the
// live paint and the labelled stale paint
function staleTableHtml() {
  const enabled = new Set(coinRows.map((c) => c.ticker));
  const rows = [...PRESETS, ...custom].map((p) => ({
    ...p,
    enabled: enabled.has(p.ticker),
    starting: activating.has(p.ticker),
    address: (coinRows.find((c) => c.ticker === p.ticker) || {}).address || '',
  }));
  for (const t of coinRows) {
    if (!rows.some((r) => r.ticker === t.ticker)) {
      rows.push({ ticker: t.ticker, servers: [], enabled: true, starting: false, address: t.address });
    }
  }
  return `
    <table class="table">
      <thead><tr><th></th><th>ticker</th><th>state</th><th class="right">address</th><th>electrum servers</th><th></th></tr></thead>
      <tbody>${rows.map(rowHtml).join('')}</tbody>
    </table>`;
}

function rowHtml(r) {
  const state = r.starting
    ? '<span class="tag pending">activating…</span>'
    : r.enabled
      ? '<span class="tag ok">enabled</span>'
      : '<span class="tag">not enabled</span>';
  const act = r.starting
    ? '<button class="btn small ghost" disabled>activating…</button>'
    : r.enabled
      ? `<button class="btn small ghost" data-act="disable" data-ticker="${esc(r.ticker)}">disable</button>`
      : `<button class="btn small" data-act="enable" data-ticker="${esc(r.ticker)}">activate</button>`;
  return `
    <tr>
      <td></td>
      <td class="mono">${esc(r.ticker)}</td>
      <td>${state}</td>
      <td class="right num muted">${esc(r.enabled ? cut(r.address, 6, 4) : '—')}</td>
      <td class="muted">${esc((r.servers || []).join(', ') || '—')}</td>
      <td>${act}</td>
    </tr>`;
}

// ---- activation --------------------------------------------------------------

// activation is a fire-and-poll loop (testnet_rpc.py pattern): repeat the
// electrum call until the coin shows in get_enabled_coins — the response
// body proves nothing (a cold daemon answers the first call with an empty
// 500 body) and once the coin is up the real daemon replies with an
// "already initialized" error instead of re-activating.
async function activate(ticker) {
  if (activating.has(ticker)) return;
  const servers = ([...PRESETS, ...custom].find((p) => p.ticker === ticker) || {}).servers;
  if (!servers || !servers.length) {
    emit('toast', { msg: `${ticker}: no electrum server configured`, kind: 'err', ms: 6000 });
    return;
  }
  activating.add(ticker);
  poll();
  let arrived = false;
  for (let i = 0; i < 30 && !arrived; i++) {   // 30 * 3s = 90s
    electrum(ticker, servers.map((u) => ({ url: u })), 2).catch(() => {});  // body proves nothing
    await new Promise((r) => setTimeout(r, 3000));
    try {
      const r = await enabledCoins();
      arrived = (Array.isArray(r.result) ? r.result : [])
        .some((c) => (c.ticker || c.coin) === ticker);
    } catch { /* daemon hiccup — keep polling */ }
  }
  activating.delete(ticker);
  if (arrived) {
    emit('toast', { msg: `${ticker} activated` });
    refresh();
  } else {
    poll();
    emit('toast', { msg: `${ticker}: activation did not land within 90s — check the daemon log`, kind: 'err', ms: 6000 });
  }
}

async function disable(ticker) {
  try {
    await disableCoin(ticker);
    emit('toast', { msg: `${ticker} disabled` });
  } catch (e) {
    emit('toast', { msg: `${ticker}: disable failed — ${e.message || e}`, kind: 'err', ms: 6000 });
  }
  refresh();
}

// ---- add-coins form ------------------------------------------------------------

function addAndActivate() {
  const errEl = document.getElementById('c-add-err');
  const tEl = document.getElementById('c-add-ticker');
  const sEl = document.getElementById('c-add-servers');
  const ticker = (tEl.value || '').trim();
  const servers = (sEl.value || '').split(',').map((s) => s.trim()).filter(Boolean);
  errEl.hidden = false;
  if (!TICKER_RE.test(ticker)) { errEl.textContent = 'ticker must be 2-20 letters/digits'; return; }
  if (!servers.length || servers.some((s) => !SERVER_RE.test(s))) {
    errEl.textContent = 'one or more valid "host:port" needed';
    return;
  }
  if (custom.some((p) => p.ticker === ticker) || PRESETS.some((p) => p.ticker === ticker)) {
    errEl.textContent = `${ticker} is already in the list`;
    return;
  }
  errEl.hidden = true;
  tEl.value = '';
  sEl.value = '';
  custom.push({ ticker, servers });
  activate(ticker);
}