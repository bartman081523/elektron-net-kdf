# regtest_env.sh -- shared environment for the regtest scenario wrappers.
# Sourced, not executed. Reads RPC credentials from the environment
# (RPC_USER/RPC_PASS); never hardcodes them.

: "${RL_ROOT:=/run/media/julian/ML5/kdf-regtest}"
: "${RPC_USER:?RPC_USER not set in environment}"
: "${RPC_PASS:?RPC_PASS not set in environment}"
export RL_ROOT RPC_USER RPC_PASS

SCEN="scripts/elektron"

log() { echo "[scenario] $*"; }

require_pid() {
  # $1 = pidfile path; fails the shell if the process is not alive
  local file="$1" pid
  [ -f "$file" ] || { log "missing pidfile $file"; return 1; }
  pid=$(cat "$file")
  kill -0 "$pid" 2>/dev/null || { log "pid $pid ($file) is not alive"; return 1; }
}