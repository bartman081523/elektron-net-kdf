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

- kdf startup: the first CLI argument is an inline JSON config
  **string**, not a file path (mm2src/mm2_main/src/mm2.rs `Cli.config`
  comment: "JSON configuration string"; a path argument makes kdf fail
  with "Couldn't parse mm2 config to JSON format!"). The canonical
  file-based start is environment variables instead:
  `MM_CONF_PATH=<mm2.json> MM_COINS_PATH=<coins.json> kdf`.

### 6. DEX fee policy: no dex fee on elektron pairs (commit `758cfc8`)

Upstream behaviour: the taker of a swap pays a "DEX fee" of 2% of the
traded volume as an extra output on the taker chain to the constant raw
pubkey `DEX_FEE_ADDR_RAW_PUBKEY` (`mm2src/common/common.rs`,
`lp_coins.rs`); tickers on the `DexFee::dex_fee_rate` discount list pay
1% (currently `["GLEEC"]`).

Fork change (`mm2src/coins/lp_coins.rs`, `DexFee::new_with_taker_pubkey`):
swaps where either side is an elektron ticker (`ELEK`, `tELEK`, `rELEK`)
return `DexFee::NoFee` instead of producing a fee. No output to the
upstream pubkey constant is ever created and the fee stays inside the
network. The GLEEC discount list and `dex_fee_rate` itself are
deliberately unchanged: pairs without an elektron side keep paying the
upstream fee.

Why NoFee is safe: `DexFee::NoFee` is a first-class upstream protocol
state, not a fork invention:

- taker state machine: the fee transaction is skipped entirely
  ("Taker fee tx not sent for dex taker",
  `lp_swap/taker_swap.rs`) and the swap goes straight to
  WaitForMakerPayment,
- maker: recomputes the identical `DexFee` from the pair and the taker
  pubkey, accepts an empty fee ident ("Taker fee is not expected for
  dex taker", `lp_swap/maker_swap.rs`) and proceeds to SendPayment,
- utxo paths handle NoFee (empty fee output set, taker payment spend
  signs only the maker payment).

Both peers derive the NoFee decision from the same pair/taker-pubkey
computation, so they cannot disagree. The fork's NoFee branch is only
taken for non-privacy coins, mirroring the upstream burn-pubkey check;
privacy coins reject NoFee explicitly (z_htlc.rs).

Fork follow-up fix (`mm2src/coins/utxo/utxo_common.rs`): upstream
`get_fee_to_send_taker_fee` builds the fee tx to price it for
trade_preimage. For NoFee the output set is empty, which upstream
surfaces as "Couldn't generate tx with empty output set" -- a state
unreachable for ordinary takers before this fork. The fork returns a
zero `TradeFee` for NoFee instead.

Verification (regtest, rELEK/rBTC, volume 10):

- pre-patch binary: `trade_preimage` taker fee = 0.2 rELEK (upstream
  2%),
- patched binary, sell rELEK/rBTC: `taker_fee` = (rELEK, 0),
  total_fees = miner fees only,
- patched binary, buy rELEK/rBTC: `taker_fee` = (rBTC, 0), total_fees
  = miner fees only.

Upstream dex-fee tests pass unchanged
(`cargo test --workspace dex_fee`: test_dex_fee_amount,
test_generate_taker_fee_tx_outputs_with_standard_dex_fee, the eth and
z_coin validate_dex_fee tests, and
test_dex_fee_burn_split_with_discount_and_standard_coins -- all green;
the discount/burn-split logic really runs in the last one).

### 7. Swap end-to-end verification on regtest (2026-10-01)

`scripts/elektron/` scenario layer on top of section 5's node setup:
`swap_rpc.py` (RPC helpers), `swap_e2e.py` (driver: `single <n> <role>
<base> <rel> <vol> <price>`; role = which instance is the maker),
`swap_fee_report.py`, the scenario wrappers (`regtest_swap_maker_elek.sh`,
`regtest_swap_taker_elek.sh`, `regtest_fee.sh`, `regtest_refund.sh`,
`regtest_teardown.sh`), three refund drivers (`refund_r1.py`: maker
killed while the taker waits, `refund_r2.py`: maker killed during a
miner freeze, `restart_recovery_watch.py`: post-restart recovery of an
expired maker payment) and a passive watcher (`refund_maker_watch.py`)
that completes maker-side locktime refunds. All records go to JSONL
files under `$RL_ROOT/runs/` (machine-local, not in the repo). Two kdf
instances ("alice" 7793, "bob" 7794, netid 2, both coins in electrum
mode against the local electrs pair) plus a seed node on 7792/7805.

Result: **17 successful swaps across both roles** -- 15 with bob as
maker (rELEK->rBTC, volume 2-10 @ price 0.001, n=10, 20-24, 30-34, 35,
991 plus the two kill-window swaps) and 2 with alice as maker
(rBTC->rELEK, n=911, 912). `swap_fee_report.py` finds no dex fee, no
leaks, and lists only miner fees for all of them (problems: 0),
consistent with section 6.

Protocol facts verified live (A-grade logs, exact event names):

- Swap event vocabulary on this build: Started, Negotiated,
  MakerPaymentInstructionsReceived, TakerFeeValidated, MakerPaymentSent,
  MakerPaymentReceived, MakerPaymentValidatedAndConfirmed,
  TakerPaymentInstructionsReceived, TakerPaymentWaitConfirmStarted,
  MakerPaymentWaitRefundStarted, MakerPaymentRefundStarted, (...Spend /),
  success and failure both end in Finished; `my_swap_status` reports
  `is_finished` + `is_success` (both false right after `buy` -- the swap
  registers in the RPC with ~0-6 s latency, lp_swap.rs "No swap with
  uuid"). A failed taker-side payment emits
  TakerPaymentTransactionFailed; a maker waiting for taker payment data
  that never arrives ends with TakerPaymentValidateFailed timeout.
- `my_swap_status` response is a bare result dict with `events`,
  `is_finished`, `is_success`, maker/taker amount+coin -- there is no
  "status" string field; consumers must derive state from the event list.
- `cancel_all_orders` (legacy): the `CancelBy` enum
  (`lp_ordermatch.rs` CancelBy: All | Pair | Coin) is parsed from a
  TOP-LEVEL body field `cancel_by`, not inside `params`
  (`{"cancel_by": {"type": "All"}}`).
- `unban_pubkeys` (legacy, dispatcher_legacy.rs): body field
  `unban_by`; form `{"unban_by": {"type": "All"}}` clears every ban
  including the ones created by ban reasons of type FailedSwap.
- `mmrpc 2.0 withdraw` returns `tx_hex` + `fee_details` (Utxo type,
  amount); broadcast with legacy `send_raw_transaction` using top-level
  `coin`/`tx_hex` fields. This is how coins move instance-to-instance
  without descriptor-wallet imports.
- Swap direction on this build (taker state machine,
  `mm2src/mm2_main/src/lp_swap/taker_swap.rs:180-210`): the taker never
  claims first. After MakerPaymentValidatedAndConfirmed the taker sends
  his own payment (`TakerPaymentSent` -> `WatcherMessageSent`, the secret
  reaches the maker off-chain) and enters `WaitForTakerPaymentSpend`; it
  is the MAKER who claims the taker's payment, and only the taker-side
  `TakerPaymentSpent` event moves the taker on to `SpendMakerPayment`
  (`MakerPaymentSpent` -> `MakerPaymentSpendConfirmed` -> `Finished`).
  The same mapping covers the failure side: with the maker dead the wait
  ends in `TakerPaymentWaitForSpendFailed` ->
  `PrepareForTakerPaymentRefund` -> `TakerPaymentRefundStarted`/`...Refunded`/
  `...RefundFinished` -> `Finished`.
- Full green swap timeline at 3-s blocks (maker-side event timestamps,
  swap 3665dbae): Started +0s, Negotiated +2.0, MakerPaymentSent ~+3.0,
  TakerPaymentReceived (taker payment data arrives -- the taker sends it
  only after her payment confirmed) +20.1, TakerPaymentValidatedAndConfirmed
  +22 ms later, TakerPaymentSpent (maker's claim broadcast) +158 ms,
  TakerPaymentSpendConfirmed +15.1, Finished: ~35 s end to end.
  Consequence: between the maker's `TakerPaymentWaitConfirmStarted` and
  `TakerPaymentValidatedAndConfirmed` lie only ~22 ms on this coin setup,
  so poll-based kill triggers must watch the TAKER's
  `MakerPaymentWaitConfirmStarted` window (~7 s: the taker's own
  confirmation wait for the maker payment) instead.
- Restart semantics (verified live, matters for every restart): a
  restarted instance restores its swap history from the DB but coins are
  NOT re-activated automatically -- the log repeats "Can't kickstart the
  swap <uuid> until the coin rELEK is activated" until an activation call
  runs again; restored swaps kickstart on their own the moment the coins
  are enabled. Every restart step of the harness therefore calls the
  coins-activation helper (`ensure_coins` in `swap_rpc.py`) before
  asserting anything about swaps.

Failure forensics (all root-caused on live logs, worth keeping because
each is a real upstream blindspot on modern regtest nodes):

1. `bad-txns-premature-spend-of-coinbase, tried to spend coinbase at
   depth 59`: a kdf withdrawal selected an immature coinbase; the
   electrum proxy surfaces the node reject and the swap fails as
   MakerPaymentTransactionFailed. Prevention: fund from a sink address
   fed by an independent miner loop, not directly from coinbases
   (section 5).
2. An rBTC maker whose coins config still carried mainnet-style Base58
   address formats offered `3...` payment addresses; the regtest node
   rejects them ("Invalid or unsupported Base58-encoded address") and
   the taker's payment broadcast fails with TakerPaymentTransactionFailed.
   The overlay coins file had to pin `segwit`/`bech32_hrp bcrt` for both
   rELEK and rBTC before swaps went green.
3. Native-mode (node RPC) activation: kdf watches maker payment
   addresses with legacy `importaddress`, which no longer exists on
   bitcoind v29 descriptor-only wallets ("Method not found"). Swapping
   in electrum mode avoids the wallet import path entirely; every green
   swap here runs electrum mode.
4. Any swap that fails bans its counterpart automatically for 1 h
   (`ban_pubkey_on_failed_swap`, `lp_swap/pubkey_banning.rs`); while the
   ban is live the other side's match requests are silently swallowed
   (`Pubkey ... is not allowed`, `lp_ordermatch.rs`) -- `buy` then
   publishes a taker ORDER and returns the order uuid, which looks like
   a swap uuid but never registers. Diagnose with
   `list_banned_pubkeys`, clear with `unban_pubkeys`; the scenario
   scripts therefore run a preflight (cancel orders + unban) against
   both instances before every match attempt, and refund drivers write
   explicit "kill window missed" records instead of failing silently.

Refund verification (scripted, records in runs/refund-results.jsonl,
run dates 2026-10-01). Two locktime rules drive everything (verified via
`my_swap_status` timestamps and the on-chain outcomes): the taker payment
becomes refundable at `started_at + 7800 s` (~2h10m), the maker payment
at `started_at + 2*7800 s` (~4h20m). Four refund shapes are recorded:

1. Maker-side refund, taker payment never arrived (`refund_maker_watch.py`,
   4 records): the maker payment stays locked; once its locktime passes the
   maker side runs
   `MakerPaymentSent -> TakerPaymentValidateFailed ->
   MakerPaymentWaitRefundStarted -> MakerPaymentRefundStarted ->
   MakerPaymentRefunded -> MakerPaymentRefundFinished -> Finished`.
2. Taker refund with a dead maker (`refund_r2.py`, swap f4679a0e): the
   maker (alice) is SIGKILLed while her payment is broadcast-but-still-
   unconfirming (the miner loop is frozen so the confirmation window
   cannot close). The taker (bob) proceeds alone --
   `TakerPaymentSent -> WatcherMessageSent -> TakerPaymentWaitForSpendFailed
   -> TakerPaymentWaitRefundStarted -> TakerPaymentRefundStarted ->
   TakerPaymentRefunded ->
   TakerPaymentRefundFinished -> Finished` at the taker locktime. The
   maker's payment is never spent; it stays HTLC-locked until the maker
   locktime. The same shape holds when the maker is killed at ANY later
   point: the taker only ever waits for the maker's claim.
3. Maker-payment recovery after restart (`restart_recovery_watch.py`,
   swap f4679a0e again): the maker instance is restarted once
   `started_at + 2*7800 s` has already passed; after the coins are
   re-enabled the restored swap state runs the maker refund chain
   immediately (`MakerPaymentWaitConfirmFailed -> WaitRefund -> Refund ->
   Finished`), returning the payment minus miner fee.
4. Kill-window swap converted into cooperative completion (`refund_r1.py`,
   swaps fb922309 + c3f0be69): the maker (bob) is SIGKILLed while the
   taker waits for his payment to confirm (trigger: the taker's
   `MakerPaymentWaitConfirmStarted` -- her event list ends exactly before
   `TakerPaymentSent`). Bob's restart restores his swap state at
   `MakerPaymentSent`; the taker's payment data arrives over the P2P
   connection, bob claims the taker payment and the swap completes green
   for both parties -- restart-resume with no state leak. Operational
   consequence: to collect refund evidence the killed side must stay DOWN
   until the counterpart's refund has run; a restarted restored state can
   still complete the swap (observed twice). With the maker staying dead,
   the pure taker-refund shape is that of scenario 2 -- the taker state
   machine is role-agnostic.

Restart acceptance (all A-grade across several restarts of both
instances on 2026-10-01): the interrupted swap resumes with its full
event history intact from the DB (no duplicate events, no state leak;
restored swaps kickstart on coin activation -- see the restart-semantics
bullet above); swaps whose counterpart stays live complete
cooperatively after a restart (swaps fb922309, c3f0be69); stuck maker
payments keep their refund retries across restarts and complete at
locktime expiry while the instance is up; an expired maker payment
refunds immediately after restart (swap f4679a0e, shape 3).

## Planned (NOT implemented)

- Testnet swaps: public BTC testnet electrum servers + ELEK testnet +
  optional ETH Sepolia.

## Upstream sync policy

- Manual security ports from upstream `main`; tag
  `kdf-upstream-<date>` at every sync point (e.g.
  `kdf-upstream-20261001` on `d56a7bc`, the first fork base).
- Untouchable by design: v2 swap protocol, HTLC scripts, electrum
  client — security-sensitive code stays as upstream ships it.

## Licenses

The upstream `LEGAL/` tree, license headers, and `AUTHORS` are kept
untouched.