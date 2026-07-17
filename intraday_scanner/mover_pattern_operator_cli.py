"""CLI boundary for the scheduled, research-only mover workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from intraday_scanner.services.mover_pattern_operator_service import (
    run_mover_daily_workflow,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one configured Dawnstrike mover paper-workflow stage."
    )
    parser.add_argument("--config", required=True)
    parser.add_argument("--stage", choices=("scan", "reconcile"), required=True)
    parser.add_argument("--date")
    parser.add_argument(
        "--cutoff",
        help="Declared ET cutoff; required for scan and forbidden for reconcile.",
    )
    parser.add_argument("--notify", choices=("telegram", "console"))
    args = parser.parse_args(argv)

    result: dict[str, Any] = run_mover_daily_workflow(
        config_path=Path(args.config),
        stage=args.stage,
        market_date=args.date,
        cutoff_et=args.cutoff,
        notification_channel=args.notify,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return (
        2
        if str(result.get("status") or "").lower()
        in {"blocked", "failed", "incomplete_pending"}
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
