"""Run one WP007 gate and persist exact command, raw output, and UTC timing."""

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
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--command-file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    evidence_root = Path(__file__).resolve().parent
    if args.command_file:
        command_text = Path(args.command_file).read_text(encoding="utf-8").strip()
        command = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command_text,
        ]
    else:
        command = list(args.command)
        if command and command[0] == "--":
            command = command[1:]
        if not command:
            parser.error("a command or --command-file is required")
        command_text = subprocess.list2cmdline(command)
    (evidence_root / f"{args.name}.command.txt").write_text(
        f"{command_text}\n",
        encoding="utf-8",
    )
    started = datetime.now(timezone.utc)
    started_clock = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=Path(args.cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    ended = datetime.now(timezone.utc)
    elapsed = time.perf_counter() - started_clock
    (evidence_root / f"{args.name}.stdout.txt").write_text(
        completed.stdout,
        encoding="utf-8",
    )
    (evidence_root / f"{args.name}.stderr.txt").write_text(
        completed.stderr,
        encoding="utf-8",
    )
    result = {
        "command": command_text,
        "cwd": str(Path(args.cwd).resolve()),
        "started_at_utc": started.isoformat(),
        "ended_at_utc": ended.isoformat(),
        "elapsed_seconds": round(elapsed, 3),
        "exit_code": completed.returncode,
        "stdout_bytes": len(completed.stdout.encode("utf-8")),
        "stderr_bytes": len(completed.stderr.encode("utf-8")),
    }
    (evidence_root / f"{args.name}.exit.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))
    sys.stdout.flush()
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
