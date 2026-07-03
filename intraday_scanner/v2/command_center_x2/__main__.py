"""CLI for Dawnstrike Command Center X2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_scanner.v2.command_center_x2.core import (
    build_calendar_command_center_x2,
    build_command_center_x2,
    build_days_command_center_x2,
    build_models_command_center_x2,
    demo_command_center_x2,
    inventory_command_center_x2,
    qa_command_center_x2,
    report_command_center_x2,
    verify_command_center_x2,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike Command Center X2")
    parser.add_argument(
        "command",
        choices=(
            "inventory",
            "build-models",
            "build-calendar",
            "build-days",
            "build",
            "qa",
            "verify",
            "report",
            "demo",
        ),
    )
    parser.add_argument("--output-root", default="data/v2_command_center_x2")
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    output_root = Path(args.output_root)
    if args.command == "inventory":
        payload = inventory_command_center_x2(repo_root=repo_root, output_root=output_root)
    elif args.command == "build-models":
        payload = build_models_command_center_x2(repo_root=repo_root, output_root=output_root)
    elif args.command == "build-calendar":
        payload = build_calendar_command_center_x2(repo_root=repo_root, output_root=output_root)
    elif args.command == "build-days":
        payload = build_days_command_center_x2(repo_root=repo_root, output_root=output_root)
    elif args.command == "build":
        payload = build_command_center_x2(repo_root=repo_root, output_root=output_root)
    elif args.command == "qa":
        payload = qa_command_center_x2(repo_root=repo_root, output_root=output_root)
    elif args.command == "verify":
        payload = verify_command_center_x2(repo_root=repo_root, output_root=output_root)
    elif args.command == "report":
        payload = report_command_center_x2(repo_root=repo_root, output_root=output_root)
    else:
        payload = demo_command_center_x2(repo_root=repo_root, output_root=output_root)

    print(f"status: {payload.get('status', payload.get('final_status', 'unknown'))}")
    for key in ("final_status", "build_id", "quality_score", "page_count", "qa_status"):
        if key in payload:
            print(f"{key}: {payload[key]}")
    print("json: " + json.dumps(payload, sort_keys=True, default=str)[:1200])
    status = payload.get("status", payload.get("final_status"))
    return 0 if status not in {"failed", "RESUME_REQUIRED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
