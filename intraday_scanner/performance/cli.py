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
    parser.add_argument("--date", "--as-of", dest="date", default=None)
    parser.add_argument("--paper-ops-root", default="data/v2_paper_ops_live")
    parser.add_argument("--out", default="build/public/data/performance.json")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--row-limit", type=int, default=250)
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--print", action="store_true", dest="print_result")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = CanonicalPerformanceService(
        Path(args.db_path),
        paper_ops_root=Path(args.paper_ops_root),
    ).reconcile(
        market_date=args.date,
        persist=args.persist,
    )
    result["paper_ops"] = _paper_ops_inventory(Path(args.paper_ops_root))
    if args.persist:
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
    return 0 if result.get("issue_count", 0) == 0 else 2


def _summary(result: dict[str, object]) -> dict[str, object]:
    raw_rows = result.get("rows")
    rows: list[object] = raw_rows if isinstance(raw_rows, list) else []
    cohort_counts: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            cohort = str(row.get("cohort") or "unknown")
            cohort_counts[cohort] = cohort_counts.get(cohort, 0) + 1
    return {
        "status": result.get("status"),
        "row_count": result.get("row_count"),
        "daily_count": result.get("daily_count"),
        "issue_count": result.get("issue_count"),
        "input_hash_sha256": result.get("input_hash_sha256"),
        "output_hash_sha256": result.get("output_hash_sha256"),
        "cohort_counts": cohort_counts,
        "discrepancies": result.get("issues") or [],
        "paper_ops": result.get("paper_ops"),
        "paper_ops_reconciliation": _paper_ops_summary(
            result.get("paper_ops_reconciliation")
        ),
        "publication": result.get("publication"),
    }


def _paper_ops_inventory(root: Path) -> dict[str, object]:
    """Report the optional PaperOps source without treating absence as zero."""

    if not root.exists():
        return {"root": str(root), "state": "missing", "files": []}
    files = sorted(
        str(path.relative_to(root)).replace("\\", "/")
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".jsonl", ".csv", ".json"}
    )
    return {"root": str(root), "state": "present", "files": files[:100], "file_count": len(files)}


def _paper_ops_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: item
        for key, item in value.items()
        if key not in {"rows", "equity", "hash_inputs", "root"}
    }


if __name__ == "__main__":
    raise SystemExit(main())
