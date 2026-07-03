"""CLI for Dawnstrike v2 OMEGA."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.omega import build_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 OMEGA Autopilot")
    parser.add_argument("command", choices=("build-all",))
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_omega")
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args(argv)

    result = build_all(
        run_date=date.fromisoformat(args.date),
        output_root=Path(args.output_root),
        allow_fetch=not args.no_fetch,
    )
    print(f"status: {result.status}")
    print(f"score: {result.quality_score}/100")
    print(f"target: {result.quality_target}/100")
    print(f"build_id: {result.build_id}")
    print(f"frozen_pick_hash: {result.frozen_pick_hash}")
    print(f"dashboard: {result.dashboard_index.as_posix()}")
    if result.blockers:
        print("blockers:")
        for blocker in result.blockers:
            print(f"- {blocker}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
