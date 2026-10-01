#!/usr/bin/env bash
# regtest_refund.sh -- refund-path scenarios (no Docker).
#
# Subcommands:
#   maker-watch : watch the natural maker-side refund of the stuck swaps
#                 (maker payment locked, CLTV expiry, refund broadcast).
#   r1          : kill the maker (bob) once the taker payment is locked;
#                 the taker then refunds her own payment at locktime.
#   r2          : kill the taker's counterpart -- kill the maker (alice)
#                 before she spends the taker payment; bob refunds his own
#                 taker payment at locktime.
#
# All three write evidence to $RL_ROOT/runs/refund-results.jsonl.
set -euo pipefail
. "$(dirname "$0")/regtest_env.sh"

CMD="${1:?usage: regtest_refund.sh maker-watch|r1|r2}"
case "$CMD" in
  maker-watch)
    python3 "$SCEN/refund_maker_watch.py" ;;
  r1)
    python3 "$SCEN/refund_r1.py" ;;
  r2)
    python3 "$SCEN/refund_r2.py" ;;
  *)
    echo "unknown subcommand: $CMD" >&2
    echo "usage: regtest_refund.sh maker-watch|r1|r2" >&2
    exit 1 ;;
  esac