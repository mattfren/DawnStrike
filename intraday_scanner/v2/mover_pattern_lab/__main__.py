"""Command-line interface for the Dawnstrike Mover Pattern Lab."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_scanner.mover_pattern_audit import audit_retained_data

from .candidate_runtime import run_candidate_study
from .core import (
    DEFAULT_CUTOFFS,
    DEFAULT_OUTPUT_ROOT,
    analyze,
    build_snapshots_from_bars,
    init,
    paper_scan,
    reconcile_paper_signals,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Point-in-time mover research and same-session simulated paper evidence"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("init", "verify"):
        child = subparsers.add_parser(command)
        _add_output_root(child)

    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    _add_output_root(audit_parser)

    build_parser = subparsers.add_parser("build-snapshots")
    build_parser.add_argument("--bars-csv", required=True)
    build_parser.add_argument("--context-csv")
    build_parser.add_argument("--date", required=True)
    build_parser.add_argument(
        "--cutoffs",
        default=",".join(DEFAULT_CUTOFFS),
        help="Comma-separated America/New_York cutoff clocks.",
    )
    build_parser.add_argument("--min-baseline-sessions", type=int, default=10)
    build_parser.add_argument("--bar-interval-minutes", type=int, default=5)
    build_parser.add_argument(
        "--bar-timestamp-semantics",
        choices=("bar_close",),
        required=True,
    )
    build_parser.add_argument(
        "--evidence-mode",
        choices=("forward_observation", "historical_replay"),
        default="historical_replay",
    )
    build_parser.add_argument("--source-captured-at")
    _add_output_root(build_parser)

    scan_parser = subparsers.add_parser("paper-scan")
    scan_parser.add_argument("--snapshots", required=True)
    scan_parser.add_argument(
        "--expected-market-dates",
        required=True,
        help="Comma-separated published market dates this run was expected to evaluate.",
    )
    _add_output_root(scan_parser)

    reconcile_parser = subparsers.add_parser("reconcile")
    reconcile_parser.add_argument("--signals", required=True)
    reconcile_parser.add_argument("--bars-csv", required=True)
    reconcile_parser.add_argument("--notional-per-trade", type=float, default=1_000.0)
    reconcile_parser.add_argument("--slippage-bps", type=float, default=10.0)
    reconcile_parser.add_argument("--fee-bps", type=float, default=1.0)
    reconcile_parser.add_argument("--bar-interval-minutes", type=int, default=5)
    reconcile_parser.add_argument(
        "--bar-timestamp-semantics",
        choices=("bar_close",),
        required=True,
    )
    _add_output_root(reconcile_parser)

    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--scan-manifest", required=True)
    analyze_parser.add_argument("--reconcile-manifest", required=True)
    _add_output_root(analyze_parser)

    candidate_parser = subparsers.add_parser("study-candidates")
    candidate_parser.add_argument("--snapshots", required=True)
    candidate_parser.add_argument("--bars-csv", required=True)
    candidate_parser.add_argument("--universe-denominators", required=True)
    candidate_parser.add_argument("--split-assignments", required=True)
    candidate_parser.add_argument("--descriptive-eod-movers")
    candidate_parser.add_argument("--bar-interval-minutes", type=int, default=5)
    candidate_parser.add_argument("--slippage-bps", type=float, default=10.0)
    candidate_parser.add_argument("--fee-bps", type=float, default=1.0)
    candidate_parser.add_argument(
        "--bar-timestamp-semantics",
        choices=("bar_close",),
        required=True,
    )
    _add_output_root(candidate_parser)

    args = parser.parse_args(argv)
    output_root = Path(getattr(args, "output_root", DEFAULT_OUTPUT_ROOT))
    result: dict[str, Any]
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "audit":
        result = audit_retained_data(
            db_path=Path(args.db_path),
            output_root=output_root,
        )
    elif args.command == "build-snapshots":
        result = build_snapshots_from_bars(
            bars_csv=Path(args.bars_csv),
            context_csv=Path(args.context_csv) if args.context_csv else None,
            market_date=args.date,
            cutoffs=tuple(
                value.strip()
                for value in str(args.cutoffs).split(",")
                if value.strip()
            ),
            min_baseline_sessions=args.min_baseline_sessions,
            bar_interval_minutes=args.bar_interval_minutes,
            bar_timestamp_semantics=args.bar_timestamp_semantics,
            evidence_mode=args.evidence_mode,
            source_captured_at=(
                _parse_aware_datetime(args.source_captured_at)
                if args.source_captured_at
                else None
            ),
            output_root=output_root,
        )
    elif args.command == "paper-scan":
        result = paper_scan(
            snapshots_path=Path(args.snapshots),
            expected_market_dates=_comma_separated(args.expected_market_dates),
            output_root=output_root,
        )
    elif args.command == "reconcile":
        result = reconcile_paper_signals(
            signals_path=Path(args.signals),
            bars_csv=Path(args.bars_csv),
            notional_per_trade=args.notional_per_trade,
            slippage_bps=args.slippage_bps,
            fee_bps=args.fee_bps,
            bar_interval_minutes=args.bar_interval_minutes,
            bar_timestamp_semantics=args.bar_timestamp_semantics,
            output_root=output_root,
        )
    elif args.command == "analyze":
        result = analyze(
            scan_manifest_path=Path(args.scan_manifest),
            reconcile_manifest_path=Path(args.reconcile_manifest),
            output_root=output_root,
        )
    elif args.command == "study-candidates":
        result = run_candidate_study(
            snapshots_path=Path(args.snapshots),
            bars_csv=Path(args.bars_csv),
            universe_denominators_path=Path(args.universe_denominators),
            split_assignments_path=Path(args.split_assignments),
            descriptive_eod_movers_path=(
                Path(args.descriptive_eod_movers)
                if args.descriptive_eod_movers
                else None
            ),
            bar_interval_minutes=args.bar_interval_minutes,
            slippage_bps=args.slippage_bps,
            fee_bps=args.fee_bps,
            bar_timestamp_semantics=args.bar_timestamp_semantics,
            output_root=output_root,
        )
    else:
        result = verify(output_root=output_root)

    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return (
        2
        if str(result.get("status") or "").lower()
        in {"blocked", "failed", "incomplete_pending"}
        else 0
    )


def _add_output_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))


def _parse_aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


def _comma_separated(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("at least one value is required")
    return values


if __name__ == "__main__":
    raise SystemExit(main())
