#!/usr/bin/env python3
"""R1: maker killed mid-swap -> taker payment refund on the taker.

Flow: bob (maker) sells 2 rELEK @0.001, alice (taker) buys. On this build
the taker never claims first: she sends the secret (WatcherMessageSent) and
waits for the maker to claim her payment; only the maker's claim
(TakerPaymentSpent) moves her on to SpendMakerPayment. So with the maker
dead after his own payment is locked, alice sends her own payment anyway
(her flow only needs his payment confirmed, which it is), waits for his
claim, the wait fails (TakerPaymentWaitForSpendFailed) and she refunds her
own taker payment at the taker locktime (started_at + 7800 s, ~2h10m -- same
shape as R2). bob's 2-rELEK maker payment is never taken: it stays
HTLC-locked until the maker locktime (started_at + 2*7800 s) and is
recovered by bob's own refund on his later restart.

How the kill window is hit: bob's WaitConfirmStarted window is essentially
nonexistent for electrum-backed coins -- the attempt-2 timestamps show
ValidatedAndConfirmed only 22 ms after WaitConfirmStarted and his claim
158 ms later, so polling bob for that state loses the race. The script
instead watches ALICE: her MakerPaymentWaitConfirmStarted spans ~7 s (her
2-confirmation wait for bob's payment) while bob's payment data is already
delivered and his own payment is locked -- and bob's claim cannot happen
before she has confirmed her own payment. SIGKILL bob the moment alice
enters that window; she then proceeds alone.

Do NOT freeze the miner loop here: with the taker's payment blocked from
confirming, bob's wait for the taker payment data times out and the swap
ends as a stuck-maker refund instead (no taker payment exists to refund).

Ends with evidence record in runs/refund-results.jsonl.
"""
import json, os, signal, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import rpc, ensure_coins, balance, preflight, ALICE, BOB

RL = "/run/media/julian/ML5/kdf-regtest"
OUT = os.path.join(RL, "runs", "refund-results.jsonl")


def log(m):
    print(m, flush=True)


def last_event(port, pw, uuid):
    r = rpc(port, pw, {"method": "my_swap_status", "params": {"uuid": uuid}})
    evs = (r.get("result") or {}).get("events") or []
    return evs[-1]["event"]["type"] if evs else None


def is_finished(port, pw, uuid):
    r = rpc(port, pw, {"method": "my_swap_status", "params": {"uuid": uuid}})
    res = r.get("result") or {}
    return bool(res.get("is_finished")), [e["event"]["type"] for e in (res.get("events") or [])]


def main():
    ensure_coins(*ALICE)
    ensure_coins(*BOB)
    # mm2 auto-bans the counterpart of every failed swap for 1h; stale orders
    # survive between runs -- clear both before matching again
    preflight(*ALICE)
    preflight(*BOB)
    pre = {n: {c: balance(port, pw, c) for c in ("rELEK", "rBTC")}
           for n, (port, pw) in (("bob", BOB), ("alice", ALICE))}
    log("pre balances: %s" % json.dumps(pre))
    r = rpc(*BOB, {"method": "setprice", "base": "rELEK", "rel": "rBTC",
                   "price": "0.001", "volume": "2", "min_volume": "1",
                   "broadcast": True, "cancel_previous": True})
    if r.get("error"):
        log("setprice failed: %s" % str(r)[:200])
        rec = {"scenario": "R1", "ok": False, "error": "setprice", "detail": str(r)[:300]}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        return 1
    log("order up")
    r2 = rpc(*ALICE, {"method": "buy", "base": "rELEK", "rel": "rBTC",
                      "volume": "2", "price": "0.001", "fill_or_kill": True})
    res = r2.get("result") or {}
    uuid = res.get("swap_uuid") or res.get("uuid")
    if not uuid:
        log("buy failed: %s" % str(r2)[:200])
        rec = {"scenario": "R1", "ok": False, "error": "buy", "detail": str(r2)[:300]}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        return 1
    log("swap uuid %s" % uuid)
    # kill bob the moment ALICE enters her 2-confirmation wait for bob's
    # payment: his payment data is delivered, his payment is locked, and his
    # claim of her payment cannot precede her own confirmation of it
    deadline = time.time() + 90
    killed = False
    trigger = None
    alice_pre = None
    while time.time() < deadline:
        le = last_event(*ALICE, uuid)
        if le == "MakerPaymentWaitConfirmStarted":
            trigger = le
            _, alice_pre = is_finished(*ALICE, uuid)
            pid = int(open(os.path.join(RL, "runs/pids/kdf-bob.pid")).read().strip())
            os.kill(pid, signal.SIGKILL)
            log("KILLED bob pid %d at alice state %s (alice events: %s)" % (pid, le, alice_pre))
            killed = True
            break
        if le == "Finished":
            trigger = "swap_finished_before_kill"
            break
        # bob can enter his claim path from his side too; any catch there is
        # as good as the alice trigger (both payments locked, claim pending)
        le_bob = last_event(*BOB, uuid)
        if le_bob in ("TakerPaymentWaitConfirmStarted", "TakerPaymentValidatedAndConfirmed"):
            trigger = le_bob
            _, alice_pre = is_finished(*ALICE, uuid)
            pid = int(open(os.path.join(RL, "runs/pids/kdf-bob.pid")).read().strip())
            os.kill(pid, signal.SIGKILL)
            log("KILLED bob pid %d at bob state %s (alice events: %s)" % (pid, le_bob, alice_pre))
            killed = True
            break
        if le_bob in ("Finished", "TakerPaymentSpent"):
            trigger = "bob_%s_before_kill" % le_bob
            break
        time.sleep(0.2)
    if not killed:
        log("bob not killed (swap %s ended: %s)" % (uuid, trigger))
        rec = {"scenario": "R1", "uuid": uuid, "killed": None, "ok": False,
               "trigger": trigger, "note": "kill window missed"}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        return 1
    # alice refunds her 0.002-rBTC taker payment at the taker locktime
    # (~2h10m); alice stays up
    wait_deadline = time.time() + 3 * 3600
    ev_list = []
    while time.time() < wait_deadline:
        fin, ev_list = is_finished(*ALICE, uuid)
        if fin:
            log("alice finished: %s" % ev_list)
            break
        time.sleep(20)
    post = {"bob": None}
    post["alice"] = {}
    for c in ("rELEK", "rBTC"):
        try:
            post["alice"][c] = balance(*ALICE, c)
        except Exception:
            post["alice"][c] = None
            log("post balance unavailable for alice %s" % c)
    log("post balances: %s" % json.dumps(post))
    rec = {"scenario": "R1", "uuid": uuid, "killed": "bob", "trigger": trigger,
           "pre": pre, "post": post, "alice_events_before_kill": alice_pre,
           "events_alice": ev_list,
           "taker_refund_ok": ("TakerPaymentRefunded" in ev_list and ev_list[-1] == "Finished")}
    with open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")
    log("R1 record written; taker_refund_ok=%s" % rec["taker_refund_ok"])
    return 0 if rec["taker_refund_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())