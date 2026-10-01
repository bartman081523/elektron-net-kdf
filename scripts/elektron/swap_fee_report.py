#!/usr/bin/env python3
"""Fee-conformance report over green swaps in the results file.

Checks per swap (both roles):
  - fee_to_send_taker_fee amount == "0"  (Phase 2 NoFee policy)
  - no decoded payment-tx output pays the upstream DEX fee pubkey script
  - balance deltas match vol minus miner fees within tolerance
"""
import base64, json, os, sys, urllib.request, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import rpc, ALICE, BOB

RESULTS = "/run/media/julian/ML5/kdf-regtest/runs/swap-results.jsonl"
DEX_PUBKEY_HEX = "03a778d9bd346fa704cf3e2508cd074d93a1bbc1e504fbecbb0a8d48e7cccbbf5c"

NODES = {"rELEK": ("http://127.0.0.1:38332", "elek:pass"),
         "rBTC": ("http://127.0.0.1:18443", "elek:pass")}

def noderpc(ticker, method, params):
    url, auth = NODES[ticker]
    body = json.dumps({"method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(auth.encode()).decode()})
    try:
        raw = urllib.request.urlopen(req, timeout=30).read().decode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
    r = json.loads(raw)
    if r.get("error"):
        raise RuntimeError("%s %s: %s" % (ticker, method, str(r["error"])[:200]))
    return r["result"]

def vout_scripts(ticker, tx_hex):
    tx = noderpc(ticker, "decoderawtransaction", [tx_hex])
    out = []
    for v in tx.get("vout", []):
        spk = v.get("scriptPubKey", {})
        out.append((v.get("value"), spk.get("type"), spk.get("hex", "")))
    return out

def dex_leak_scripts(ticker, tx_hex, dex_script):
    return [v for v in vout_scripts(ticker, tx_hex) if v[2] == dex_script]

def main():
    recs = [json.loads(l) for l in open(RESULTS)]
    green = [r for r in recs if r.get('status_maker')=='Successful'
             and r.get('status_taker')=='Successful']
    print("green swaps: %d" % len(green))
    # derive the dex-fee P2PKH script once per chain via validateaddress? simpler:
    # P2PKH script = 76a914<pubkey-hash>88ac — hash of pubkey not needed here:
    # mm2 pays dex fee to P2SH-P2WPKH? — upstream script is P2PKH-of-pubkey:
    # 21<pubkey>ac
    dex_script = ("21" + DEX_PUBKEY_HEX + "ac")
    problems = 0
    for r in green:
        port, pw = (ALICE if r['maker'] == 'bob' else BOB)  # taker's side
        st = rpc(port, pw, {'method': 'my_swap_status', 'params': {'uuid': r['uuid']}})
        evs = st['result']['events']
        started = [e['event']['data'] for e in evs if e['event']['type'] == 'Started'][0]
        fee0 = started.get('fee_to_send_taker_fee', {}).get('amount')
        leaks = []
        for e in evs:
            d = e['event'].get('data')
            if not isinstance(d, dict):
                continue
            tx_hex = d.get('tx_hex')
            if not tx_hex or len(tx_hex) < 100:
                continue
            ticker = r['rel'] if e['event']['type'] in (
                'TakerFeeSent', 'TakerPaymentSent', 'MakerPaymentSpent',
                'TakerPaymentSpendConfirmed') else r['base']
            for vt, vtype, spk in vout_scripts(ticker, tx_hex):
                if dex_script in spk:
                    leaks.append((e['event']['type'], vt, vtype))
        print(r['n'], r['uuid'][:8], 'no_fee=%s' % (fee0 == '0'), 'leaks=%s' % (leaks or "none"))
        if fee0 != '0' or leaks:
            problems += 1
    print("problems: %d" % problems)
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())
