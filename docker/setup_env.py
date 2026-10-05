#!/usr/bin/env python3
"""Runtime setup for the elektron-net marketplace container.

Generates (in MM_STATE_DIR, default /run/elek):
  coins.json       tBTC (+ tELEK when MM_TELEK_ELECTRS is set)
  market/MM2.json  market-maker daemon config (i_am_seed)
  trader/MM2.json  visitor-facing daemon config (seednodes -> 127.0.0.1)

Secrets (rpc password, wallet passphrases) are written ONLY to these files
(0600); stdout never receives them — only their provenance.
"""

import json
import os
import secrets
import stat

STATE_DIR = os.environ.get("MM_STATE_DIR", "/run/elek")
COINS_SRC = os.environ.get(
    "MM_COINS_SRC", "/app/docker/coins-testnet.json")
TELEK_TEMPLATE = os.environ.get(
    "MM_TELEK_TEMPLATE", "/app/docker/telek-template.json")
TELEK_ELECTRS = os.environ.get("MM_TELEK_ELECTRS", "").strip()

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
    generated = secrets.token_hex(16)
    print("rpc password: generated (only used container-internally)")
    return generated


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
    if TELEK_ELECTRS:
        with open(TELEK_TEMPLATE, encoding="utf-8") as f:
            telek = json.load(f)
        for server in telek.get("electrum", []):
            if server.get("url") == "TELEK_ELECTRUM_PLACEHOLDER":
                server["url"] = TELEK_ELECTRS
        out.append(telek)
    print(f"coins: {', '.join(c['coin'] for c in out)}"
          + ("" if TELEK_ELECTRS else "  (tELEK off: MM_TELEK_ELECTRS not set)"))
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