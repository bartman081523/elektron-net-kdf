#!/usr/bin/env bash
# regtest_teardown.sh -- stop the kdf instances the harness started (PID
# files under $RL_ROOT/runs/pids). Node/electrs processes are owned by the
# operator's regtest setup, not by this script.
#
# Usage: regtest_teardown.sh [--pids ALIVE,ALIVE2 ... | --all]
#   --all  kills every pidfile that exists in runs/pids and matches kdf/swap
set -euo pipefail
. "$(dirname "$0")/regtest_env.sh"

kill_pidfile() {
  local file="$1" pid
  [ -f "$file" ] || return 0
  pid=$(cat "$file" 2>/dev/null || true)
  [ -n "$pid" ] || return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    log "sent TERM to $pid ($file)"
  else
    log "pid $pid ($file) already dead"
  fi
  rm -f "$file"
}

[ $# -eq 0 ] && { echo "usage: regtest_teardown.sh <pidfile>..." >&2; exit 1; }

for f in "$@"; do
  kill_pidfile "$RL_ROOT/runs/pids/$f"
done
log "teardown done"