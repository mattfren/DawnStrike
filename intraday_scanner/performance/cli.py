"""Operator CLI for canonical performance reconciliation and publication."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.performance.snapshot import write_public_snapshot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="performance-reconcile")
    parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    parser.add_argument("--date", default=None)
    parser.add_argument("--out", default="build/public/data/performance.json")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--row-limit", type=int, default=250)
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = CanonicalPerformanceService(Path(args.db_path)).reconcile(
        market_date=args.date,
        persist=not args.no_persist,
    )
    if not args.no_persist:
        result["publication"] = write_public_snapshot(
            args.db_path,
            args.out,
            market_date=args.date,
            days=args.days,
            row_limit=args.row_limit,
        )
    print(
        json.dumps(
            result if args.as_json else _summary(result), indent=2, sort_keys=True, default=str
        )
    )
    return 0 if result["status"] not in {"DEGRADED"} else 2


def _summary(result: dict[str, object]) -> dict[str, object]:
    return {
        "status": result.get("status"),
        "row_count": result.get("row_count"),
        "daily_count": result.get("daily_count"),
        "issue_count": result.get("issue_count"),
        "input_hash_sha256": result.get("input_hash_sha256"),
        "publication": result.get("publication"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
