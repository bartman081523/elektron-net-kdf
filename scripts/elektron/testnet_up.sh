#!/usr/bin/env bash
# testnet_up.sh -- bring up a fresh private Elektron *testnet* chain
# (CTestNetParams) plus its electrs instance, all under TL_ROOT. The chain
# has no seeds (elektrond chainparams clear vSeeds on testnet), so this is
# a single-node chain we mine ourselves -- but everything else is real
# testnet mechanics: real testnet genesis, real PoW retargeting (60 s
# target), 100-block coinbase maturity, and consensus.MandatoryPruneDepth
# = 300 (the node prunes blocks > 300 deep, exactly like the regtest legs,
# so electrs must be running before the chain passes ~250 blocks).
#
# Docker-free, no upstream touched. kdf itself is NOT started here.
#
# Required env:
#   RPC_USER / RPC_PASS  node credentials (never committed)
#   ELEKTROND            path to the elektrond binary
#   ELECTRS              path to the electrs binary (fork build with the
#                        Network::Signet stand-in for Elektron testnet)
#
# Optional env:
#   TL_ROOT              base dir (default /run/media/julian/ML5/elektron-testnet)
#   TN_BLOCKS            initial coinbases (default 210)
#   TN_SKIP_MINING       1 = skip initial mining (electrs must be up first
#                        if the chain already grew past ~250 blocks)
#
# Mining target: node-wallet address, so the node holds spendable outputs
# that testnet_fund.sh can later send to kdf's tELEK addresses. The sink
# trick from regtest is not needed here: on testnet the miner loop keeps
# producing new mature coinbases continuously, so premature-spend of aged
# coinbases cannot occur.
#
# PIDFILES: elektrond -daemon writes its own file at
# <datadir>/testnet3/elektrond.pid. Read THAT into pids/ -- never
# pkill/pgrep by pattern (a pattern can match the invoking shell itself).

set -euo pipefail

TL_ROOT="${TL_ROOT:-/run/media/julian/ML5/elektron-testnet}"
: "${RPC_USER:?RPC_USER not set}"
: "${RPC_PASS:?RPC_PASS not set}"
: "${ELEKTROND:?ELEKTROND not set}"
: "${ELECTRS:?ELECTRS not set}"
TN_BLOCKS="${TN_BLOCKS:-210}"
CREDS="$RPC_USER:$RPC_PASS"

mkdir -p "$TL_ROOT"/{etn1,electrs-tn,logs,pids}

# --- conf: rpcbind/rpcport are network-gated by Core and only apply on
# testnet when set in the [test] section; global options (daemon,
# listenonion, rpcuser/rpcpassword/rpcallowip) stay top-level ---
cat > "$TL_ROOT/etn1/bitcoin.conf" <<EOF
daemon=0
listenonion=0
rpcuser=$RPC_USER
rpcpassword=$RPC_PASS
rpcallowip=127.0.0.1
# A young chain has no mempool fee history: the estimator returns nothing
# and sendtoaddress aborts with "Fee estimation failed. Fallbackfee is
# disabled" (code -6). 1 atom/vB ~= the coins-file txfee rate.
fallbackfee=0.00001
[test]
rpcport=18332
rpcbind=127.0.0.1
EOF

# --- JSON-RPC helper (plain HTTP) --------------------------------------------
jrpc() {
  local creds="$1" rpc="$2" method="$3" params="$4"
  curl -s --max-time 5 --user "$creds" -H 'content-type: text/plain;' \
    "http://127.0.0.1:$rpc" \
    -d "{\"jsonrpc\":\"1.0\",\"id\":\"e\",\"method\":\"$method\",\"params\":$params}"
}

node_ready() {
  jrpc "$CREDS" 18332 getblockchaininfo '[]' | grep -q '"chain"'
}

# --- start elektrond testnet (idempotent: skip when RPC already answers) -----
if ! node_ready; then
  echo "starting elektrond testnet ..."
  # -daemon: self-backgrounds and testnet3/<datadir>/elektrond.pid appears
  "$ELEKTROND" -testnet -datadir="$TL_ROOT/etn1" -daemon
fi

for _ in $(seq 1 30); do
  node_ready && break
  sleep 1
done
node_ready || { echo "etn1 JSON-RPC not up" >&2; exit 1; }

# --- sanity: the chain must be the Elektron testnet genesis ------------------
GENESIS=$(jrpc "$CREDS" 18332 getblockhash '[0]' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')
[ "$GENESIS" = "00000078233832d6ba39d7693ad96e9e6a8bc869a5f49cfaf4760f2c73c73e4f" ] \
  || { echo "UNEXPECTED testnet genesis: $GENESIS" >&2; exit 1; }
echo "etn1 genesis ok: $GENESIS"

# --- spendable node wallet (real keys: the funding source for kdf) ----------
if ! jrpc "$CREDS" 18332 listwallets '[]' | grep -q '"w"'; then
  # positional: wallet_name, disable_private_keys=false, blank=false,
  # no passphrase, avoid_reuse=false, descriptors=true, load_on_startup=true
  jrpc "$CREDS" 18332 createwallet '["w",false,false,null,false,true,true]' >/dev/null
  echo "wallet w created"
fi
NODE_ADDR=$(jrpc "$CREDS" 18332 getnewaddress '["node","bech32"]' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')
echo "node wallet address: $NODE_ADDR"

# --- pidfile: read elektrond's own testnet3/elektrond.pid --------------------
if [ -f "$TL_ROOT/etn1/testnet3/elektrond.pid" ]; then
  read -r pid < "$TL_ROOT/etn1/testnet3/elektrond.pid"
  echo "$pid" > "$TL_ROOT/pids/etn1.pid"
  echo "elektrond etn1 pid $pid"
fi

# --- initial mining (BEFORE electrs starts: prune depth 300 would eat
# blocks otherwise; electrs must be up before ~block 250 -- TN_BLOCKS=210
# keeps us below that) ---------------------------------------------------------
if [ "${TN_SKIP_MINING:-0}" != "1" ]; then
  cur=$(jrpc "$CREDS" 18332 getblockcount '[]' | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"])')
  echo "mining $TN_BLOCKS more coinbases on etn1 (now at $cur) to $NODE_ADDR"
  # An explicit maxtries is essential: at genesis difficulty the target is
  # 2^231 (~33.5M hashes per block) while the default sweep is capped at
  # 1e6 attempts -- and the daemon's generateBlocks breaks SILENTLY on a
  # failed sweep (GenerateBlock() false -> break, no error) returning [].
  # A long curl max-time: the node aborts the job when the client vanishes,
  # so the sweep must outlive the ~TN_BLOCKS * 1s of CPU grind.
  curl -s --max-time 600 --user "$CREDS" -H 'content-type: text/plain;' \
    "http://127.0.0.1:18332" \
    -d "{\"jsonrpc\":\"1.0\",\"id\":\"e\",\"method\":\"generatetoaddress\",\"params\":[$TN_BLOCKS,\"$NODE_ADDR\",1000000000]}" \
    | grep -q '"error":null' || echo "warning: initial mining may be incomplete" >&2
fi

# --- electrs toml (Network::Signet stand-in for Elektron testnet) -------------
# The fork's Network::Signet seeds the Elektron testnet genesis
# (elektron_testnet_genesis_header(), chain.rs); signet_magic carries the
# elektrond testnet P2P magic 0b110907 (CTestNetParams pchMessageStart).
# No utxo_snapshot_dir: the electrs instance starts while the chain is
# still young (<= 210 blocks) and indexes from genesis onward.
cat > "$TL_ROOT/electrs-tn/electrs-tn.toml" <<EOF
network = "signet"
signet_magic = "0b110907"
auth = "$CREDS"
daemon_dir = "$TL_ROOT/etn1"
daemon_rpc_addr = "127.0.0.1:18332"
daemon_p2p_addr = "127.0.0.1:18333"
db_dir = "$TL_ROOT/electrs-tn/db"
electrum_rpc_addr = "127.0.0.1:50005"
monitoring_addr = "127.0.0.1:54326"
log_filters = "INFO"
EOF

if [ -f "$TL_ROOT/pids/electrs-tn.pid" ] && kill -0 "$(cat "$TL_ROOT/pids/electrs-tn.pid")" 2>/dev/null; then
  echo "electrs-tn already running (pid $(cat "$TL_ROOT/pids/electrs-tn.pid"))"
else
  nohup "$ELECTRS" --conf "$TL_ROOT/electrs-tn/electrs-tn.toml" \
    > "$TL_ROOT/logs/electrs-tn.log" 2>&1 &
  echo $! > "$TL_ROOT/pids/electrs-tn.pid"
  echo "electrs-tn started (pid $(cat "$TL_ROOT/pids/electrs-tn.pid"))"
fi

echo "testnet infra up: TL_ROOT=$TL_ROOT"
echo "next: testnet_miner.sh (1 block per ~5 s), then testnet_fund.sh, then kdf activation of tELEK"