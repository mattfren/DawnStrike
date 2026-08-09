import hashlib
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import StorageError
from intraday_scanner.services.scenario_intelligence_service import scenario_doctor
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_canonical_connection_is_query_only_and_rejects_writes(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence (id INTEGER PRIMARY KEY)")
    with connect_read_only(path) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        for statement in (
            "INSERT INTO evidence VALUES (1)",
            "UPDATE evidence SET id = 2",
            "CREATE TABLE forbidden (id INTEGER)",
        ):
            with pytest.raises(sqlite3.OperationalError):
                connection.execute(statement)


def test_missing_database_and_parent_are_never_created(tmp_path: Path) -> None:
    path = tmp_path / "absent-parent" / "state.sqlite"
    with pytest.raises(StorageError, match="does not exist"):
        connect_read_only(path)
    assert not path.exists()
    assert not path.parent.exists()


def test_read_only_store_does_not_initialize_or_persist(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite"
    writer = SQLiteScanStore(path)
    writer.initialize()
    before = _sha256(path)
    observer = SQLiteScanStore(path, read_only=True)
    observer.initialize()
    assert observer.load_latest_scan() is None
    with pytest.raises(StorageError):
        observer.persist_monitor_events(
            [{"ticker": "TEST", "event_type": "test", "severity": "info", "created_at": "now"}]
        )
    assert _sha256(path) == before


def test_scenario_doctor_preserves_schema_21_fixture_without_registration(tmp_path: Path) -> None:
    path = tmp_path / "schema21.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER, applied_at TEXT)")
        connection.execute("INSERT INTO schema_version VALUES (21, '2026-08-09T12:30:16Z')")
        connection.execute(
            "CREATE TABLE scenario_model_registry (created_at TEXT, payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO scenario_model_registry VALUES (?, ?)",
            ("2026-08-09T12:30:16Z", "{}"),
        )
    before = _sha256(path)
    with connect_read_only(path) as connection:
        schema_before = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        registry_before = connection.execute(
            "SELECT created_at FROM scenario_model_registry LIMIT 1"
        ).fetchone()[0]
    scenario_doctor(
        db_path=path,
        config=ScannerConfig(database_path=path, scenario_intelligence_enabled=True),
    )
    assert _sha256(path) == before
    with connect_read_only(path) as connection:
        schema_after = connection.execute("SELECT version FROM schema_version").fetchone()[0]
        assert schema_after == schema_before == 21
        assert connection.execute(
            "SELECT created_at FROM scenario_model_registry LIMIT 1"
        ).fetchone()[0] == registry_before == "2026-08-09T12:30:16Z"
