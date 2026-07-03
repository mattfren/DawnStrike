"""CLI for Dawnstrike v2 OMEGA AutoData Gateway."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.autodata import (
    build,
    demo,
    feed_filltruth,
    fetch,
    fetch_pending,
    init,
    providers,
    readiness,
    reconcile,
    report,
    trial_day,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 OMEGA AutoData Gateway")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "providers",
            "readiness",
            "fetch",
            "fetch-pending",
            "build",
            "reconcile",
            "feed-filltruth",
            "trial-day",
            "verify",
            "report",
            "demo",
        ),
    )
    parser.add_argument("--symbol", default="")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--interval", default="1min")
    parser.add_argument("--provider-id", default="")
    parser.add_argument("--output-root", default="data/v2_autodata")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    provider_id = args.provider_id or None
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "providers":
        result = providers(output_root=output_root)
    elif args.command == "readiness":
        result = readiness(output_root=output_root)
    elif args.command == "fetch":
        if not args.symbol:
            parser.error("fetch requires --symbol")
        result = fetch(
            symbol=args.symbol,
            run_date=run_date,
            interval=args.interval,
            provider_id=provider_id,
            output_root=output_root,
        )
    elif args.command == "fetch-pending":
        result = fetch_pending(
            run_date=run_date,
            interval=args.interval,
            provider_id=provider_id,
            output_root=output_root,
        )
    elif args.command == "build":
        result = build(run_date=run_date, output_root=output_root)
    elif args.command == "reconcile":
        result = reconcile(run_date=run_date, output_root=output_root)
    elif args.command == "feed-filltruth":
        result = feed_filltruth(run_date=run_date, output_root=output_root)
    elif args.command == "trial-day":
        result = trial_day(run_date=run_date, commit=args.commit, output_root=output_root)
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
