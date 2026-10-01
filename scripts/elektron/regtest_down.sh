#!/usr/bin/env bash
# Stop the native regtest infrastructure started by regtest_up.sh.
# Kills by pidfile only (never pkill by pattern), logs remain.
set -uo pipefail

RL_ROOT="${RL_ROOT:-/run/media/julian/ML5/kdf-regtest}"

for name in electrs-rt rl1 rb1; do
  pidfile="$RL_ROOT/pids/$name.pid"
  if [ -s "$pidfile" ]; then
    pid=$(cat "$pidfile")
    if kill "$pid" 2>/dev/null; then
      echo "stopped $name (pid $pid)"
    else
      echo "$name (pid $pid) not running"
    fi
    rm -f "$pidfile"
  fi
done

# kdf instances are stopped via their own RPC (stop), not here.
echo "regtest infra down."