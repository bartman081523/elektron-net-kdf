#!/usr/bin/env bash
# regtest_swap_maker_elek.sh -- ELEK-side maker swap:
# bob (maker) sells rELEK for rBTC; alice (taker) buys.
#
# Usage: regtest_swap_maker_elek.sh [n] [vol] [price]
#   defaults: n=900 vol=10 price=0.001
# Requires kdf up (regtest_up.sh) and both instances RPC-reachable.
set -euo pipefail
. "$(dirname "$0")/regtest_env.sh"

N="${1:-900}"
VOL="${2:-10}"
PRICE="${3:-0.001}"

require_pid "$RL_ROOT/runs/pids/kdf-bob.pid"
require_pid "$RL_ROOT/runs/pids/kdf-alice.pid"

python3 "$SCEN/swap_e2e.py" single "$N" bob rELEK rBTC "$VOL" "$PRICE"