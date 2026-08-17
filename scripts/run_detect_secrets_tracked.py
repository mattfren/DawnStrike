"""Run the reviewed detect-secrets hook over all tracked files without argv limits."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from detect_secrets.pre_commit_hook import main as pre_commit_hook_main

ROOT = Path(__file__).resolve().parents[1]


def tracked_files(*, include_untracked: bool = False) -> tuple[str, ...]:
    """Return Git's exact NUL-delimited tracked path set in deterministic order."""

    command = ["git", "ls-files", "-z"]
    if include_untracked:
        command.extend(["--cached", "--others", "--exclude-standard"])
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return tuple(
        os.fsdecode(value)
        for value in completed.stdout.split(b"\0")
        if value
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", default=".secrets.baseline")
    parser.add_argument("--include-untracked", action="store_true")
    args = parser.parse_args(argv)
    files = tracked_files(include_untracked=args.include_untracked)
    if not files:
        parser.error("Git returned no tracked files")
    previous = Path.cwd()
    try:
        os.chdir(ROOT)
        return pre_commit_hook_main(["--baseline", args.baseline, *files])
    finally:
        os.chdir(previous)


if __name__ == "__main__":
    raise SystemExit(main())
