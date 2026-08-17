"""Run one gate command and persist its exact terminal evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    evidence_dir = Path(__file__).resolve().parent
    prefix = evidence_dir / args.name
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    ended_at = datetime.now(timezone.utc)
    elapsed = time.perf_counter() - started
    (prefix.with_suffix(".command.txt")).write_text(
        subprocess.list2cmdline(command) + "\n", encoding="utf-8"
    )
    (prefix.with_suffix(".stdout.txt")).write_text(
        completed.stdout, encoding="utf-8"
    )
    (prefix.with_suffix(".stderr.txt")).write_text(
        completed.stderr, encoding="utf-8"
    )
    (prefix.with_suffix(".exit.json")).write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.gate_evidence.v1",
                "started_at_utc": started_at.isoformat(),
                "ended_at_utc": ended_at.isoformat(),
                "elapsed_seconds": elapsed,
                "exit_code": completed.returncode,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
