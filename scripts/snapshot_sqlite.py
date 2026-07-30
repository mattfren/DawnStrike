"""Create an atomic, read-only SQLite snapshot for the publication pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


def snapshot_database(source: str | Path, target: str | Path) -> dict[str, object]:
    source_path = Path(source).resolve()
    target_path = Path(target).resolve()
    if source_path == target_path:
        raise ValueError("source and target databases must be different")
    if not source_path.is_file():
        raise FileNotFoundError(f"source database not found: {source_path}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = target_path.with_name(
        f".{target_path.name}.{os.getpid()}.snapshot"
    )
    temporary_path.unlink(missing_ok=True)
    source_uri = f"file:{quote(source_path.as_posix(), safe='/:')}?mode=ro"
    try:
        with closing(
            sqlite3.connect(source_uri, uri=True, timeout=30)
        ) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            with closing(
                sqlite3.connect(temporary_path, timeout=30)
            ) as target_connection:
                source_connection.backup(target_connection)
                quick_check = str(
                    target_connection.execute("PRAGMA quick_check").fetchone()[0]
                )
                if quick_check != "ok":
                    raise sqlite3.DatabaseError(
                        f"snapshot quick_check failed: {quick_check}"
                    )
                target_connection.commit()
        os.replace(temporary_path, target_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    payload = target_path.read_bytes()
    return {
        "status": "PASS",
        "source_path": str(source_path),
        "target_path": str(target_path),
        "target_bytes": len(payload),
        "target_sha256": hashlib.sha256(payload).hexdigest(),
        "source_mode": "read_only_sqlite_backup",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--target-db", required=True)
    args = parser.parse_args(argv)
    try:
        result = snapshot_database(args.source_db, args.target_db)
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {
            "status": "FAILED",
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(result, sort_keys=True, indent=2))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
