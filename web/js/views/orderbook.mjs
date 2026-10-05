import { orderbook as fetchBook } from '../api.mjs';
import { num, vol, esc } from '../format.mjs';
import { on, off, emit } from '../store.mjs';
import * as sse from '../sse.mjs';
import { loadPair, savePair, coinList } from '../market.mjs';

// Orderbook view. SSE events are a "dirty" flag only: the daemon serializes
// live order payloads in rational-number arrays (rat form, no decimals), so
// an event triggers a debounced refresh and the 8s poll stays the truth.
// The book is filled ONLY by the P2P maker-order loopback: setprice() maker
// orders appear (own node + peers) within a beat; taker orders (sell/buy)
// never appear here at all.

const POLL_MS = 8000;
const DEBOUNCE_MS = 400;

let coins = [];            // enabled tickers
let pair = null;           // {base, rel}
let book = null;           // last parsed orderbook response
let err = '';              // fetch error message
let streamOn = false;      // sse-state of the current pair stream
const prevPrice = new Map();   // uuid -> price of the previous paint
let unsubscribePair = null;    // pair currently subscribed on the transport

let pollTimer = null;
let debounceTimer = null;
let refs = null;               // live element refs (kept between refreshes)

export function render(root) {
  root.innerHTML = `
    <div class="page">
      <div class="panel-head">
        <h2>orderbook</h2>
        <div class="pair-row">
          <select id="ob-base" title="base coin"></select>
          <span class="muted">/</span>
          <select id="ob-rel" title="quote coin"></select>
          <button id="ob-flip" class="btn small ghost" title="swap base/quote">&hArr;</button>
          <button id="ob-refresh" class="btn small ghost">refresh</button>
        </div>
      </div>
      <section class="panel">
        <div class="panel-head">
          <h3 id="ob-ticker"></h3>
          <span class="side muted" id="ob-note"><span id="ob-live" class="live-dot" hidden></span><span id="ob-note-text"></span></span>
        </div>
        <div id="ob-table"><div class="state loading">loading book…</div></div>
      </section>
    </div>`;

  refs = {
    base: document.getElementById('ob-base'),
    rel: document.getElementById('ob-rel'),
    flip: document.getElementById('ob-flip'),
    refresh: document.getElementById('ob-refresh'),
    ticker: document.getElementById('ob-ticker'),
    note: document.getElementById('ob-note-text'),
    live: document.getElementById('ob-live'),
    table: document.getElementById('ob-table'),
  };

  refs.base.addEventListener('change', () => setPair({ base: refs.base.value, rel: refs.rel.value }));
  refs.rel.addEventListener('change', () => setPair({ base: refs.base.value, rel: refs.rel.value }));
  refs.flip.addEventListener('click', () => setPair({ base: refs.rel.value, rel: refs.base.value }));
  refs.refresh.addEventListener('click', () => refresh());

  on('sse:orderbook', onBookEvent);
  on('sse-state', onSseState);

  (async () => {
    try {
      coins = await coinList();
    } catch (e) {
      refs.table.innerHTML = `<div class="state error">failed to load enabled coins: ${esc(e.message)}</div>`;
      return;
    }
    if (coins.length < 2) {
      refs.table.innerHTML =
        '<div class="state empty">a book needs two enabled coins — activate more under <a href="#/coins">coins</a>.</div>';
      return;
    }
    // the selects ship empty in the template — fill them like trade.mjs does,
    // or refs.base.value = ... silently no-ops and the pair falls back to
    // coins[0]/coins[1] (daemon order), which can be a different pair entirely
    const opts = coins.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
    refs.base.innerHTML = opts;
    refs.rel.innerHTML = opts;
    const wanted = loadPair();
    pair = coins.includes(wanted.base) && coins.includes(wanted.rel) && wanted.base !== wanted.rel
      ? wanted
      : { base: coins[0], rel: coins[1] };
    refs.base.value = pair.base;
    refs.rel.value = pair.rel;
    await refresh();
    subscribePair();       // stream is polish; poll carries regardless
    pollTimer = setInterval(refresh, POLL_MS);
  })();

  return cleanup;
}

function cleanup() {
  off('sse:orderbook', onBookEvent);
  off('sse-state', onSseState);
  if (pollTimer) clearInterval(pollTimer);
  if (debounceTimer) clearTimeout(debounceTimer);
  pollTimer = debounceTimer = null;
  if (unsubscribePair) sse.unsubscribe(unsubscribePair.base, unsubscribePair.rel);
  unsubscribePair = null;
  prevPrice.clear();
  refs = null;
}

function setPair(next) {
  if (!pair || !next.base || !next.rel || next.base === next.rel) return;
  if (next.base === pair.base && next.rel === pair.rel) return;
  pair = next;
  savePair(pair);
  refs.base.value = pair.base;
  refs.rel.value = pair.rel;
  book = null;
  err = '';
  prevPrice.clear();
  paint();
  unsubscribePairSwap();
  subscribePair();
  refresh();
}

// keep at most one pair subscribed: switch = drop old, enable new
function unsubscribePairSwap() {
  if (unsubscribePair) sse.unsubscribe(unsubscribePair.base, unsubscribePair.rel);
  unsubscribePair = null;
}

function subscribePair() {
  if (!pair) return;
  unsubscribePair = pair;
  // enable() connects the transport first (the server only knows clients
  // through the open /event-stream); a failure here just means no live
  // polish — the poll interval keeps rendering.
  sse.orderbook(pair.base, pair.rel).catch(() => {});
}

// SSE event for the subscribed pair (or any pair — payload is not trusted,
// rat form) -> schedule one debounced refresh.
function onBookEvent() {
  if (debounceTimer) return;
  debounceTimer = setTimeout(() => {
    debounceTimer = null;
    refresh();
  }, DEBOUNCE_MS);
}

function onSseState({ state }) {
  streamOn = state === 'ok';
  paintNote();
}

async function refresh() {
  if (!refs || !pair) return;
  try {
    const r = await fetchBook(pair.base, pair.rel);
    book = r.result || r;          // legacy() normalizes bare responses
    err = '';
    paint();
    paintNote();
  } catch (e) {
    err = e.message;
    paint();
    paintNote();
  }
}

function paintNote() {
  if (!refs) return;
  refs.ticker.textContent = pair ? `${pair.base}/${pair.rel}` : '';
  refs.live.hidden = !streamOn;
  refs.note.textContent = !streamOn ? '' : ' live';
}

// ---- book rendering ---------------------------------------------------------

function rowsOf(list, dir) {
  const sorted = [...(list || [])].sort((a, b) =>
    (Number(a.price) - Number(b.price)) * (dir === 'bid' ? -1 : 1));
  let cum = 0;
  const max = sorted.reduce((m, e) => Math.max(m, Number(e.base_max_volume) || 0), 0) || 1;
  return sorted.map((e) => {
    cum += Number(e.base_max_volume) || 0;   // base-coin volume both directions
    return { e, depth: cum / max, side: dir };
  });
}

function paint() {
  if (!refs || !pair) return;
  if (err && !book) {
    refs.table.innerHTML = `<div class="state error">orderbook failed: ${esc(err)}</div>`;
    return;
  }
  if (!book) {
    refs.table.innerHTML = '<div class="state">loading book…</div>';
    return;
  }
  const askRows = rowsOf(book.asks, 'ask').reverse();  // worst ask on top … best at the spread
  const bidRows = rowsOf(book.bids, 'bid');            // best bid first … worst below

  const bestAsk = askRows.reduce((m, r) => Math.min(m, Number(r.e.price)), Infinity);
  const bestBid = bidRows.reduce((m, r) => Math.max(m, Number(r.e.price)), -Infinity);
  const spread = askRows.length && bidRows.length ? bestAsk - bestBid : null;

  const rowHtml = (r) => {
    const e = r.e;
    const dir = prevPrice.has(e.uuid) && Number(prevPrice.get(e.uuid)) !== Number(e.price)
      ? (Number(e.price) > Number(prevPrice.get(e.uuid)) ? 'flash' : 'flash-down')
      : '';
    prevPrice.set(e.uuid, e.price);
    const cls = `${r.side} ${e.is_mine ? 'mine' : ''}`.trim();
    return `
      <tr class="${cls}" data-uuid="${esc(e.uuid)}">
        <td class="num price ${dir}">${num(e.price)}</td>
        <td class="right num">${vol(e.base_max_volume)}</td>
        <td class="right num muted">${vol(e.rel_max_volume)}</td>
        <td class="bars"><div class="bar-fill ${r.side}" style="width:${(r.depth * 100).toFixed(1)}%"></div></td>
      </tr>`;
  };

  const empty = !askRows.length && !bidRows.length;
  if (empty && !err) {
    // The empty book IS a product: say why it can be empty.
    refs.table.innerHTML = `
      <div class="state empty">
        <h2>no resting orders</h2>
        <p class="muted">Makers use “rest” on the <a href="#/trade">trade</a> view
        (setprice); the book fills via P2P within a beat. You have no orders in
        this pair either.</p>
      </div>`;
    return;
  }

  refs.table.className = '';
  refs.table.innerHTML = `
    <table class="table book">
      <thead><tr>
        <th>price <span class="muted">${esc(pair.rel)}</span></th>
        <th class="right">vol <span class="muted">${esc(pair.base)}</span></th>
        <th class="right">vol <span class="muted">${esc(pair.rel)}</span></th>
        <th style="width:34%"></th>
      </tr></thead>
      <tbody>
        ${askRows.map(rowHtml).join('')}
        ${askRows.length || bidRows.length ? `
          <tr class="spread"><td colspan="4" class="num ${spread !== null ? '' : 'muted'}">
            ${spread !== null ? 'spread ' + num(spread, 8) : 'one-sided book'}
          </td></tr>` : ''}
        ${bidRows.map(rowHtml).join('')}
      </tbody>
    </table>
    <div class="book-totals muted num">
      Σ asks ${vol(book.total_asks_base_vol)} ${esc(pair.base)}
      · Σ bids ${vol(book.total_bids_base_vol)} ${esc(pair.base)}
      · ${esc(String(book.numasks ?? askRows.length))}/${esc(String(book.numbids ?? bidRows.length))} orders
      ${err ? `<span class="err-text"> · ${esc(err)}</span>` : ''}
    </div>`;
}