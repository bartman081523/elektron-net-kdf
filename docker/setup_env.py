#!/usr/bin/env python3
"""Runtime setup for the elektron-net marketplace container.

Generates (in MM_STATE_DIR, default /run/elek):
  coins.json       tBTC (testnet default: MM_COINS_SRC) — plus the optional
                   eleks coin: tELEK when MM_TELEK_ELECTRS is set (testnet
                   mode) or ELEK when MM_ELEK_ELECTRS is set (mainnet option)
  market/MM2.json  market-maker daemon config (i_am_seed)
  trader/MM2.json  visitor-facing daemon config (seednodes -> 127.0.0.1)

Secrets (rpc password, wallet passphrases) are written ONLY to these files
(0600); stdout never receives them — only their provenance.
"""

import itertools
import json
import os
import secrets
import stat
import string

STATE_DIR = os.environ.get("MM_STATE_DIR", "/run/elek")
COINS_SRC = os.environ.get(
    "MM_COINS_SRC", "/app/docker/coins-testnet.json")
TELEK_TEMPLATE = os.environ.get(
    "MM_TELEK_TEMPLATE", "/app/docker/telek-template.json")
TELEK_ELECTRS = os.environ.get("MM_TELEK_ELECTRS", "").strip()
ELEK_TEMPLATE = os.environ.get(
    "MM_ELEK_TEMPLATE", "/app/docker/elek-template.json")
ELEK_ELECTRS = os.environ.get("MM_ELEK_ELECTRS", "").strip()

NETID = int(os.environ.get("MM_NETID", "8888"))
MARKET_RPC = 7795
TRADER_RPC = 7796
GUI = "elektron-net"


def write_private(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(os.path.dirname(path), 0o700)


def rpc_pass() -> str:
    env = os.environ.get("MM_RPC_PASS", "")
    if env.strip():
        print("rpc password: from MM_RPC_PASS")
        return env.strip()
    # kdf applies its password policy to this password at boot (it is also the
    # wallet password): >=8 chars, digit + lower + upper + special, not more
    # than 2 equal characters in a row, no "password" substring — see
    # mm2src/common/password_policy.rs. Hex output fails on upper + special,
    # so sample a mixed alphabet until everything is satisfied.
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_="
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(20))
        if (any(c.isdigit() for c in pw)
                and any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(not c.isalnum() for c in pw)
                and max((len(list(g)) for _, g in itertools.groupby(pw))) < 3
                and "password" not in pw.lower()):
            print("rpc password: generated policy-compliant "
                  "(only used container-internally)")
            return pw


def wallet_passphrase(instance: str) -> str:
    base = os.environ.get("MM_TEST_SEED", "").strip()
    if base:
        print(f"{instance} wallet: passphrase derived from MM_TEST_SEED "
              f"(addresses stable across restarts)")
        return f"{base}-{instance}"
    print(f"{instance} wallet: random passphrase "
          f"(ephemeral disk -> new addresses every restart)")
    return secrets.token_hex(24)


def coins() -> list:
    with open(COINS_SRC, encoding="utf-8") as f:
        out = json.load(f)

    def append_elek(template: str, env_value: str) -> None:
        with open(template, encoding="utf-8") as f:
            eleks = json.load(f)
        for server in eleks.get("electrum", []):
            if server.get("url", "").endswith("_ELECTRUM_PLACEHOLDER"):
                server["url"] = env_value
        out.append(eleks)

    if TELEK_ELECTRS:
        append_elek(TELEK_TEMPLATE, TELEK_ELECTRS)
    if ELEK_ELECTRS:
        append_elek(ELEK_TEMPLATE, ELEK_ELECTRS)
    print(f"coins from {COINS_SRC}: {', '.join(c['coin'] for c in out)}"
          + ("" if TELEK_ELECTRS else "  (tELEK off: MM_TELEK_ELECTRS not set)")
          + ("" if ELEK_ELECTRS else "  (ELEK off: MM_ELEK_ELECTRS not set)"))
    return out


def daemon_conf(kind: str, rpcport: int, passphrase: str, passwd: str) -> dict:
    conf = {
        "gui": GUI,
        "netid": NETID,
        "passphrase": passphrase,
        "rpcip": "127.0.0.1",
        "rpcport": rpcport,
        "rpc_password": passwd,
        "event_streaming_configuration": {},
        "dbdir": os.path.join(STATE_DIR, kind, "db"),
    }
    if kind == "market":
        conf["i_am_seed"] = True
        conf["is_bootstrap_node"] = True
    else:
        conf["seednodes"] = ["127.0.0.1"]
    return conf


def main() -> None:
    passwd = rpc_pass()
    market_dir = os.path.join(STATE_DIR, "market")
    trader_dir = os.path.join(STATE_DIR, "trader")
    coins_json = os.path.join(STATE_DIR, "coins.json")

    os.makedirs(STATE_DIR, exist_ok=True)
    with open(coins_json, "w", encoding="utf-8") as f:
        json.dump(coins(), f, indent=2)

    write_private(os.path.join(market_dir, "MM2.json"),
                  daemon_conf("market", MARKET_RPC, wallet_passphrase("market"), passwd))
    write_private(os.path.join(trader_dir, "MM2.json"),
                  daemon_conf("trader", TRADER_RPC, wallet_passphrase("trader"), passwd))
    print(f"setup_env: state dir {STATE_DIR}, netid {NETID}, "
          f"market RPC 127.0.0.1:{MARKET_RPC}, trader RPC 127.0.0.1:{TRADER_RPC}")


if __name__ == "__main__":
    main()