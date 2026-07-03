"""CLI for Dawnstrike v2 Autonomous Learning Foundry."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.learning_foundry import (
    backtest_candidates,
    build_features,
    build_labels,
    build_regimes,
    daily_learn,
    demo,
    evaluate,
    generate_candidates,
    ingest_news,
    init,
    promote_review,
    report,
    shadow_run,
    train,
    verify,
    write_lesson,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Autonomous Learning Foundry")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "features",
            "labels",
            "regimes",
            "news",
            "train",
            "generate-candidates",
            "backtest-candidates",
            "shadow-run",
            "evaluate",
            "promote-review",
            "lesson",
            "daily-learn",
            "verify",
            "report",
            "demo",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_learning_foundry")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    as_of = date.fromisoformat(args.asof)

    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "features":
        result = build_features(run_date=run_date, output_root=output_root)
    elif args.command == "labels":
        result = build_labels(run_date=run_date, output_root=output_root)
    elif args.command == "regimes":
        result = build_regimes(run_date=run_date, output_root=output_root)
    elif args.command == "news":
        result = ingest_news(run_date=run_date, output_root=output_root)
    elif args.command == "train":
        result = train(as_of=as_of, output_root=output_root)
    elif args.command == "generate-candidates":
        result = generate_candidates(run_date=run_date, output_root=output_root)
    elif args.command == "backtest-candidates":
        result = backtest_candidates(run_date=run_date, output_root=output_root)
    elif args.command == "shadow-run":
        result = shadow_run(run_date=run_date, output_root=output_root)
    elif args.command == "evaluate":
        result = evaluate(run_date=run_date, output_root=output_root)
    elif args.command == "promote-review":
        result = promote_review(run_date=run_date, output_root=output_root)
    elif args.command == "lesson":
        result = write_lesson(run_date=run_date, output_root=output_root)
    elif args.command == "daily-learn":
        result = daily_learn(run_date=run_date, output_root=output_root)
    elif args.command == "verify":
        result = verify(output_root=output_root)
    elif args.command == "report":
        result = report(output_root=output_root)
    else:
        result = demo(output_root=output_root)

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
