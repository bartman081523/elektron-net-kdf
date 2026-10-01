#!/usr/bin/env python3
"""Verify alice's post-restart resume of the R2 swap (maker-side leftovers).

Alice was SIGKILLed mid-swap in R2 with her 0.001-rBTC maker payment already
confirmed on-chain and never claimed: the taker waits for the maker's claim,
and with the maker dead the taker refunded his own payment instead. Alice's
payment stays HTLC-locked until the maker locktime (started_at + 2*7800 s),
which has already passed, so kdf should refund it as soon as her instance
has re-scanned the swap after this restart. Appends one record to
runs/refund-results.jsonl.
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import rpc, ALICE

RL = "/run/media/julian/ML5/kdf-regtest"
OUT = os.path.join(RL, "runs", "refund-results.jsonl")
UUID = sys.argv[1] if len(sys.argv) > 1 else "f4679a0e-1663-427b-a19c-981abf8fdd5c"


def log(m):
    print(m, flush=True)


def status(port, pw, uuid):
    r = rpc(port, pw, {"method": "my_swap_status", "params": {"uuid": uuid}})
    return r.get("result") or {}


def bal(port, pw, coin):
    try:
        return rpc(port, pw, {"method": "my_balance", "coin": coin}).get("balance")
    except Exception as e:
        return "unavailable (%s)" % type(e).__name__


def main():
    log("alice rBTC balance before resume: %s" % bal(*ALICE, "rBTC"))
    deadline = time.time() + 20 * 60
    last = None
    while time.time() < deadline:
        try:
            res = status(*ALICE, UUID)
        except Exception as e:
            log("  rpc down (%s)" % type(e).__name__)
            time.sleep(3)
            continue
        if res.get("is_finished"):
            evs = [e["event"]["type"] for e in (res.get("events") or [])]
            log("alice finished: %s" % evs)
            after = bal(*ALICE, "rBTC")
            log("alice rBTC balance after: %s" % after)
            rec = {"scenario": "R2-RESTART-REFUND", "uuid": UUID,
                   "killed": "alice (SIGKILL mid-swap in R2)",
                   "trigger": "maker_locktime_expired_recovery_on_restart",
                   "events_alice_after_restart": evs,
                   "maker_refund_ok": ("MakerPaymentRefunded" in evs and evs[-1] == "Finished"),
                   "rbtc_balance_after": after}
            with open(OUT, "a") as f:
                f.write(json.dumps(rec) + "\n")
            log("record written; maker_refund_ok=%s" % rec["maker_refund_ok"])
            return 0 if rec["maker_refund_ok"] else 1
        evs = res.get("events") or []
        le = evs[-1]["event"]["type"] if evs else None
        if le != last:
            last = le
            log("  alice -> %s" % le)
        time.sleep(5)
    log("TIMEOUT: alice swap not finished in 20m")
    return 1


if __name__ == "__main__":
    sys.exit(main())