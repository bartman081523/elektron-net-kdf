#!/usr/bin/env python3
"""Shared RPC helpers for the elektron-net regtest swap harness."""
import json, os, time, urllib.error, urllib.request

RL = os.environ.get("RL_ROOT", "/run/media/julian/ML5/kdf-regtest")


def _cred(name):
    """kdf rpc passwords come in via env; the repo keeps no credentials."""
    v = os.environ.get(name)
    if not v:
        raise SystemExit("%s not set in environment; the harness keeps "
                         "credentials out of the repo" % name)
    return v


ALICE = (7793, _cred("KDF_RPC_PASS_ALICE"))
BOB = (7794, _cred("KDF_RPC_PASS_BOB"))

def rpc(port, password, body):
    payload = dict(userpass=password)
    payload.update(body)
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d" % port, data=data,
        headers={"Content-Type": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=60).read().decode()
    except urllib.error.HTTPError as e:
        # mm2 returns 500 with a JSON body for RPC-level errors
        raw = e.read().decode()
    return json.loads(raw)

def ok(resp):
    if resp.get("error") is not None:
        return False
    return "result" in resp

def enabled_tickers(port, password):
    r = rpc(port, password, {"method": "get_enabled_coins"})
    if isinstance(r, list):
        return {c.get("ticker") for c in r}
    if isinstance(r, dict) and isinstance(r.get("result"), list):
        return {c.get("ticker") for c in r["result"]}
    return set()

def ensure_coins(port, password):
    """Idempotent activation of rELEK (electrum) and rBTC (native)."""
    t = enabled_tickers(port, password)
    if "rELEK" not in t:
        rpc(port, password, {"method": "electrum", "coin": "rELEK",
                             "servers": [{"url": "127.0.0.1:50003"}],
                             "required_confirmations": 2})
        time.sleep(2)
    if "rBTC" not in t:
        # electrum mode: first activation commonly returns an empty body; retry
        for _ in range(5):
            try:
                r = rpc(port, password, {"method": "electrum", "coin": "rBTC",
                                         "servers": [{"url": "127.0.0.1:50004"}]})
                if r.get("result") == "success":
                    break
            except Exception:
                pass
            time.sleep(3)
        time.sleep(2)
    # Superset semantics: an instance may already carry testnet coins
    # (tELEK/tBTC from the phase-4 legs), activated interactively or
    # restored from its own database -- the swap path only requires the
    # regtest pair to be present.
    t = enabled_tickers(port, password)
    missing = {"rELEK", "rBTC"} - t
    assert not missing, "coins not enabled: missing %s (have %s)" % (
        sorted(missing), sorted(t))
    return t

def balance(port, password, coin):
    r = rpc(port, password, {"method": "my_balance", "coin": coin})
    return float(r.get("balance", "0") or 0)

def unban_all(port, password):
    """mm2 auto-bans a failed-swap counterpart for 1h (pubkey banning).
    A ban silently swallows the next match request, so clear them pre-run."""
    return rpc(port, password, {"method": "unban_pubkeys",
                                "unban_by": {"type": "All"}})

def cancel_orders(port, password):
    """cancel_all_orders legacy form: the CancelBy enum sits top-level."""
    return rpc(port, password, {"method": "cancel_all_orders",
                                "cancel_by": {"type": "All"}})

def preflight(port, password):
    """Make the instance match-ready: no bans, no stale orders."""
    cancel_orders(port, password)
    unban_all(port, password)
