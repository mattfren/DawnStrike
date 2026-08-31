"""Collect once deterministically and run one complete, non-overlapping pytest shard."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def collect_nodes() -> tuple[str, ...]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    nodes = tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", " "))
    )
    if not nodes or len(nodes) != len(set(nodes)):
        raise SystemExit("pytest collection is empty or contains duplicate node IDs")
    return nodes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    if args.shard_count < 1 or not 0 <= args.shard_index < args.shard_count:
        parser.error("shard index must be within [0, shard-count)")
    nodes = collect_nodes()
    selected = tuple(
        node for ordinal, node in enumerate(nodes) if ordinal % args.shard_count == args.shard_index
    )
    payload = {
        "schema_version": "dawnstrike.pytest_shard.v1",
        "shard_index": args.shard_index,
        "shard_count": args.shard_count,
        "collected_count": len(nodes),
        "selected_count": len(selected),
        "selected_nodes": selected,
    }
    summary = {key: value for key, value in payload.items() if key != "selected_nodes"}
    print(json.dumps(summary, sort_keys=True))
    return_code = 0
    if not args.collect_only:
        return_code = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
            check=False,
        ).returncode
    if args.manifest is not None:
        # Governance tests inspect the checkout's Git cleanliness.  Emit the
        # CI upload artifact only after pytest has finished so the harness
        # cannot make its own candidate dirty while those tests execute.
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(
            json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
