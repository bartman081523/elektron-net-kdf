#!/bin/sh
# Marketplace container entrypoint (POSIX sh):
#   setup_env.py -> kdf market + kdf trader -> selftest.py -> elek-web (exec)
# Selftest failure aborts the container (deploy gate). MM_SKIP_SELFTEST=1 skips.
# The daemons keep running after the selftest: their coin activations live in
# RAM and would be lost on a restart.
set -eu

STATE_DIR="${MM_STATE_DIR:-/run/elek}"
PORT="${PORT:-10000}"
PROXY_UPSTREAM="${MM_PROXY_UPSTREAM:-7796}"
export MM_STATE_DIR

selftest_state="enabled"
[ "${MM_SKIP_SELFTEST:-0}" = "1" ] && selftest_state="SKIPPED"
echo "elektron-market: testnet mode, web on 0.0.0.0:${PORT}, selftest ${selftest_state}"
python3 /app/docker/setup_env.py

PASS="$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['MM_STATE_DIR'],'trader','MM2.json')))['rpc_password'])")"

port_up() {
    python3 -c "import socket,sys;s=socket.socket();s.settimeout(1);sys.exit(0 if s.connect_ex(('127.0.0.1',$1)) == 0 else 1)" 2>/dev/null
}

wait_port() {
    # wait_port <port> <seconds>
    n=0
    while ! port_up "$1"; do
        if [ "$n" -ge "$2" ]; then
            echo "elektron-market: kdf port $1 did not come up within $2 s" >&2
            return 1
        fi
        n=$((n + 1))
        sleep 1
    done
    echo "elektron-market: kdf port $1 is up (after ${n}s)"
}

start_daemon() {
    # start_daemon <kind>
    kind="$1"
    MM_CONF_PATH="$STATE_DIR/$kind/MM2.json" \
    MM_COINS_PATH="$STATE_DIR/coins.json" \
    kdf >"$STATE_DIR/$kind/kdf.out" 2>&1 &
    echo $! >"$STATE_DIR/$kind/kdf.pid"
}

kill_daemons() {
    for kind in market trader; do
        if [ -f "$STATE_DIR/$kind/kdf.pid" ]; then
            kill "$(cat "$STATE_DIR/$kind/kdf.pid")" 2>/dev/null || true
        fi
    done
}

start_daemon market
start_daemon trader
trap kill_daemons INT TERM
if ! wait_port 7795 240; then
    kill_daemons
    exit 1
fi
if ! wait_port "$PROXY_UPSTREAM" 240; then
    kill_daemons
    exit 1
fi

if [ "${MM_SKIP_SELFTEST:-0}" != "1" ]; then
    if ! python3 /app/docker/selftest.py; then
        echo "elektron-market: SELFTEST FAILED — last kdf log lines:" >&2
        tail -n 40 "$STATE_DIR/market/kdf.out" "$STATE_DIR/trader/kdf.out" >&2 || true
        kill_daemons
        exit 1
    fi
fi

# elek-web in the foreground: SPA + same-origin reverse proxy to the trader
# daemon. The rpc password is injected server-side; browsers never receive it.
trap - INT TERM
echo "elektron-market: serving on 0.0.0.0:${PORT} (proxy -> 127.0.0.1:${PROXY_UPSTREAM})"
exec env \
    MM_WEB_ADDR="0.0.0.0:${PORT}" \
    MM_WEB_PROXY_URL="http://127.0.0.1:${PROXY_UPSTREAM}" \
    MM_WEB_RPC_PASS="$PASS" \
    MM_WEB_ROOT=/app/web \
    elek-web