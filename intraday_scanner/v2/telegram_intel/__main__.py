"""CLI for Dawnstrike v2 Telegram Intelligence."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.telegram_intel import (
    demo,
    draft,
    init,
    readiness,
    report,
    send,
    test_send,
    verify,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Telegram Intelligence")
    parser.add_argument(
        "command",
        choices=("init", "readiness", "draft", "send", "test-send", "verify", "report", "demo"),
    )
    parser.add_argument(
        "--kind",
        choices=("morning", "after-close", "watchdog", "no-picks", "test"),
    )
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_telegram_intel")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    if args.command == "init":
        result = init(output_root=output_root)
    elif args.command == "readiness":
        result = readiness(output_root=output_root)
    elif args.command == "draft":
        if not args.kind:
            parser.error("--kind is required for draft")
        result = draft(kind=args.kind, run_date=run_date, output_root=output_root)
    elif args.command == "send":
        if not args.kind:
            parser.error("--kind is required for send")
        result = send(kind=args.kind, run_date=run_date, output_root=output_root)
    elif args.command == "test-send":
        result = test_send(output_root=output_root)
    elif args.command == "verify":
        result = verify(output_root=output_root)
    elif args.command == "report":
        result = report(output_root=output_root)
    else:
        result = demo(output_root=output_root)

    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
