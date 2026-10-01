#!/usr/bin/env python3
"""coins_merge.py -- merge the upstream coins file with the elektron overlay.

Two-layer coin model in the elektron-net-kdf fork:
  1. coins/upstream_coins         unmodified copy of KomodoPlatform/coins (data pull)
  2. coins/elektron_overlay.json  elektron entries (ELEK, tELEK, rELEK, rBTC)
  3. coins/elektron_coins         output of this script -> MM_COINS_PATH

Overlay entries win on ticker collision (exact replacement, not a
field-wise merge). The output is deterministic: sorted by the coin
field, 2-space indent, no trailing-newline drift.

Usage:
  coins_merge.py --upstream coins/upstream_coins --overlay coins/elektron_overlay.json \
                 --output coins/elektron_coins
"""

import argparse
import json
import sys
from pathlib import Path


def load_entries(path: Path, label: str) -> list:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: {label} not readable: {exc}", file=sys.stderr)
        raise SystemExit(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"error: {label} is not valid JSON: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if not isinstance(data, list):
        print(f"error: {label} must contain a JSON array", file=sys.stderr)
        raise SystemExit(1)
    for entry in data:
        if not isinstance(entry, dict) or "coin" not in entry:
            print(f"error: {label} has an entry without 'coin' field", file=sys.stderr)
            raise SystemExit(1)
    return data


def validate_overlay(overlay: list) -> None:
    """Overlay entries must carry the minimum fields mm2 needs."""
    required = ("coin", "protocol", "mm2")
    for entry in overlay:
        coin = entry["coin"]
        for field in required:
            if field not in entry:
                print(f"error: overlay entry '{coin}' lacks required field '{field}'", file=sys.stderr)
                raise SystemExit(1)
        if entry.get("protocol", {}).get("type") not in ("UTXO", "ETH", "ERC20", "SLP", "QTUM", "ZCASH", "ARRR", "SIA"):
            print(f"error: overlay entry '{coin}' has unsupported protocol.type", file=sys.stderr)
            raise SystemExit(1)
        # segwit config: a missing bech32_hrp is a classic that only blows up at
        # activation time. Catch it here.
        if entry.get("segwit"):
            if "bech32_hrp" not in entry:
                print(f"error: segwit overlay entry '{coin}' lacks bech32_hrp", file=sys.stderr)
                raise SystemExit(1)
            fmt = entry.get("address_format", {}).get("format")
            if fmt not in ("segwit", "bech32", None):
                print(f"error: segwit overlay entry '{coin}' has unexpected address_format '{fmt}'", file=sys.stderr)
                raise SystemExit(1)


def merge(upstream: list, overlay: list) -> list:
    merged: dict = {}
    for entry in upstream:
        merged[entry["coin"]] = entry
    for entry in overlay:
        merged[entry["coin"]] = entry  # Overlay gewinnt pro Ticker
    return [merged[ticker] for ticker in sorted(merged)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge upstream coins file with elektron overlay")
    parser.add_argument("--upstream", type=Path, required=True, help="Path to upstream coins file (JSON array)")
    parser.add_argument("--overlay", type=Path, required=True, help="Path to elektron_overlay.json")
    parser.add_argument("--output", type=Path, required=True, help="Path for merged output file")
    args = parser.parse_args()

    upstream = load_entries(args.upstream, "upstream coins file")
    overlay = load_entries(args.overlay, "overlay")

    overlay_tickers = {entry["coin"] for entry in overlay}
    validate_overlay(overlay)

    # clash report: which tickers get replaced (should be rare for us)
    upstream_tickers = {entry["coin"] for entry in upstream}
    clashes = sorted(overlay_tickers & upstream_tickers)
    if clashes:
        print(f"note: overlay replaces upstream entries for: {', '.join(clashes)}")

    merged = merge(upstream, overlay)
    out_text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"

    if args.output.exists() and args.output.read_text(encoding="utf-8") == out_text:
        print(f"unchanged: {args.output}")
        return
    args.output.write_text(out_text, encoding="utf-8")
    print(f"wrote {len(merged)} coins to {args.output} ({len(overlay)} overlay entries applied)")


if __name__ == "__main__":
    main()