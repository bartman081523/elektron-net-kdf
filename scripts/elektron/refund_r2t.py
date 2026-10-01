#!/usr/bin/env python3
"""R2t: taker refund on the phase-4 testnet legs (adapted from R2).

Same shape as R2, moved to the testnet coin set: alice (maker) sells
10 rELEK @1 tELEK, bob (taker) buys paying tELEK. The regtest miner loop
(SINGLE process alternating the rELEK and rBTC chains) is SIGSTOPped for
the kill window so alice's rELEK payment can never confirm; the tELEK
testnet miner (separate loop, testnet_miner.py) stays RUNNING -- after
alice is killed the taker's tELEK payment confirms normally and bob is
left alone waiting for a maker claim that will never come.

On this build the taker never claims first: it is the maker who claims
the taker's payment, and only TakerPaymentSpent moves the taker on to
SpendMakerPayment (taker_swap.rs). With alice dead bob waits until the
taker locktime (started_at + 7800 s, ~2h10m) and refunds his own tELEK
payment -- TakerPaymentRefunded. Alice's rELEK payment stays HTLC-locked
until the maker locktime (started_at + 2*7800) and is recovered by the
maker side's own refund once her instance is restarted and the locktime
has passed (restart handled outside this script, same as R2).

Because the taker is the survivor here, HIS refund completes on a living
instance: no restart needed for the taker side at all.

Run ONLY while no other swap is running (the miner freeze stalls every
rELEK/rBTC payment meanwhile). Ends with an evidence record in
$TL_ROOT/runs/refund-results-testnet.jsonl.
"""
import json, os, signal, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import rpc, balance, preflight, ALICE, BOB
from testnet_rpc import ensure_coins_testnet

TL = os.environ.get("TL_ROOT", "/run/media/julian/ML5/elektron-testnet")
RL = "/run/media/julian/ML5/kdf-regtest"
OUT = os.path.join(TL, "runs", "refund-results-testnet.jsonl")


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
    for port, pw in (ALICE, BOB):
        ensure_coins_testnet(port, pw, ["rELEK", "tELEK"])
    # mm2 auto-bans the counterpart of every failed swap for 1h; stale orders
    # survive between runs -- clear both before matching again
    preflight(*ALICE)
    preflight(*BOB)
    pre = {n: {c: balance(port, pw, c) for c in ("rELEK", "tELEK")}
           for n, (port, pw) in (("alice", ALICE), ("bob", BOB))}
    log("pre balances: %s" % json.dumps(pre))
    # freeze the regtest miner loop (it alternates BOTH regtest chains, so
    # one freeze stalls alice's rELEK payment); the tELEK testnet miner
    # keeps running for the taker's tELEK payment later on
    miner_pid = int(open(os.path.join(RL, "runs/pids/miner.pid")).read().strip())
    log("pausing regtest miner loop pid %d (tELEK miner stays up)" % miner_pid)
    os.kill(miner_pid, signal.SIGSTOP)
    r = rpc(*ALICE, {"method": "setprice", "base": "rELEK", "rel": "tELEK",
                     "price": "1", "volume": "10", "min_volume": "5",
                     "broadcast": True, "cancel_previous": True})
    if r.get("error"):
        log("setprice failed: %s" % str(r)[:200])
        rec = {"scenario": "R2t", "ok": False, "error": "setprice", "detail": str(r)[:300]}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        os.kill(miner_pid, signal.SIGCONT)
        return 1
    log("order up")
    r2 = rpc(*BOB, {"method": "buy", "base": "rELEK", "rel": "tELEK",
                    "volume": "10", "price": "1", "fill_or_kill": True})
    res = r2.get("result") or {}
    uuid = res.get("swap_uuid") or res.get("uuid")
    if not uuid:
        log("buy failed: %s" % str(r2)[:200])
        rec = {"scenario": "R2t", "ok": False, "error": "buy", "detail": str(r2)[:300]}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        os.kill(miner_pid, signal.SIGCONT)
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
            # broadcast-but-unconfirmed proof
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
    log("regtest miner resumed (SIGCONT %d)" % miner_pid)
    if not killed:
        log("alice not killed (swap %s ended: %s)" % (uuid, trigger))
        rec = {"scenario": "R2t", "uuid": uuid, "killed": None, "ok": False,
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
    post = {"bob": {}, "alice": None}
    for c in ("rELEK", "tELEK"):
        try:
            post["bob"][c] = balance(*BOB, c)
        except Exception:
            post["bob"][c] = None
            log("post balance unavailable for bob %s" % c)
    log("post balances: %s" % json.dumps(post))
    rec = {"scenario": "R2t", "uuid": uuid, "killed": "alice", "trigger": trigger,
           "pre": pre, "post": post, "alice_events_before_kill": alice_pre,
           "events_bob": ev_list,
           "taker_refund_ok": ("TakerPaymentRefunded" in ev_list and ev_list[-1] == "Finished")}
    with open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")
    log("R2t record written; taker_refund_ok=%s" % rec["taker_refund_ok"])
    return 0 if rec["taker_refund_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())