"""CLI for Dawnstrike v2 Market Masters."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.market_masters import (
    backtest,
    demo,
    evaluate,
    extract_methodologies,
    generate_challengers,
    generate_primitives,
    init,
    report,
    research,
    shadow_run,
    source_register,
    sync_learning_foundry,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Market Masters")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "research",
            "source-register",
            "extract-methodologies",
            "generate-primitives",
            "generate-challengers",
            "backtest",
            "shadow-run",
            "evaluate",
            "sync-learning-foundry",
            "report",
            "verify",
            "demo",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_market_masters")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "research":
        result = research(run_date=run_date, output_root=output_root)
    elif args.command == "source-register":
        result = source_register(output_root=output_root)
    elif args.command == "extract-methodologies":
        result = extract_methodologies(output_root=output_root)
    elif args.command == "generate-primitives":
        result = generate_primitives(output_root=output_root)
    elif args.command == "generate-challengers":
        result = generate_challengers(run_date=run_date, output_root=output_root)
    elif args.command == "backtest":
        result = backtest(run_date=run_date, output_root=output_root)
    elif args.command == "shadow-run":
        result = shadow_run(run_date=run_date, output_root=output_root)
    elif args.command == "evaluate":
        result = evaluate(run_date=run_date, output_root=output_root)
    elif args.command == "sync-learning-foundry":
        result = sync_learning_foundry(run_date=run_date, output_root=output_root)
    elif args.command == "report":
        result = report(output_root=output_root)
    elif args.command == "verify":
        result = verify(output_root=output_root)
    else:
        result = demo(output_root=output_root)

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
