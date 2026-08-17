"""Capture the canonical unique pytest inventory and its portable digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
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
        return completed.returncode
    nodes = tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", " "))
    )
    if not nodes or len(nodes) != len(set(nodes)):
        raise RuntimeError("canonical pytest inventory is empty or contains duplicates")
    digest = hashlib.sha256(("\n".join(nodes) + "\n").encode("utf-8")).hexdigest()
    payload = {
        "schema_version": "dawnstrike.canonical_pytest_inventory.v1",
        "hex_encoding": "colon-delimited-groups-of-8",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "node_count": len(nodes),
        "inventory_sha256": _portable_hex(digest),
        "nodes": nodes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: payload[key] for key in ("node_count", "inventory_sha256")}))
    return 0


def _portable_hex(value: str) -> str:
    return ":".join(value[index : index + 8] for index in range(0, len(value), 8))


if __name__ == "__main__":
    raise SystemExit(main())
