# kdf deployment (Elektron Net market, LAN/Tailnet)

Deployment layer for running the fork as long-lived services. No Docker
(hard project rule): systemd user units + plain config files.

## Layout

| File | Purpose |
|------|---------|
| `kdf@.service` | templated systemd user unit; instance name = `%i` |
| `kdf.env.example` | EnvironmentFile template (`%h/.config/kdf/<instance>.env`) |
| `MM2.json.seed.example` | bootstrap node config (`is_bootstrap_node` + `i_am_seed`) |
| `MM2.json.trade.example` | trade-node config (client, dials the seed) |
| `nftables-kdf.nft` | firewall: only the seed's P2P port is reachable, LAN/Tailnet only |

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