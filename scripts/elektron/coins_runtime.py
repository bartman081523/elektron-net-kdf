#!/usr/bin/env python3
"""coins_runtime.py -- build a runtime coins file for a node setup.

Starts from the committed two-layer model (upstream snapshot + elektron
overlay, see coins/README.md) and injects per-node settings that must
stay out of the committed data:

  --confpath TICKER=PATH   sets conf["confpath"] (native UTXO mode); the
                           path points at the node daemon's conf file
                           and carries the RPC credentials.
  --rpcport TICKER=PORT    overrides conf["rpcport"].

Everything already merged (upstream + overlay) is passed through
unmodified; overlay still wins per ticker. Output is deterministic.

Usage:
  coins_runtime.py --upstream coins/upstream_coins \
      --overlay coins/elektron_overlay.json \
      --output "$RL_ROOT/coins-rt.json" \
      --confpath rELEK="$RL_ROOT/rl1/bitcoin.conf" \
      --confpath rBTC="$RL_ROOT/rb1/bitcoin.conf"
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coins_merge import load_entries, merge  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a runtime coins file for kdf")
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confpath", action="append", default=[],
                        metavar="TICKER=PATH",
                        help="native-mode conf path for a ticker (repeatable)")
    parser.add_argument("--rpcport", action="append", default=[],
                        metavar="TICKER=PORT",
                        help="rpcport override for a ticker (repeatable)")
    args = parser.parse_args()

    def split_set(specs):
        out = {}
        for spec in specs:
            if "=" not in spec:
                print(f"error: expected KEY=VALUE, got '{spec}'", file=sys.stderr)
                raise SystemExit(1)
            key, _, value = spec.partition("=")
            out[key.strip()] = value.strip()
        return out

    confpaths = split_set(args.confpath)
    rpcports = split_set(args.rpcport)

    upstream = load_entries(args.upstream, "upstream coins file")
    overlay = load_entries(args.overlay, "overlay")
    merged = merge(upstream, overlay)

    by_ticker = {entry["coin"]: entry for entry in merged}
    for ticker, path in confpaths.items():
        if ticker not in by_ticker:
            print(f"error: --confpath ticker not present in merged coins: {ticker}",
                  file=sys.stderr)
            raise SystemExit(1)
        by_ticker[ticker]["confpath"] = path
    for ticker, port in rpcports.items():
        if ticker not in by_ticker:
            print(f"error: --rpcport ticker not present in merged coins: {ticker}",
                  file=sys.stderr)
            raise SystemExit(1)
        by_ticker[ticker]["rpcport"] = int(port)

    out_text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(out_text, encoding="utf-8")
    print(f"wrote {len(merged)} coins to {args.output} "
          f"({len(confpaths)} confpath, {len(rpcports)} rpcport overrides)")


if __name__ == "__main__":
    main()