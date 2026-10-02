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
- `orderbook` items carry `is_mine`/`address`; SSE items don't (real
  OrderbookP2PItem has no such fields).
- the SSE `?id=` defaults to 0 in the real daemon (sse_handler.rs) — two
  tabs with the same id collide; sse.mjs therefore generates a random u64
  per session and retries with a new one on collision.