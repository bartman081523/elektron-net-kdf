# elek-swap

CLI interface to the elektron-net p2p market. Thin fork of the upstream
Komodo `adex-cli` (deprecated upstream, excluded from the kdf workspace):
it drives the `kdf` binary instead of `mm2`.

Differences to upstream adex-cli:

- `init` fetches the coin set from the fork repository
  (`coins/elektron_coins`) instead of KomodoPlatform/coins
- the activation scheme is derived locally from that coins file (coins
  with an `electrum` server list are activated through those servers);
  there is no stats.kmd.io call
- the daemon spawned next to the CLI binary is called `kdf`
- the default netid is the elektron-net market netid (`2`), not 6133

Build inside the kdf workspace:

    CARGO_TARGET_DIR=/path/to/target cargo build -p elek-swap

Typical workflow:

    # collect seed, rpc password, seednodes (interactive prompt)
    elek-swap init --mm-coins-path coins --mm-conf-path MM2.json
    elek-swap start
    elek-swap enable <ticker>
    elek-swap balance <ticker>
    elek-swap orderbook <base> <rel>
    elek-swap sell <base> <rel> <price> <volume>

`init` prompts interactively (seed phrase, rpc password, seednodes);
`start` spawns the `kdf` binary placed next to the elek-swap binary and
sets MM_CONF_PATH / MM_COINS_PATH / MM_LOG for it. Coins without an
`electrum` entry in the coins file cannot be activated through this CLI
(a native node activation needs the daemon-side RPC).