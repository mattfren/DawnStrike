"""CLI for Dawnstrike v2 OMEGA Sentinel."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.omega_sentinel import (
    after_close,
    clear_stale_locks,
    commit_filltruth,
    doctor,
    generate_scheduler_scripts,
    init,
    lock_status,
    morning_check,
    omega,
    report,
    resolve_pending,
    run,
    run_today,
    status,
    trial_status,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 OMEGA Sentinel")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "run",
            "run-today",
            "status",
            "verify",
            "report",
            "trial-status",
            "scheduler-scripts",
            "doctor",
            "omega",
            "morning-check",
            "after-close",
            "resolve-pending",
            "commit-filltruth",
            "lock-status",
            "clear-stale-locks",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_omega_sentinel")
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--use-real-intraday", action="store_true")
    parser.add_argument("--autodata", action="store_true")
    parser.add_argument("--learn", action="store_true")
    parser.add_argument("--telegram", action="store_true")
    parser.add_argument("--market-masters", action="store_true")
    parser.add_argument("--stale-after-minutes", type=int, default=240)
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "run":
        result = run(
            run_date=run_date,
            output_root=output_root,
            allow_fetch=args.fetch,
            stale_after_minutes=args.stale_after_minutes,
        ).to_dict()
    elif args.command == "run-today":
        result = run_today(
            output_root=output_root,
            allow_fetch=args.fetch,
            stale_after_minutes=args.stale_after_minutes,
        ).to_dict()
    elif args.command == "status":
        result = status(output_root=output_root)
    elif args.command == "verify":
        result = verify(output_root=output_root)
    elif args.command == "report":
        result = report(output_root=output_root)
    elif args.command == "trial-status":
        result = trial_status(output_root=output_root)
    elif args.command == "scheduler-scripts":
        result = generate_scheduler_scripts(output_root=output_root)
    elif args.command == "doctor":
        result = doctor(output_root=output_root)
    elif args.command == "omega":
        result = omega(run_date=run_date, output_root=output_root, allow_fetch=args.fetch)
    elif args.command == "morning-check":
        result = morning_check(
            run_date=run_date,
            output_root=output_root,
            use_real_intraday=args.use_real_intraday,
            autodata=args.autodata,
            learn=args.learn,
            telegram=args.telegram,
            market_masters=args.market_masters,
        )
    elif args.command == "after-close":
        result = after_close(
            run_date=run_date,
            output_root=output_root,
            use_real_intraday=args.use_real_intraday,
            autodata=args.autodata,
            learn=args.learn,
            telegram=args.telegram,
            market_masters=args.market_masters,
        )
    elif args.command == "resolve-pending":
        result = resolve_pending(run_date=run_date, output_root=output_root, autodata=args.autodata)
    elif args.command == "commit-filltruth":
        result = commit_filltruth(run_date=run_date, output_root=output_root)
    elif args.command == "lock-status":
        result = lock_status(output_root=output_root)
    else:
        result = clear_stale_locks(output_root=output_root)

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
