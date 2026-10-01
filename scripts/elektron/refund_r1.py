#!/usr/bin/env python3
"""R1: maker killed before MakerPaymentSent -> taker payment refund on the taker.

Flow: bob (maker) sells 2 rELEK @0.001, alice (taker) buys. As soon as bob's
swap state hits MakerPaymentInstructionsReceived, bob is SIGKILLed (crash
simulation). Alice's taker payment then refunds at the taker payment locktime
(~2h10m). Ends with evidence record in runs/refund-results.jsonl.
"""
import json, os, signal, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import rpc, ensure_coins, balance, preflight, ALICE, BOB

RL = "/run/media/julian/ML5/kdf-regtest"
OUT = os.path.join(RL, "runs", "refund-results.jsonl")

def log(m): print(m, flush=True)

def last_event(port, pw, uuid):
    r = rpc(port, pw, {'method': 'my_swap_status', 'params': {'uuid': uuid}})
    evs = (r.get('result') or {}).get('events') or []
    return evs[-1]['event']['type'] if evs else None

def is_taker_finished(port, pw, uuid):
    r = rpc(port, pw, {'method': 'my_swap_status', 'params': {'uuid': uuid}})
    res = r.get('result') or {}
    return bool(res.get('is_finished')), [e['event']['type'] for e in (res.get('events') or [])]

def main():
    ensure_coins(*ALICE)
    ensure_coins(*BOB)
    # mm2 auto-bans the counterpart of every failed swap for 1h; stale orders
    # survive between runs -- clear both before matching again
    preflight(*ALICE)
    preflight(*BOB)
    pre = {n: {c: balance(port, pw, c) for c in ('rELEK', 'rBTC')}
           for n, (port, pw) in (('bob', BOB), ('alice', ALICE))}
    log("pre balances: %s" % json.dumps(pre))
    r = rpc(*BOB, {'method': 'setprice', 'base': 'rELEK', 'rel': 'rBTC',
                   'price': '0.001', 'volume': '2', 'min_volume': '1',
                   'broadcast': True, 'cancel_previous': True})
    if r.get('error'):
        log("setprice failed: %s" % str(r)[:200]); return 1
    log("order up")
    r2 = rpc(*ALICE, {'method': 'buy', 'base': 'rELEK', 'rel': 'rBTC',
                      'volume': '2', 'price': '0.001', 'fill_or_kill': True})
    res = r2.get('result') or {}
    uuid = res.get('swap_uuid') or res.get('uuid')
    if not uuid:
        log("buy failed: %s" % str(r2)[:200]); return 1
    log("swap uuid %s" % uuid)
    # kill bob the moment his last event is TakerPaymentWaitConfirmStarted:
    # alice's taker payment is sent and confirmed (locked), bob has not yet
    # spent it, and bob's own maker payment is also locked. Refund at locktime.
    deadline = time.time() + 90
    killed = False
    trigger = None
    while time.time() < deadline:
        le = last_event(*BOB, uuid)
        if le in ('TakerPaymentWaitConfirmStarted', 'TakerPaymentValidatedAndConfirmed'):
            trigger = le
            pid = int(open(os.path.join(RL, 'runs/pids/kdf-bob.pid')).read().strip())
            os.kill(pid, signal.SIGKILL)
            log("KILLED bob pid %d at state %s" % (pid, le))
            killed = True
            break
        if le == 'Finished':
            trigger = 'swap_finished_before_kill'
            break
        time.sleep(0.5)
    if not killed:
        log("bob not killed (swap %s ended: %s)" % (uuid, trigger))
        rec = {"scenario": "R1", "uuid": uuid, "killed": None, "ok": False,
               "trigger": trigger, "note": "kill window missed"}
        with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
        return 1
    # wait for alice's taker payment refund (~2h10m); poll every 20s
    wait_deadline = time.time() + 3*3600
    while time.time() < wait_deadline:
        fin, ev_list = is_taker_finished(*ALICE, uuid)
        if fin:
            log("alice finished: %s" % ev_list)
            break
        time.sleep(20)
    post = {}
    for n, (port, pw) in (('bob', BOB), ('alice', ALICE)):
        post[n] = {}
        for c in ('rELEK', 'rBTC'):
            try:
                post[n][c] = balance(port, pw, c)
            except Exception:
                post[n][c] = None  # killed instance is down
                log("post balance unavailable for %s %s (instance down)" % (n, c))
    log("post balances: %s" % json.dumps(post))
    rec = {"scenario": "R1", "uuid": uuid, "killed": "bob", "trigger": trigger,
           "pre": pre, "post": post,
           "events_alice": ev_list,
           "taker_refund_ok": ("TakerPaymentRefunded" in ev_list and ev_list[-1] == 'Finished')}
    with open(OUT, "a") as f: f.write(json.dumps(rec) + "\n")
    log("R1 record written; taker_refund_ok=%s" % rec["taker_refund_ok"])
    return 0 if rec["taker_refund_ok"] else 1

if __name__ == "__main__":
    sys.exit(main())
