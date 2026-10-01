# elektron.md — fork divergence notes

This fork: `bartman081523/elektron-net-kdf` of
`KomodoPlatform/komodo-defi-framework` (current upstream location:
`GLEECBTC/komodo-defi-framework`).

This document lists **only changes implemented in this fork**
(branch `elektron/main`). Planned work is in the "Planned" section and
is **not** implemented yet. Everything not listed here follows upstream
`main` as of the most recent `kdf-upstream-*` tag.

## Implemented changes

### 1. `mm_err_handle`: implement `NotMmError` for `NonNull` (toolchain compatibility)

Commit `7ea584a`.

Newer std layouts store custom `std::io::Error` variants behind
`NonNull<dyn StdError + Send + Sync>`. Rustc derives auto traits
structurally, so the derivation of `MmError<E: NotMmError>` now walks
through those `NonNull` pointers and fails in `mm2_io` with E0277 on
recent rustc toolchains (rustc 1.98.1 in the verification below: 64x
E0277 + 5x E0599 follow-up errors in `fs.rs`/`file_lock.rs`).

Fix: implement `NotMmError` for `NonNull` as the fourth sibling of the
existing `Box`/`Arc`/`UnsafeCell` escape hatches in
`mm2src/mm2_err_handle/src/mm_error.rs`.

Verification (rustc 1.98.1):

- `cargo build --workspace`: green (298 crates).
- Unit tests: `mm2_err_handle` 4 passed / 0 failed, `mm2_io` 5 passed /
  0 failed.
- A standalone minimal crate reproduced the E0277 and confirmed the
  fix; implementing the trait directly for the trait object type is not
  possible (E0321: auto traits with a default impl cannot be
  implemented for trait objects), so a leaf type impl is required.

Upstream-sync rule: if upstream ever ships the same impl, drop this
patch duplicate during the sync instead of keeping both.

### 2. Note on nightly feature gates on stable (unchanged upstream mechanism)

Upstream `.cargo/config.toml` sets `RUSTC_BOOTSTRAP` with a per-crate
allowlist (`mm2_state_machine`, `mm2_err_handle`, `mocktopus`,
`mocktopus_macros`, `docker_tests_main`) so that their `#![feature]`
gates compile on the stable toolchain used in CI. This mechanism is
documented here because it is load-bearing for the build above; this
fork does not modify it and must not remove crates from that list.

### 3. Test baseline as of 2026-10-01 (upstream tests, fork at `7ea584a`)

- `cargo test --workspace --no-fail-fast`: everything compiles and
  runs. Failures are exclusively in tests that require external
  infrastructure which is unavailable/not reachable from this
  environment as of 2026-10-01:
  - QRC20/QTUM electrum servers (`electrum*.cipig.net:10071/20071`):
    connection refused (verified directly, not only inside tests).
  - RICK/MORTY electrum for withdraw/seedprice history tests.
  - ARRR/z_coin lightwalletd: "All the current light clients are
    unavailable."
  - Lightning node tests, orderbook network tests.
- Upstream CI (`.github/workflows/test.yml`) reaches the same split:
  unit tests (`--bins --lib`) plus integration tests
  (`--test mm2_tests_main`) with `BOB_PASSPHRASE`/`ALICE_PASSPHRASE`
  provided as GitHub secrets. Locally, providing own test seeds moves
  integration from 76 passed / 28 failed to 80 passed / 24 failed;
  the remaining failures are all the external-infrastructure classes
  listed above.
- These failures are the fork's regression baseline: during fork work,
  only deviations from this set count as regressions.

### 4. Coins layer: two-layer coins model + elektron tickers

Commits `916da1c` + `dafd7eb`.

- `coins/upstream_coins` is a frozen snapshot of the upstream coins
  repository (782 entries), `coins/elektron_overlay.json` is the fork's
  overlay, and `scripts/elektron/coins_merge.py` generates
  `coins/elektron_coins` (786 entries) deterministically.
- Overlay tickers: `ELEK` (hrp `be`), `tELEK` (hrp `tb`), `rELEK`
  (hrp `bcrt`), `rBTC` (hrp `bcrt`, own elektrond regtest); all segwit,
  `address_format` segwit, `mature_confirmations` 1.
- `scripts/elektron/coins_runtime.py` injects machine-local settings
  (`--confpath TICKER=PATH` for native mode, `--rpcport TICKER=PORT`)
  into a runtime coins file; those values never enter the repo.
- `derivation_path` entries are account level (`m/84'/1370'`) without a
  trailing index: mm2 appends the address index itself. Verified live on
  regtest -- the kdf-derived first address matches `m/84'/1370'/0'`
  derivation.
- ELEK activation is verified on regtest in both modes:
  - electrum mode against the fork electrs on 127.0.0.1:50003:
    activate -> balance -> withdraw (sign-only) ->
    `send_raw_transaction` (broadcast; the request **requires** a `coin`
    field) -> confirm -> balance convergence exact to the satoshi.
  - native mode (rBTC first, same pattern on the second chain): funding
    via `importdescriptors` into the node's keyless watch-only wallet
    "wo", then balance/transfer through the node RPC.

### 5. Native regtest harness (Docker-free) (commit `dafd7eb`)

`scripts/elektron/regtest_up.sh`, `regtest_fund.sh`, `regtest_down.sh`.

Key facts verified live on regtest, now encoded in the scripts:

- elektrond regtest params: coinbase maturity 100; block subsidy halves
  every 150 blocks (5.0 measured before h150, 2.5 around h150-299, 1.25
  measured after h300); no `generate` RPC (use `generatetoaddress`);
  `-fallbackfee` is an init arg.
- electrs (fork build) is driven with `network = "testnet"` as the
  stand-in for the regtest chain and `signet_magic = "fabfb5da"` (the
  elektrond regtest P2P magic, kernel/chainparams.cpp); pruned chains
  (MandatoryPruneDepth 100) are seeded once from `dumptxoutset` via
  `utxo_snapshot_dir` -> one-time `electrs-bootstrap.dat` holding the
  real coin txids (no synthetic ones).
- electrs logging: `log_filters` must be a plain env_logger spec
  ("INFO"). A bracket suffix makes electrs warn "invalid logging spec"
  and log nothing but the config line.
- electrs electrum port is newline-delimited JSON over TCP, not HTTP --
  HTTP probes surface as `-32700 parse error` disconnects in the electrs
  log and empty client responses.
- electrs exposes immature coinbases as spendable UTXOs; immaturity is
  only enforced at the node. If a kdf withdrawal picks an immature
  coinbase, the reject surfaces verbatim in the kdf log as an electrum
  error: `{"code": 2, "message": "bad-txns-premature-spend-of-coinbase,
  tried to spend coinbase at depth 17"}`.
- elektrond `-daemon` writes its own pid file at
  `<datadir>/regtest/elektrond.pid`; scripts read that file (never
  pkill/pgrep by pattern -- the pattern text can match the invoking
  shell itself).

## Planned (NOT implemented)

- DEX fee policy for ELEK pairs.
- Swap end-to-end tests: >= 10 native regtest swaps + refunds, then
  testnet swaps (public BTC testnet electrum servers + ELEK testnet +
  optional ETH Sepolia).

## Upstream sync policy

- Manual security ports from upstream `main`; tag
  `kdf-upstream-<date>` at every sync point (e.g.
  `kdf-upstream-20261001` on `d56a7bc`, the first fork base).
- Untouchable by design: v2 swap protocol, HTLC scripts, electrum
  client — security-sensitive code stays as upstream ships it.

## Licenses

The upstream `LEGAL/` tree, license headers, and `AUTHORS` are kept
untouched.