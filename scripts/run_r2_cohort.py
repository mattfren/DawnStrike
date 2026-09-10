"""Prepare or resume the finite ten-session R2 observer-only cohort."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.observation.cohort import (  # noqa: E402
    APPROVED_PYTHON,
    CohortError,
    prepare_cohort,
    resume_cohort,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    _common(prepare)
    prepare.add_argument("--start-date", default="2026-09-10")
    prepare.add_argument("--source-config-sha256", default="")
    prepare.add_argument("--entitlement-receipt", type=Path)
    prepare.add_argument("--runtime-env", type=Path)
    prepare.add_argument("--max-pages", type=int, default=10000)
    prepare.add_argument("--max-events", type=int, default=10000)
    prepare.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    prepare.add_argument("--max-rss-bytes", type=int, default=256 * 1024 * 1024)
    prepare.add_argument("--max-wall-seconds", type=int, default=1800)

    resume = subparsers.add_parser("resume")
    _common(resume)
    resume.add_argument(
        "--execute",
        action="store_true",
        help=(
            "bind already retained session inputs through producer/observer; "
            "never contacts a provider"
        ),
    )
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--scope-root", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", type=Path, default=APPROVED_PYTHON)


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "prepare":
            value = prepare_cohort(
                output_root=args.output_root,
                input_root=args.input_root,
                scope_root=args.scope_root,
                repo_root=args.repo_root,
                start_date=args.start_date,
                source_config_hash=args.source_config_sha256,
                entitlement_receipt=args.entitlement_receipt,
                runtime_env=args.runtime_env,
                python_path=args.python,
                max_pages=args.max_pages,
                max_events=args.max_events,
                max_bytes=args.max_bytes,
                max_rss_bytes=args.max_rss_bytes,
                max_wall_seconds=args.max_wall_seconds,
            )
        else:
            value = resume_cohort(
                output_root=args.output_root,
                input_root=args.input_root,
                repo_root=args.repo_root,
                execute=args.execute,
            )
    except CohortError as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
