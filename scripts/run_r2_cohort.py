"""Prepare or resume the finite ten-session R2 observer-only cohort."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.observation.cohort import (  # noqa: E402
    APPROVED_PYTHON,
    PRODUCER_REDUCTION_MODES,
    CohortError,
    prepare_cohort,
    resume_cohort,
)
from intraday_scanner.observation.ops09 import (  # noqa: E402
    Ops09Error,
    prepare_ops09_cohort,
    resume_ops09_cohort,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    _common(prepare)
    prepare.add_argument("--start-date", default="2026-09-10")
    prepare.add_argument("--mode", choices=("r2", "ops09"), default="r2")
    prepare.add_argument("--source-config-sha256", default="")
    prepare.add_argument("--entitlement-receipt", type=Path)
    prepare.add_argument("--runtime-env", type=Path)
    prepare.add_argument("--max-pages", type=int, default=10000)
    prepare.add_argument("--max-events", type=int, default=10000)
    prepare.add_argument("--max-bytes", type=int, default=64 * 1024 * 1024)
    prepare.add_argument("--max-rss-bytes", type=int, default=256 * 1024 * 1024)
    prepare.add_argument("--max-wall-seconds", type=int, default=1800)
    prepare.add_argument(
        "--reduction-mode",
        choices=PRODUCER_REDUCTION_MODES,
        default="bounded_derivative",
    )

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
    resume.add_argument("--mode", choices=("r2", "ops09"), default="r2")
    resume.add_argument("--database-root", type=Path)
    resume.add_argument("--fixture-root", type=Path)
    resume.add_argument("--decision-root", type=Path)
    resume.add_argument("--now", help="UTC ISO timestamp used for bounded offline verification")
    prepare.add_argument("--database-root", type=Path)
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
            if args.mode == "ops09":
                if args.database_root is None:
                    raise Ops09Error("OPS09 prepare requires --database-root")
                value = prepare_ops09_cohort(
                    output_root=args.output_root, input_root=args.input_root,
                    scope_root=args.scope_root, database_root=args.database_root,
                    repo_root=args.repo_root, start_date=args.start_date,
                    source_config_hash=args.source_config_sha256,
                    entitlement_receipt=args.entitlement_receipt,
                    runtime_env=args.runtime_env, python_path=args.python,
                )
            else:
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
                    reduction_mode=args.reduction_mode,
                )
        else:
            if args.mode == "ops09":
                if args.database_root is None:
                    raise Ops09Error("OPS09 resume requires --database-root")
                value = resume_ops09_cohort(
                    output_root=args.output_root, input_root=args.input_root,
                    scope_root=args.scope_root, database_root=args.database_root,
                    repo_root=args.repo_root, execute=args.execute,
                    now=(__import__("datetime").datetime.fromisoformat(args.now.replace("Z", "+00:00"))
                         if args.now else None),
                    fixture_root=args.fixture_root, decision_root=args.decision_root,
                )
            else:
                value = resume_cohort(
                    output_root=args.output_root, input_root=args.input_root,
                    repo_root=args.repo_root, execute=args.execute,
                )
    except (CohortError, Ops09Error) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(value, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
