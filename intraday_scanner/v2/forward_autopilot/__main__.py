"""CLI for Dawnstrike v2 Forward Evidence Autopilot."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.forward_autopilot import (
    autopilot,
    build_calendar,
    dashboard,
    evaluate,
    freeze_picks,
    preflight,
    rebuild_evidence,
    run_day,
    shadow_replay,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Forward Evidence Autopilot")
    parser.add_argument(
        "command",
        choices=(
            "preflight",
            "freeze-picks",
            "run-day",
            "evaluate",
            "shadow-replay",
            "rebuild-evidence",
            "verify",
            "calendar",
            "dashboard",
            "autopilot",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--output-root", default="data/v2_forward_evidence")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    allow_fetch = not args.no_fetch
    if args.command == "preflight":
        result = preflight(run_date=run_date, output_root=output_root, allow_fetch=allow_fetch)
    elif args.command == "freeze-picks":
        result = freeze_picks(
            run_date=run_date, output_root=output_root, allow_fetch=allow_fetch
        ).to_dict()
    elif args.command == "run-day":
        result = run_day(run_date=run_date, output_root=output_root, allow_fetch=allow_fetch)
    elif args.command == "evaluate":
        result = evaluate(run_date=run_date, output_root=output_root)
    elif args.command == "shadow-replay":
        start = date.fromisoformat(args.start or args.date)
        end = date.fromisoformat(args.end or args.date)
        result = shadow_replay(
            start=start, end=end, output_root=output_root, allow_fetch=allow_fetch
        )
    elif args.command == "rebuild-evidence":
        result = rebuild_evidence(output_root=output_root)
    elif args.command == "verify":
        result = verify(output_root=output_root)
    elif args.command == "calendar":
        result = build_calendar(output_root=output_root).to_dict()
    elif args.command == "dashboard":
        result = dashboard(output_root=output_root).to_dict()
    else:
        result = autopilot(
            run_date=run_date, output_root=output_root, allow_fetch=allow_fetch
        ).to_dict()
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
