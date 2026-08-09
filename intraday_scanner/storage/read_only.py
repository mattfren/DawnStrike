"""Fail-closed SQLite connections for observer surfaces."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from intraday_scanner.errors import StorageError


def connect_read_only(
    db_path: str | Path,
    *,
    timeout: float = 5.0,
    row_factory: type[sqlite3.Row] | None = None,
) -> sqlite3.Connection:
    """Open an existing SQLite database with URI read-only and query_only enforcement."""
    path = Path(db_path)
    if not path.is_file():
        raise StorageError(f"Read-only SQLite database does not exist or is not a file: {path}")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=timeout
        )
        connection.execute("PRAGMA query_only=ON")
        enabled = connection.execute("PRAGMA query_only").fetchone()
        if not enabled or int(enabled[0]) != 1:
            raise StorageError(f"Could not enable query_only for read-only SQLite database: {path}")
        if row_factory is not None:
            connection.row_factory = row_factory
        return connection
    except StorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise StorageError(f"Could not open read-only SQLite database {path}: {exc}") from exc
