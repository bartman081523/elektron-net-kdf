import { setPrice, takerOrder, myOrders, cancelOrder, cancelAllOrders, tradePreimage, RpcError } from '../api.mjs';
import { num, vol, ago, esc, cut } from '../format.mjs';
import { emit } from '../store.mjs';
import { loadPair, savePair, coinList } from '../market.mjs';

// Trade view. Two order families, mirroring the daemon's split (both pinned
// on regtest, lp_ordermatch.rs):
//  - rest  -> `setprice` (maker): a resting ask on base/rel; the maker book is
//    filled only by the P2P MAKER_ORDER_CREATED loopback. To rest a bid, flip
//    the pair — a setprice on rel/base shows up as a bid in the base/rel book.
//  - immediate -> `sell`/`buy` (taker): never touches the book. Crossing
//    volume matches immediately; otherwise a GTC order rests invisibly in
//    my_orders.taker_orders. FOK kills the order when it cannot fill exactly
//    the requested volume.
// Daemon errors pass through verbatim (e.g. dust limits are chain-specific).

const ORDERS_POLL_MS = 10000;
const FEE_DEBOUNCE_MS = 600;

let coins = [];
let pair = null;               // {base, rel}
let side = 'sell';             // taker side (immediate mode)
let mode = 'rest';             // rest | immediate
let busy = false;              // place/cancel in flight
let orders = null;             // last my_orders result
let ordersErr = '';
let refs = null;
let ordersTimer = null;
let feeTimer = null;

export function render(root) {
  root.innerHTML = `
  <div class="page">
    <div class="panel-head">
      <h2>trade</h2>
      <div class="pair-row">
        <select id="tr-base" title="base coin"></select>
        <span class="muted">/</span>
        <select id="tr-rel" title="quote coin"></select>
        <button id="tr-flip" class="btn small ghost" title="swap base/quote">&hArr;</button>
      </div>
    </div>
    <div class="page-cols">
      <section class="panel">
        <div class="panel-head">
          <h3 id="tr-title">new order</h3>
          <span class="side muted" id="tr-side-note"></span>
        </div>
        <div class="seg" id="tr-mode-seg">
          <button type="button" class="seg-btn" id="tr-mode-rest">rest (maker)</button>
          <button type="button" class="seg-btn" id="tr-mode-immediate">immediate (taker)</button>
        </div>
        <form id="tr-form" autocomplete="off">
          <div class="form-row" id="tr-side-row">
            <div class="field"><label>side</label>
              <div class="seg" id="tr-side-seg">
                <button type="button" class="seg-btn sell" id="tr-sell">sell base</button>
                <button type="button" class="seg-btn buy" id="tr-buy">buy base</button>
              </div>
            </div>
          </div>
          <div class="form-row two">
            <div class="field"><label id="tr-price-lab">price <span class="muted" id="tr-price-unit"></span></label>
              <input id="tr-price" inputmode="decimal" placeholder="0.0" /></div>
            <div class="field"><label id="tr-vol-lab">volume <span class="muted" id="tr-vol-unit"></span></label>
              <input id="tr-vol" inputmode="decimal" placeholder="0.0" /></div>
          </div>
          <div class="form-row two">
            <div class="field" id="tr-minwrap"><label>min volume <span class="muted">(base)</span></label>
              <input id="tr-min" inputmode="decimal" placeholder="0.0" /></div>
            <div class="field" id="tr-type-wrap"><label>order type</label>
              <select id="tr-type">
                <option value="FillOrKill" selected>FillOrKill — fill all or kill</option>
                <option value="GoodTillCancelled">GoodTillCancelled — rest until filled</option>
              </select></div>
          </div>
          <div class="form-err" id="tr-err"></div>
          <div class="fee-preview mono muted" id="tr-fee"></div>
          <button class="btn primary" id="tr-place" type="submit">place order</button>
        </form>
      </section>
      <section class="panel">
        <div class="panel-head">
          <h3>my orders</h3>
          <button class="btn small danger" id="tr-cancel-all">cancel all</button>
        </div>
        <div id="tr-orders"><div class="state loading">loading orders…</div></div>
      </section>
    </div>
  </div>`;

  refs = {
    base: document.getElementById('tr-base'),
    rel: document.getElementById('tr-rel'),
    flip: document.getElementById('tr-flip'),
    modeSeg: document.getElementById('tr-mode-seg'),
    modeRest: document.getElementById('tr-mode-rest'),
    modeImm: document.getElementById('tr-mode-immediate'),
    sideRow: document.getElementById('tr-side-row'),
    sell: document.getElementById('tr-sell'),
    buy: document.getElementById('tr-buy'),
    price: document.getElementById('tr-price'),
    vol: document.getElementById('tr-vol'),
    minwrap: document.getElementById('tr-minwrap'),
    min: document.getElementById('tr-min'),
    typeWrap: document.getElementById('tr-type-wrap'),
    type: document.getElementById('tr-type'),
    title: document.getElementById('tr-title'),
    sideNote: document.getElementById('tr-side-note'),
    priceUnit: document.getElementById('tr-price-unit'),
    volUnit: document.getElementById('tr-vol-unit'),
    fee: document.getElementById('tr-fee'),
    err: document.getElementById('tr-err'),
    place: document.getElementById('tr-place'),
    orders: document.getElementById('tr-orders'),
    cancelAll: document.getElementById('tr-cancel-all'),
    form: document.getElementById('tr-form'),
  };

  refs.base.addEventListener('change', () => setPair({ base: refs.base.value, rel: refs.rel.value }));
  refs.rel.addEventListener('change', () => setPair({ base: refs.base.value, rel: refs.rel.value }));
  refs.flip.addEventListener('click', () => setPair({ base: refs.rel.value, rel: refs.base.value }));
  refs.modeRest.addEventListener('click', () => setMode('rest'));
  refs.modeImm.addEventListener('click', () => setMode('immediate'));
  refs.sell.addEventListener('click', () => setSide('sell'));
  refs.buy.addEventListener('click', () => setSide('buy'));
  refs.cancelAll.addEventListener('click', cancelEverything);
  refs.form.addEventListener('submit', place);

  for (const el of [refs.price, refs.vol, refs.min, refs.type]) {
    el.addEventListener('input', debounceFee);
    el.addEventListener('change', debounceFee);
  }

  (async () => {
    try {
      coins = await coinList();
    } catch (e) {
      refs.orders.innerHTML = `<div class="state error">failed to load enabled coins: ${esc(e.message)}</div>`;
      return;
    }
    if (coins.length < 2) {
      refs.orders.innerHTML =
        '<div class="state empty">trading needs two enabled coins — activate more under <a href="#/coins">coins</a>.</div>';
      return;
    }
    const opts = coins.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    refs.base.innerHTML = opts;
    refs.rel.innerHTML = opts;
    const wanted = loadPair();
    pair = coins.includes(wanted.base) && coins.includes(wanted.rel) && wanted.base !== wanted.rel
      ? wanted
      : { base: coins[0], rel: coins[1] };
    refs.base.value = pair.base;
    refs.rel.value = pair.rel;
    paintControls();
    refreshOrders();
    ordersTimer = setInterval(refreshOrders, ORDERS_POLL_MS);
  })();

  return cleanup;
}

function cleanup() {
  if (ordersTimer) clearInterval(ordersTimer);
  if (feeTimer) clearTimeout(feeTimer);
  ordersTimer = feeTimer = null;
  refs = null;
}

// ---- form state -----------------------------------------------------------

function setPair(next) {
  if (!next.base || !next.rel || next.base === next.rel) return;
  if (pair && next.base === pair.base && next.rel === pair.rel) return;
  pair = next;
  savePair(pair);
  if (!refs) return;
  refs.base.value = pair.base;
  refs.rel.value = pair.rel;
  paintControls();
  clearFee();
  refreshOrders();   // orders panel is per-pair
}

function setMode(next) {
  mode = next;
  paintControls();
  clearFee();
}

function setSide(next) {
  side = next;
  paintControls();
  clearFee();
}

// Units and visibility follow the mode; labels always name the actual coin.
function paintControls() {
  if (!refs || !pair) return;
  refs.modeRest.classList.toggle('active', mode === 'rest');
  refs.modeImm.classList.toggle('active', mode === 'immediate');
  refs.sideRow.hidden = mode !== 'immediate';
  refs.minwrap.hidden = mode !== 'rest';
  refs.typeWrap.hidden = mode !== 'immediate';

  refs.sell.classList.toggle('active', side === 'sell');
  refs.buy.classList.toggle('active', side === 'buy');

  refs.title.textContent = mode === 'rest' ? 'rest maker order (ask)' : 'taker order';
  refs.sideNote.textContent = mode === 'rest'
    ? 'daemon makers are asks — flip the pair to rest a bid'
    : (side === 'sell' ? `sell ${pair.base} for ${pair.rel}` : `buy ${pair.base} for ${pair.rel}`);
  refs.priceUnit.textContent = `(${pair.rel} per ${pair.base})`;
  refs.volUnit.textContent = `(${pair.base})`;
  refs.place.textContent = mode === 'rest' ? 'rest order' : `place ${side} (taker)`;
}

function showErr(msg) {
  if (!refs) return;
  refs.err.textContent = msg || '';
}

function clearFee() {
  if (!refs) return;
  if (feeTimer) clearTimeout(feeTimer);
  feeTimer = null;
  refs.fee.textContent = '';
}

// ---- fee preview ------------------------------------------------------------
// Generic rendering: trade_preimage returns total_fees rows live (taker form
// pinned; the setprice maker form stays defensive — unknown shape renders as
// key/value lines or a JSON fallback, never a fabricated number).

function debounceFee() {
  if (!refs || !pair) return;
  showErr('');
  refs.fee.textContent = 'estimating fees…';
  if (feeTimer) clearTimeout(feeTimer);
  feeTimer = setTimeout(runFeePreview, FEE_DEBOUNCE_MS);
}

function preimageParams() {
  const price = refs.price.value.trim();
  const volume = refs.vol.value.trim();
  const base = mode === 'immediate' && side === 'buy' ? pair.rel : pair.base;
  const rel = mode === 'immediate' && side === 'buy' ? pair.base : pair.rel;
  if (mode === 'immediate') {
    return { base, rel, swap_method: side, volume, price };
  }
  return { base: pair.base, rel: pair.rel, swap_method: 'setprice', volume, price };
}

async function runFeePreview() {
  if (!refs) return;
  try {
    const res = await tradePreimage(preimageParams());
    refs.fee.innerHTML = feeLines(res);
  } catch (e) {
    refs.fee.innerHTML = `<span class="err-text">${esc(String(e.message || e))}</span>`;
  }
}

function feeLines(res) {
  const fees = res && Array.isArray(res.total_fees) ? res.total_fees : [];
  if (fees.length) {
    const rows = fees.map((f) =>
      `<div>${esc(f.coin || f.coin_from || '?')}: ${esc(String(f.amount ?? f.fee_amount ?? '?'))}</div>`);
    return rows.join('');
  }
  if (res && res.result && Array.isArray(res.result.total_fees) && res.result.total_fees.length) {
    return feeLines(res.result);
  }
  // Unpinned shape: dump the known top-level keys instead of inventing numbers.
  const flat = JSON.stringify(res);
  return `<div>${esc(cut(flat, 60, 40))}</div>`;
}

// ---- placement ---------------------------------------------------------------

function extractUuid(res) {
  const r = res && res.result !== undefined ? res.result : null;
  if (!r) return null;
  if (typeof r === 'string') return r;
  if (r.uuid) return r.uuid;
  if (r.order && r.order.uuid) return r.order.uuid;
  return null;
}

async function place(ev) {
  ev.preventDefault();
  if (!refs || !pair || busy) return;
  showErr('');
  const price = refs.price.value.trim();
  const volume = refs.vol.value.trim();
  const minVolume = refs.min.value.trim();
  const orderType = refs.type.value;
  if (!(Number(price) > 0)) return showErr('price must be > 0');
  if (!(Number(volume) > 0)) return showErr('volume must be > 0');
  if (mode === 'rest' && minVolume && !(Number(minVolume) > 0)) return showErr('min volume must be > 0');

  busy = true;
  refs.place.disabled = true;
  try {
    let res;
    if (mode === 'rest') {
      res = await setPrice({
        base: pair.base, rel: pair.rel, price, volume,
        minVolume: minVolume || undefined, orderType: 'GoodTillCancelled',
      });
    } else {
      res = await takerOrder({ side, base: pair.base, rel: pair.rel, price, volume, orderType });
    }
    const uuid = extractUuid(res);
    emit('toast', { msg: (mode === 'rest' ? 'maker order ' : 'taker order ') + (uuid ? uuid.slice(0, 8) : 'placed'), kind: 'ok' });
    refs.price.value = '';
    refs.vol.value = '';
    refs.min.value = '';
    refs.fee.textContent = '';
    refreshOrders();
  } catch (e) {
    showErr(e instanceof RpcError && e.rpcType && e.rpcType !== 'RpcError'
      ? `${e.rpcType}: ${e.message}` : String(e.message || e));
  } finally {
    busy = false;
    if (refs) refs.place.disabled = false;
  }
}

// ---- my orders ----------------------------------------------------------------

async function refreshOrders() {
  if (!refs) return;
  try {
    const res = await myOrders();
    orders = (res && res.result) || {};
    ordersErr = '';
    paintOrders();
  } catch (e) {
    ordersErr = e.message;
    paintOrders();
  }
}

// my_orders result: {maker_orders: {uuid: MakerOrderForMyOrdersRpc},
//  taker_orders: {uuid: TakerOrderForRpc}} (maps, keys are the uuids).
function rowsFrom(makerMap, takerMap) {
  const mk = Object.entries(makerMap || {}).map(([uuid, o]) => ({
    uuid, kind: 'maker', o,
    base: o.base || (o.orderbook_ticker && o.orderbook_ticker.split && o.orderbook_ticker.split('/')[0]) || '?',
    rel: o.rel_orderbook_ticker || o.rel || '?',
    price: o.price !== undefined ? num(o.price, 8) : '—',
    amount: o.available_amount ?? o.max_base_vol ?? '—',
    amountLabel: 'avail',
    when: o.created_at,
    cancellable: o.cancellable !== false,
  }));
  // Taker value (TakerOrderForRpc): everything lives in o.request — the
  // SellBuyResponse the order was created from (base/rel/base_amount/
  // rel_amount/action); only created_at/order_type/cancellable are top level.
  const tk = Object.entries(takerMap || {}).map(([uuid, o]) => {
    const req = o.request || o;
    const baseAmt = Number(req.base_amount), relAmt = Number(req.rel_amount);
    const price = baseAmt && relAmt && Number.isFinite(baseAmt) && Number.isFinite(relAmt)
      ? num(relAmt / baseAmt, 8) : '—';
    return {
      uuid, kind: 'taker', o,
      base: req.base || '?',
      rel: req.rel || '?',
      price,
      amount: req.base_amount ?? '—',
      amountLabel: 'vol',
      when: o.created_at,
      cancellable: o.cancellable !== false,
    };
  });
  const all = [...mk, ...tk];
  all.sort((a, b) => (b.when || 0) - (a.when || 0));
  return all;
}

function paintOrders() {
  if (!refs) return;
  if (ordersErr && !orders) {
    refs.orders.innerHTML = `<div class="state error">my_orders failed: ${esc(ordersErr)}</div>`;
    return;
  }
  if (!orders) {
    refs.orders.innerHTML = '<div class="state">loading orders…</div>';
    return;
  }
  const rows = rowsFrom(orders.maker_orders, orders.taker_orders);
  const pairRows = rows; // all coins; pair filter would hide crossing takers
  if (!pairRows.length) {
    refs.orders.innerHTML =
      `<div class="state empty">no own orders on this daemon —
       rest one on the left (maker book via P2P) or hit the book as taker.</div>`;
    return;
  }
  refs.orders.innerHTML = `
    <table class="table">
      <thead><tr>
        <th></th><th>pair</th><th class="right">price</th>
        <th class="right">${esc('amount')}</th><th>age</th><th></th>
      </tr></thead>
      <tbody>
        ${pairRows.map(orderRowHtml).join('')}
      </tbody>
    </table>`;
}

function orderRowHtml(r) {
  const cls = [r.kind === 'taker' ? 'muted' : '', r.o.matches && Object.keys(r.o.matches).length ? 'strong' : '']
    .join(' ').trim();
  return `
    <tr class="${cls}">
      <td><span class="tag ${r.kind}">${esc(r.kind)}</span></td>
      <td class="mono">${esc(`${r.base}/${r.rel}`)}</td>
      <td class="num right">${esc(r.price)}</td>
      <td class="num right">${esc(String(r.amount))} <span class="muted">${esc(r.amountLabel)}</span></td>
      <td>${esc(ago(Math.floor(Number(r.when || 0) / 1000)))}</td>
      <td>${r.cancellable
        ? `<button class="btn small danger" data-act="cancel" data-uuid="${esc(r.uuid)}">cancel</button>`
        : '<span class="muted">—</span>'}</td>
    </tr>`;
}

async function cancelEverything() {
  if (!refs || busy) return;
  busy = true;
  try {
    const res = await cancelAllOrders({ type: 'All' });
    const n = res && res.result && Array.isArray(res.result.cancelled) ? res.result.cancelled.length : 0;
    emit('toast', { msg: `cancelled ${n} order(s)`, kind: 'ok' });
    refreshOrders();
  } catch (e) {
    emit('toast', { msg: 'cancel all failed: ' + (e.message || e), kind: 'err' });
  } finally {
    busy = false;
  }
}

// Cancel via delegation (rows re-render every poll).
document.addEventListener('click', (ev) => {
  if (!refs) return; // view not mounted
  const btn = ev.target.closest('[data-act="cancel"]');
  if (!btn) return;
  const uuid = btn.getAttribute('data-uuid');
  cancelOrder(uuid)
    .then(() => emit('toast', { msg: `cancelled ${cut(uuid, 6, 4)}`, kind: 'ok' }))
    .catch((e) => emit('toast', { msg: 'cancel failed: ' + (e.message || e), kind: 'err' }))
    .then(refreshOrders);
});