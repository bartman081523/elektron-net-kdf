#!/usr/bin/env python3
"""Fund kdf's tELEK addresses from the etn1 node wallet ("w", spendable).

Run after testnet_up.sh + testnet_miner.py: testnet coinbase maturity is
100 blocks, so the initial TN_BLOCKS coinbases only become spendable once
the miner loop has grown the tip past (their height + 100). Funding
retries around premature-spend-of-coinbase until then.

No credentials in the repo: node creds via RPC_USER / RPC_PASS, kdf rpc
passwords via KDF_RPC_PASS_ALICE / KDF_RPC_PASS_BOB (testnet_rpc).

Usage:
  testnet_fund.py [--coin tELEK] [--amount 50] [--min-balance 45]
"""
import base64, json, os, sys, time, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from testnet_rpc import ALICE, BOB, rpc, ensure_coins_testnet, get_address

NODE_URL = "http://127.0.0.1:18332/wallet/w"  # wallet-w endpoint (testnet_up.sh)

RPC_USER = None
RPC_PASS = None


def require_creds():
    global RPC_USER, RPC_PASS
    RPC_USER = os.environ.get("RPC_USER")
    RPC_PASS = os.environ.get("RPC_PASS")
    if not RPC_USER or not RPC_PASS:
        raise SystemExit("RPC_USER / RPC_PASS not set in environment; the "
                         "harness keeps credentials out of the repo")


def nrpc(method, params):
    body = json.dumps({"jsonrpc": "1.0", "id": "e", "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(NODE_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(
            ("%s:%s" % (RPC_USER, RPC_PASS)).encode()).decode()})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
    r = json.loads(raw)
    if r.get("error"):
        raise RuntimeError("%s: %s" % (method, str(r["error"])[:200]))
    return r["result"]


def kdf_unspent(port, pw, coin):
    r = rpc(port, pw, {"method": "my_balance", "coin": coin})
    d = r.get("result")
    if not isinstance(d, dict):
        d = r
    try:
        return float(d.get("balance", "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def wait_spendable(timeout_s=240):
    """Node-wallet balance is 0 while its coinbases are still immature."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        bal = nrpc("getbalance", [])
        if bal and float(bal) > 0:
            return float(bal)
        time.sleep(10)
    raise SystemExit("node wallet still unspendable after %ds "
                     "(coinbase maturity 100, miner loop running?)"
                     % timeout_s)


def fund_instance(name, port, pw, coin, amount):
    addr = get_address(port, pw, coin)
    print("%s: funding %s address %s with %s" % (name, coin, addr, amount),
          flush=True)
    txid = None
    t0 = time.time()
    while txid is None and time.time() - t0 < 120:
        try:
            # Object params: sendtoaddress takes fee_rate (atom/vB) as a named
            # argument. On this young chain the fee estimator has no mempool
            # history and the fallback fee is disabled, so an estimated-fee
            # send fails with "Fee estimation failed. Fallbackfee is
            # disabled" (code -6) -- an explicit fee_rate avoids the
            # estimator entirely. Named params also dodge the fork's
            # positional arg quirks.
            txid = nrpc("sendtoaddress", {"address": addr,
                                          "amount": float(amount),
                                          "comment": "kdf %s fund" % coin.lower(),
                                          "fee_rate": 1})
        except RuntimeError as e:
            # immature coinbases: retry until the miner has aged them
            print("  sendtoaddress retry (%s)" % str(e)[:100], flush=True)
            time.sleep(10)
    if txid is None:
        raise SystemExit("sendtoaddress never succeeded")
    print("%s: txid %s" % (name, txid), flush=True)
    # No explicit confirmation mining here: the miner loop (testnet_miner.py,
    # 1 block/~6 s) keeps growing the tip, and required_confirmations=2 per
    # coin config accrue on their own. (A node-side generatetoaddress call
    # would inherit the silent-break trap on a maxtries-capped sweep.)
    wait_spendable()
    bal0 = kdf_unspent(port, pw, coin)
    t0 = time.time()
    while kdf_unspent(port, pw, coin) <= bal0 and time.time() - t0 < 120:
        time.sleep(5)
    bal = kdf_unspent(port, pw, coin)
    print("%s: kdf %s balance now %s" % (name, coin, bal), flush=True)
    return {"name": name, "coin": coin, "address": addr, "txid": txid,
            "balance": bal}


def main():
    require_creds()
    coin = sys.argv[sys.argv.index("--coin") + 1] if "--coin" in sys.argv else "tELEK"
    amount = (float(sys.argv[sys.argv.index("--amount") + 1])
              if "--amount" in sys.argv else 50.0)
    for name, (port, pw) in (("alice", ALICE), ("bob", BOB)):
        ensure_coins_testnet(port, pw, [coin])
        fund_instance(name, port, pw, coin, amount)


if __name__ == "__main__":
    main()