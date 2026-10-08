#!/bin/sh
# Self-contained testnet infra for the marketplace container (MM_LOCAL_TESTNET=1):
# elektrond testnet genesis chain + miner loop + electrs (Signet stand-in) with
# the FX wiring into the market daemon's orderbook.
#
# All addresses loopback; the container has no host dependencies and NO custody:
# the miner address belongs to the infra faucet wallet only (testnet supply for
# client wallets in e2e runs) — the market/trader daemons stay at zero and are
# never an order participant.
#
# Called from entrypoint AFTER setup_env.py (the kdf rpc password exists then)
# and BEFORE the kdf daemons start. Returns once electrum :50005 answers;
# elektrond and the miner loop keep running in the background.
#
# Adapted from scripts/elektron/testnet_up.sh (host recipe): same genesis check,
# same Signet-stand-in wiring (magic 0b110907), same "electrs before ~block 250"
# discipline — but NO bulk mining at boot: the chain is mined one block at a
# time by the loop so the young chain never outruns the indexer.
set -eu

DATA="${MM_LOCAL_TESTNET_DIR:-/data/elektron-testnet}"
NODE_USER="${MM_LOCAL_NODE_RPC_USER:-elek}"
NODE_PASS="${MM_LOCAL_NODE_RPC_PASS:-localtestnode}"
ELECTRS_BIN="${ELECTRS_BIN:-/usr/local/bin/electrs}"
ELEKTROND_BIN="${ELEKTROND_BIN:-/usr/local/bin/elektrond}"
KDF_PASS="$1"  # rpc password of the two kdf daemons (same for market/trader)
# Registry reference rate: the fork default URL already points at the project
# registry; the env override exists for a kutlusoy/main switch (registry repo).
FX_RATE_URL="${MM_FX_RATE_URL:-https://raw.githubusercontent.com/bartman081523/elektron-net-registry/main/rate.json}"

GENESIS="00000078233832d6ba39d7693ad96e9e6a8bc869a5f49cfaf4760f2c73c73e4f"

# port probe with NORMALIZED exit code: connect_ex returns an errno on failure
# (e.g. 111), not 1 — sys.exit(0 if …==0 else 1) so `if ! port_up` means closed.
port_up() {
    python3 -c "import socket,sys;s=socket.socket();s.settimeout(1);sys.exit(0 if s.connect_ex(('127.0.0.1',$1)) == 0 else 1)" 2>/dev/null
}

mkdir -p "$DATA"/etn1 "$DATA"/electrs-tn/db "$DATA"/logs

jrpc() {
    curl -s --max-time 5 --user "$NODE_USER:$NODE_PASS" \
        -H 'content-type: text/plain;' "http://127.0.0.1:18332" \
        -d "{\"jsonrpc\":\"1.0\",\"id\":\"e\",\"method\":\"$1\",\"params\":$2}"
}

node_ready() {
    jrpc getblockchaininfo '[]' | grep -q '"chain"'
}

# --- elektrond testnet (fresh chain, no seeds: a single-node chain we mine) --
cat > "$DATA/etn1/bitcoin.conf" <<EOF
daemon=1
listenonion=0
rpcuser=$NODE_USER
rpcpassword=$NODE_PASS
rpcallowip=127.0.0.1
# A young chain has no mempool fee history: the estimator returns nothing and
# sendtoaddress aborts with "Fee estimation failed. Fallbackfee is disabled".
# 1 atom/vB ~= the coins-file txfee rate.
fallbackfee=0.00001
[test]
rpcport=18332
rpcbind=127.0.0.1
EOF

if ! node_ready; then
    echo "  infra: starting elektrond testnet ..."
    "$ELEKTROND_BIN" -testnet -datadir="$DATA/etn1"
fi

i=0
while ! node_ready; do
    i=$((i + 1))
    [ "$i" -ge 30 ] && { echo "infra: etn1 JSON-RPC not up" >&2; exit 1; }
    sleep 1
done
echo "  infra: elektrond rpc is up"

GEN=$(jrpc getblockhash '[0]' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')
[ "$GEN" = "$GENESIS" ] || { echo "infra: UNEXPECTED testnet genesis $GEN" >&2; exit 1; }
echo "  infra: testnet genesis ok"

# --- faucet wallet (infra only — the funding source for client wallets) ------
if ! jrpc listwallets '[]' | grep -q '"w"'; then
    jrpc createwallet '["w",false,false,null,false,true,true]' >/dev/null
    echo "  infra: faucet wallet w created"
fi
FAUCET_ADDR=$(jrpc getaddressesbylabel '["node"]' \
    | python3 -c 'import json,sys;a=json.load(sys.stdin)["result"];print(next(iter(a)) if a else "")' 2>/dev/null || true)
if [ -z "$FAUCET_ADDR" ]; then
    FAUCET_ADDR=$(jrpc getnewaddress '["node","bech32"]' \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')
fi
printf '%s\n' "$FAUCET_ADDR" > "$DATA/faucet-address.txt"
echo "  infra: faucet address (infra wallet, NOT the market): $FAUCET_ADDR"

# --- electrs toml (Network::Signet stand-in for Elektron testnet) -------------
# The kdf orderbook is FX source 1 (src/fx.rs): mid from tELEK/tBTC over the
# fx_btc_prices_url USD chain; the registry rate.json is the fallback source,
# so a fresh empty book serves the estimate until users place orders.
cat > "$DATA/electrs-tn/electrs-tn.toml" <<EOF
network = "signet"
signet_magic = "0b110907"
auth = "$NODE_USER:$NODE_PASS"
daemon_dir = "$DATA/etn1"
daemon_rpc_addr = "127.0.0.1:18332"
daemon_p2p_addr = "127.0.0.1:18333"
db_dir = "$DATA/electrs-tn/db"
electrum_rpc_addr = "127.0.0.1:50005"
monitoring_addr = "127.0.0.1:54326"
log_filters = "INFO,electrs::fx=info"
fx_rate_url = "$FX_RATE_URL"
fx_refresh_secs = 60
fx_snapshot_path = "$DATA/electrs-tn/fx-prices.json"
fx_rates_path = "$DATA/electrs-tn/fx-rates.json"
fx_orderbook_rpc_url = "http://127.0.0.1:7795"
fx_orderbook_userpass = "$KDF_PASS"
fx_orderbook_base = "tELEK"
fx_orderbook_rel = "tBTC"
fx_http_addr = "127.0.0.1:14998"
EOF

if port_up 50005; then
    echo "  infra: electrs already listening on 50005"
else
    ("$ELECTRS_BIN" --conf "$DATA/electrs-tn/electrs-tn.toml" \
        > "$DATA/logs/electrs-tn.log" 2>&1 &)
fi

i=0
while ! port_up 50005; do
    i=$((i + 1))
    [ "$i" -ge 180 ] && {
        echo "infra: electrs electrum_rpc 50005 not up within 180 s; last log:" >&2
        tail -n 20 "$DATA/logs/electrs-tn.log" >&2 || true
        exit 1
    }
    sleep 1
done
echo "  infra: electrs electrum rpc is up on 50005"

# --- miner loop (kept running; 1 block per round, maxtries sweeps the target) -
# An explicit maxtries is essential: at genesis difficulty the target needs
# ~33.5M hashes per block while the default sweep is capped at 1e6 attempts —
# and the daemon's generateBlocks breaks SILENTLY on a failed sweep.
if [ "${MM_LOCAL_TESTNET_MINER:-1}" = "1" ]; then
    while :; do
        if jrpc generatetoaddress "[1,\"$FAUCET_ADDR\",1000000000]" | grep -q '"error":null'; then
            printf '%s mined\n' "$(jrpc getblockcount '[]' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')" >> "$DATA/logs/miner.log"
        fi
        sleep "${MM_LOCAL_TESTNET_MINE_INTERVAL:-5}"
    done &
    echo $! > "$DATA/miner-loop.pid"
    echo "  infra: miner loop running (pid $(cat "$DATA/miner-loop.pid"))"
fi