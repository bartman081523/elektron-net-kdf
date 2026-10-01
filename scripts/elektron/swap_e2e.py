#!/usr/bin/env python3
"""Elektron-net native regtest swap harness (Docker-free)."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import ALICE, BOB, rpc, ok, ensure_coins, balance

RL = "/run/media/julian/ML5/kdf-regtest"
RESULTS = os.path.join(RL, "runs", "swap-results.jsonl")
COINS = ("rELEK", "rBTC")
SUCCESS_STATES = ("Successful",)

def log(m): print(m, flush=True)

def my_swaps(port, pw):
    r = rpc(port, pw, {"method": "my_swaps"})
    return r if isinstance(r, list) else r.get("result", r)

def swap_status(port, pw, uuid):
    return rpc(port, pw, {"method": "my_swap_status", "params": {"uuid": uuid}})

def poll_swap(port, pw, uuid, want, timeout=240):
    """Poll my_swap_status until status enters `want`; returns status string."""
    t0 = time.time(); seen = []
    while time.time() - t0 < timeout:
        r = swap_status(port, pw, uuid)
        err = r.get("error")
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
                log("  swap %s -> %s (%d s)" % (uuid[:8], st, time.time()-t0))
            if st in want:
                return st
        if err and "not found" in str(err).lower():
            pass
        elif err:
            log("  status error: %s" % str(err)[:120])
        time.sleep(3)
    log("  TIMEOUT after %ds, seen=%s" % (timeout, seen))
    return None

def run_swap(n, maker, taker, base, rel, vol, price):
    """Maker setprices (sells) `base` for `rel`; taker buys `base` (pays rel)."""
    mport, mpw = maker
    tport, tpw = taker
    log("swap %d: maker sells %s for %s, vol=%s price=%s" % (n, base, rel, vol, price))
    for port, pw in (maker, taker):
        ensure_coins(port, pw)
        preflight(port, pw)
    pre = {side: {c: balance(port, pw, c) for c in COINS} for side, (port, pw) in
           (("maker", maker), ("taker", taker))}
    r = rpc(mport, mpw, {"method": "setprice", "base": base, "rel": rel,
                         "price": str(price), "volume": str(vol),
                         "min_volume": str(float(vol) / 2), "broadcast": True,
                         "cancel_previous": True})
    if r.get("error"):
        rec = {"n": n, "error": "setprice failed: %s" % str(r)[:300]}
        with open(RESULTS, "a") as f: f.write(json.dumps(rec) + "\n")
        return rec

    r2 = rpc(tport, tpw, {"method": "buy", "base": base, "rel": rel,
                          "volume": str(vol), "price": str(price),
                          "fill_or_kill": True})
    res = r2.get("result") if isinstance(r2.get("result"), dict) else {}
    uuid = res.get("swap_uuid") or res.get("uuid")
    if not uuid:
        rec = {"n": n, "error": "buy failed: %s" % str(r2)[:300]}
        with open(RESULTS, "a") as f: f.write(json.dumps(rec) + "\n")
        return rec

    st_m = poll_swap(mport, mpw, uuid, SUCCESS_STATES)
    st_t = poll_swap(tport, tpw, uuid, SUCCESS_STATES)
    post = {side: {c: balance(port, pw, c) for c in COINS} for side, (port, pw) in
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
        # single <n> <role:bob|alice> <base> <rel> <vol> <price>
        #   role = who is the maker; the other instance is the taker
        n, role, base, rel, vol, price = (
            int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7])
        maker, taker = (BOB, ALICE) if role == "bob" else (ALICE, BOB)
        rec = run_swap(n, maker, taker, base, rel, vol, price)
        log(json.dumps(rec, default=str)[:400])
    elif which == "batch":
        seq = sys.argv[2] if len(sys.argv) > 2 else "bob"
        start = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        for i in range(start, start + 5):
            if seq == "bob":
                run_swap(i, BOB, ALICE, "rELEK", "rBTC", 10, "0.001")
            else:
                run_swap(i, ALICE, BOB, "rBTC", "rELEK", 0.01, "1000")
    else:
        r = my_swaps(*ALICE)
        for s in (r if isinstance(r, list) else []):
            print(s.get("uuid"), s.get("status", {}).get("status"), s.get("maker_coin"), s.get("taker_coin"))

if __name__ == "__main__":
    main()
