# elektron-market container (testnet mode)

One container = the whole marketplace: two kdf daemons and the web UI with a
same-origin RPC proxy. Test-mode-by-design: only TESTNET coins are enabled,
and a JSON-RPC selftest must pass before the web service is exposed.

```
browser ── https://<service>/            (static SPA, hash router)
   │  POST /rpc            get forwarded to the trader daemon,
   │  GET  /rpc/event-stream  the rpc password is injected server-side,
   └  GET  /healthz           browsers never receive it
                ▼
        elek-web ─ proxy ─► kdf trader (127.0.0.1:7796)
                                │ seednodes=127.0.0.1 (DEX netid MM_NETID)
                                ▼
                            kdf market (127.0.0.1:7795, i_am_seed)  ← maker orders
```

## Build & run

```sh
docker build -t elektron-market .
docker run --rm -p 10000:10000 -e MM_TEST_SEED=some-fixed-phrase elektron-market
```

Open `http://localhost:10000` — the SPA auto-connects through `/rpc`
(proxy mode, no connect form). Container logs show both daemon logs,
the selftest result table, and the funding banner.

## Environment

| variable           | default            | meaning                                        |
|--------------------|--------------------|------------------------------------------------|
| `PORT`             | `10000`            | public web port (Render injects its own value) |
| `MM_NETID`         | `8888`             | DEX P2P netid of the local market/trader pair  |
| `MM_TEST_SEED`     | random             | wallet passphrases derive from it (`<seed>-market` / `<seed>-trader`); with a fixed value the deposit addresses are IDENTICAL on every restart, otherwise the wallet starts empty and unfindable after a new boot |
| `MM_RPC_PASS`      | generated          | daemon rpc password (container-internal only)  |
| `MM_TELEK_ELECTRS` | unset              | electrum host:port for tELEK — when unset the coins file only contains tBTC and no pair can form; e.g. `192.168.178.21:50005` (LAN) |
| `MM_SKIP_SELFTEST` | unset (runs)       | `1` skips the startup selftest                 |
| `MM_STATE_DIR`     | `/run/elek`        | runtime dir for configs/logs (ephemeral OK)    |

## Selftest gate

`docker/selftest.py` (stdlib only) runs before the web service binds:

- hard matrix (any failure ⇒ container exit 1, deploy fails): version,
  tBTC/tELEK activation (3 retries), enabled-coins check, balances, my_orders
  envelope, cancel_all write path, already-activated idempotency, withdraw
  insufficient-probe, trade_preimage bogus-coin probe, swap-status bogus-uuid
  probe, SSE reachability.
- fund-gated matrix (skipped loudly until testnet funds arrive at the
  addresses printed by the `★ FUND` banner): maker setprice, visibility of
  the order on the other daemon (P2P ordermatch), cancel.

Real swaps are exercised in the local E2E campaign (`T5`/`T6` tasks); this
selftest deliberately does not burn faucet funds.

## Render.com

The service deploys from the ghcr image built by
`.github/workflows/docker-publish.yml` (push to `elektron/main`) with
health check path `/healthz`. Free-plan disclosures: instance sleeps
unvisited, disk is ephemeral (`MM_TEST_SEED` keeps the wallet address
stable), and a funded selfnet wallet's seed sits in the service env
(testnet-only, not production secrets).

## Security notes

The rpc password exists only inside the container (`0600` MM2.json files +
env of the two binaries); `/rpc` overwrites whatever `userpass` a browser
sends; `/elek-web-config.json` hands over `rpc_pass: ""`. The SSE endpoint
inherits the upstream daemon's unauthenticated-event-stream behaviour
(marktdaten only; tracked upstream TODO).