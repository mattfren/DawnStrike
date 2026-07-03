"""CLI for Titan RiskHub."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.riskhub import build_risk_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 RiskHub")
    parser.add_argument("command", choices=("report",))
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_titan")
    args = parser.parse_args(argv)

    result = build_risk_report(
        run_date=date.fromisoformat(args.date),
        output_root=Path(args.output_root),
    )
    print(f"status: {result.status}")
    print(f"kill_switch: {result.kill_switch}")
    print(f"risk_warnings: {len(result.warnings)}")
    print(f"output_root: {result.output_root.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
