#!/usr/bin/env bash
# regtest_swap_taker_elek.sh -- role-reversed swap:
# alice (maker) sells rBTC for rELEK; bob (taker) buys.
#
# Usage: regtest_swap_taker_elek.sh [n] [vol] [price]
#   defaults: n=910 vol=0.01 price=1000
set -euo pipefail
. "$(dirname "$0")/regtest_env.sh"

N="${1:-910}"
VOL="${2:-0.01}"
PRICE="${3:-1000}"

require_pid "$RL_ROOT/runs/pids/kdf-bob.pid"
require_pid "$RL_ROOT/runs/pids/kdf-alice.pid"

python3 "$SCEN/swap_e2e.py" single "$N" alice rBTC rELEK "$VOL" "$PRICE"