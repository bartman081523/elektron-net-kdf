import { activeSwaps, swapStatus, recentSwaps } from '../api.mjs';
import { num, ago, esc, cut } from '../format.mjs';
import { on, off } from '../store.mjs';
import * as sse from '../sse.mjs';

// Swaps view. A swap is either resting (active) or finished (history):
//   - the v2 routes answer UNIFORM tagged items {swap_type, swap_data}
//     (swap_v2_rpcs.rs:286); active_swaps(true) carries the statuses,
//     my_recent_swaps pages the history, and my_swap_status fills the gap
//     for a uuid that is neither in the active set nor on the history page.
//   - the v2 swap routes apply NO hide_secrets — V1 swap_data events carry
//     the revealed secret — so this view renders curated fields only
//     (coins, amounts, step names, tx-hash cuts, error text). Never an
//     event dump, never event-data passthrough beyond tx_hash/error.
// Live polish comes from the global SWAP_STATUS streamer (one enable covers
// every swap of the daemon); the polls are the truth (3s active / 15s hist).

const ACTIVE_POLL_MS = 3000;
const HISTORY_POLL_MS = 15000;
const DEBOUNCE_MS = 400;

// V1 status semantics, mirrored from the daemon (maker_swap.rs):
//   is_finished(): the LAST event is Finished — even failed V1 swaps reach
//     Finished; a refund-completed one ends on a failure-name terminal.
//   is_success():  finished AND no event with is_error().
//   is_error():    !is_success() per event — exactly the variants whose
//     names are serialized in the swap's own static error_events list
//     (MAKER/TAKER_ERROR_EVENTS). A SUCCESSful swap carries the FULL list,
//     so the list is an event-name oracle, never a failure record.
// V2 rows (MySwapForRpc) ship no error_events; their failure variants (the
// same event enums) all end in 'Failed' or are refund steps — hence the
// pattern fallback.
const FAIL_RE = /(^Error$|Failed$|Refund)/;

let refs = null;                 // live element refs
let activeTimer = null;
let histTimer = null;
let kickTimer = null;            // debounced post-SSE refresh of the active set
let closeKickTimer = null;       // one early history refresh after a swap closes
let streamOn = false;

let activeItems = new Map();     // uuid -> tagged item (live truth by poll)
let histItems = [];              // tagged items, newest first
const fetched = new Map();       // uuid -> tagged item (my_swap_status misses)
let selRef = null;               // uuid whose detail is open
let selErr = '';                 // my_swap_status failure for the open detail

export function render(root) {
  root.innerHTML = `
    <div class="page">
      <div class="panel-head">
        <h2>swaps</h2>
        <div class="pair-row">
          <span class="side muted"><span id="sw-live" class="live-dot" hidden></span><span id="sw-note-text">polling only</span></span>
          <button id="sw-refresh" class="btn small ghost">refresh</button>
        </div>
      </div>
      <div class="page-cols">
        <section class="panel">
          <div class="panel-head"><h3>active</h3><span class="side muted num" id="sw-count"></span></div>
          <div id="sw-active"><div class="state loading">loading swaps…</div></div>
        </section>
        <section class="panel">
          <div class="panel-head"><h3>swap detail</h3></div>
          <div id="sw-detail"><div class="state empty">pick a swap to see its timeline.</div></div>
        </section>
      </div>
      <section class="panel">
        <div class="panel-head"><h3>history</h3></div>
        <div id="sw-history"><div class="state loading">loading history…</div></div>
      </section>
    </div>`;

  refs = {
    note: document.getElementById('sw-note-text'),
    live: document.getElementById('sw-live'),
    refresh: document.getElementById('sw-refresh'),
    count: document.getElementById('sw-count'),
    active: document.getElementById('sw-active'),
    detail: document.getElementById('sw-detail'),
    history: document.getElementById('sw-history'),
  };
  fetched.clear();

  on('sse:swap', onSwapEvent);
  on('sse-state', onSseState);
  refs.refresh.addEventListener('click', () => { refreshActive(); refreshHistory(); });

  refreshActive();
  refreshHistory();
  if (activeTimer) clearInterval(activeTimer);
  if (histTimer) clearInterval(histTimer);
  activeTimer = setInterval(refreshActive, ACTIVE_POLL_MS);
  histTimer = setInterval(refreshHistory, HISTORY_POLL_MS);
  paintNote();

  // polish only — a failed/absent stream leaves the polls running
  sse.swapStatus().catch(() => {});

  return cleanup;
}

function cleanup() {
  off('sse:swap', onSwapEvent);
  off('sse-state', onSseState);
  if (activeTimer) clearInterval(activeTimer);
  if (histTimer) clearInterval(histTimer);
  if (kickTimer) clearTimeout(kickTimer);
  if (closeKickTimer) clearTimeout(closeKickTimer);
  activeTimer = histTimer = kickTimer = closeKickTimer = null;
  sse.unsubscribeSwapStatus();
  activeItems = new Map();
  histItems = [];
  selRef = null;
  streamOn = false;
  refs = null;
}

// ---- data --------------------------------------------------------------------

const dataFor = (uuid) => (!uuid ? null
  : activeItems.get(uuid)
  || histItems.find((it) => it && it.swap_data && it.swap_data.uuid === uuid)
  || fetched.get(uuid)
  || null);

function openDetail(uuid) {
  selRef = uuid;
  selErr = '';
  paintDetail();
  if (dataFor(uuid)) return;     // already on screen — no extra call
  swapStatus(uuid)
    .then((item) => {
      if (item && item.swap_data) fetched.set(uuid, item);
      if (selRef === uuid) {
        selErr = item && item.swap_data ? '' : 'daemon answered without swap data';
        paintDetail();
      }
    })
    .catch((e) => {
      if (selRef === uuid) {
        selErr = e.message || String(e);
        paintDetail();
      }
    });
}

function onSwapEvent(msg) {
  const sd = (msg && msg.swap_data) || {};
  const ev = (sd.event && sd.event.event) || sd.event || {};
  kick();
  // Finished AND failure terminals (refund-completed swaps never reach
  // Finished) both move the swap out of the active table; mid-chain refresh
  // is debounced and harmless.
  if (ev.type === 'Finished' || FAIL_RE.test(ev.type || '')) {
    if (closeKickTimer) clearTimeout(closeKickTimer);
    closeKickTimer = setTimeout(refreshHistory, 1200);
  }
}

function onSseState({ state }) {
  streamOn = state === 'ok';
  paintNote();
}

function kick() {
  if (kickTimer) clearTimeout(kickTimer);
  kickTimer = setTimeout(() => {
    kickTimer = null;
    refreshActive();
  }, DEBOUNCE_MS);
}

function paintNote() {
  if (!refs) return;
  refs.live.hidden = !streamOn;
  refs.note.textContent = streamOn ? 'live swap events' : 'polling only';
}

async function refreshActive() {
  if (!refs) return;
  try {
    // one call with statuses (tagged items) instead of uuids + n lookups
    const r = await activeSwaps(true);
    const items = ((r && r.uuids) || []).map((u) =>
      ((r.statuses && r.statuses[u]) || { swap_type: '', swap_data: { uuid: u } }));
    activeItems = new Map(items.map((it) => [it.swap_data.uuid, it]));
    if (!selRef && activeItems.size === 1) {
      selRef = items[0].swap_data.uuid;   // exactly one swap running — open it
    }
    if (selRef && !dataFor(selRef)) openDetail(selRef); // left the active set mid-poll? refetch
    paintActive('');
    if (dataFor(selRef)) paintDetail();  // live update of an open detail
  } catch (e) {
    paintActive(e.message || String(e));
  }
}

async function refreshHistory() {
  if (!refs) return;
  try {
    const r = await recentSwaps({}, { limit: 50, page_number: 1 });
    // my_recent_swaps carries every saved swap incl. still-running ones (the
    // active set lives in the same table) — the active panel owns unfinished
    // swaps, so history keeps only completed ones.
    histItems = (Array.isArray(r && r.swaps) ? r.swaps : [])
      .filter((it) => metaOf(it).finished);
    paintHistory('');
  } catch (e) {
    paintHistory(e.message || String(e));
  }
}

// ---- rendering ---------------------------------------------------------------

function paintActive(err) {
  if (!refs) return;
  const items = [...activeItems.values()];
  refs.count.textContent = items.length ? `${items.length} running` : '';
  if (!items.length && err) {
    refs.active.innerHTML = `<div class="state error">active_swaps failed: ${esc(err)}</div>`;
    return;
  }
  if (!items.length) {
    refs.active.innerHTML = `
      <div class="state empty">
        <p class="muted">no active swaps — take an order on the
        <a href="#/trade">trade</a> view or rest one for a peer to hit.</p>
      </div>`;
    return;
  }
  refs.active.innerHTML = `
    <table class="table">
      <thead><tr>
        <th></th><th>pair</th><th class="right">my amount</th>
        <th class="right">price</th><th>step</th><th>age</th><th></th>
      </tr></thead>
      <tbody>${items.map((it) => swapRowHtml(it, false)).join('')}</tbody>
    </table>`;
}

function paintHistory(err) {
  if (!refs) return;
  if (!histItems.length && err) {
    refs.history.innerHTML = `<div class="state error">my_recent_swaps failed: ${esc(err)}</div>`;
    return;
  }
  if (!histItems.length) {
    refs.history.innerHTML =
      '<div class="state empty"><p class="muted">no swaps yet — the first completed swap lands here.</p></div>';
    return;
  }
  if (dataFor(selRef)) paintDetail(); // a selected swap just landed in history
  refs.history.innerHTML = `
    <table class="table">
      <thead><tr>
        <th></th><th>pair</th><th class="right">my amount</th>
        <th class="right">price</th><th>step</th><th>age</th><th></th>
      </tr></thead>
      <tbody>${histItems.map((it) => swapRowHtml(it, true)).join('')}</tbody>
    </table>`;
}

function swapRowHtml(it, isHist) {
  const m = metaOf(it);
  const badge = isHist
    ? `<span class="tag ${m.status}">${m.status === 'ok' ? 'done' : m.status === 'fail' ? 'failed' : 'active'}</span>`
    : '<span class="tag pending">active</span>';
  return `
    <tr class="rowlink${selRef === m.uuid ? ' sel' : ''}" data-uuid="${esc(m.uuid)}">
      <td><span class="tag ${m.side}">${esc(m.side)}</span></td>
      <td class="mono">${esc(m.otherCoin || '–')}/${esc(m.myCoin || '–')}</td>
      <td class="right num">${num(m.myAmt, 8)} <span class="muted">${esc(m.myCoin || '')}</span></td>
      <td class="right num">${num(m.price, 8)}</td>
      <td>${esc(m.step || '–')}</td>
      <td>${esc(ago(m.started))}</td>
      <td>${badge}</td>
    </tr>`;
}

const chip = (k, v) => `
  <span class="chip"><span class="chip-k">${esc(k)}</span>
  <span class="chip-v mono">${esc(String(v))}</span></span>`;

function paintDetail() {
  if (!refs) return;
  const it = dataFor(selRef);
  if (!it) {
    refs.detail.innerHTML = selRef
      ? (selErr
        ? `<div class="state error">my_swap_status failed: ${esc(selErr)}</div>`
        : '<div class="state">loading swap…</div>')
      : '<div class="state empty">pick a swap to see its timeline.</div>';
    return;
  }
  selErr = '';
  const m = metaOf(it);
  const chips = [
    ['uuid', cut(m.uuid, 10, 8)],
    ['pair', m.otherCoin && m.myCoin ? `${m.otherCoin}/${m.myCoin}` : ''],
    ['my', m.myAmt != null && m.myCoin ? `${num(m.myAmt, 8)} ${m.myCoin}` : ''],
    ['other', m.otherAmt != null && m.otherCoin ? `${num(m.otherAmt, 8)} ${m.otherCoin}` : ''],
    ['price', m.price != null ? num(m.price, 8) : ''],
    ['lock', m.lockdur ? `${Number(m.lockdur)}s` : ''],
    ['started', m.started ? ago(m.started) : ''],
  ].filter(([, v]) => v).map(([k, v]) => chip(k, v)).join('');
  const lines = [...m.evs].reverse().map((e) => {
    const cls = e.err ? 'tl-err' : e.type === 'Finished' ? 'tl-done' : '';
    const extra = e.data && e.data.error
      ? `<span class="tl-err-text">${esc(String(e.data.error))}</span>`
      : e.data && e.data.tx_hash
        ? `<span class="muted mono">${esc(cut(String(e.data.tx_hash), 10, 8))}</span>`
        : '';
    return `<li class="${cls}"><span class="tl-dot"></span><span class="mono">${esc(e.type || '–')}</span>${extra}<span class="tl-when">${esc(ago(e.ts))}</span></li>`;
  }).join('');
  const badgeTxt = m.status === 'ok' ? 'done' : m.status === 'fail' ? 'failed' : 'active';
  refs.detail.innerHTML = `
    <div class="chips">side <span class="tag ${m.side}">${esc(m.side)}</span>
    <span class="tag ${m.status}">${badgeTxt}</span>${m.step ? `<span class="chip"><span class="chip-k">step</span><span class="chip-v mono">${esc(m.step)}</span></span>` : ''}</div>
    <div class="chips">${chips || '<span class="muted">no overview data</span>'}</div>
    <ul class="timeline">${lines || '<li class="muted">no event lines yet</li>'}</ul>`;
}

// ---- normalization -----------------------------------------------------------

const secOf = (t) => {
  const n = Number(t || 0);
  return n > 1e12 ? Math.round(n / 1000) : n; // event wrapper: ms; started_at: s
};

// [{timestamp, event:{type, data}}] wrapper — tolerate the bare string form
// {"event": "Finished"} some daemon versions serialize.
const timelineEvents = (evs) => (evs || []).map((ev) => {
  const e = ev && ev.event ? ev.event : ev;
  if (e && typeof e === 'object') {
    return { ts: secOf(ev.timestamp), type: String(e.type || ''), data: e.data || {} };
  }
  return { ts: secOf(ev && ev.timestamp), type: String(e || ''), data: {} };
});

const stepOf = (evs) => {
  for (let i = evs.length - 1; i >= 0; i--) if (evs[i].type) return evs[i].type;
  return '';
};

const nz = (a, b) => (a !== null && a !== undefined && a !== '' ? a : b);

// Normalizes either orientation into "my" vs "other". Two item shapes reach
// this view: v2 tagged items {swap_type, swap_data} (active_swaps(true)
// statuses, my_swap_status) and the FLAT legacy items of my_recent_swaps
// (uuid, events, is_finished, error_events at top level). Read both: d
// prefers swap_data and falls back to the item itself.
function metaOf(it) {
  const type = String((it && (it.swap_type || it.type)) || '');
  const d = (it && it.swap_data) || it || {};
  const uuid = (d && d.uuid) || '';
  const isV2 = type.endsWith('V2');
  const isTaker = type.startsWith('Taker');
  const evs = timelineEvents(d.events);
  const s = evs.length ? evs[0].data : {};
  let myCoin; let otherCoin; let myAmt; let otherAmt;
  if (isV2) {
    myCoin = d.my_coin || '';
    otherCoin = d.other_coin || '';
    myAmt = isTaker ? d.taker_volume : d.maker_volume;
    otherAmt = isTaker ? d.maker_volume : d.taker_volume;
  } else {
    myCoin = (isTaker ? s.taker_coin : s.maker_coin) || (isTaker ? d.taker_coin : d.maker_coin) || '';
    otherCoin = (isTaker ? s.maker_coin : s.taker_coin) || (isTaker ? d.maker_coin : d.taker_coin) || '';
    myAmt = isTaker ? nz(s.taker_amount, d.taker_amount) : nz(s.maker_amount, d.maker_amount);
    otherAmt = isTaker ? nz(s.maker_amount, d.maker_amount) : nz(s.taker_amount, d.taker_amount);
  }
  const errorEvents = Array.isArray(d.error_events) ? d.error_events : [];
  const isErrEv = (e) => errorEvents.includes(e.type) || FAIL_RE.test(e.type);
  const evs2 = evs.map((e) => ({ ...e, err: isErrEv(e) }));
  const term = (() => {
    for (let i = evs2.length - 1; i >= 0; i--) if (evs2[i].type) return evs2[i].type;
    return '';
  })();
  // daemon is_finished() accepts only the terminal event; a refund-completed
  // V1 swap ends on a failure-name terminal (e.g. MakerPaymentRefundFinished)
  // and still counts as completed here, so it never vanishes between the two
  // panels. Flat legacy items carry is_finished outright.
  const finished = typeof d.is_finished === 'boolean'
    ? !!d.is_finished
    : (term === 'Finished' || errorEvents.includes(term));
  const failed = evs2.some((e) => e.err);
  const my = Number(myAmt); const other = Number(otherAmt);
  const price = my > 0 && other > 0 ? other / my : null;
  return {
    uuid,
    side: isTaker ? 'taker' : 'maker',
    myCoin, otherCoin, myAmt, otherAmt,
    price,
    step: stepOf(evs),
    started: secOf(isV2 ? d.started_at : (s.started_at || 0)),
    finished, failed,
    status: !finished ? 'pending' : (failed ? 'fail' : 'ok'),
    lockdur: s.lock_duration || d.lock_duration || '',
    evs: evs2,
  };
}

// Row pick via delegation (tables re-render on every poll/SSE).
document.addEventListener('click', (ev) => {
  if (!refs) return; // view not mounted
  const row = ev.target.closest('tr.rowlink');
  if (!row || !refs.detail) return;
  const page = refs.detail.closest('.page');
  if (!page || !page.contains(row)) return;
  const uuid = row.getAttribute('data-uuid');
  if (uuid) openDetail(uuid);
});