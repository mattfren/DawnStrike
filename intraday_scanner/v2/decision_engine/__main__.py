"""CLI for the v2 Decision Engine."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.decision_engine import build_decision_engine


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Decision Engine")
    parser.add_argument("command", choices=("scan",))
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_titan")
    args = parser.parse_args(argv)

    result = build_decision_engine(
        run_date=date.fromisoformat(args.date),
        output_root=Path(args.output_root),
    )
    print(f"status: {result.status}")
    print(f"decision_cards: {result.decision_card_count}")
    print(f"watchlist: {result.watchlist_count}")
    print(f"blocked: {result.blocked_count}")
    print(f"output_root: {result.output_root.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
