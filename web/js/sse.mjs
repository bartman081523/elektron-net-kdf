// Server-Sent-Events client for the daemon's /event-stream endpoint.
//
// Wire facts (sse_handler.rs, event.rs — pinned in F0):
// - GET {url}/event-stream?id=<u64>; a missing id defaults to 0 server-side
//   and a duplicate id that is still registered answers 500 "ID already in
//   use" — so each session carries a random u64 client id, and this module
//   reboots (new id + reopen + re-enable) when the browser's retry loop with
//   the old id does not recover.
// - each event: data: {"_type": "<streamer id>", "message": <payload>}
//   an error event keeps the stream id but gets an "ERROR:" prefix in _type.
// - subscribing is a separate RPC: stream::<kind>::enable (see api.mjs); the
//   endpoint merely delivers what the client registered for *that id*.

import { loadSession, saveSession, newClientId, emit } from './store.mjs';
import { v2, RpcError } from './api.mjs';

let es = null;                      // active EventSource
let state = 'off';                  // off | connecting | ok | error
const active = new Map();           // "kind:base:rel" -> stream spec
let errors = [];                    // timestamps of recent onerror fires

const setState = (s) => {
  if (state === s) return;
  state = s;
  emit('sse-state', { state: s });
};

const keyOf = (spec) => `${spec.kind}:${spec.base}:${spec.rel}`;

/** The alphabetically sorted pair form used in streamer ids (orbk:<ALB>). */
export function albPair(base, rel) {
  return [base, rel].sort().join(':');
}

export function streamerId(base, rel) {
  return 'ORDERBOOK_UPDATE:orbk:' + albPair(base, rel);
}

function handleMessage(ev) {
  let data;
  try {
    data = JSON.parse(ev.data);
  } catch (e) {
    return; // tolerate partial frames; the next event resyncs
  }
  const type = data && data._type ? String(data._type) : '';
  const payload = data ? data.message : undefined;
  if (!type.startsWith('ERROR:')) {
    if (type.startsWith('ORDERBOOK_UPDATE:')) emit('sse:orderbook', payload);
    else if (type.startsWith('SWAP_STATUS')) emit('sse:swap', payload);
    else if (type.startsWith('BALANCE:')) emit('sse:balance', { coin: type.slice(8), message: payload });
    else if (type.startsWith('TX_HISTORY:')) emit('sse:txhistory', { coin: type.slice(11), message: payload });
    return;
  }
  emit('sse:error', { type, payload });
}

function openEvents(session) {
  // EventSource reconnects on its own; this promise settles on first open.
  return new Promise((resolve, reject) => {
    es = new EventSource(session.url.replace(/\/+$/, '') + '/event-stream?id=' + session.clientId);
    es.onopen = () => {
      errors = [];
      setState('ok');
      resolve(es);
    };
    es.onerror = () => {
      // A 500 (stale duplicate id) or a dead server both land here while the
      // browser retries with the same id forever. Count recent failures and
      // reboot with a fresh id when the loop does not recover.
      const now = Date.now();
      errors = errors.filter((t) => now - t < 12000);
      errors.push(now);
      setState('error');
      if (errors.length >= 4) reboot();
      else resolve(es);          // still let the caller proceed; retries happen
    };
    es.onmessage = handleMessage;
    setTimeout(() => reject(new RpcError('event stream did not open', { type: 'SSE' })), 15000);
  });
}

async function ensureConnected() {
  if (es && es.readyState <= 1) return es;
  const s = loadSession();
  if (!s) throw new RpcError('not connected (no session)', { type: 'Session' });
  try {
    return await openEvents(s);
  } catch (e) {
    throw e;
  }
}

/** Fresh client id, reopen, re-enable everything the views had subscribed. */
async function reboot() {
  setState('off');
  if (es) es.close();
  es = null;
  const s = loadSession();
  if (!s) return;
  s.clientId = newClientId();
  saveSession(s);
  const specs = [...active.values()];
  active.clear();
  for (const spec of specs) {
    try {
      await enable(spec, { silent: true });
    } catch (e) {
      emit('toast', { msg: 'stream re-enable failed: ' + e.message, kind: 'err' });
    }
  }
}

async function enable(spec, { silent = false } = {}) {
  const s = loadSession();
  if (!s) throw new RpcError('not connected (no session)', { type: 'Session' });
  active.set(keyOf(spec), spec);
  if (spec.kind === 'orderbook') {
    await v2('stream::orderbook::enable', { client_id: s.clientId, base: spec.base, rel: spec.rel });
  } else {
    throw new RpcError('unknown stream kind: ' + spec.kind, { type: 'SSE' });
  }
  await ensureConnected();
  if (!silent) setState(es && es.readyState <= 1 ? 'ok' : 'error');
}

/** Subscribe to the orderbook of a pair; idempotent per pair. */
export function orderbook(base, rel) {
  return enable({ kind: 'orderbook', base, rel });
}

/** Unsubscribe one pair (best effort — a failed disable is not fatal). */
export async function unsubscribe(base, rel) {
  const spec = { kind: 'orderbook', base, rel };
  active.delete(keyOf(spec));
  const s = loadSession();
  if (!s) return;
  try {
    await v2('stream::disable', { client_id: s.clientId, streamer_id: streamerId(base, rel) });
  } catch (e) {
    /* the streamer may already be gone */
  }
}

/** Drop every subscription without closing the transport. */
export async function unsubscribeAll() {
  for (const spec of [...active.values()]) {
    if (spec.kind === 'orderbook') await unsubscribe(spec.base, spec.rel);
  }
}

/** Close the transport only (used when navigating away from live views). */
export function closeTransport() {
  if (es) {
    es.close();
    es = null;
    setState('off');
  }
}