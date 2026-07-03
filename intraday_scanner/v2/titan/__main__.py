"""CLI for Dawnstrike Titan Buildroom."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.forward_autopilot import autopilot
from intraday_scanner.v2.titan import build_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Titan Buildroom")
    parser.add_argument("command", choices=("build-all", "daily"))
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_titan")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)

    run_date = date.fromisoformat(args.date)
    allow_fetch = not args.no_fetch
    if args.command == "daily":
        daily_result = autopilot(run_date=run_date, allow_fetch=allow_fetch)
        print(f"status: {daily_result.status}")
        print(f"score: {daily_result.quality_score}/100")
        print(f"target: {daily_result.quality_target}/100")
        print(f"run_id: {daily_result.run_id}")
        print(f"frozen_pick_hash: {daily_result.frozen_pick_hash}")
        print(f"command_center: {daily_result.dashboard_index.as_posix()}")
        return 0

    build_result = build_all(
        run_date=run_date,
        output_root=Path(args.output_root),
        allow_fetch=allow_fetch,
    )
    print(f"status: {build_result.status}")
    print(f"score: {build_result.quality_score}/100")
    print(f"target: {build_result.quality_target}/100")
    print(f"run_id: {build_result.run_id}")
    print(f"command_center: {build_result.command_center_index.as_posix()}")
    if build_result.blockers:
        print("blockers:")
        for blocker in build_result.blockers:
            print(f"- {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
