"""CLI for Dawnstrike Interface Apex."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from intraday_scanner.v2.interface_apex.core import (
    build_apex_calendar,
    build_apex_days,
    build_apex_models,
    build_interface_apex,
    demo_interface_apex,
    qa_interface_apex,
    report_interface_apex,
    serve_interface_apex,
    verify_interface_apex,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike Interface Apex")
    parser.add_argument(
        "command",
        choices=(
            "build-models",
            "build-calendar",
            "build-days",
            "build",
            "qa",
            "verify",
            "report",
            "demo",
            "serve",
        ),
    )
    parser.add_argument("--output-root", default="data/v2_interface_apex")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    output_root = Path(args.output_root)
    if args.command == "build-models":
        payload = build_apex_models(repo_root=repo_root, output_root=output_root)
    elif args.command == "build-calendar":
        payload = build_apex_calendar(repo_root=repo_root, output_root=output_root)
    elif args.command == "build-days":
        payload = build_apex_days(repo_root=repo_root, output_root=output_root)
    elif args.command == "build":
        payload = build_interface_apex(repo_root=repo_root, output_root=output_root)
    elif args.command == "qa":
        payload = qa_interface_apex(repo_root=repo_root, output_root=output_root)
    elif args.command == "verify":
        payload = verify_interface_apex(repo_root=repo_root, output_root=output_root)
    elif args.command == "report":
        payload = report_interface_apex(repo_root=repo_root, output_root=output_root)
    elif args.command == "demo":
        payload = demo_interface_apex(repo_root=repo_root, output_root=output_root)
    else:
        payload = serve_interface_apex(output_root=output_root, host=args.host, port=args.port)

    print(f"status: {payload.get('status', payload.get('final_status', 'unknown'))}")
    for key in (
        "final_status",
        "build_id",
        "quality_score",
        "page_count",
        "top_level_nav_count",
        "qa_status",
        "verify_status",
        "browser_verification_status",
    ):
        if key in payload:
            print(f"{key}: {payload[key]}")
    print("json: " + json.dumps(payload, sort_keys=True, default=str)[:1600])
    status = payload.get("status")
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
