"""CLI for the static v2 Command Center."""

from __future__ import annotations

import argparse
from pathlib import Path

from intraday_scanner.v2.command_center import build_command_center


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 Command Center")
    parser.add_argument("command", choices=("build",))
    parser.add_argument("--output-root", default="data/v2_command_center")
    parser.add_argument("--titan-root", default="data/v2_titan")
    args = parser.parse_args(argv)

    result = build_command_center(
        output_root=Path(args.output_root),
        titan_root=Path(args.titan_root),
    )
    print(f"status: {result.status}")
    print(f"pages: {len(result.pages)}")
    print(f"index: {result.index_path.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
