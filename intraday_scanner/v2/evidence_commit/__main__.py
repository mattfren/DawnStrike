"""CLI for Dawnstrike v2 Evidence CommitBridge."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.evidence_commit import (
    commit,
    demo,
    init,
    propose,
    rebuild_state,
    reconcile,
    reject,
    report,
    review,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Evidence CommitBridge")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "propose",
            "review",
            "commit",
            "reject",
            "rebuild-state",
            "reconcile",
            "report",
            "verify",
            "demo",
        ),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--reason", default="")
    parser.add_argument("--output-root", default="data/v2_evidence_commit")
    parser.add_argument("--require-real-intraday", action="store_true")
    parser.add_argument("--require-provider-intraday", action="store_true")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "propose":
        result = propose(
            run_date=run_date,
            output_root=output_root,
            require_real_intraday=args.require_real_intraday,
            require_provider_intraday=args.require_provider_intraday,
        )
    elif args.command == "review":
        result = review(run_date=run_date, output_root=output_root)
    elif args.command == "commit":
        result = commit(
            run_date=run_date,
            output_root=output_root,
            require_real_intraday=args.require_real_intraday,
            require_provider_intraday=args.require_provider_intraday,
        )
    elif args.command == "reject":
        if not args.reason:
            parser.error("reject requires --reason")
        result = reject(run_date=run_date, reason=args.reason, output_root=output_root)
    elif args.command == "rebuild-state":
        result = rebuild_state(run_date=run_date, output_root=output_root)
    elif args.command == "reconcile":
        result = reconcile(run_date=run_date, output_root=output_root)
    elif args.command == "report":
        result = report(output_root=output_root)
    elif args.command == "verify":
        result = verify(output_root=output_root)
    else:
        result = demo(output_root=output_root)

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
