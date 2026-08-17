"""Capture active SQLite identity using an immutable read-only connection."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def file_identity(path: Path) -> dict[str, object]:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    args = parser.parse_args()
    path = Path(args.database).resolve()
    before = file_identity(path)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        schema_row = connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        schema_version = int(schema_row[0]) if schema_row is not None else 0
    finally:
        connection.close()
    after = file_identity(path)
    payload = {
        "schema_version": "dawnstrike.recovery_active_state.v1",
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
    }
    print(json.dumps(payload, sort_keys=True, indent=2))
    valid = (
        before == after
        and not before["sidecars"]
        and query_only == 1
        and quick_check == "ok"
        and schema_version == 26
    )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
