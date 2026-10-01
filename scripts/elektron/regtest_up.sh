#!/usr/bin/env bash
# regtest_up.sh -- bring up the native regtest infrastructure for kdf/ELEK
# testing. Lives only in the elektron fork; nothing upstream is touched.
#
# Shape (no Docker, hard project rule): two independent one-node elektrond
# regtest networks (rl1 = rELEK side, rb1 = rBTC side), one electrs (fork
# build) per chain, all datadirs under RL_ROOT. The nodes are deliberately
# NOT cross-connected: each side of the swap tests is fundable on its own.
# kdf itself is NOT started here -- kdf instances are started per run with
# their own MM2.json (seed and RPC password come in via env, never
# committed).
#
# Required env:
#   RPC_USER / RPC_PASS  credentials written into the generated confs
#                        (never committed anywhere)
#   ELEKTROND            path to the elektrond binary
#   ELECTRS              path to the electrs binary (elektron-net-electrs
#                        fork build)
#
# Optional env:
#   RL_ROOT              base dir for datadirs/dbs/pids/logs
#                        (default /run/media/julian/ML5/kdf-regtest)
#   RPC_USER_2/RPC_PASS_2  rb1 credentials (default: same as rl1)
#   RL_BLOCKS            coinbases mined per node at bring-up (default 110)
#   RL_SKIP_MINING       1 = skip initial mining
#
# Wallets: each node gets exactly one keyless watch-only descriptor wallet
# "wo" (createwallet args verified against elektrond source:
# src/wallet/rpc/wallet.cpp:353-360 -- positional order is
# wallet_name, disable_private_keys, blank, passphrase, avoid_reuse,
# descriptors, load_on_startup; descriptors is forced true in the handler,
# so every wallet is a descriptor wallet). Keys are never generated here:
# mining goes via generatetoaddress to explicit addresses, and kdf native
# coins import their mm2-derived descriptors into "wo" (see
# regtest_fund.sh).
#
# Mining target: a fixed, unspendable "sink" address, derived once from a
# constant tag (sha256 of a fixed string truncated to 20 bytes -- no key
# exists, the coins can never be spent) and stored at RL_ROOT/sink.addr.
# Funding kdf addresses is a separate explicit step (regtest_fund.sh),
# always followed by >= 100 sink blocks: regtest coinbase maturity is 100,
# and electrs exposes immature coinbases as spendable, so kdf withdrawals
# pick them and get rejected by the node
# (bad-txns-premature-spend-of-coinbase) unless the sink mining happens
# right after funding.
#
# PIDFILES: elektrond -daemon writes its own file at
# <datadir>/regtest/elektrond.pid. We read THAT file into pids/ -- never
# pkill/pgrep by pattern, a pattern can match the invoking shell itself.

set -euo pipefail

RL_ROOT="${RL_ROOT:-/run/media/julian/ML5/kdf-regtest}"
: "${RPC_USER:?RPC_USER not set}"
: "${RPC_PASS:?RPC_PASS not set}"
RPC_USER_2="${RPC_USER_2:-$RPC_USER}"
RPC_PASS_2="${RPC_PASS_2:-$RPC_PASS}"
: "${ELEKTROND:?ELEKTROND not set}"
: "${ELECTRS:?ELECTRS not set}"
RL_BLOCKS="${RL_BLOCKS:-110}"

mkdir -p "$RL_ROOT"/{rl1,rb1,electrs-rt,electrs-rt2,\
electrs-rt/snapshot,electrs-rt2/snapshot,logs,pids,runs}

# --- JSON-RPC helper (node must be configured for plain HTTP RPC) ----------
jrpc() {
  # params must be given as valid JSON text (e.g. "[]"); $*-style
  # interpolation would join args with spaces and produce invalid JSON
  local creds="$1" rpc="$2" method="$3" params="$4"
  curl -s --max-time 5 --user "$creds" -H 'content-type: text/plain;' \
    "http://127.0.0.1:$rpc" \
    -d "{\"jsonrpc\":\"1.0\",\"id\":\"e\",\"method\":\"$method\",\"params\":$params}"
}

# true when the node's JSON-RPC answers
node_ready() {
  jrpc "$1" "$2" getblockchaininfo '[]' | grep -q '"chain"'
}

# --- confs -------------------------------------------------------------------
# NOTE: elektrond auto-generates datadir/bitcoin.conf on first launch and
# refuses to start when a second file is passed via -conf, so we write the
# conf files ourselves (no fallbackfee here is fine for pure mining; send
# workflows on these nodes import mm2 descriptors and are driven by kdf).
cat > "$RL_ROOT/rl1/bitcoin.conf" <<EOF
rpcuser=$RPC_USER
rpcpassword=$RPC_PASS
rpcallowip=127.0.0.1

[regtest]
rpcbind=127.0.0.1
rpcport=38332
port=38333
listenonion=0
daemon=0
EOF

cat > "$RL_ROOT/rb1/bitcoin.conf" <<EOF
rpcuser=$RPC_USER_2
rpcpassword=$RPC_PASS_2
rpcallowip=127.0.0.1

[regtest]
rpcbind=127.0.0.1
rpcport=18443
port=18444
listenonion=0
daemon=0
EOF

# --- sink address (constant, keyless, unspendable) ---------------------------
if [ ! -s "$RL_ROOT/sink.addr" ]; then
  SINK_DERIVED=$(python3 - <<'PY'
import hashlib
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

def hrp_expand(h):
    return [ord(x) >> 5 for x in h] + [0] + [ord(x) & 31 for x in h]

def polymod(values):
    G = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1ffffff) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= G[i]
    return chk

def convertbits(data, frombits, tobits, pad=True):
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    return ret

def encode(hrp, data):
    mod = polymod(hrp_expand(hrp) + data + [0] * 6) ^ 1
    checksum = [(mod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data) + \
        "".join(CHARSET[d] for d in checksum)

pkh = hashlib.sha256(b"elektron-net-regtest-sink").digest()[:20]
print(encode("bcrt", [0] + convertbits(list(pkh), 8, 5)))
PY
)
  echo "$SINK_DERIVED" > "$RL_ROOT/sink.addr"
fi
SINK_ADDR=$(cat "$RL_ROOT/sink.addr")

# --- start elektrond nodes (idempotent: skip when RPC already answers) ------
ensure_node() {
  local name="$1" creds="$2" rpc="$3"
  if node_ready "$creds" "$rpc"; then
    echo "elektrond $name already up"
    return
  fi
  echo "starting elektrond $name ..."
  # -daemon: self-backgrounds and regtest/<datadir>/elektrond.pid appears
  "$ELEKTROND" -regtest -datadir="$RL_ROOT/$name" -daemon
}

for spec in "rl1 $RPC_USER:$RPC_PASS 38332" "rb1 $RPC_USER_2:$RPC_PASS_2 18443"; do
  set -- $spec
  ensure_node "$1" "$2" "$3"
done

for _ in $(seq 1 30); do
  if node_ready "$RPC_USER:$RPC_PASS" 38332 \
     && node_ready "$RPC_USER_2:$RPC_PASS_2" 18443; then
    break
  fi
  sleep 1
done
node_ready "$RPC_USER:$RPC_PASS" 38332 \
  || { echo "rl1 JSON-RPC not up" >&2; exit 1; }
node_ready "$RPC_USER_2:$RPC_PASS_2" 18443 \
  || { echo "rb1 JSON-RPC not up" >&2; exit 1; }

# --- wallet "wo" (keyless, watch-only, descriptor, load on startup) ---------
ensure_wo() {
  local creds="$1" rpc="$2"
  if jrpc "$creds" "$rpc" listwallets '[]' | grep -q '"wo"'; then
    return
  fi
  # arg order verified against src/wallet/rpc/wallet.cpp:353-360;
  # disable_private_keys=true, blank=false, no passphrase, descriptors=true
  # (forced anyway), load_on_startup=true
  jrpc "$creds" "$rpc" createwallet \
    '["wo",true,false,null,false,true,true]' >/dev/null
}
ensure_wo "$RPC_USER:$RPC_PASS" 38332
ensure_wo "$RPC_USER_2:$RPC_PASS_2" 18443

# --- pidfiles: read elektrond's own regtest/elektrond.pid --------------------
for name in rl1 rb1; do
  if [ -f "$RL_ROOT/$name/regtest/elektrond.pid" ]; then
    read -r pid < "$RL_ROOT/$name/regtest/elektrond.pid"
    echo "$pid" > "$RL_ROOT/pids/$name.pid"
    echo "elektrond $name pid $pid"
  fi
done

# --- initial mining: everything to the sink ---------------------------------
if [ "${RL_SKIP_MINING:-0}" != "1" ]; then
  for spec in "rl1 $RPC_USER:$RPC_PASS 38332" "rb1 $RPC_USER_2:$RPC_PASS_2 18443"; do
    set -- $spec
    cur=$(jrpc "$2" "$3" getblockcount '[]' | \
      python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')
    echo "mining $RL_BLOCKS more coinbases on $1 (now at $cur) to sink $SINK_ADDR"
    jrpc "$2" "$3" generatetoaddress "[$RL_BLOCKS,\"$SINK_ADDR\"]" >/dev/null
  done
fi

# --- electrs tomls (regenerated; testnet is the fork's regtest stand-in) ----
# The fork repurposes Network::Testnet for the real Elektron Net regtest and
# seeds its genesis there; Network::Regtest stays reserved for the fork CI.
# signet_magic is the elektrond regtest P2P magic (kernel/chainparams.cpp).
# The chain is pruned from height 100 (MandatoryPruneDepth), so electrs
# seeds its scripthash index once from dumptxoutset via utxo_snapshot_dir
# (one-time electrs-bootstrap.dat, real txids) and indexes live blocks on
# top. log_filters must be a plain env_logger spec ("INFO" is fine); a
# bracket suffix gets ignored with a warning and electrs logs nothing.
for spec in "electrs-rt rl1 38332 38333 50003 14324 $RPC_USER $RPC_PASS" \
            "electrs-rt2 rb1 18443 18444 50004 14325 $RPC_USER_2 $RPC_PASS_2"; do
  set -- $spec
  base="$1"; node="$2"; drpc="$3"; dp2p="$4"; erpc="$5"; mon="$6"; creds="$7:$8"
  cat > "$RL_ROOT/$base/$base.toml" <<EOF
network = "testnet"
signet_magic = "fabfb5da"
auth = "$creds"
daemon_dir = "$RL_ROOT/$node"
daemon_rpc_addr = "127.0.0.1:$drpc"
daemon_p2p_addr = "127.0.0.1:$dp2p"
db_dir = "$RL_ROOT/$base/db"
utxo_snapshot_dir = "$RL_ROOT/$base/snapshot"
electrum_rpc_addr = "127.0.0.1:$erpc"
monitoring_addr = "127.0.0.1:$mon"
log_filters = "INFO"
EOF
done

start_electrs() {
  local base="$1" port="$2"
  if [ -f "$RL_ROOT/pids/$base.pid" ] && kill -0 "$(cat "$RL_ROOT/pids/$base.pid")" 2>/dev/null; then
    echo "$base already running (pid $(cat "$RL_ROOT/pids/$base.pid"))"
    return
  fi
  nohup "$ELECTRS" --conf "$RL_ROOT/$base/$base.toml" \
    > "$RL_ROOT/logs/$base.log" 2>&1 &
  echo $! > "$RL_ROOT/pids/$base.pid"
  echo "$base started (pid $(cat "$RL_ROOT/pids/$base.pid"))"
}
start_electrs electrs-rt 50003
start_electrs electrs-rt2 50004

echo "regtest infra up: RL_ROOT=$RL_ROOT, sink=$SINK_ADDR"
echo "next: fund kdf addresses with regtest_fund.sh, then start kdf instances"