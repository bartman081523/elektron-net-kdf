# kdf deployment (Elektron Net market, LAN/Tailnet)

Deployment layer for running the fork as long-lived services. No Docker
(hard project rule): systemd user units + plain config files.

## Layout

| File | Purpose |
|------|---------|
| `kdf@.service` | templated systemd user unit; instance name = `%i` |
| `kdf.env.example` | EnvironmentFile template (`%h/.config/kdf/<instance>.env`) |
| `elek-web@.service` | templated systemd user unit for the static web UI server |
| `kdf-web.env.example` | EnvironmentFile template for elek-web (`%h/.config/kdf/web.env`) |
| `MM2.json.seed.example` | bootstrap node config (`is_bootstrap_node` + `i_am_seed`) |
| `MM2.json.trade.example` | trade-node config (client, dials the seed) |
| `nftables-kdf.nft` | firewall: only the seed's P2P port is reachable, LAN/Tailnet only |

The MM2.json examples ship `"rpccors": "http://localhost:3000"` and
`"event_streaming_configuration": {}` — the two keys the web UI needs in
EVERY daemon it connects to (see "Web UI" below).

## Install

```sh
# build (memory discipline: CARGO_TARGET_DIR out of the repo tree)
cd <fork checkout> && CARGO_TARGET_DIR=/run/.../kdf-release-target cargo build --workspace

# binary on an always-mounted path (the unit runs %h/.local/bin/kdf;
# systemd does not expand ${VAR} in the ExecStart executable path)
install -m0755 <dir>/kdf-release-target/release/kdf ~/.local/bin/kdf

# unit + env
cp deploy/kdf@.service ~/.config/systemd/user/
systemctl --user daemon-reload

# one env file per instance (adapts MM_COINS_PATH / MM_CONF_PATH)
mkdir -p ~/.config/kdf
cp deploy/kdf.env.example ~/.config/kdf/seed.env

# one MM2.json per instance, at the absolute path named in the env file
# (contains the seed phrase + rpc password: machine-local, NEVER commit)
cp deploy/MM2.json.seed.example <machine-local>/seed/MM2.json
```

Start: `systemctl --user start kdf@seed kdf@trade1`. Logs: the instance's
MM_LOG path (kdf logs to file, not journald).

## Port plan (netid 2)

Formula from `mm2src/mm2_main/src/lp_network.rs`: base = `(netid/10)*40 +
7783 + (netid%10)`, rpc = base+10, p2p = base+20, wss = base+30 ->
netid 2: rpc 7795, p2p 7805, wss 7815. `rpcport` in MM2.json overrides the
RPC value explicitly (the formula applies to P2P/WSS).

Observed shape (regtest + testnet phases, A-grade): only the SEED node
binds the P2P port (7805); trade instances dial the seed and run
client-only (no local listener, distinct explicit RPC ports 7792+),
ordermatch relayed through the seed. This is how every green swap in
phases 3/4 ran.

## Web UI

The browser talks DIRECTLY to the local kdf RPC (no proxy, no backend):
`http://localhost:3000` serves the SPA (elek-web, hyper static server,
GET/HEAD only, loopback only); the SPA POSTs JSON-RPC to the daemon
entered in the connect form and reads events from its unauthenticated
`/event-stream` (details in doc/elektron.md section 11).

Two MM2.json keys are required in every daemon the UI connects to:

- `"rpccors": "http://localhost:3000"` — the daemon answers every RPC
  response with this exact `Access-Control-Allow-Origin`; the browser
  must therefore display the UI as `http://localhost:3000` (the elek-web
  default binds `MM_WEB_ADDR=127.0.0.1:3000`, same thing in the URL bar).

  `"event_streaming_configuration": {}` — the mere PRESENCE of the key
  enables the SSE endpoint (its absence answers "Event streaming is
  disabled", sse_handler.rs); `{}` takes the defaults
  (`access_control_allow_origin: "*"`). `worker_path` is web-assembly
  only and irrelevant here (lp_native_dex.rs).

Install:

```sh
# build + install the static server (next to kdf in ~/.local/bin)
cd <fork checkout> && CARGO_TARGET_DIR=<dir>/kdf-release-target cargo build --release -p elek-web
install -m0755 <dir>/kdf-release-target/release/elek-web ~/.local/bin/elek-web

cp deploy/elek-web@.service ~/.config/systemd/user/
mkdir -p ~/.config/kdf
cp deploy/kdf-web.env.example ~/.config/kdf/web.env   # adapt MM_WEB_ROOT
systemctl --user daemon-reload
systemctl --user start elek-web@web
```

Browse from another machine (LAN/Tailnet) — tunnel the UI and the daemon
RPC from the SAME host, e.g. on the seed host: `ssh -L 3000:127.0.0.1:3000
-L 7795:127.0.0.1:7795 <seed-host>`; the remote browser reaches both through
127.0.0.1, so enter `http://127.0.0.1:7795` in the connect form while the
UI origin (`http://localhost:3000`) still matches `rpccors`.

## After every restart

Coins restore from the instance database, but VERIFY rather than assume:

```sh
# get_enabled_coins -> re-enable missing coins (electrum/enable) before trading;
# the RPC envelope shape and helpers live in scripts/elektron/testnet_rpc.py
```

## Firewall

The only externally reachable port is the seed's P2P (7805). Apply
`nftables-kdf.nft` so it accepts LAN (192.168.178.0/24) and Tailnet
(100.64.0.0/10) only. RPC ports bind 127.0.0.1 (rpcip in MM2.json); LAN
access happens through SSH tunnels with the rpc password, never through
open ports.

## Secrets discipline

- seed phrases and rpc passwords live ONLY in machine-local MM2.json
  files / env files at `%h/.config/kdf/` -- never in the repo, never in
  shell history for real funds.
- testnet/regtest seeds used during phase 3/4 are disposable and live on
  the test machine only.