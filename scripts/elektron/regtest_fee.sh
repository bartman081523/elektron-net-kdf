#!/usr/bin/env bash
# regtest_fee.sh -- fee-conformance check over the recorded swap results:
# NoFee policy applied on ELEK pairs, no DEX-fee script leaks, balance deltas
# consistent. Exit 0 == consistent.
set -euo pipefail
. "$(dirname "$0")/regtest_env.sh"

python3 "$SCEN/swap_fee_report.py"