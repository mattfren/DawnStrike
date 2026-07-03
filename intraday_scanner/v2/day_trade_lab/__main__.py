"""CLI for Dawnstrike OMEGA Day Trade Lab."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_scanner.v2.day_trade_lab import (
    build_corpus,
    build_sessions,
    compare,
    compare_corpus,
    corpus_plan,
    corpus_report,
    demo,
    evaluate_refinements,
    fetch_corpus,
    generate_refinements,
    import_data,
    init,
    report,
    robustness,
    robustness_report,
    run,
    run_corpus,
    split_test,
    stress_slippage,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike OMEGA Day Trade Lab")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "import-data",
            "build-sessions",
            "run",
            "compare",
            "report",
            "corpus-plan",
            "fetch-corpus",
            "build-corpus",
            "run-corpus",
            "compare-corpus",
            "corpus-report",
            "robustness",
            "stress-slippage",
            "split-test",
            "generate-refinements",
            "evaluate-refinements",
            "robustness-report",
            "verify",
            "demo",
        ),
    )
    parser.add_argument("--months", type=int, default=6)
    parser.add_argument("--interval", default="1min", choices=("1min", "5min", "15min"))
    parser.add_argument("--intervals", nargs="*", default=["1min,5min"])
    parser.add_argument("--asof", default="today")
    parser.add_argument("--output-root", default="data/v2_day_trade_lab")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    repo_root = Path(args.repo_root)
    if args.command == "init":
        payload = init(output_root=output_root, repo_root=repo_root)
    elif args.command == "import-data":
        payload = import_data(
            months=args.months,
            interval=args.interval,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "build-sessions":
        payload = build_sessions(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "run":
        payload = run(
            months=args.months,
            interval=args.interval,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
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
    elif args.command == "corpus-plan":
        payload = corpus_plan(
            months=args.months,
            intervals=args.intervals,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "fetch-corpus":
        payload = fetch_corpus(
            months=args.months,
            interval=args.interval,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "build-corpus":
        payload = build_corpus(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "run-corpus":
        payload = run_corpus(
            months=args.months,
            interval=args.interval,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "compare-corpus":
        payload = compare_corpus(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "corpus-report":
        payload = corpus_report(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "robustness":
        payload = robustness(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "stress-slippage":
        payload = stress_slippage(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "split-test":
        payload = split_test(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "generate-refinements":
        payload = generate_refinements(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "evaluate-refinements":
        payload = evaluate_refinements(
            months=args.months,
            asof=args.asof,
            output_root=output_root,
            repo_root=repo_root,
        )
    elif args.command == "robustness-report":
        payload = robustness_report(
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
        "interval",
        "session_count",
        "strategy_count",
        "trade_count",
        "comparison_rows",
        "candidate_count",
        "holdout_beats_parent_count",
        "fragility_count",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}")
    print("json: " + json.dumps(payload, sort_keys=True, default=str)[:1600])
    return 0 if status not in {"failed", "RESUME_REQUIRED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
