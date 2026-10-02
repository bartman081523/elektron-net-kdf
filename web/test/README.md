# mock daemon

Stdlib-only mock of the kdf JSON-RPC + SSE surface so the SPA can be iterated
without a daemon, chain or funds.

## Run

```sh
python3 web/test/mock_daemon.py --port 7993        # default mode ok
```

Then open the UI (elek-web on :3000), connect to `http://127.0.0.1:7993`
with ANY password (the mock accepts all passwords).

## Falsification modes

POST `{"method": "mock.set_mode", "params": {"mode": "..."}}` (or start with
`--mode`):

| mode   | behavior                                                       |
|--------|----------------------------------------------------------------|
| ok     | canned successes (default)                                     |
| err    | every method answers the v2 error form (UI must show errors)   |
| empty  | every method answers HTTP 500 + empty body (dead-end shape)    |
| slow   | every method sleeps 2s (timeout-path iteration)                |

```sh
# example: force error states in the UI
curl -s http://127.0.0.1:7993 -X POST \
  -d '{"method":"mock.set_mode","params":{"mode":"err"}}'
curl -s http://127.0.0.1:7993 -X POST -d '{"method":"version"}'     # -> v2 error form
```

## SSE

`GET /event-stream?id=1` streams the canned ELEK/tBTC book: an initial
snapshot (all items as `NewOrUpdatedItem`) and then one scripted step every
5s (cycle: price update, remove, re-add — both sides). Event shape:

```
data: {"_type": "ORDERBOOK_UPDATE:orbk:ELEK:tBTC", "message": {"order_type": "NewOrUpdatedItem", "order_data": {…}}}
```

```sh
curl -N --max-time 8 'http://127.0.0.1:7993/event-stream?id=7'    # ~2 scripted events
```

The real daemon appends a harmless trailing space to each data line
(`format!("data: {data} \n\n")`, sse_handler.rs) — the mock does not; both
parse identically. Error events on the wire carry an `ERROR:` prefix in
`_type` (event.rs: Event::get). A duplicate SSE `?id=` answers
500 "ID already in use" (use a fresh id per tab session; sse.mjs retries).

## Provisional shapes (pinned against a live daemon in phase F1)

my_orders, sell/buy, cancel_order, setprice, trade_preimage, withdraw,
stream::* enable/disable return placeholder-ish bodies. Legacy `withdraw`
answers NoSuchMethod here (the UI only uses the v2 envelope). When F1's
selftest runs against a real daemon, wrong shapes get corrected in this
file — the mock must never be allowed to "validate" an invented contract.

## Fidelity notes (A-grade)

- legacy top-level vs v2 `params` + the v2 error form mirror
  doc/elektron.md section 9 and the live activations of phases 5/6.
- RPC-level LEGACY errors answer HTTP 500 WITH a json body; v2 error forms
  answer 200 in-band (both pinned live on the regtest daemon) — the mock
  mirrors this: every legacy `{"error": …}` body leaves as a 500.
- `orderbook` items carry `is_mine`/`address`; SSE items don't (real
  OrderbookP2PItem has no such fields).
- the SSE `?id=` defaults to 0 in the real daemon (sse_handler.rs) — two
  tabs with the same id collide; sse.mjs therefore generates a random u64
  per session and retries with a new one on collision.

## Swaps (F3) — verified against the regtest daemons

Swap data arrives in two uniform tagged shapes `{swap_type, swap_data}`; the
view accepts both and derives status from the daemon's own semantics:

| source                 | swap_type location          | shape                                                              |
|------------------------|-----------------------------|--------------------------------------------------------------------|
| v2 `my_recent_swaps`   | `swaps[i].swap_type`        | `{swaps:[{swap_type, swap_data}], from_uuid, skipped, limit, total}`|
| v2 `my_swap_status`    | `result.swap_type` (NOT inside swap_data) | `{result:{swap_type, swap_data}}`                    |
| v2 `active_swaps(true)`| `items[i].swap_type`        | `{uuids:[…], items:[{swap_type, swap_data}]}`                      |
| v1 (SSE swap_status msg, legacy storage) | — legacy events — | v1 `my_swap_status` reads `params.uuid` (lp_swap.rs:1111) |

- **v1 status semantics mirrored from the daemon** (maker_swap.rs:67,
  :117, :2077, :2139; taker_swap.rs:787): `is_finished` = last event is
  `Finished` — failed v1 swaps STILL reach `Finished` unless refunded;
  `is_error` ≡ one of the event names in the swap's static `error_events`
  list. That list is an **event-name oracle, never a failure record**: a
  SUCCESSFUL swap carries the full 15-name list. The view therefore marks
  an event failing when its type is in `error_events` OR matches
  `/(^Error$|Failed$|Refund)/` (the v2 rows ship no `error_events`; the
  pattern covers their failure variants). Badge logic: terminal `Finished`
  + no failing event → `done`; any failing event → `failed`; else
  `pending`.
- **SSE**: `stream::swap_status::enable` (v2 envelope,
  `params:{client_id}`) answers `{streamer_id:"SWAP_STATUS"}`; registration
  is PER client_id — every tab/collector must enable its own stream. Frame:
  `data: {"_type":"SWAP_STATUS","message":{"swap_type":…,"swap_data":{"uuid":…,"event":{"timestamp":<ms>,"event":{"type":…,"data":…}}}}}`.
  Event timestamps are ms (swap_data `started_at` is s) — api layer
  normalizes.
- **`my_recent_swaps` carries every saved swap incl. still-active ones**
  (the active set lives in the same store) — history filters on
  "reached a terminal event", active keeps the rest.
- The mock's swap fixtures use FAKE secret bytes; the REAL v1 swap_data
  carries the revealed secret on the v2 routes (no hide_secrets applied).
  The view renders curated fields only (coins, amounts, step names,
  tx-hash cuts, error text) — the regtest harness asserts the real secret
  hash is present in the RPC answer but never in the DOM.

## Coins (F4) — verified against the mock and the regtest daemon

Activation is the **legacy `electrum` envelope** (the v2 method does not exist
on this build, doc/elektron.md section 10) and `disable_coin` is legacy
top-level. `get_enabled_coins` is the only truth — the view never uses an
activation answer's body.

Pinned wire shapes (live regtest, alice :7793):

| call                            | answer shape (pinned)                                                                 |
|---------------------------------|---------------------------------------------------------------------------------------|
| `electrum` success (cold coin)  | HTTP 200, BARE `{result:"success", address, balance, unspendable_balance, coin, required_confirmations, requires_notarization, mature_confirmations}` ~16ms |
| `electrum` already-initialized  | HTTP 500 legacy string `…lp_coins:5231] Coin <T> already initialized` — harmless, no-op |
| `electrum` first call, cold daemon | HTTP 500 **empty body** — an empty 500 cannot be told apart from a starting coin: poll for truth |
| `electrum` unknown ticker       | HTTP 500 legacy string `rpc:198] RPC call failed: legacy:144] lp_coins:6249] mm2 param is not set neither in coins config nor enable request…` (~1-7ms — the daemon treats an unknown ticker as an UNSET param; it never names the coin) |
| `disable_coin` success          | HTTP 200 WRAPPED `{result:{coin, cancelled_orders:[], passivized:false}}` ~100ms        |
| `disable_coin` unknown coin     | HTTP 500 `{"error":"No such coin: NoSuchCoin!!","orders":{…},"active_swaps":[]}`        |
| `get_enabled_coins` entries     | `{"ticker":…,"address":…}` ONLY — nothing else (no balance, no rpcport)                |

- **Ticker contract**: real tickers are case-sensitive, lower-case-prefixed on
  the test chains (`rELEK`, `rBTC`, `tBTC`) — no forced uppercasing anywhere in
  the UI or mock; the add form accepts `2-20` `[A-Za-z0-9]`.
- **Activation is fire-and-poll** (the same pattern as
  scripts/elektron/testnet_rpc.py): repeat the `electrum` call on a 3s cadence
  and check `get_enabled_coins`, up to 90s, because the response body proves
  nothing (empty 500 on a cold daemon, "already initialized" on a warm one).
  The daemon also restores its enabled set from its instance db on restart —
  there is no available-coins RPC, so the view's ticker list is a curated
  preset (mirroring testnet_rpc.py SERVERS + the deployed ELEK electrs of the
  doc) plus tab-local custom entries.
- **Degraded states**: a failed `get_enabled_coins` paints an error block —
  alone when no list was ever seen, above the last known table otherwise
  (stale beats blank, but labelled); the add form and refresh never become
  unusable. Verified for mock `err`/`empty`/`slow`/daemon-down on ALL views
  and for same-document revisits (a CDP `Page.navigate` between hash URLs is
  same-document — module state survives and the stale paint is labelled).

## Daemon gotchas (regtest evidence)

- **`withdraw` is build+sign ONLY** — it never broadcasts. The trait
  `build()` ends in `Ok(TransactionDetails{…})` with zero broadcast code
  (utxo_withdraw.rs ≈:158-243) and `StandardUtxoWithdraw::on_finishing`
  is a no-op; the broadcast step belongs to the caller via legacy
  `{method:"send_raw_transaction", coin, tx_hex}` → `{tx_hash}`
  (mm2's own tests do exactly this two-step, mm2_tests_inner.rs:916).
  The wallet view does the same (v2 `withdraw{broadcast:false}` preview →
  `send_raw_transaction` on confirm). Live proof: a withdrawn rBTC hex
  alone never reaches mempool/chain; sending it afterwards lands and
  credits. The `WithdrawRequest.broadcast` flag is MetaMask/ETH-specific
  (lp_coins.rs) and irrelevant for UTXO.