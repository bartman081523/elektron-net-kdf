# elektron-market container (testnet default; mainnet option)

One container = the whole marketplace: two kdf daemons, the web UI with a
same-origin RPC proxy — and by default the complete testnet back-end
(`elektrond` node, `electrs` indexer, miner-as-faucet), so the container is
self-contained: no operator host, no LAN addresses, no external chain. The
daemons are passive (book/price only); custody lives exclusively in the
users' own wallets.

```
browser ── https://<service>/            (static SPA, hash router)
   │  POST /rpc            get forwarded to the trader daemon,
   │  GET  /rpc/event-stream  the rpc password is injected server-side,
   └  GET  /healthz           browsers never receive it
                ▼
        elek-web ─ proxy ─► kdf trader (127.0.0.1:7796)
                                │ seednodes=127.0.0.1 (DEX netid MM_NETID)
                                ▼
                            kdf market (127.0.0.1:7795, i_am_seed)  ← passive seed node (holds no coins)
                                ▲ fx_orderbook: book mid → ELEK rate
                            electrs-tn (127.0.0.1:50005)  ← indexes elektrond testnet
                            elektrond (testnet, mined by the container loop)
```

## Build & run

```sh
docker build \
  --build-arg KDF_BUILD_TAG=$(git rev-parse --short HEAD) \
  --build-context bin=/path/to/staging \   # staging dir with `elektrond` + `electrs`
  -t elektron-market .
docker run --rm -p 10000:10000 -e MM_TEST_SEED=some-fixed-phrase elektron-market
```

The node binaries (elektrond, electrs) come from the extra build context
`bin` and are NOT part of the repository; CI builds them from source
(separate step). `KDF_BUILD_TAG` stamps the daemon version string; the
default fallback is `elektron-docker` when the argument is omitted.

Open `http://localhost:10000` — the SPA auto-connects through `/rpc`
(proxy mode, no connect form). Container logs show both daemon logs and
the selftest result table (including the custody proof).

## Boot order & watchdog

The entrypoint boots the web service FIRST (`/healthz` + SPA answer from
~1 s, while infra and daemons still boot behind it) so a platform health
check never sees a cold-start 502. The selftest gate still decides the
container's life: any hard failure exits the container. After the gate:

- a watchdog probe re-checks the two daemon ports every 30 s; 3 consecutive
  misses abort the container (platform restart of a wedged stack);
- if the web process dies, the container exits with it.

Because the web binds before the daemons, the SPA's auto-connect retries
the probe (bounded, 5 s backoff) — visitors just see the spinner for a few
seconds on a cold boot instead of the connect form.

## Environment

| variable            | default                          | meaning                                        |
|---------------------|----------------------------------|------------------------------------------------|
| `PORT`              | `10000`                          | public web port (Render injects its own value) |
| `MM_NETID`          | `8888`                           | DEX P2P netid of the local market/trader pair (mainnet option uses `0`) |
| `MM_COINS_SRC`      | `/app/docker/coins-testnet.json` | coins file the container runs on — the mainnet option points it at `/app/docker/coins-mainnet.json` |
| `MM_TEST_SEED`      | random                           | wallet passphrases derive from it (`<seed>-market` / `<seed>-trader`); these wallets are PASSIVE (book/price only, no custody — never deposit to them). With a fixed value the addresses are IDENTICAL on every restart |
| `MM_RPC_PASS`       | generated                        | daemon rpc password (container-internal only)  |
| `MM_LOCAL_TESTNET`  | `1`                              | self-contained mode: elektrond + electrs run INSIDE the container and the miner loop digs a fresh testnet chain from genesis. `0` opts out (external electrs via `MM_TELEK_ELECTRS`, e.g. the mainnet option — no faucet then) |
| `MM_TELEK_ELECTRS`  | `127.0.0.1:50005` (in-container) | electrum host:port for tELEK — defaults to the in-container electrs in self-contained mode; with `MM_LOCAL_TESTNET=0` supply your own LAN/remote electrs server (e.g. `<your-host>:<port>`) |
| `MM_LOCAL_TESTNET_MINER` | `1`                         | `0` pauses the miner loop (chain frozen, faucet stops) |
| `MM_LOCAL_TESTNET_DIR`   | `/data/elektron-testnet`     | state dir of the local chain (ephemeral OK — the chain is re-mined from genesis on every fresh disk) |
| `MM_TELEK_TEMPLATE` | `/app/docker/telek-template.json`| coins-file template for tELEK; its `TELEK_ELECTRUM_PLACEHOLDER` url is rewritten to `MM_TELEK_ELECTRS` |
| `MM_ELEK_ELECTRS`   | unset                            | electrum host:port for mainnet ELEK (mainnet option) — the coin is appended to the set only when this is set |
| `MM_ELEK_TEMPLATE`  | `/app/docker/elek-template.json` | coins-file template for mainnet ELEK; its `ELEK_ELECTRUM_PLACEHOLDER` url is rewritten to `MM_ELEK_ELECTRS` |
| `MM_WEB_FX_RATES`   | self-contained: LIVE in-container electrs file · otherwise `/app/docker/fx-rates.json` when present | path to an electrs rich-shape FX snapshot that elek-web serves same-origin at `/fx/rates.json` — the SPA draws its ELEK rate line from it (rate line absent, never fabricated, when unset) |
| `MM_SKIP_SELFTEST`  | unset (runs)                     | `1` skips the startup selftest                 |
| `MM_STATE_DIR`      | `/run/elek`                      | runtime dir for configs/logs (ephemeral OK)    |

The default `docker/coins-testnet.json` ships public TESTNET electrum servers
for tBTC, and tELEK comes from the in-container electrs — no deployment env
needed for a working pair. The self-contained infra (`docker/start_testnet_infra.sh`)
starts elektrond with a genesis check, creates the faucet wallet (its address
lands in `<MM_LOCAL_TESTNET_DIR>/faucet-address.txt`), generates the electrs
config (orderbook → market RPC, registry fallback rate) and waits for the
electrum RPC before anything else starts. The miner loop digs one block per
round — the young chain never outruns the indexer, the faucet supply matures
continuously, and boot stays fast (no bulk mining).

`MM_LOCAL_TESTNET=0` turns all of that off: the container then behaves like
the pre-self-contained image (external electrs via `MM_TELEK_ELECTRS`, baked
`docker/fx-rates.json` as the only FX source — e.g. the mainnet option).

## Selftest gate

`docker/selftest.py` (stdlib only) runs behind the already-bound web service
and decides the container's life (web first, gate second):

- hard matrix (any failure ⇒ container exit 1, deploy fails): version,
  BTC-coin + eleks-coin activation (3 retries), enabled-coins check,
  my_orders envelope, cancel_all write path, already-activated idempotency,
  withdraw insufficient-probe, trade_preimage bogus-coin probe,
  swap-status bogus-uuid probe, SSE reachability,
  **no-custody proof**: both daemons must report ZERO balances and ZERO
  orders — a funded market wallet fails the deploy.
- coin-gated matrix (skipped loudly when a coin is not in the set): the
  old maker setprice path is REMOVED by the no-custody contract; order
  visibility and ordermatch are exercised between client kdf instances
  (campaign A4), not by the daemons.

Coin selection is derived from the ACTUAL coins.json (the setup_env.py
output), not from hardcoded tickers: the BTC-family coin is the first of
`tBTC`/`BTC` carrying a coins-file electrum list, the eleks coin is `tELEK`
when `MM_TELEK_ELECTRS` is set or `ELEK` when `MM_ELEK_ELECTRS` is set (one
net per deployment). Coins without a coins-file electrum list are not
self-activatable; hard steps needing them then skip loudly instead of
failing the gate (no BTC-coin ⇒ activation/enabled/withdraw/preimage steps
skip, no pair ⇒ the pair step skips). In self-contained mode both coins are
present, so the full matrix runs; with `MM_LOCAL_TESTNET=0` and no
`MM_TELEK_ELECTRS` only the tELEK steps skip, and the mainnet mode deploys
green without any testnet coin in the chain.

Real swaps are exercised between client-side kdf instances (user-owned
wallets, `T5`/`T6` campaign) — the container itself deliberately holds no
funds on any chain, testnet or mainnet.

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

- the daemon wallets stay passive and EMPTY on mainnet too: `MM_TEST_SEED`
  and `MM_RPC_PASS` exist only for the book/price service, never for custody
  — a deposit into a daemon address would be a contract breach (the selftest
  FAILS on a nonzero balance on every chain);
- the selftest runs the same SAFE probes (they never move balances); real
  swaps happen between user-side kdf instances only;
- the built-in FX snapshot is a static registry reference rate — point
  `MM_WEB_FX_RATES` at a live file (from an electrs running
  `fx_rate_url`/`fx_orderbook` config) for a live mainnet rate.

## Render.com

The service deploys from the ghcr image built by
`.github/workflows/docker-publish.yml` (push to `elektron/main`) with
health check path `/healthz`. Free-plan disclosures: instance sleeps
unvisited, disk is ephemeral (`MM_TEST_SEED` keeps the passive wallet
addresses stable) — in self-contained mode the ephemeral disk means the
local chain is RE-MINED from genesis on every cold boot, so testnet
faucet coins are re-dug rather than persisted (a paid plan + persistent
disk keeps the chain). The deployed Render service runs the testnet default;
the daemon wallets are never funded; the faucet/miner wallet is infra-only
(client wallets get the coins).

## Security notes

The rpc password exists only inside the container (`0600` MM2.json files +
env of the two binaries); `/rpc` overwrites whatever `userpass` a browser
sends; `/elek-web-config.json` hands over `rpc_pass: ""`. The SSE endpoint
inherits the upstream daemon's unauthenticated-event-stream behaviour
(marktdaten only; tracked upstream TODO).