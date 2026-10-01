#!/usr/bin/env python3
"""Testnet coin activation for the elektron-net swap harness (phase 4).

The phase-4 legs and their electrum servers:
  tELEK  Elektron testnet    127.0.0.1:50005 (electrs-tn; the electrs fork
                             serves the Elektron testnet genesis on the
                             Network::Signet stand-in, see doc/elektron.md)
  tBTC   Bitcoin testnet3    testnet.aranguren.org:51001 (public Fulcrum)
  rELEK  Elektron regtest    127.0.0.1:50003 (kept so mixed-chain pairs run
         leg            against the phase-3 regtest infra)
  rBTC   regtest BTC leg     127.0.0.1:50004

kdf RPC primitives come from swap_rpc (same instance layout: alice 7793,
bob 7794; kdf rpc passwords via KDF_RPC_PASS_ALICE / KDF_RPC_PASS_BOB).
"""
import os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swap_rpc import ALICE, BOB, rpc, balance, preflight  # noqa: F401

SERVERS = {
    "tELEK": [{"url": "127.0.0.1:50005"}],
    "tBTC": [{"url": "testnet.aranguren.org:51001"}],
    "rELEK": [{"url": "127.0.0.1:50003"}],
    "rBTC": [{"url": "127.0.0.1:50004"}],
}


def _enabled_tickers(port, password):
    r = rpc(port, password, {"method": "get_enabled_coins"})
    if isinstance(r, list):
        return {c.get("ticker") for c in r}
    if isinstance(r, dict) and isinstance(r.get("result"), list):
        return {c.get("ticker") for c in r["result"]}
    return set()


def ensure_coins_testnet(port, password, coins=None):
    """Idempotent activation of the given tickers (default: all four)."""
    coins = tuple(SERVERS) if coins is None else tuple(coins)
    for coin in coins:
        if coin in _enabled_tickers(port, password):
            continue
        # legacy electrum activation: the first call can return an empty
        # 500 body -- retry until get_enabled_coins lists the coin
        for _ in range(5):
            try:
                rpc(port, password, {"method": "electrum", "coin": coin,
                                     "servers": SERVERS[coin],
                                     "required_confirmations": 2})
            except Exception:
                pass
            time.sleep(3)
            if coin in _enabled_tickers(port, password):
                break
        time.sleep(2)
    missing = set(coins) - _enabled_tickers(port, password)
    assert not missing, "coins not enabled: missing %s (have %s)" % (
        sorted(missing), sorted(_enabled_tickers(port, password)))
    return _enabled_tickers(port, password)


def get_address(port, password, coin):
    """Receiving address of an Iguana-activated coin.

    my_balance carries the address (get_new_address would require
    HD-wallet activation -- see get_new_address.rs
    CoinIsActivatedNotWithHDWallet). The regtest harness used the same
    path (regtest_fund.sh: "from kdf my_balance.address")."""
    r = rpc(port, password, {"method": "my_balance", "coin": coin})
    d = r.get("result")
    if not isinstance(d, dict):
        d = r
    addr = None
    if isinstance(d, dict):
        addr = d.get("address")
        if not addr and isinstance(d.get(coin), dict):
            addr = d[coin].get("address")
    if not addr:
        raise RuntimeError("no address in my_balance %s: %s" % (coin, str(r)[:300]))
    return addr