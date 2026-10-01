#!/usr/bin/env bash
# regtest_fund.sh -- fund one kdf-managed address on one regtest node and
# make its coinbases mature. To be run after regtest_up.sh (same RL_ROOT).
#
# Why the sink mining is part of this script: regtest coinbase maturity is
# 100 blocks, and electrs (which kdf contacts in electrum mode) exposes
# immature coinbases as spendable. A kdf withdrawal that picks one is
# rejected by the node with bad-txns-premature-spend-of-coinbase. Funding
# therefore always ends with >= 100 blocks mined to the sink address, which
# makes every coinbase generated here mature at the final tip.
#
# No keys are handled here: the kdf address is passed in explicitly
# (from kdf "my_balance".address). For native-mode coins whose descriptors
# are not yet in the node's keyless "wo" wallet, pass --descriptor
# "addr(<addr>)#<checksum>" to importdescriptors it (kdf electrum mode
# never needs the import -- electrs indexes the scripthash on its own).
#
# Usage:
#   regtest_fund.sh --rpc 38332 --addr bcrt1q... \
#       [--blocks 110] [--sink-blocks 100] [--sink-only]
#   [--descriptor 'addr(bcrt1q...)#<checksum>']
#
# Required env:
#   RPC_USER / RPC_PASS  credentials of the target node
#   RL_ROOT              same base dir as regtest_up.sh
#                       (default /run/media/julian/ML5/kdf-regtest)
#
# Alternative funding path (electrum-mode kdf, keyless node wallets):
#   kdf withdraw (mmrpc 2.0) from an already-funded instance plus
#   send_raw_transaction -- used in this harness for instance-to-instance
#   funding; see doc/elektron.md.
# Optional env: WALLETS may be overridden ("wo" by default).

set -euo pipefail

RL_ROOT="${RL_ROOT:-/run/media/julian/ML5/kdf-regtest}"
: "${RPC_USER:?RPC_USER not set}"
: "${RPC_PASS:?RPC_PASS not set}"
WALLETS="${WALLETS:-wo}"

ADDR=""
BLOCKS=110
SINK_BLOCKS=100
SINK_ONLY=0
DESCRIPTOR=""
RPC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --rpc) RPC="$2"; shift 2 ;;
    --addr) ADDR="$2"; shift 2 ;;
    --blocks) BLOCKS="$2"; shift 2 ;;
    --sink-blocks) SINK_BLOCKS="$2"; shift 2 ;;
    --sink-only) SINK_ONLY=1; shift ;;
    --descriptor) DESCRIPTOR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done
[ -n "$RPC" ] || { echo "--rpc PORT required" >&2; exit 1; }

SINK_ADDR="$RL_ROOT/sink.addr"
[ -f "$SINK_ADDR" ] || { echo "$SINK_ADDR missing -- run regtest_up.sh first" >&2; exit 1; }
SINK_ADDR=$(cat "$SINK_ADDR")

jrpc() {
  # $2 must be valid JSON params text (e.g. "[110,\"bcrt1q...\"]").
  # Targets the wallet endpoint explicitly: the regtest node runs its keyless
  # descriptor wallet under $WALLETS, and a node with that one wallet loaded
  # may still route unnamed requests elsewhere. Prints node-side errors to
  # stderr and returns nonzero on them (set -e aborts the script then).
  local resp
  resp=$(curl -s --max-time 15 --user "$RPC_USER:$RPC_PASS" -H 'content-type: text/plain;' \
    "http://127.0.0.1:$RPC/wallet/$WALLETS" \
    -d "{\"jsonrpc\":\"1.0\",\"id\":\"e\",\"method\":\"$1\",\"params\":$2}") || return 1
  python3 -c '
import sys, json
try:
    r = json.loads(sys.stdin.read())
    err = r.get("error")
except Exception:
    err = "unparseable node reply"
if err:
    sys.stderr.write(str(err) + "\n")
sys.exit(1) if err else sys.exit(0)
' <<<"$resp" || return 1
}

height() {
  local resp
  resp=$(curl -s --max-time 15 --user "$RPC_USER:$RPC_PASS" -H 'content-type: text/plain;' \
    "http://127.0.0.1:$RPC/wallet/$WALLETS" \
    -d '{"jsonrpc":"1.0","id":"e","method":"getblockcount","params":[]}')
  python3 -c 'import json,sys;print(json.loads(sys.stdin.read())["result"])' <<<"$resp"
}

if [ -n "$DESCRIPTOR" ]; then
  echo "importing descriptor into wallet(s): $WALLETS"
  jrpc importdescriptors \
    "[{\"desc\":\"$DESCRIPTOR\",\"label\":\"mm2\",\"active\":true}]"
fi

if [ "$SINK_ONLY" != "1" ] && [ -n "$ADDR" ]; then
  echo "mining $BLOCKS coinbases to $ADDR"
  jrpc generatetoaddress "[$BLOCKS,\"$ADDR\"]"
elif [ "$SINK_ONLY" != "1" ]; then
  echo "--addr required (or --sink-only)" >&2
  exit 1
fi

echo "mining $SINK_BLOCKS sink blocks to $SINK_ADDR (maturity fix)"
jrpc generatetoaddress "[$SINK_BLOCKS,\"$SINK_ADDR\"]"

tip=$(height)
echo "done. node tip is now $tip."
echo "funded coinbases mature at (their height + 100); the $SINK_BLOCKS sink blocks guarantee that."