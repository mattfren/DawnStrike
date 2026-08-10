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
    resolved = path.resolve()
    sidecars = tuple(
        candidate
        for candidate in (
            Path(f"{resolved}-wal"),
            Path(f"{resolved}-shm"),
            Path(f"{resolved}-journal"),
        )
        if candidate.exists()
    )
    if sidecars:
        names = ", ".join(candidate.name for candidate in sidecars)
        raise StorageError(
            "Read-only SQLite observer blocked because active SQLite sidecar(s) exist "
            f"for {path}: {names}"
        )
    try:
        with resolved.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        raise StorageError(f"Could not inspect read-only SQLite database {path}: {exc}") from exc
    if (
        len(header) >= 20
        and header[:16] == b"SQLite format 3\x00"
        and (header[18] == 2 or header[19] == 2)
    ):
        raise StorageError(
            "Read-only SQLite observer blocked by WAL-mode SQLite header before connection: "
            f"{path}"
        )
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro", uri=True, timeout=timeout
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
