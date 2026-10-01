#!/usr/bin/env python3
"""R2: taker refund when the maker (alice) dies mid-swap, stays dead.

Flow: alice (maker) sells 0.001 rBTC @1000, bob (taker) buys paying rELEK.
The miner loop is SIGSTOPped before the order is placed; under the freeze
alice's payment may still be broadcast, but it can never confirm: without
blocks bob stays at MakerPaymentWaitConfirmStarted forever (his window waits
for confirmations of alice's payment, and no blocks means no confirmations).
The moment bob's last event is MakerPaymentWaitConfirmStarted, alice's full
event history is captured (proof of broadcast-but-unconfirmed) and alice is
SIGKILLed. Miner SIGCONTed, bob proceeds alone: alice's payment confirms,
bob broadcasts his own taker payment, sends the secret to the maker
(WatcherMessageSent) and waits -- on this build the taker never claims
first; it is the maker who claims the taker's payment, and only the
TakerPaymentSpent event moves the taker on to SpendMakerPayment
(taker_swap.rs state machine). Alice is dead and never claims, so the
wait fails (TakerPaymentWaitForSpendFailed) and bob refunds his own
taker payment at the taker locktime (started_at + 7800 s, ~2h10m).
Alice's payment is never taken: it stays HTLC-locked until the maker
locktime (started_at + 2*7800 s) and is recovered by the maker side's
own refund on a later restart. Ends with evidence record in
runs/refund-results.jsonl.

Killing the maker at any later point is the same outcome: once the taker
has sent payment and secret he is only waiting for the maker's claim
(WatcherMessageSent -> WaitForTakerPaymentSpend), and with the maker dead
the wait fails into the same refund. The miner freeze exists only to pin
the kill window while the taker is still early (WaitConfirmStarted):
without the freeze the swap confirms and completes green in ~35 s.

Run ONLY after R1 completed (alice must stay up for R1's refund first).
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
    # freeze the miner loop for the whole kill window: the NoFee policy leaves
    # the maker nothing to wait for at the instructions stage (an empty fee
    # validates against the mempool with 0 confs), so it is the TAKER that
    # stalls: bob's MakerPaymentWaitConfirmStarted blocks on confirmations of
    # alice's payment and, with no mined blocks, that window never closes
    miner_pid = int(open(os.path.join(RL, "runs/pids/miner.pid")).read().strip())
    log("pausing miner loop pid %d" % miner_pid)
    os.kill(miner_pid, signal.SIGSTOP)
    r = rpc(*ALICE, {"method": "setprice", "base": "rBTC", "rel": "rELEK",
                     "price": "1000", "volume": "0.001", "min_volume": "0.0005",
                     "broadcast": True, "cancel_previous": True})
    if r.get("error"):
        log("setprice failed: %s" % str(r)[:200])
        rec = {"scenario": "R2", "ok": False, "error": "setprice", "detail": str(r)[:300]}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        return 1
    log("order up")
    r2 = rpc(*BOB, {"method": "buy", "base": "rBTC", "rel": "rELEK",
                    "volume": "0.001", "price": "1000", "fill_or_kill": True})
    res = r2.get("result") or {}
    uuid = res.get("swap_uuid") or res.get("uuid")
    if not uuid:
        log("buy failed: %s" % str(r2)[:200])
        rec = {"scenario": "R2", "ok": False, "error": "buy", "detail": str(r2)[:300]}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        return 1
    log("swap uuid %s" % uuid)
    deadline = time.time() + 90
    killed = False
    trigger = None
    alice_pre = None
    while time.time() < deadline:
        le = last_event(*BOB, uuid)
        if le == "MakerPaymentWaitConfirmStarted":
            trigger = le
            # capture alice's event history while her instance is alive:
            # the broadcast-but-unconfirmed proof the docstring promises
            _, alice_pre = is_finished(*ALICE, uuid)
            pid = int(open(os.path.join(RL, "runs/pids/kdf-alice.pid")).read().strip())
            os.kill(pid, signal.SIGKILL)
            log("KILLED alice pid %d at bob state %s (alice events: %s)" % (pid, le, alice_pre))
            killed = True
            break
        if le == "Finished":
            trigger = "swap_finished_before_kill"
            break
        time.sleep(0.2)
    os.kill(miner_pid, signal.SIGCONT)
    log("miner resumed (SIGCONT %d)" % miner_pid)
    if not killed:
        log("alice not killed (swap %s ended: %s)" % (uuid, trigger))
        rec = {"scenario": "R2", "uuid": uuid, "killed": None, "ok": False,
               "trigger": trigger, "note": "kill window missed (miner resumed)"}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        return 1
    # bob refunds his taker payment at taker locktime (~2h10m); bob stays up
    wait_deadline = time.time() + 3 * 3600
    ev_list = []
    while time.time() < wait_deadline:
        fin, ev_list = is_finished(*BOB, uuid)
        if fin:
            log("bob finished: %s" % ev_list)
            break
        time.sleep(20)
    post = {}
    post["bob"] = {}
    for c in ("rELEK", "rBTC"):
        try:
            post["bob"][c] = balance(*BOB, c)
        except Exception:
            post["bob"][c] = None
            log("post balance unavailable for bob %s" % c)
    post["alice"] = None  # killed instance is down; restart handled outside
    log("post balances: %s" % json.dumps(post))
    rec = {"scenario": "R2", "uuid": uuid, "killed": "alice", "trigger": trigger,
           "pre": pre, "post": post, "alice_events_before_kill": alice_pre,
           "events_bob": ev_list,
           "taker_refund_ok": ("TakerPaymentRefunded" in ev_list and ev_list[-1] == "Finished")}
    with open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")
    log("R2 record written; taker_refund_ok=%s" % rec["taker_refund_ok"])
    return 0 if rec["taker_refund_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())