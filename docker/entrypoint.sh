#!/bin/sh
# Marketplace container entrypoint (POSIX sh), boot-liveness ordering:
#   setup_env.py -> elek-web binds FIRST (healthcheck + SPA live from ~1 s)
#   -> local testnet infra + kdf market + kdf trader boot BEHIND the web
#   -> selftest.py gate (any hard failure aborts the container) -> watchdog.
# MM_SKIP_SELFTEST=1 skips the gate. The daemons keep running after the
# selftest: their coin activations live in RAM and would be lost on a restart.
set -eu

STATE_DIR="${MM_STATE_DIR:-/run/elek}"
PORT="${PORT:-10000}"
PROXY_UPSTREAM="${MM_PROXY_UPSTREAM:-7796}"
export MM_STATE_DIR

# NOTE: MM_TELEK_ELECTRS must be exported BEFORE setup_env.py — the coins file
# (and thus the coin set) is written there; the infra itself needs the
# generated rpc password, so it starts BEHIND the web below.
export MM_LOCAL_TESTNET_DIR="${MM_LOCAL_TESTNET_DIR:-/data/elektron-testnet}"
if [ "${MM_LOCAL_TESTNET:-1}" = "1" ]; then
    export MM_TELEK_ELECTRS="${MM_TELEK_ELECTRS:-127.0.0.1:50005}"
    mode="self-contained (elektrond + electrs + miner inside)"
else
    mode="external electrs (${MM_TELEK_ELECTRS:-tBTC-only})"
fi
selftest_state="enabled"
[ "${MM_SKIP_SELFTEST:-0}" = "1" ] && selftest_state="SKIPPED"
echo "elektron-market: ${mode}, selftest ${selftest_state}"
python3 /app/docker/setup_env.py

PASS="$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['MM_STATE_DIR'],'trader','MM2.json')))['rpc_password'])")"

# Rich-shape FX source for the SPA, decided BEFORE the web starts: in
# self-contained mode the LIVE file the in-container electrs writes (it may
# not exist yet — the first electrs poll fills it ~60 s in); otherwise the
# baked registry reference snapshot is the only FX source available.
if [ "${MM_LOCAL_TESTNET:-1}" = "1" ]; then
    export MM_WEB_FX_RATES="${MM_WEB_FX_RATES:-$MM_LOCAL_TESTNET_DIR/electrs-tn/fx-rates.json}"
elif [ -z "${MM_WEB_FX_RATES:-}" ] && [ -f /app/docker/fx-rates.json ]; then
    export MM_WEB_FX_RATES=/app/docker/fx-rates.json
fi

# p2p federation over HTTPS: remote kdf nodes dial their seed as
# wss://<public-domain>:443 (RelayAddress::WebSocket fork patch), render
# terminates TLS at its edge and hands the plain websocket upgrade to
# elek-web, which splices it onto the market daemon's plain-ws listener
# (fork patch in mm2_p2p/src/behaviours/atomicdex.rs — the market has no
# wss_certs, so it binds /ws on the netid-derived wss port, +30).
# Websocket framing stays end-to-end between the dialing node and the
# market. During the boot window the market has not bound yet — the splice
# then answers 502 and the dialing node reschedules after
# DIAL_RETRY_DELAY (300 s, atomicdex.rs:74); long-lived dialers re-try.
netid="$(python3 -c "import json,os;print(json.load(open(os.path.join(os.environ['MM_STATE_DIR'],'market','MM2.json'))).get('netid',8888))")"
market_ws_port="$(( (netid / 10) * 40 + 7783 + netid % 10 + 30 ))"

# elek-web in the background FIRST: healthcheck/SPA answer immediately while
# everything else boots. Browser POSTs to /rpc land on a not-yet-up daemon
# until the trader port is up — the SPA's auto-connect retries that gap.
MM_WEB_ADDR="0.0.0.0:${PORT}" \
MM_WEB_PROXY_URL="http://127.0.0.1:${PROXY_UPSTREAM}" \
MM_WEB_RPC_PASS="$PASS" \
MM_WEB_P2P_BRIDGE="127.0.0.1:${market_ws_port}" \
MM_WEB_ROOT=/app/web \
    elek-web &
WEB_PID=$!
echo "elektron-market: elek-web binding 0.0.0.0:${PORT} first (proxy -> 127.0.0.1:${PROXY_UPSTREAM}; p2p ws bridge -> 127.0.0.1:${market_ws_port}; daemon boot behind it)"

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

cleanup() {
    trap - INT TERM EXIT
    kill_daemons 2>/dev/null || true
    kill "$WEB_PID" 2>/dev/null || true
}
on_signal() {
    cleanup
    exit 128
}
trap on_signal INT TERM
trap cleanup EXIT

# ---- the actual boot, behind the web -----------------------------------------
if [ "${MM_LOCAL_TESTNET:-1}" = "1" ]; then
    if ! /app/docker/start_testnet_infra.sh "$PASS"; then
        echo "elektron-market: testnet infra failed — electrs log tail:" >&2
        tail -n 30 "$MM_LOCAL_TESTNET_DIR/logs/electrs-tn.log" 2>/dev/null >&2 || true
        exit 1
    fi
    echo "elektron-market: local testnet infra up (faucet: $(cat "$MM_LOCAL_TESTNET_DIR/faucet-address.txt" 2>/dev/null || echo n/a))"
fi

start_daemon market
# The trader's only seed is the market daemon, and it dials
# '/ip4/127.0.0.1/tcp/<p2p>' right away while booting. A dial that lands in the
# seconds before the market has bound its p2p listen socket fails, and mm2 then
# suppresses further attempts to the same address for DIAL_RETRY_DELAY = 300 s
# (mm2_p2p/src/behaviours/atomicdex.rs:74 — the 30 s heartbeat only logs
# "Connection attempt was already made recently"; smoke #4: the trader stayed
# peerless for minutes and the orderbook gate failed 22/23). So wait for the
# market's derived p2p listen port BEFORE starting the trader. The port derives
# from the netid (lp_network.rs:lp_ports — tcp = (netid/10*40) + 7783 +
# netid%10 + 20; netid 8888 -> 43331, as both daemons log at boot).
market_p2p_port="$(python3 -c "import json,os;netid=json.load(open(os.path.join(os.environ['MM_STATE_DIR'],'market','MM2.json'))).get('netid',8888);print((netid//10)*40+7783+netid%10+20)")"
if ! wait_port "$market_p2p_port" 120; then
    exit 1
fi
start_daemon trader
if ! wait_port 7795 240; then
    exit 1
fi
if ! wait_port "$PROXY_UPSTREAM" 240; then
    exit 1
fi

if [ "${MM_SKIP_SELFTEST:-0}" != "1" ]; then
    if ! python3 /app/docker/selftest.py; then
        echo "elektron-market: SELFTEST FAILED — last kdf log lines:" >&2
        tail -n 40 "$STATE_DIR/market/kdf.out" "$STATE_DIR/trader/kdf.out" >&2 || true
        exit 1
    fi
fi

# Supervisor phase. Watchdog FIRST: 3 consecutive missed daemon-port probes
# (90 s) abort the container so the platform restarts a wedged stack. It only
# counts misses once the gate has passed — boot time is the gate's business,
# not the watchdog's (infra alone may legitimately take minutes to index).
# Both it and the supervisor run beside the serving web; TERM to this pid-1
# shell (docker stop, or the watchdog itself as its child) triggers the traps.
(
    misses=0
    while :; do
        sleep 30
        if port_up 7795 && port_up "$PROXY_UPSTREAM"; then
            misses=0
        else
            misses=$((misses + 1))
            if [ "$misses" -ge 3 ]; then
                echo "elektron-market: kdf daemons unresponsive for ${misses}x30 s — aborting" >&2
                kill -TERM "$PPID" 2>/dev/null
                exit 1
            fi
        fi
    done
) &
echo $! >"$STATE_DIR/watchdog.pid"

echo "elektron-market: gate passed — serving 0.0.0.0:${PORT}"
wait "$WEB_PID"