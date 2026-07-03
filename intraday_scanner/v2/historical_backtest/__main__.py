"""CLI for Dawnstrike v2 six-month historical backtests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_scanner.v2.historical_backtest import (
    build_snapshot,
    compare,
    demo,
    import_data,
    init,
    report,
    run,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 historical backtest")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "import-data",
            "build-snapshot",
            "run",
            "compare",
            "report",
            "verify",
            "demo",
        ),
    )
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--asof", default="today")
    parser.add_argument("--output-root", default="data/v2_historical_backtests/six_month")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--include-champions", action="store_true")
    parser.add_argument("--include-benchmarks", action="store_true")
    parser.add_argument("--include-shadow-challengers", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    repo_root = Path(args.repo_root)
    if args.command == "init":
        payload = init(output_root=output_root, repo_root=repo_root)
    elif args.command == "import-data":
        payload = import_data(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "build-snapshot":
        payload = build_snapshot(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "run":
        include_champions = args.include_champions
        include_benchmarks = args.include_benchmarks
        include_shadow = args.include_shadow_challengers
        if not (include_champions or include_benchmarks or include_shadow):
            include_champions = True
            include_benchmarks = True
        payload = run(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
            include_champions=include_champions,
            include_benchmarks=include_benchmarks,
            include_shadow_challengers=include_shadow,
        )
    elif args.command == "compare":
        payload = compare(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "report":
        payload = report(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "verify":
        payload = verify(output_root=output_root, repo_root=repo_root)
    else:
        payload = demo(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )

    status = str(payload.get("status", payload.get("final_status", "unknown")))
    print(f"status: {status}")
    for key in (
        "final_status",
        "quality_score",
        "snapshot_id",
        "strategy_count",
        "shadow_challenger_count",
        "comparison_rows",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}")
    print("json: " + json.dumps(payload, sort_keys=True, default=str)[:1600])
    return 0 if status not in {"failed", "RESUME_REQUIRED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
