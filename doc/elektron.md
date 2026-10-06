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
  `coins/elektron_coins` (787 entries) deterministically.
- Overlay tickers: `ELEK` (hrp `be`), `tELEK` (hrp `tb`), `rELEK`
  (hrp `bcrt`), `rBTC` (hrp `bcrt`, own elektrond regtest), `tBTC`
  (public Bitcoin testnet3); all UTXO entries segwit, `address_format`
  segwit, `mature_confirmations` 1 except tBTC (100, testnet3 rules).
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

### 8. Testnet layer: private ELEK testnet + swaps + refunds (2026-10-01)

The testnet layer reuses the regtest harness core (`swap_rpc.py`) with a
testnet coin set and two drivers of its own (`testnet_swap_e2e.py`,
`testnet_rpc.py` for activation helpers, `testnet_fund.py` for funding;
node/electrs bring-up in `testnet_up.sh`). Coin set: `tELEK` (private
Elektron testnet chain "etn1", real testnet genesis and PoW retargeting,
60 s target spacing, 100-block coinbase maturity, its own electrs
instance), `rBTC`/`rELEK` (the kept regtest legs) and `tBTC` (public
Bitcoin testnet3 via upstream electrum servers, overlay entry).

Chain bring-up facts (`testnet_up.sh`, verified live):

- elektrond clears the seed list on testnet (`CTestNetParams`), so the
  chain is single-node and mined by a local loop (machine-local
  `testnet_miner.py`, never committed).
- Initial mining REQUIRES an explicit high `maxtries`: at genesis
  difficulty the target is 2^231 (~33.5M hashes per block) while the
  default sweep is capped at 1e6 attempts, and the fork's
  `GenerateBlock` breaks SILENTLY on a failed sweep (no RPC error) --
  `generatetoaddress [..., 1000000000]` is the working form.
- A young chain has no mempool fee history: the wallet estimator returns
  nothing and `sendtoaddress` aborts with "Fee estimation failed.
  Fallbackfee is disabled" (code -6). The fork has no `settxfee` RPC;
  the working paths are `-fallbackfee` in the node config or a named
  `fee_rate` (atom/vB) argument on `sendtoaddress` in object-params
  form (`{"address": ..., "amount": ..., "fee_rate": 1}`), which is what
  `testnet_fund.py` uses. 1 atom/vB matches the coins-file txfee.

Runtime behaviour verified on this layer:

- Coins activation on the testnet legs uses upstream public electrum
  servers for tBTC (no funding required for activation):
  `tELEK`/`tBTC` activate on both instances; SEPOLIAETH activates via
  legacy `enable` with the test helper's RPC urls
  (sepolia.drpc.org et al.) and the upstream test swap contract
  `0xeA6D65434A15377081495a9E7C5893543E7c32cB` -> address + balance 0.
- "mm2 param is not set neither in coins config nor enable request"
  (`lp_coins.rs` coins_conf_check) is NOT evidence against the coins
  file: when the ticker is absent from the LOADED registry at all, the
  same error fires. It is therefore a stale-process symptom -- the
  first tBTC blocker here was an old kdf instance still holding a coins
  file from before the overlay entry existed. Check the process owning
  the RPC port, not only the file on disk.
- Mixed-chain swaps run green across chains and roles:
  4x bob-as-maker tELEK->rBTC (records 1-3 + one earlier unrecorded
  run) and 3x alice-as-maker rELEK->tELEK (records 7-9 of
  `$TL_ROOT/runs/swap-results-testnet.jsonl`), volumes 10 at price
  0.001 / 1. All balance deltas reconcile to the vol minus miner fees.
- `trade_preimage` on tELEK/rBTC reports base_coin_fee + rel_coin_fee
  (miner fees, the rel fee flagged paid_from_trading_vol) and NO dex
  fee entry, consistent with section 6's NoFee policy on elektron
  pairs.

Refund + recovery evidence on the testnet legs (records in
`$TL_ROOT/runs/refund-results-testnet.jsonl`):

1. Kill + restart mid-swap, cooperative completion (swap 6591a540,
   `refund_r2t.py` run 1; taker_refund_ok=false by design of the
   record): alice (maker) is SIGKILLed at `MakerPaymentSent` (trigger:
   bob's `MakerPaymentWaitConfirmStarted`, miner frozen for the window
   then resumed); the restarted instance kickstarts the restored swap
   and completes it green -- bob's events carry the full success chain
   ending `TakerPaymentSpent -> MakerPaymentSpent ->
   MakerPaymentSpendConfirmed -> Finished`, both sides Successful, and
   bob learned the secret from alice's on-chain claim tx, not from the
   dead instance. Same restart-resume shape as regtest scenario 4,
   now proven across a testnet/regtest mix.
2. Maker stays dead -> taker locktime refund (swap d894e4fd): same
   kill window, but the maker instance stays down; the taker refunds
   his own tELEK payment at the taker locktime (started_at + 7800 s).
   Bob's full event chain:
   `Started -> Negotiated -> TakerFeeSent (empty: no dex fee on
   elektron pairs) -> TakerPaymentInstructionsReceived ->
   MakerPaymentReceived -> MakerPaymentWaitConfirmStarted ->
   MakerPaymentValidatedAndConfirmed -> TakerPaymentSent (real P2SH
   HTLC tx) -> WatcherMessageSent -> TakerPaymentWaitForSpendFailed ->
   TakerPaymentWaitRefundStarted -> TakerPaymentRefundStarted ->
   TakerPaymentRefunded (real refund tx with CLTV-input signature) ->
   TakerPaymentRefundFinished -> Finished`. `taker_refund_ok: true`
   in the record; bob's tELEK live balance after recovery is
   19.99997879 (pre-swap 19.99998548 -- the 10 are back, cost is the
   payment + refund tx fees, ~669 atoms). The record's `post` balance
   fields understate the truth: they were captured during a mid-refund
   infra outage (see below); live balances after recovery are
   authoritative. Alice's 10 rELEK stay in the maker HTLC until her
   locktime (started_at + 15600 s), refundable after that.

   Infra outage inside the refund window (recorded honestly): both the
   testnet elektrond and its electrs instance terminated at 22:32
   (clean `Shutdown done` daemon shutdowns, no OOM evidence, no reboot,
   root cause of the signal wave unidentified), exactly while bob was
   between `TakerPaymentRefundStarted` and broadcast confirmation.
   The swap protocol weathered it: kdf retried the electrum transport
   every 30 s (`can_refund_htlc` retry loop) and completed the refund
   as soon as the electrs instance was brought back -- no state loss,
   no re-negotiation. Property proven: a taker refund survives a total
   server outage, as long as the chain stack returns before the maker
   locktime.

   Maker-side completion (verified after the fact): the swap was
   restored in alice's restarted instance (23:50, relogged
   `MakerPaymentWaitConfirmFailed -> MakerPaymentWaitRefundStarted ->
   MakerPaymentRefundStarted` immediately on restart) and the refund
   branch ran unattended at her locktime: `MakerPaymentRefunded ->
   MakerPaymentRefundFinished -> Finished` at 01:44:51, 19 s past
   started + 15600 s (01:44:33). Her rELEK live balance is back at
   969.49985483 (pre-swap 969.49986424; only tx-fee dust missing).
   d894e4fd therefore closes both kill-test branches: the taker
   refunded while the maker was dead, and an unattended maker instance
   refunded immediately at its locktime.

Swap-fee final tally over both result files (swap_fee_report.py): 15
regtest + 6 testnet green swaps, every one `no_fee=True`, zero
DEX-fee-to-pubkey script leaks, zero sides unavailable for
verification (the three regtest `UNVERIFIED` entries from the earlier
partial run resolved once the killed alice instance was restarted and
the full report re-run).


- Funded swaps on public testnet chains: tBTC volume via public
  faucets is impractical today (dispenses <=0.001 per request with
  per-IP caps); swaps on tBTC/sepolia legs need a funded tap first.
  All public-chain coin activation paths are verified (section 8).
- Funded Sepolia (SEPOLIAETH) swaps: activation verified, funding not
  yet arranged (public Sepolia faucets, then a trade).

### 9. elek-swap CLI (fork of `mm2src/adex_cli`) (2026-10-02)

`mm2src/elek_swap/`: a thin CLI wrapper that runs alongside the kdf
service binary, for users who want command-line access to the p2p
market without hand-writing RPC calls. Workspace member `-p elek-swap`
(requires the root `Cargo.toml` membership); binary lands next to `kdf`
in the same target dir. Fork deltas, all build/runtime-verified on
regtest (port 7798, netid 2, seed 7805):

- `activation_scheme_db/init_activation_scheme.rs`: upstream adex_cli
  downloads the activation scheme from stats.kmd.io; the fork derives
  it locally from the `--mm-coins-path` file instead — every coin with
  an `electrum` server list becomes an `electrum` activation entry.
  Coins without `electrum` are not activatable through this CLI (they
  need native node RPC params in the coins file).
- `Cargo.toml`: consumer-side `chrono` features. Root declares chrono
  `default-features = false`; in the full workspace the walletconnect
  members (`pairing_api`, `relay_rpc`) pull in feature `clock`, in the
  `-p elek-swap` closure nothing does → E0432/E0599 in `common/log.rs`.
  Fix mirrors upstream's own `mm2_bin_lib:46` pattern:
  `chrono = { workspace = true, features = ["clock", "std"] }`.
- `init_activation_scheme.rs`: caller-side imports for
  `error_anyhow!` — `macro_rules!` expands textually at the call site,
  so the calling module must import `anyhow!` and `error!` itself
  (same pattern as `init_mm2_cfg.rs`).
- Removed dead `stuff` dependency entry.

CLI surface (`cli.rs`): `start` (env `MM_CONF_PATH`/`MM_COINS_PATH`/
`MM_LOG`, then spawns `kdf` from the binary's own directory — detached,
non-blocking), `stop` (RPC stop, only the configured URI instance),
`status`/`kill` (**machine-wide**: list/kill every `kdf` process found
by name — do not run `kill` on a host that runs other kdf instances),
`enable`, `balance`, `get-enabled`, `orderbook`, `sell`/`buy`
(positionals: `base rel volume price`; `--uuid`/`--public` are
*match* selectors for `match_uuids`/`match_publics`, **not** an
order-visibility flag), `version`, `init` (interactive passphrase
prompt, needs a TTY).

Regtest E2E evidence (2026-10-02, all via the CLI + raw RPC):
build green, 10/10 unit tests green; rELEK/rBTC activation; funding of
the maker address 100 rELEK via the section 5 documented
withdraw+send_raw_transaction path (regtest coinbase subsidy is 0 at
current heights, so mining funds nothing); `sell rELEK rBTC 1.0 10`
(GTC) became a maker order after the ~30 s taker matching window and
propagated through the netid-2 seed; a taker on another instance took
the full order via legacy-typed `buy` (FOK); both sides reported
`Finished`; balances converged (maker -1 rELEK +10 rBTC, taker +1
rELEK -10 rBTC, only network fees — no DEX fee on the pair, per
section 6's policy).

Caveats verified live:
- A fresh `sell` spends up to 30 s in the taker matching phase; the
  orderbook only shows it after conversion to maker.
- `/home/<user>/.config/elek-swap/activation_scheme.json` is rewritten
  by `cargo test -p elek-swap` (the scheme test writes its fixture to
  the real config dir). Regenerate the real scheme afterwards.
- kdf rejects weak rpc passwords at startup (`Password should contain
  at least 1 uppercase character` etc.) and exits immediately; fixture
  passwords must satisfy the policy (>=8 chars, one of each class, no
  3 repeated, no `<>&`).
- Legacy RPC forms (`orderbook`, `cancel_order`) take their fields at
  body top level (no `params`), and legacy `orderbook` returns the
  book top level (no `result` wrapper). `cancel_order` needs the
  `uuid` at top level.

### 10. Deployment layer (systemd user units, netid-2 market) (2026-10-02)

`deploy/`: run the fork as long-lived services without Docker.

- `kdf@.service`: templated systemd **user** unit (instance name `%i`).
  Pitfall verified live the hard way (journal, status 203/EXEC,
  "Unable to locate executable '${KDF_BIN}'"): systemd does **not**
  expand `${VAR}` from EnvironmentFile in the ExecStart executable path
  — use a specifier-resolved fixed path (`ExecStart=%h/.local/bin/kdf`)
  and install the release binary there (`install -m0755`). Environment
  variables from the EnvironmentFile (RUST_LOG, MM_LOG, MM_COINS_PATH,
  MM_CONF_PATH) reach the process normally.
- `kdf.env.example` + `MM2.json.{seed,trade}.example`: one env file at
  `~/.config/kdf/<instance>.env` and one MM2.json per instance. MM2.json
  holds seed phrase + rpc password: machine-local, `0600`, never
  committed and never printed.
- Port plan (netid 2, formula in `lp_network.rs`): seed RPC 7795, trade
  RPC 7796 (loopback only, `rpcip 127.0.0.1`), seed P2P 7805 — the only
  externally reachable port. Trade nodes dial the seed (client-only).
- `nftables-kdf.nft`: inet table `elektron_kdf`, input chain at
  priority filter+10 dropping tcp/7805 from every source outside
  LAN `192.168.178.0/24` and Tailnet `100.64.0.0/10` (loopback exempt).
  Applied live: `nft list table inet elektron_kdf` shows both rules.
  A functional drop test needs a non-LAN source, which this host cannot
  produce — rule presence is the verification level reached.
- Coins do NOT reliably rejoin the enabled set on restart: observed live
  on both deployed instances (2026-10-02, right after the web-key patch),
  the restored electrum client re-engaged (its server `connected via
  TCP` log line) while `get_enabled_coins` kept answering
  `{"result":{"coins":[]}}` for well over 40s — the coin becomes visible
  again only once a fresh `electrum` call lands (one call per instance
  completed activation instantly). Always verify with
  `get_enabled_coins` and re-activate missing ones. Also verified live:
  the mmrpc-2.0 `electrum` method **does not exist** on this build
  (dispatcher "No such method"), and the mmrpc-2.0 envelope rejects a
  top-level `coin` field — activation is the legacy envelope
  `{"method": "electrum", "coin": <ticker>, "servers": [{"url": ...,
  "protocol": "TCP"}], "required_confirmations": N}` top-level, exactly
  as `scripts/elektron/testnet_rpc.py` does it on regtest/testnet. The
  first call may return an empty 500 body; retry and confirm via
  `get_enabled_coins`.
- Live state after deployment (netid 2): `kdf@seed` + `kdf@trade1`
  active (running) under systemd, Linger=yes; both RPCs answer
  `3.0.0-beta_f600dd5`; seed binds 0.0.0.0:7805 + loopback 7795, trade1
  binds loopback 7796 and dials the seed; ELEK activated on both
  instances with mainnet-hrp `be1...` addresses against the
  machine-local electrs (192.168.178.21:50002); the elek-swap CLI
  config points at the trade1 RPC.

### 11. Web UI: market SPA + `elek-web` static server (2026-10-02)

Architecture (no proxy, no backend, no accounts — the browser is the
only client and talks straight to the user's own daemon):

```
browser (http://localhost:3000, hash router)
  ├─ GET static files            → elek-web (hyper, loopback :3000)
  ├─ POST JSON-RPC (CORS)        → kdf (loopback 779x, `rpccors`)
  └─ GET /event-stream?id=<u64>  → kdf (SSE, unauthenticated)
```

- `mm2src/elek_web` serves `web/` from disk. hyper + tokio only (the
  workspace ships no embedding/mime crates, checked at plan time).
  hardening in `resolve()` (main.rs): no percent-encoding, no `..` or
  empty path segments, canonicalize + prefix check against the root
  (symlink/`..` escapes fail), directories are not listed. GET/HEAD
  only (RPC to the daemon, never to the server). Every response carries
  `x-elek-web: 1` and `Cache-Control: no-cache`; `.mjs` maps to
  `text/javascript` (a `octet-stream` MIME would make the browser
  refuse the ES modules). `MM_WEB_ROOT`/`MM_WEB_ADDR` env, defaults
  `web` / `127.0.0.1:3000`; deploy unit `deploy/elek-web@.service` +
  `deploy/kdf-web.env.example`.
- There is **no WebSocket RPC on this build** (0 hits for
  orderbook_ws/wsport/accept_async); the live channel is the SSE
  endpoint `GET /event-stream` (rpc.rs:347-356 →
  `mm2_net::event_streaming::sse_handler`).
- **SSE endpoint is unauthenticated** (upstream TODO at rpc.rs:345):
  whoever can reach the RPC port can READ streamed events
  (`ORDERBOOK_UPDATE:orbk/<pair>` — the pair in alb-sorted `base:rel`
  form, e.g. `orbk/rBTC:rELEK` — plus swap status, balance...). Nothing
  secret is carried in event payloads (orders/swaps of the local net),
  and enabling or disabling a stream stays behind the rpc password —
  but a password-holding client can cross-enable events for another
  (unknown-id) client. Exposure is bounded by the loopback-only RPC
  bind (`rpcip`) and the tunnel recipe in deploy/README.md; disclosed
  here because upstream has not made the call yet.
- Config keys (both live-patched into the deployed MM2.json files):
  - `rpccors`: single string, the daemon stamps this exact
    `Access-Control-Allow-Origin` onto every RPC response
    (rpc.rs:255-264); HTTP default `http://localhost:3000`, HTTPS
    default `https://localhost:3000`. Browse the UI as
    `http://localhost:3000` or preflights fail.
  - `event_streaming_configuration`: its mere PRESENCE turns the SSE
    endpoint on — without it `/event-stream` answers "Event streaming
    is disabled" (sse_handler.rs:15-17, `from_value(Null)` fails →
    None). `{}` is enough on native: `worker_path` is wasm-only
    (lp_native_dex.rs:365-371), `access_control_allow_origin` defaults
    to `*` and is stamped on SSE responses (configuration.rs,
    sse_handler.rs:50-51). Duplicate `?id=` answers 500 "ID already in
    use" — the SPA's `sse.mjs` picks a random u64 per tab session and
    retries on collision. The daemon appends a trailing space to each
    `data:` line (`format!("data: {data} \n\n")`); the mock does not,
    both parse identically.
  - SSE stream lifecycle (live-pinned 2026-10-02 on the seed and
    regtest-alice): a `stream::*::enable` call is only valid for a
    client that is CURRENTLY connected — enable before the SSE
    `GET /event-stream?id=N` answers with v2 `UnknownClient`
    (`error_type EnableError`); enable with a connected id succeeds and
    echoes the canonical streamer id (`{"streamer_id":
    "ORDERBOOK_UPDATE:orbk/rBTC:rELEK"}`). There is NO initial snapshot:
    the first event only arrives with the next book change (a fresh
    `setprice` produced the first frame; a `stream::disable` after the
    connection closed answers `UnknownClient` too — the daemon prunes
    disconnected clients). `stream::heartbeat::enable` requires
    `params.config` (`{"config": {}}` takes the 5s default,
    `stream_interval_seconds`); its frames are
    `data: {"_type":"HEARTBEAT","message":{}}`.
  - Auto-connect handoff: with `MM_WEB_RPC_URL` + `MM_WEB_RPC_PASS` in
    elek-web's env, the static server serves
    `{"rpc_url": …, "rpc_pass": …}` at `/elek-web-config.json` (404
    without the pair) and the SPA's boot fetch
    (`tryAutoConnect`, `web/js/main.mjs`) probes the daemon and saves
    the session — the connect form disappears from live operation.
    Precedence in the SPA: existing tab session > dev-autologin query >
    this endpoint; a failed probe or missing pair falls back to the
    connect form, and "forget this daemon" disconnects for the tab only
    (the next reload auto-connects again). Disclosed trust model: this
    publishes the daemon's rpc password to every browser that can reach
    the static server — acceptable under the loopback/ssh-tunnel-only
    deployment, invalid if port 3000 is ever opened to the network.
    Verified live (plain `http://localhost:3000` visit: session +
    `#/orderbook`, no form; the dev-autologin test harnesses stay green
    because the dev query outranks the endpoint).
- Envelope contract (pinned live in phases 3-6 + F1-F4, mirrored in
  web/test/mock_daemon.py):
  - v2 envelope (`mmrpc:"2.0"`, everything inside `params`) exists for
    `version`, `withdraw` (preview; **never broadcasts** — build+sign
    only, see section 5's two-step), `trade_preimage`,
    `my_swap_status` (reads `params.uuid` — the one legacy exception),
    `get_enabled_coins`, `stream::*::enable`.
  - legacy top-level for everything else: `electrum` (activation;
    the v2 method does not exist on this build), `disable_coin`,
    `orderbook`, `sell`/`buy`, `cancel_order` (uuid top-level),
    `cancel_all_orders`, `my_orders`, `my_balance`, `my_tx_history`,
    `send_raw_transaction` `{coin, tx_hex}` → `{tx_hash}`,
    `my_recent_swaps`, `active_swaps`.
  - error semantics: legacy RPC-level errors answer **HTTP 500 with a
    JSON body**, v2 errors answer **200 in-band** (`mmrpc: "2.0"` +
    `error`/`error_data`); the response STATUS is never load-bearing
    to the client — it parses whichever body arrives.
  - ticker contract: case-sensitive (test-chain prefixes stay lowercase,
    `rELEK`/`rBTC`/`tBTC`); no `.toUpperCase()` anywhere in the UI.
- Sessions: the rpc password lives in `sessionStorage` only (dies with
  the tab); nothing is stored server-side or in localStorage. The UI
  holds no trading state — the daemon is the truth (its instance db
  restores the enabled coin set); the SPA polls as the fallback truth
  (orderbook ~8 s) and uses SSE as live polish.
- Degraded states are design product, not afterthought: every view
  paints an `error` block inside its table holder on RPC failure, the
  coins view additionally paints its last-known table labelled STALE
  (error banner above it) once it has seen a list, and the add-coin
  form stays usable while the daemon is down. Verified for mock modes
  err/empty/slow and a killed daemon on all five views
  (CDP sweep harness; the harness files live in /tmp, not the repo).
- `web/test/mock_daemon.py`: stdlib-only mock of RPC + SSE for UI
  iteration without a daemon, with four falsification modes
  (`ok`/`err`/`empty`/`slow`) settable at runtime
  (`mock.set_mode`); its shapes were corrected against the live
  regtest daemon, never the other way round. The selftest page
  (`/selftest.html`) asserts the contract in-page against whichever
  RPC URL it is pointed at; the dev-autologin query
  (`#/connect?dev-url=…&dev-pass=…&next=<view>`) is a test-only
  convenience (the password lands in sessionStorage, not the repo).
- Deployed live: elek-web built release (`-p elek-web`), installed at
  `%h/.local/bin/elek-web`, unit `elek-web@web` running with
  `MM_WEB_ROOT` at the checkout's `web/`; both deployed MM2.json files
  patched with the two keys and the daemons restarted (see
  deploy/README.md "Web UI").

### 12. UI test campaign: two real users end-to-end (2026-10-05)

Same-origin proxy variant of the section-11 architecture: elek-web doubles
as a one-daemon reverse proxy (`MM_WEB_PROXY_URL` + `MM_WEB_RPC_PASS`) —
`POST /rpc` forwards to the kdf RPC with `userpass` injected server-side,
`GET /rpc/event-stream` passes the SSE through with the query intact,
`/healthz` reports liveness, and `/elek-web-config.json` hands over
`{"rpc_url":"/rpc","rpc_pass":"","rpc_proxy":true}` so the SPA connects
without ever holding the daemon password (`web/js/main.mjs:148-158`
saves the proxy session). The direct-CORS mode of section 11 coexists.

Setup and truth source (A-grade, everything executed live):

- two regtest kdf instances as two independent users — alice RPC :7793,
  bob RPC :7794 — behind two elek-web proxy instances (:3010 → alice,
  :3011 → bob). A headless-chromium CDP harness drove the real SPA per
  user (fresh browser profile per run); balances are asserted as
  `my_balance` deltas around each step, order/swap truth is `my_orders`,
  `orderbook`, `my_recent_swaps`, `my_swap_status` read in-page through
  the same proxy path the views use. Harness files are /tmp-only.
- results by phase: T1 instance/port discovery; T2 the full section-11
  envelope contract exercised in-page from both users; T3 deposits with
  exact-delta asserts (rELEK/rBTC); T4 the two-step withdraw with fee
  preimage, error forms, Max and Discard, plus real broadcasts (rBTC
  0.05, tELEK 1.0, rELEK 0.5) and a peer-side receipt verified.
- T5 trades, both directions fully in the UI, fresh campaign with its
  own order set (ledger in the harness state file):
  - **leg 1, maker alice / taker bob**: rest-mode setprice rELEK/rBTC
    ask 0.001 vol 10 min 0.1 → order `7a236605…`; visible on bob's
    orderbook via the P2P loopback within ~3 s and stable across polls.
    Bob buys via immediate/buy → taker swap `fed72d08…` (TakerV1, 13
    events, ends Finished) and consumes the maker order.
    Balance deltas: alice −10.00000309 rELEK / +0.00999504 rBTC; bob
    +9.99999504 rELEK / −0.01000173 rBTC (the offset from nominal ±10 /
    ∓0.01 is the pair's fee legs).
  - **cancel path**: a throwaway order placed BEFORE the resting one,
    then cancelled via its row in my_orders — success toast
    `cancelled <uuid-cut>`, row gone from my_orders and from the
    orderbook refresh (an order can be replaced, see below; the
    resting order is always placed last on the pair).
  - **replace semantics pinned live**: setprice's `cancel_previous`
    defaults to TRUE (`lp_ordermatch.rs` SetPriceReq, defaults
    struct; handling at the two setprice call sites) and the SPA does
    not send it — so a second rest on the same pair SILENTLY replaces
    the first. Asserted in the campaign: bob's first ask
    0.0025@1 (`throwaway`) was replaced by 0.0015@4 min 0.5 → first
    order gone from my_orders, distinct uuid, book shows the new one.
  - **leg 2, maker bob / taker alice**: rest 0.0015@4 vol 4 min 0.5 →
    `2ead0c0a…`; alice's orderbook renders the peer ask (P2P visibility
    proven in both directions); alice buys → taker swap `0d71e524…`
    Finished; deltas alice +~4 rELEK / −0.006 rBTC, bob mirrors.
  - end state: `my_orders` maker/taker empty on BOTH daemons, books
    empty, both swaps Finished and rendered done in each side's swaps
    history.
- **UI defects the campaign found and fixed** (served from the same
  checkout, both fixes live-proven inside the campaign flows):
  1. `web/js/views/orderbook.mjs` shipped its two pair `<select>`s
     empty and render() only ASSIGNED `refs.base.value` — a silent
     no-op without `<option>` children. The pair fell back to
     `coins[0]/coins[1]` in `get_enabled_coins` daemon order (bob:
     `[rBTC, tELEK, rELEK]` → the default book rendered rBTC/tELEK,
     a pair nobody ever traded, hence "empty book"). Fix mirrors the
     trade view: populate both selects from the coin list. The P2P
     path itself was never at fault — the daemon propagated the peer
     ask within seconds.
  2. `web/js/views/swaps.mjs` `metaOf` now reads BOTH
     `my_recent_swaps` serializations: the v2 tagged item
     `{swap_type, swap_data}` (pinned for the UI paging envelope
     `{filter: {}, paging: {…}}`) and the flat legacy item
     (uuid/is_finished/error_events/events at top level, reached e.g.
     by a top-level `{"limit": 2}` call). Which shape arrives is
     envelope-dependent (pinned live against the daemon), so the view
     accepts both and derives `is_finished` from either the explicit
     boolean or the terminal event name.

- **T6 testnet campaign** (same proxy UI, pair tELEK × rBTC — private
  ELEK testnet via the local testnet electrs `:50005`, BTC regtest via
  the local regtest electrs `:50004`), fresh order ledger:
  - **leg 1, maker bob / taker alice**: a throwaway ask 0.002@1 min 0.1
    placed first and silently replaced (replace semantics re-proven on
    this pair), resting ask tELEK 5 @0.001 min 0.5; alice bought
    immediate/buy → swap Finished on both sides. Balance deltas anchor
    on placement-boot `my_balance` reads carried in the harness state
    file (no hardcoded balances), within fee tolerance.
  - **leg 2, maker alice / taker bob**: cancel-path throwaway first
    (cancelled via its my_orders row, order then absent from
    `my_orders` and the book), resting ask tELEK 4 @0.0015 min 0.4; bob
    bought immediate/buy → swap Finished; mirrored deltas.
  - all eight trade flows green (bob-make / alice-take / alice-done /
    bob-watch2 / alice-make / bob-take / bob-watch / alice-watch);
    SSE `ORDERBOOK_UPDATE` and peer-side order visibility exercised in
    both directions again.
- **coins view: custom add form + disable, end-to-end** (TESTBTC — the
  only UI path for a registry coin with no preset row; servers entered
  as `testnet.aranguren.org:51001`):
  - one green run proves the full cycle: add form → coin listed in
    `get_enabled_coins` (observed in-page through the same proxy path)
    → row disable → coin gone → cold re-add → coin back → wallet row
    rendered. `my_balance` answers for TESTBTC with address
    `mgomJaU1dQHBpLtKNnnaaFdNZ83izL3qwB` (balance 0), matching the
    daemon log (`disabling TESTBTC coin` at the disable step, first
    re-activation request racing the old client's teardown with the
    usual "no active connections" warn — harmless).
  - the form's activation is a fire-and-poll loop (30 × 3 s, then an
    error toast); the coin's re-activation landed well inside the
    campaign's 420 s observer window.
- **campaign harness defect, documented for honesty** (fixed
  mid-campaign; the first three coins-test runs reported 6/7): the
  harness's enabled-set observer called `api.getEnabledCoins`, which
  does not exist — `api.mjs` exports `enabledCoins` (legacy
  `get_enabled_coins`, the same helper both the wallet and the coins
  views use) — so every poll threw inside a swallowing `catch` and
  returned an empty list, faking "enabled: no" while the daemon had
  long listed the coin. Direct-RPC probes (`get_enabled_coins`,
  `my_balance` on the daemon RPC) disproved the false negative; the
  fixed observer turned the same flow green in one run. No UI or
  daemon defect — the 6/7s were harness artifacts.
- **known UI gap, left open deliberately**: the add-form server field
  accepts only `host:port`, so an ETH-style coin (activated over a
  `ws://` endpoint) cannot be enabled through the UI — Sepolia stays
  exercisable at the RPC layer only (section 8).

### 13. Unified price estimate: electrs as the single point of responsibility (2026-10-05)

One ELEK price everywhere — marketplace price suggestions, electrs, the
Electrum wallet, the stats page — with ONE component computing it: electrs.
Everything downstream consumes, nothing derives its own number.

- **Rate chain in `electrs` (`src/fx.rs`)**, refreshed every `fx_refresh_secs`
  (configured: 15 s):
  1. `p2p_market` — the live kdf order book (`fx_orderbook_rpc_url`,
     `fx_orderbook_base/rel`): when at least one resting side exists, the
     rate IS the market (one-sided books use the only side).
  2. registry reference — `fx_rate_url` (the project's `rate.json`),
     used when the order book is empty.
  3. mining cost floor — a lower bound in the banner, never presented as a
     market rate.
  The BTC reference (`usd_per_btc`, for EUR derivation and cross rates) comes
  from `fx_btc_prices_url` (mempool.space), not from an ELEK exchange.
- **exposures, all four implemented and verified live** (section 7 config +
  `blockchain.fx.rates` test): the electrs banner; the
  `blockchain.fx.rates` RPC (rich shape incl. `source`, `usd_per_btc`, and
  the market levels `best_bid/best_ask/mid` + side counts);
  `fx_snapshot_path` (mempool-compatible price JSON for the stats page);
  `fx_rates_path` (the rich-shape file for the web UI).
- **secret handling**: the order book RPC password never sits in a config
  file with a wide Debug surface — `fx_orderbook_userpass` is a
  `SensitiveUserPass` newtype (redacted `<sensitive>` in `Debug`, same
  technique as `daemon_auth`) and is passed at start via
  `ELECTRS_FX_ORDERBOOK_USERPASS` (configure_me `env_prefix`). Verified: the
  startup config line prints `fx_orderbook_userpass: <sensitive>`.
- **web layer: SPA + `elek-web`** (this section's implementation):
  - `mm2src/elek_web` serves the `fx_rates_path` file same-origin at
    `/fx/rates.json` when `MM_WEB_FX_RATES` is set (works in static and
    proxy deployments alike — it is just a disk file). Unset or missing file
    → 404; nothing is invented at the web layer.
  - `web/js/fx.mjs` fetches it with a trust gate: non-OK, non-JSON, missing
    or non-positive `usd/eur`/`time`, or a malformed `market` all normalize
    to `null` — no rate line, never a fabricated number.
  - orderbook view: a muted rate strip (`1 ELEK ≈ $X ≈ €Y (source · age)`),
    30 s poll.
  - trade view: the same strip plus click-to-use price suggestions —
    `use ask/mid/bid` buttons ONLY when the strip's market pair matches the
    view's pair; for a different pair quoting in a BTC-family coin a derived
    cross rate over electrs' `usd_per_btc` is shown instead. Filling sets the
    price field and reprices the fee preview.
  - selftest step 5b: 404 → WARN (feature off), 200 but untrusted → FAIL
    (broken contract), valid snapshot → PASS with the values recorded.
- **mock**: `web/test/mock_daemon.py` serves a canned `/fx/rates.json`
  (shape-correct, prices from the mock book) so UI iteration without
  electrs exercises the same gate.
- **stays open (gates, not done here)**: mainnet `electrs.toml` carries NO
  `fx_orderbook_*` — enabling it means a kdf rpc password on mainnet disk and
  wiring electrs to a live trading daemon (`kdf@trade1`) is the operator's
  call; the stats page still shows its own estimate and would consume
  `fx_snapshot_path`/`/fx/rates.json` as the follow-up; the Electrum wallet
  fork already consumes the project registry rate (see the implemented
  changes) and can be pointed at the same chain later.

## Upstream sync policy

- Manual security ports from upstream `main`; tag
  `kdf-upstream-<date>` at every sync point (e.g.
  `kdf-upstream-20261001` on `d56a7bc`, the first fork base).
- Untouchable by design: v2 swap protocol, HTLC scripts, electrum
  client — security-sensitive code stays as upstream ships it.

## Licenses

The upstream `LEGAL/` tree, license headers, and `AUTHORS` are kept
untouched.