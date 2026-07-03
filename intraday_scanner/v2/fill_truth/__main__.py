"""CLI for Dawnstrike v2 OMEGA FillTruth."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.fill_truth import (
    build,
    compare_models,
    demo,
    evaluate,
    import_intraday,
    init,
    report,
    resolve_pending,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 OMEGA FillTruth")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "import-intraday",
            "build",
            "resolve-pending",
            "evaluate",
            "compare-models",
            "report",
            "verify",
            "demo",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--path")
    parser.add_argument("--source-label")
    parser.add_argument("--output-root", default="data/v2_fill_truth")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "import-intraday":
        if not args.path:
            parser.error("import-intraday requires --path")
        result = import_intraday(
            path=Path(args.path),
            output_root=output_root,
            source_label=args.source_label,
        )
    elif args.command == "build":
        result = build(run_date=run_date, output_root=output_root)
    elif args.command == "resolve-pending":
        result = resolve_pending(run_date=run_date, output_root=output_root)
    elif args.command == "evaluate":
        result = evaluate(run_date=run_date, output_root=output_root)
    elif args.command == "compare-models":
        start = date.fromisoformat(args.start or args.date)
        end = date.fromisoformat(args.end or args.date)
        result = compare_models(start=start, end=end, output_root=output_root)
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
