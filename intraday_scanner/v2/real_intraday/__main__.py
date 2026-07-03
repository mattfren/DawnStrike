"""CLI for Dawnstrike v2 OMEGA Real Intraday Intake."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.real_intraday import (
    aggregate_daily,
    build,
    demo,
    import_intraday,
    init,
    inspect_imports,
    readiness,
    reconcile_daily,
    report,
    template,
    trial_day,
    validate,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 OMEGA Real Intraday Intake")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "inspect-imports",
            "template",
            "import",
            "validate",
            "aggregate-daily",
            "reconcile-daily",
            "build",
            "readiness",
            "verify",
            "demo",
            "trial-day",
            "report",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--path")
    parser.add_argument("--source-label", default="real_local_intraday")
    parser.add_argument("--source-name", default="")
    parser.add_argument("--source-timezone", default="UTC")
    parser.add_argument("--market-timezone", default="America/New_York")
    parser.add_argument("--output-root", default="data/v2_real_intraday")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "inspect-imports":
        result = inspect_imports(output_root=output_root)
    elif args.command == "template":
        result = template(output_root=output_root)
    elif args.command == "import":
        if not args.path:
            parser.error("import requires --path")
        result = import_intraday(
            path=Path(args.path),
            source_label=args.source_label,
            source_name=args.source_name,
            source_timezone=args.source_timezone,
            market_timezone=args.market_timezone,
            output_root=output_root,
        )
    elif args.command == "validate":
        result = validate(run_date=run_date, output_root=output_root)
    elif args.command == "aggregate-daily":
        result = aggregate_daily(run_date=run_date, output_root=output_root)
    elif args.command == "reconcile-daily":
        result = reconcile_daily(run_date=run_date, output_root=output_root)
    elif args.command == "build":
        result = build(run_date=run_date, output_root=output_root)
    elif args.command == "readiness":
        result = readiness(run_date=run_date, output_root=output_root)
    elif args.command == "verify":
        result = verify(output_root=output_root)
    elif args.command == "demo":
        result = demo(output_root=output_root)
    elif args.command == "trial-day":
        result = trial_day(run_date=run_date, commit=args.commit, output_root=output_root)
    else:
        result = report(output_root=output_root)

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
