import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from scripts.snapshot_sqlite import snapshot_database


def test_snapshot_database_reads_source_without_mutating_it(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    target = tmp_path / "runtime" / "snapshot.sqlite"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO evidence(value) VALUES ('observed')")
        connection.commit()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    result = snapshot_database(source, target)

    assert result["status"] == "PASS"
    assert result["source_mode"] == "read_only_sqlite_backup"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    with closing(
        sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
    ) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (
            "observed",
        )


def test_snapshot_database_replaces_existing_target_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES ('fresh')")
        connection.commit()
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("CREATE TABLE stale (value TEXT)")
        connection.commit()

    snapshot_database(source, target)

    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (
            "fresh",
        )
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stale'"
            ).fetchone()
            is None
        )


def test_snapshot_database_rejects_same_source_and_target(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)):
        pass

    with pytest.raises(ValueError, match="must be different"):
        snapshot_database(source, source)
