#!/usr/bin/env python3
"""Watch the maker-side refund of previously stuck swaps (maker ran uninterrupted).

Four swaps have maker payments locked on-chain (bob = maker, 2 rELEK each;
the taker's payment never arrived). Their maker_payment_lock is
started_at + 2*lock_duration (~4h20m after start). As long as bob keeps
running, mm2 retries the refund and broadcasts once CLTV unlocks. Appends
one record per swap to runs/refund-results.jsonl.

The watcher keeps polling while the kdf instance is down (connection
refused): refund attempts resume after a restart, so downtime here is
expected, not fatal.
"""
import json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import rpc, BOB

RL = "/run/media/julian/ML5/kdf-regtest"
OUT = os.path.join(RL, "runs", "refund-results.jsonl")

UUIDS = ["76878b0b-674d-4892-bc6f-bf2456755f85",
         "14f6db4b-7246-40ab-b4e7-2cece68b98f9",
         "669c742c-e2f7-438c-b857-5deb44aa9fb1",
         "612ef02b-aff9-4b19-a5f8-d826fdaa4f18"]


def log(m):
    print(m, flush=True)


def status(port, pw, uuid):
    r = rpc(port, pw, {"method": "my_swap_status", "params": {"uuid": uuid}})
    return r.get("result") or {}


def main():
    todo = dict.fromkeys(UUIDS)
    deadline = time.time() + 4 * 3600
    log("watching %d stuck maker payments for refund" % len(todo))
    while todo and time.time() < deadline:
        for uuid in list(todo):
            try:
                res = status(*BOB, uuid)
            except Exception as e:
                if todo[uuid] != "rpc_down":
                    todo[uuid] = "rpc_down"
                    log("  %s -> rpc down (%s); refund retries resume on restart" %
                        (uuid[:8], type(e).__name__))
                continue
            if not res.get("is_finished"):
                evs = res.get("events") or []
                le = evs[-1]["event"]["type"] if evs else None
                if le != todo[uuid]:
                    todo[uuid] = le
                    log("  %s -> %s" % (uuid[:8], le))
                continue
            evs = res.get("events") or []
            ev_types = [e["event"]["type"] for e in evs]
            refunded = "MakerPaymentRefunded" in ev_types
            last = ev_types[-1] if ev_types else None
            log("%s FINISHED refunded=%s last=%s" % (uuid[:8], refunded, last))
            rec = {"scenario": "MAKER-REFUND", "uuid": uuid, "killed": None,
                   "trigger": "maker_payment_lock_expiry",
                   "events_maker": ev_types,
                   "maker_refund_ok": refunded and last == "Finished"}
            with open(OUT, "a") as f:
                f.write(json.dumps(rec) + "\n")
            log("record written; maker_refund_ok=%s" % rec["maker_refund_ok"])
            del todo[uuid]
        if todo:
            time.sleep(30)
    if todo:
        log("TIMEOUT: never finished: %s" % [u[:8] for u in todo])
    return 0 if not todo else 1


if __name__ == "__main__":
    sys.exit(main())