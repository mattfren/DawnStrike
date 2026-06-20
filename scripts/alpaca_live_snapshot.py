"""Fetch and write an Alpaca-backed canonical snapshot.

This is a market-data utility only. It does not submit broker orders.
"""

from __future__ import annotations

import argparse

from intraday_scanner.config import load_config
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.services.universe_service import parse_symbols
from intraday_scanner.snapshot_builder import write_snapshot_csv


def main() -> int:
    parser = argparse.ArgumentParser(description="Write Alpaca live snapshot CSV")
    parser.add_argument("--symbols", required=True, help="Comma-separated symbols")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    config = load_config(provider="alpaca")
    rows = AlpacaProvider(config).get_premarket_snapshot(parse_symbols(args.symbols), config)
    write_snapshot_csv(rows, args.out)
    print(f"Wrote {len(rows)} Alpaca snapshot row(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
