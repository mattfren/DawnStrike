"""CLI for Dawnstrike v2 OMEGA Autonomous Runner."""

from __future__ import annotations

import argparse
from pathlib import Path

from intraday_scanner.v2.autonomous_runner import (
    doctor,
    init,
    install,
    report,
    status,
    test_run,
    uninstall,
    verify,
    watchdog,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 OMEGA Autonomous Runner")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "install",
            "uninstall",
            "status",
            "verify",
            "test-run",
            "doctor",
            "report",
            "watchdog",
        ),
    )
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--output-root", default="data/v2_autonomous_runner")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    if args.command == "init":
        result = init(output_root=output_root)
        code = 0
    elif args.command == "install":
        code, result = install(yes=args.yes, output_root=output_root)
    elif args.command == "uninstall":
        code, result = uninstall(yes=args.yes, output_root=output_root)
    elif args.command == "status":
        result = status(output_root=output_root)
        code = 0
    elif args.command == "verify":
        result = verify(output_root=output_root)
        code = 0 if result.get("status") in {"passed", "install_ready"} else 1
    elif args.command == "test-run":
        code, result = test_run(output_root=output_root)
    elif args.command == "doctor":
        result = doctor(output_root=output_root)
        code = 0 if result.get("status") in {"passed", "install_ready"} else 1
    elif args.command == "report":
        result = report(output_root=output_root)
        code = 0
    else:
        result = watchdog(output_root=output_root)
        code = 0 if result.get("status") in {"passed", "passed_with_warnings"} else 1

    for key, value in result.items():
        print(f"{key}: {value}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
