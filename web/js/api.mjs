// The kdf JSON-RPC contract layer. One POST per call to the daemon root.
//
// Envelope facts (verified phases 5/6 + code, doc/elektron.md §9-§11):
// - legacy: top-level {"userpass", "method", ...fields} -> {"result": ...}
// - v2: {"mmrpc": "2.0", "userpass", "method", "params"} -> {"mmrpc": "2.0", "result": ...}
//   (userpass is a top-level field of the v2 request too)
// - error shapes seen in the wild, all three handled by unwrap():
//   (1) v2 object: {"mmrpc":"2.0","error":{error_path, error_type, error_trace, error_data}}
//   (2) top-level string error + sibling fields:
//       {"mmrpc":"2.0","error":"No such method","error_path":"dispatcher",
//        "error_trace":"dispatcher:305]","error_type":"NoSuchMethod"}   (live evidence)
//   (3) plain legacy: {"error": "<string>"}
// Success wrap is MIXED on real daemons (live regtest kdf 3.0.0-beta):
//   wrapped -> version, get_enabled_coins, my_orders
//   bare    -> my_balance, orderbook
// legacy() normalizes bare objects to {result: <object>} so callers read one
// shape; the selftest page records the responses it sees.

import { loadSession, emit } from './store.mjs';

export class RpcError extends Error {
  constructor(message, { type = 'RpcError', data = undefined, status = undefined } = {}) {
    super(message);
    this.name = 'RpcError';
    this.rpcType = type;
    this.rpcData = data;
    this.httpStatus = status;
  }
}

function session() {
  const s = loadSession();
  if (!s) throw new RpcError('not connected (no session in this tab)', { type: 'Session' });
  return s;
}

async function post(base, body, timeoutMs = 30000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  let resp;
  let json = null;
  try {
    resp = await fetch(base + '/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: ctl.signal,
    });
    json = await resp.json();
  } catch (e) {
    emit('daemon-state', { ok: false });
    if (e.name === 'AbortError') throw new RpcError('timeout', { type: 'Timeout' });
    throw new RpcError('daemon unreachable: ' + (e.message || e.name), { type: 'Network' });
  } finally {
    clearTimeout(timer);
  }
  emit('daemon-state', { ok: true });
  if (json === null || json === undefined) {
    throw new RpcError('empty/invalid response body (HTTP ' + resp.status + ')', { type: 'Http' });
  }
  return json;
}

function unwrap(json) {
  const err = json && json.error;
  if (err === undefined || err === null) return json;
  if (typeof err === 'string') {
    throw new RpcError(err, { type: json.error_type || 'RpcError', data: json.error_data });
  }
  throw new RpcError(
    err.error_type || 'RpcError',
    { type: err.error_type || 'RpcError', data: err.error_data ?? (err.data ?? err.detail) },
  );
}

// ---- transport ---------------------------------------------------------------

/** Legacy top-level envelope with the session's password. Normalizes bare
 * (unwrapped) success responses to {result: <value>} — see the header note. */
export async function legacy(method, fields = {}, timeoutMs = 30000) {
  const s = session();
  const json = await post(s.url, { userpass: s.userpass, method, ...fields }, timeoutMs);
  const unwrapped = unwrap(json);
  return (unwrapped && typeof unwrapped === 'object' && 'result' in unwrapped)
    ? unwrapped
    : { result: unwrapped };
}

/** v2 params envelope (mmrpc 2.0) with the session's password. */
export async function v2(method, params = {}, timeoutMs = 30000) {
  const s = session();
  const json = unwrap(await post(s.url, { mmrpc: '2.0', userpass: s.userpass, method, params }, timeoutMs));
  return json.result ?? null;
}

/** POST without a session (connect probe / selftest wrong-password check). */
export async function raw(base, body, timeoutMs = 30000) {
  return unwrap(await post(base, body, timeoutMs));
}

// ---- connect -----------------------------------------------------------------

/** Validate a daemon URL + password WITHOUT a session: public `version`
 * first (URL reachable), then `get_enabled_coins` (password accepted).
 * Returns {version, coins} for the session record. */
export async function probe(base, userpass) {
  const verResp = await raw(base, { method: 'version' });
  const res = verResp && verResp.result;
  const version = typeof res === 'string'
    ? res
    : (res && typeof res === 'object' ? (res.rpc_version || res.version || 'unknown') : 'unknown');
  const coinsResp = await raw(base, { userpass, method: 'get_enabled_coins' });
  const list = Array.isArray(coinsResp && coinsResp.result) ? coinsResp.result : [];
  return { version, coins: list };
}

// ---- methods -----------------------------------------------------------------
// One export per RPC; every import site reads like the contract inventory.

export const version = () => legacy('version');

export const enabledCoins = () => legacy('get_enabled_coins');
export const balance = (coin) => legacy('my_balance', { coin });
export const orderbook = (base, rel) => legacy('orderbook', { base, rel });
export const myOrders = () => legacy('my_orders');
export const cancelOrder = (uuid) => legacy('cancel_order', { uuid });
export const cancelAllOrders = (cancelBy = { type: 'All' }) =>
  legacy('cancel_all_orders', { cancel_by: cancelBy });
// Maker placement is `setprice` (lp_ordermatch.rs create_maker_order): the
// order enters the maker book via the P2P MAKER_ORDER_CREATED loopback.
// `sell`/`buy` are TAKER-only in this daemon (lp_auto_buy -> TakerOrderBuilder,
// lp_ordermatch.rs:4709): they never touch the book; a GTC taker order rests
// in my_orders.taker_orders until it matches an incoming maker order.
export const setPrice = ({ base, rel, price, volume, minVolume = undefined, orderType = 'GoodTillCancelled' }) =>
  legacy('setprice', {
    base, rel, price, volume,
    min_volume: minVolume,
    order_type: { type: orderType },
  });
export const takerOrder = ({ side, base, rel, price, volume, orderType = 'GoodTillCancelled' }) =>
  legacy(side, {
    base, rel, price, volume,
    order_type: { type: orderType },
  });
// OrderStatusReq carries `uuid` (lp_ordermatch.rs:5765); the response is
// {"result": {type: "Maker"|"Taker", order}}.
export const orderStatus = (uuid) => legacy('order_status', { uuid });
// legacy withdraw takes the whole body (dispatcher_legacy.rs); the UI uses v2:
export const withdraw = (p) => v2('withdraw', p);
export const sendRaw = (coin, txHex) => legacy('send_raw_transaction', { coin, tx_hex: txHex });
export const tradePreimage = (p) => v2('trade_preimage', p);
// ---- swaps -----------------------------------------------------------------
// Both dispatchers expose the swap RPCs (dispatcher.rs:237/273/274 and the
// legacy twins); the SPA uses the v2 forms only:
//  - v2 items are UNIFORM SwapRpcData {"swap_type","swap_data"} externally
//    tagged (swap_v2_rpcs.rs:286); legacy items differ per swap version.
//  - V1 swap_data carries the revealed secret inside events (no hide_secrets
//    on the v2 routes) — views render curated fields only, never event dumps.
//  - legacy twins: active_swaps answers BARE with `statuses: null`
//    (lp_swap.rs:1576-1615), my_swap_status reads params.uuid top-level
//    (lp_swap.rs:1111) and applies hide_secrets; only used by selftest.
export const swapStatus = (uuid) => v2('my_swap_status', { uuid });
export const activeSwaps = (includeStatus = false) =>
  v2('active_swaps', { include_status: includeStatus });
export const recentSwaps = (filter = {}, paging = {}) =>
  v2('my_recent_swaps', { ...filter, ...paging });
export const electrum = (coin, servers, confs = 2) =>
  legacy('electrum', { coin, servers, required_confirmations: confs, mature_confirmations: 1 }, 180000);
// deactivation answers only after the coin is actually stopped (dispatcher awaits);
// get_enabled_coins is the poll truth afterwards (coins view).
export const disableCoin = (coin) => legacy('disable_coin', { coin });