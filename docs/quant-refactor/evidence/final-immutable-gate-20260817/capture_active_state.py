"""Capture immutable read-only identity and health of the active Dawnstrike database."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def identity(path: Path) -> dict[str, object]:
    stat = path.stat()
    sidecars = []
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            sidecar_stat = candidate.stat()
            sidecars.append(
                {
                    "name": candidate.name,
                    "length": sidecar_stat.st_size,
                    "mtime_ns": sidecar_stat.st_mtime_ns,
                }
            )
    return {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "length": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        "sidecars": sidecars,
    }


def capture(path: Path) -> dict[str, object]:
    before = identity(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        row = connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        schema_version = int(row[0]) if row is not None else 0
    finally:
        connection.close()
    after = identity(path)
    valid = (
        before == after
        and not before["sidecars"]
        and query_only == 1
        and quick_check == "ok"
        and schema_version == 26
    )
    return {
        "schema_version": "dawnstrike.final_gate_active_state.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "path": str(path),
        "before_read": before,
        "after_read": after,
        "identity_unchanged_during_read": before == after,
        "sqlite_uri_mode": "ro",
        "sqlite_immutable": True,
        "query_only": query_only,
        "quick_check": quick_check,
        "database_schema_version": schema_version,
        "valid": valid,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = capture(args.database.resolve())
    rendered = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if payload["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
