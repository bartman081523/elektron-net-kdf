#!/usr/bin/env python3
"""Testnet swap harness (phase 4): green swaps + results file.

Same driver core as swap_e2e.py, pointing at the testnet coin set:
tELEK (private Elektron testnet, electrs-tn 127.0.0.1:50005) and tBTC
(public Bitcoin testnet3 via testnet.aranguren.org:51001) alongside the
kept regtest legs rELEK / rBTC, so mixed-chain pairs
(tELEK:rBTC, rELEK:tELEK) work against already-running infra.

Results: $TL_ROOT/runs/swap-results-testnet.jsonl
    (default TL_ROOT /run/media/julian/ML5/elektron-testnet)

Usage:
  testnet_swap_e2e.py single <n> <maker:bob|alice> <base> <rel> <vol> <price> [timeout]
      maker = who posts the order; the other instance takes.
  testnet_swap_e2e.py batch <bob|alice-seq> <start-n> [count]
      bob-seq:   BOB makes (tELEK, rBTC)
      alice-seq: ALICE makes (rELEK, tELEK)
  testnet_swap_e2e.py swaps            -- list my_swaps
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import ALICE, BOB, rpc, balance, preflight
from testnet_rpc import ensure_coins_testnet

TL = os.environ.get("TL_ROOT", "/run/media/julian/ML5/elektron-testnet")
RESULTS = os.path.join(TL, "runs", "swap-results-testnet.jsonl")
os.makedirs(os.path.dirname(RESULTS), exist_ok=True)
SUCCESS_STATES = ("Successful",)


def log(m):
    print(m, flush=True)


def my_swaps(port, pw):
    r = rpc(port, pw, {"method": "my_swaps"})
    return r if isinstance(r, list) else r.get("result", r)


def swap_status(port, pw, uuid):
    return rpc(port, pw, {"method": "my_swap_status", "params": {"uuid": uuid}})


def poll_swap(port, pw, uuid, want, timeout=420):
    t0 = time.time()
    seen = []
    while time.time() - t0 < timeout:
        r = swap_status(port, pw, uuid)
        st = None
        payload = r.get("result", r)
        if isinstance(payload, dict):
            if payload.get("is_success"):
                st = "Successful"
            elif payload.get("is_error"):
                st = "Failed"
            else:
                evs = payload.get("events") or []
                st = evs[-1]["event"]["type"] if evs else None
        if isinstance(st, str):
            if st != (seen[-1] if seen else None):
                seen.append(st)
                log("  swap %s -> %s (%d s)" % (uuid[:8], st, time.time() - t0))
            if st in want:
                return st
        time.sleep(3)
    log("  TIMEOUT after %ds, seen=%s" % (timeout, seen))
    return None


def run_swap(n, maker, taker, base, rel, vol, price, timeout=420):
    """Maker setprices (sells) `base` for `rel`; taker buys `base`."""
    mport, mpw = maker
    tport, tpw = taker
    log("swap %d: maker sells %s for %s, vol=%s price=%s" % (n, base, rel, vol, price))
    for port, pw in (maker, taker):
        ensure_coins_testnet(port, pw, (base, rel))
        preflight(port, pw)
    pre = {side: {c: balance(port, pw, c) for c in (base, rel)} for side, (port, pw) in
           (("maker", maker), ("taker", taker))}
    r = rpc(mport, mpw, {"method": "setprice", "base": base, "rel": rel,
                         "price": str(price), "volume": str(vol),
                         "min_volume": str(float(vol) / 2), "broadcast": True,
                         "cancel_previous": True})
    if r.get("error"):
        rec = {"n": n, "error": "setprice failed: %s" % str(r)[:300]}
        with open(RESULTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    r2 = rpc(tport, tpw, {"method": "buy", "base": base, "rel": rel,
                          "volume": str(vol), "price": str(price),
                          "fill_or_kill": True})
    res = r2.get("result") if isinstance(r2.get("result"), dict) else {}
    uuid = res.get("swap_uuid") or res.get("uuid")
    if not uuid:
        rec = {"n": n, "error": "buy failed: %s" % str(r2)[:300]}
        with open(RESULTS, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec

    st_m = poll_swap(mport, mpw, uuid, SUCCESS_STATES, timeout)
    st_t = poll_swap(tport, tpw, uuid, SUCCESS_STATES, timeout)
    post = {side: {c: balance(port, pw, c) for c in (base, rel)} for side, (port, pw) in
            (("maker", maker), ("taker", taker))}
    rec = {"n": n, "uuid": uuid, "base": base, "rel": rel, "vol": vol, "price": price,
           "maker": "bob" if maker == BOB else "alice",
           "pre": pre, "post": post,
           "status_maker": st_m, "status_taker": st_t}
    rpc(mport, mpw, {"method": "cancel_all_orders"})
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "batch"
    if which == "single":
        n, role, base, rel, vol, price = (
            int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7])
        timeout = int(sys.argv[8]) if len(sys.argv) > 8 else 420
        maker, taker = (BOB, ALICE) if role == "bob" else (ALICE, BOB)
        rec = run_swap(n, maker, taker, base, rel, vol, price, timeout)
        log(json.dumps(rec, default=str)[:400])
    elif which == "batch":
        seq = sys.argv[2] if len(sys.argv) > 2 else "bob"
        start = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        count = int(sys.argv[4]) if len(sys.argv) > 4 else 3
        if seq == "bob":
            maker, taker, base, rel, vol, price = BOB, ALICE, "tELEK", "rBTC", 10, "0.001"
        else:
            maker, taker, base, rel, vol, price = ALICE, BOB, "rELEK", "tELEK", 10, "1"
        for i in range(start, start + count):
            run_swap(i, maker, taker, base, rel, vol, price)
    elif which == "swaps":
        r = my_swaps(*ALICE)
        for s in (r if isinstance(r, list) else []):
            print(s.get("uuid"), s.get("status", {}).get("status"),
                  s.get("maker_coin"), s.get("taker_coin"))


if __name__ == "__main__":
    main()