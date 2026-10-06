# elektron-market container (testnet default; mainnet option)

One container = the whole marketplace: two kdf daemons and the web UI with a
same-origin RPC proxy. Test-mode-by-design: by default only TESTNET coins are
enabled, and a JSON-RPC selftest must pass before the web service is exposed.

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
docker build --build-arg KDF_BUILD_TAG=$(git rev-parse --short HEAD) -t elektron-market .
docker run --rm -p 10000:10000 -e MM_TEST_SEED=some-fixed-phrase elektron-market
```

`KDF_BUILD_TAG` stamps the daemon version string; the default fallback is
`elektron-docker` when the argument is omitted.

Open `http://localhost:10000` — the SPA auto-connects through `/rpc`
(proxy mode, no connect form). Container logs show both daemon logs,
the selftest result table, and the funding banner.

## Environment

| variable            | default                          | meaning                                        |
|---------------------|----------------------------------|------------------------------------------------|
| `PORT`              | `10000`                          | public web port (Render injects its own value) |
| `MM_NETID`          | `8888`                           | DEX P2P netid of the local market/trader pair (mainnet option uses `0`) |
| `MM_COINS_SRC`      | `/app/docker/coins-testnet.json` | coins file the container runs on — the mainnet option points it at `/app/docker/coins-mainnet.json` |
| `MM_TEST_SEED`      | random                           | wallet passphrases derive from it (`<seed>-market` / `<seed>-trader`); with a fixed value the deposit addresses are IDENTICAL on every restart, otherwise the wallet starts empty and unfindable after a new boot |
| `MM_RPC_PASS`       | generated                        | daemon rpc password (container-internal only)  |
| `MM_TELEK_ELECTRS`  | unset                            | electrum host:port for tELEK — when unset the coins file only contains tBTC and no pair can form; supply your own LAN/remote electrs server (e.g. `<your-host>:<port>`) |
| `MM_TELEK_TEMPLATE` | `/app/docker/telek-template.json`| coins-file template for tELEK; its `TELEK_ELECTRUM_PLACEHOLDER` url is rewritten to `MM_TELEK_ELECTRS` |
| `MM_ELEK_ELECTRS`   | unset                            | electrum host:port for mainnet ELEK (mainnet option) — the coin is appended to the set only when this is set |
| `MM_ELEK_TEMPLATE`  | `/app/docker/elek-template.json` | coins-file template for mainnet ELEK; its `ELEK_ELECTRUM_PLACEHOLDER` url is rewritten to `MM_ELEK_ELECTRS` |
| `MM_WEB_FX_RATES`   | `/app/docker/fx-rates.json` when present | path to an electrs rich-shape FX snapshot that elek-web serves same-origin at `/fx/rates.json` — the SPA draws its ELEK rate line from it (rate line absent, never fabricated, when unset) |
| `MM_SKIP_SELFTEST`  | unset (runs)                     | `1` skips the startup selftest                 |
| `MM_STATE_DIR`      | `/run/elek`                      | runtime dir for configs/logs (ephemeral OK)    |

The default `docker/coins-testnet.json` ships public TESTNET electrum servers
for tBTC, so the default container is self-contained (no LAN addresses, no
operator host). tELEK/ELEK has no public electrum server — the deployment
must supply one via the env vars above; for ELEK this is typically the
operator's own electrs instance. The image also bakes `docker/fx-rates.json`
(a snapshot from that electrs in the registry-reference shape), so the SPA
shows the ELEK rate line out of the box; a deployment that wants a live rate
points `MM_WEB_FX_RATES` at its own continuously-refreshed file instead.

## Selftest gate

`docker/selftest.py` (stdlib only) runs before the web service binds:

- hard matrix (any failure ⇒ container exit 1, deploy fails): version,
  BTC-coin + eleks-coin activation (3 retries), enabled-coins check, balances,
  my_orders envelope, cancel_all write path, already-activated idempotency,
  withdraw insufficient-probe, trade_preimage bogus-coin probe,
  swap-status bogus-uuid probe, SSE reachability.
- fund-gated matrix (skipped loudly until testnet funds arrive at the
  addresses printed by the `★ FUND` banner): maker setprice, visibility of
  the order on the other daemon (P2P ordermatch), cancel.

Coin selection is derived from the ACTUAL coins.json (the setup_env.py
output), not from hardcoded tickers: the BTC-family coin is the first of
`tBTC`/`BTC` carrying a coins-file electrum list, the eleks coin is `tELEK`
when `MM_TELEK_ELECTRS` is set or `ELEK` when `MM_ELEK_ELECTRS` is set (one
net per deployment). Coins without a coins-file electrum list are not
self-activatable; hard steps needing them then skip loudly instead of
failing the gate (no BTC-coin ⇒ activation/enabled/withdraw/preimage steps
skip, no pair ⇒ the maker path skips). On the testnet default only tELEK
steps skip when `MM_TELEK_ELECTRS` is unset — the self-contained default
stays deployable, and the mainnet mode deploys green without any testnet
coin in the chain.

Real swaps are exercised in the local E2E campaign (`T5`/`T6` tasks); this
selftest deliberately does not burn faucet funds.

## Mainnet option

The default deployment is testnet-only by design. A mainnet marketplace is
the SAME image driven by different env — no code-path changes:

```sh
MM_COINS_SRC=/app/docker/coins-mainnet.json   # BTC with public cipig electrum
MM_ELEK_ELECTRS=<your-elek-electrs-host>:50002  # ELEK has no public electrum
MM_NETID=0                                    # the mainnet DEX space
MM_TEST_SEED=<production seed phrase>         # wallet passphrases derive from it
```

`coins-mainnet.json` carries BTC mainnet with the public electrum servers
from the KomodoPlatform/coins registry (`electrums/BTC`:
`btc.electrum1.cipig.net:10000`, `btc.electrum3.cipig.net:10000`, verified
live). ELEK stays operator-supplied as above.

Consequences to weigh before flipping the switch:

- mainnet wallets hold REAL coins: `MM_TEST_SEED` and `MM_RPC_PASS` are then
  production secrets (on Render they sit in the service env, readable by
  service collaborators) — treat them accordingly;
- the selftest runs the same SAFE probes (they never move balances); the
  fund-gated maker path only runs once a real wallet is funded, and any real
  swap that follows is a real-money swap;
- the built-in FX snapshot is a static registry reference rate — point
  `MM_WEB_FX_RATES` at a live file (from an electrs running
  `fx_rate_url`/`fx_orderbook` config) for a live mainnet rate.

## Render.com

The service deploys from the ghcr image built by
`.github/workflows/docker-publish.yml` (push to `elektron/main`) with
health check path `/healthz`. Free-plan disclosures: instance sleeps
unvisited, disk is ephemeral (`MM_TEST_SEED` keeps the wallet address
stable), and a funded selfnet wallet's seed sits in the service env
(testnet-only, not production secrets). The deployed Render service runs
the testnet default; a mainnet deployment (env above) on a public host
would be production-grade and needs the secrets discipline.

## Security notes

The rpc password exists only inside the container (`0600` MM2.json files +
env of the two binaries); `/rpc` overwrites whatever `userpass` a browser
sends; `/elek-web-config.json` hands over `rpc_pass: ""`. The SSE endpoint
inherits the upstream daemon's unauthenticated-event-stream behaviour
(marktdaten only; tracked upstream TODO).