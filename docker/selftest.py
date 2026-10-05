#!/usr/bin/env python3
"""Marketplace container selftest (stdlib only).

Exercises the full daemon JSON-RPC surface the SPA uses — against BOTH local
daemons (market 7795, trader 7796) in TESTNET mode. Nothing touches mainnet;
a failure in the hard matrix aborts the container (deploy gate). Funded
steps (maker orders need real testnet balances) are skipped loudly with the
funding addresses printed, so a testnet faucet deposit flips them on.

Envelope facts this script relies on (verified in the local T2 campaign):
  legacy  : {"userpass", "method", ...fields} top-level orderbook/setprice/
            my_orders/cancel_order/cancel_all_orders/electrum/my_balance
  v2      : {"mmrpc":"2.0", "userpass", "method", "params"} for withdraw /
            trade_preimage / my_swap_status
  my_orders legacy returns {maker_orders:{},taker_orders:{}} (object form)
  activating an already-activated electrum coin errors with a message
            containing "already" (idempotency probe)
  the electrum activation request key is `servers` — the coins file spells
            the same values "url"; sending "urls" deserializes to null
            ("invalid type: null, expected a sequence", utxo.rs)
"""

import http.client
import json
import os
import socket
import sys
import time

STATE_DIR = os.environ.get("MM_STATE_DIR", "/run/elek")
MARKET_PORT = 7795
TRADER_PORT = 7796
TELEK = bool(os.environ.get("MM_TELEK_ELECTRS", "").strip())

CONF_PATHS = {
    "market": os.path.join(STATE_DIR, "market", "MM2.json"),
    "trader": os.path.join(STATE_DIR, "trader", "MM2.json"),
}
PASS = None  # same rpc password for both daemons (setup_env.py guarantees it)

BOGUS_UUID = "11111111-1111-1111-1111-111111111111"


class RpcError(Exception):
    def __init__(self, payload, kind="legacy"):
        self.payload = payload
        self.kind = kind
        super().__init__(f"{kind}: {payload}")


def rpc(port, method, fields=None, params=None):
    """One JSON-RPC call. fields=legacy top-level, params=v2 envelope."""
    if params is not None:
        body = {"mmrpc": "2.0", "userpass": PASS, "method": method, "params": params}
    else:
        body = {"userpass": PASS, "method": method}
        body.update(fields or {})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=90)
    conn.request("POST", "/", json.dumps(body), {"Content-Type": "application/json"})
    resp = conn.getresponse()
    try:
        data = json.loads(resp.read().decode())
    finally:
        conn.close()
    if "error" in data:
        raise RpcError(data["error"], "v2" if params is not None else "legacy")
    return data.get("result", data)


def sse_probe(port):
    """GET /event-stream must answer 200 + text/event-stream immediately."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", "/event-stream?id=981234500001")
    resp = conn.getresponse()
    ok = resp.status == 200 and "text/event-stream" in (resp.getheader("Content-Type") or "")
    conn.close()
    return ok


def activate(port, coin, servers):
    """electrum activation; an 'already' error means activated and passes.

    The request key is `servers` (as in web/js/api.mjs electrum()); the coins
    file calls the same strings "url" — different key, same values.
    """
    for attempt in range(3):
        try:
            rpc(port, "electrum", fields={
                "coin": coin, "servers": servers,
                "required_confirmations": 2, "mature_confirmations": 1})
            return f"activated on attempt {attempt + 1}"
        except RpcError as e:
            if "already" in str(e.payload).lower():
                return "already activated"
            if attempt == 2:
                raise
        time.sleep(5)
    return "?"


def wait_enabled(port, coin, seconds=90):
    end = time.time() + seconds
    while time.time() < end:
        try:
            coins = [c.get("ticker") for c in rpc(port, "get_enabled_coins") or []]
            if coin in coins:
                return True
        except RpcError:
            return False
        time.sleep(3)
    return False


def balance(port, coin):
    return rpc(port, "my_balance", fields={"coin": coin})


def n_orders(res):
    r = res.get("result", res) if isinstance(res, dict) else res
    if not isinstance(r, dict):
        return -1
    return len(r.get("maker_orders") or {}) + len(r.get("taker_orders") or {})


RESULTS = []


def record(name, kind, ok, detail=""):
    RESULTS.append({"name": name, "kind": kind, "ok": ok, "detail": detail})
    tag = {True: "PASS", None: "SKIP", False: "FAIL"}[ok if ok is not None else None]
    print(f"  [{tag}] {name}{' :: ' + detail if detail else ''}")


def skip(name, reason):
    record(name, "fund-gated", None, f"skipped: {reason}")


def Number(x):  # kdf balance strings arrive as decimal strings
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main():
    global PASS
    with open(CONF_PATHS["trader"], encoding="utf-8") as f:
        PASS = json.load(f)["rpc_password"]

    coins = json.load(open(os.path.join(STATE_DIR, "coins.json"), encoding="utf-8"))  # noqa: SIM115
    by_ticker = {c["coin"]: c for c in coins}
    btc = "tBTC"
    telek = "tELEK" if TELEK else None
    urls = lambda c: [s["url"] for s in by_ticker[c]["electrum"]]  # noqa: E731
    # (URL strings from the coins file ride the `servers` request key, never "urls")

    print(f"selftest: market :{MARKET_PORT} | trader :{TRADER_PORT} | coins {list(by_ticker)}")

    # ---- hard matrix: the daemon API contract the SPA depends on ----------
    ver_t = rpc(TRADER_PORT, "version")
    record("trader version", "hard", bool(ver_t), str(ver_t))
    ver_m = rpc(MARKET_PORT, "version")
    record("market version", "hard", bool(ver_m), str(ver_m))

    try:
        note = activate(TRADER_PORT, btc, urls(btc))
        record("activate tBTC (trader)", "hard", True, note)
    except RpcError as e:
        record("activate tBTC (trader)", "hard", False, str(e.payload)[:300])
    try:
        note = activate(MARKET_PORT, btc, urls(btc))
        record("activate tBTC (market)", "hard", True, note)
    except RpcError as e:
        record("activate tBTC (market)", "hard", False, str(e.payload)[:300])

    record("trader get_enabled_coins has tBTC", "hard",
           wait_enabled(TRADER_PORT, btc, 120))
    record("market get_enabled_coins has tBTC", "hard",
           wait_enabled(MARKET_PORT, btc, 120))

    if telek:
        for who, port in (("trader", TRADER_PORT), ("market", MARKET_PORT)):
            try:
                note = activate(port, telek, urls(telek))
                record(f"activate tELEK ({who})", "hard", True, note)
            except RpcError as e:
                record(f"activate tELEK ({who})", "hard", False, str(e.payload)[:300])

    print("  ---- FUND THE TESTNET WALLET(S): deposit testnet coins to these addresses ----")
    bal_t, bal_m = {}, {}
    for who, port in (("trader", TRADER_PORT), ("market", MARKET_PORT)):
        for coin in [btc] + ([telek] if telek else []):
            try:
                b = balance(port, coin)
                (bal_t if who == "trader" else bal_m)[coin] = b
                print(f"  ★ FUND {who:6s} {coin:6s} -> {b.get('address')} "
                      f"(balance {b.get('balance', b.get('spendable_balance'))})")
            except RpcError as e:
                record(f"balance {coin} ({who})", "hard", False, str(e.payload)[:200])

    ob_pair = (btc, telek) if telek else None
    if ob_pair:
        try:
            ob = rpc(TRADER_PORT, "orderbook", fields={"base": ob_pair[0], "rel": ob_pair[1]})
            ok = isinstance(ob, dict) and isinstance(ob.get("asks"), list) \
                and isinstance(ob.get("bids"), list)
            record("trader orderbook tBTC/tELEK structure", "hard", ok)
        except RpcError as e:
            record("trader orderbook tBTC/tELEK structure", "hard",
                   False, str(e.payload)[:200])
    else:
        skip("trader orderbook structure", "single-coin mode (set MM_TELEK_ELECTRS for a real pair)")

    mo = rpc(TRADER_PORT, "my_orders")
    record("trader my_orders object form", "hard", "maker_orders" in mo and "taker_orders" in mo)

    ca = rpc(TRADER_PORT, "cancel_all_orders", fields={"cancel_by": {"type": "All"}})
    record("trader cancel_all_orders (write path)", "hard", ca is not None, str(ca)[:120])

    try:
        rpc(TRADER_PORT, "electrum", fields={
            "coin": btc, "servers": urls(btc),
            "required_confirmations": 2, "mature_confirmations": 1})
        record("re-activate tBTC idempotency (expect err)", "hard", False,
               "unexpected success — already-activated coin must err")
    except RpcError as e:
        record("re-activate tBTC idempotency (expect err)", "hard",
               "already" in str(e.payload).lower())

    try:
        to = bal_t.get(btc, {}).get("address") if bal_t else None
        rpc(TRADER_PORT, "withdraw", params={
            "coin": btc, "to": to or "tb1qinsufficientbalanceprobe", "amount": 999999999})
        record("withdraw huge amount -> err", "hard", False, "unexpected success")
    except RpcError as e:
        record("withdraw huge amount -> err", "hard", True, str(e.payload)[:160])

    try:
        rpc(TRADER_PORT, "trade_preimage", params={
            "base": "NOSUCHCOIN", "rel": btc, "swap_method": "setprice", "price": "1", "volume": "0.001"})
        record("trade_preimage bogus coin -> err", "hard", False, "unexpected success")
    except RpcError as e:
        record("trade_preimage bogus coin -> err", "hard", True, str(e.payload)[:160])

    try:
        rpc(TRADER_PORT, "my_swap_status", params={"uuid": BOGUS_UUID})
        record("my_swap_status bogus -> err", "hard", False, "unexpected success")
    except RpcError as e:
        record("my_swap_status bogus -> err", "hard", True, str(e.payload)[:160])

    try:
        act = rpc(TRADER_PORT, "active_swaps")
        rec = rpc(TRADER_PORT, "my_recent_swaps")  # legacy name; "recent_swaps" does not exist
        record("trader active_swaps + my_recent_swaps", "hard",
               act is not None and rec is not None)
    except RpcError as e:
        record("trader active_swaps + my_recent_swaps", "hard", False, str(e.payload)[:200])

    record("trader SSE /event-stream reachable", "hard", sse_probe(TRADER_PORT),
           "GET /event-stream -> 200 text/event-stream")

    # ---- maker-order path (needs funded testnet balances, else skip) ----
    if not telek:
        skip("maker setprice/orderbook-visibility/cancel",
             "no tELEK configured (MM_TELEK_ELECTRS) — single-coin mode cannot form a pair")
    else:
        bal_m_btc = Number((bal_m.get(btc) or {}).get("balance"))
        bal_m_tek = Number((bal_m.get(telek) or {}).get("balance"))
        if bal_m_btc <= 0 and bal_m_tek <= 0:
            skip("maker setprice/orderbook-visibility/cancel",
                 f"market wallet is empty — fund {btc} or {telek} at the ★ addresses above")
        else:
            try:
                base_c, rel_c = (btc, telek) if bal_m_btc > 0 else (telek, btc)
                price = "1"  # test-mode order; price is irrelevant for the ledger
                volume = str(max(Number((bal_m.get(base_c) or {}).get("balance")) * 0.02, 0))
                order = rpc(MARKET_PORT, "setprice", fields={
                    "base": base_c, "rel": rel_c, "price": price,
                    "volume": volume, "min_volume": str(Number(volume) / 10 or volume),
                    "order_type": {"type": "GoodTillCancelled"}})
                uuid = order.get("uuid")
                record("maker setprice " + "-".join((base_c, rel_c)), "fund-gated",
                       isinstance(uuid, str) and len(uuid) > 10)
                n_mine = n_orders(rpc(MARKET_PORT, "my_orders"))
                record("maker my_orders == 1", "fund-gated", n_mine == 1, f"n={n_mine}")
                if uuid:
                    seen = False
                    end = time.time() + 90
                    while time.time() < end and not seen:
                        ob = rpc(TRADER_PORT, "orderbook", fields={"base": base_c, "rel": rel_c})
                        side = ob.get("asks" if base_c == btc else "bids") or []
                        seen = any(o.get("is_mine") or o.get("price") for o in side)
                        if seen:
                            break
                        time.sleep(5)
                    record("order visible on trader daemon", "fund-gated", seen,
                           f"polling {'asks' if base_c == btc else 'bids'}")
                    rpc(MARKET_PORT, "cancel_order", fields={"uuid": uuid})
                    record("maker cancel_order", "fund-gated", True)
                n_after = n_orders(rpc(MARKET_PORT, "my_orders"))
                record("maker my_orders == 0 after cancel", "fund-gated", n_after == 0, f"n={n_after}")
            except RpcError as e:
                record("maker order path", "fund-gated", False, str(e.payload)[:300])

    fails = [r for r in RESULTS if r["ok"] is False]
    skips = [r for r in RESULTS if r["ok"] is None]
    print(f"SELFTEST RESULT: {'PASS' if not fails else 'FAIL'} "
          f"({len(RESULTS) - len(fails) - len(skips)}/{len(RESULTS)} ok, {len(skips)} skipped)")
    for r in fails:
        print(f"  FAIL {r['name']}: {r['detail']}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()