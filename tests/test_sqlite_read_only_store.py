import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner import cli
from intraday_scanner.config import ScannerConfig
from intraday_scanner.dashboard.data_loader import load_sqlite
from intraday_scanner.errors import StorageError
from intraday_scanner.services import return_attribution_service
from intraday_scanner.services.alpha_alert_replay_service import write_alpha_alert_replay_report
from intraday_scanner.services.alpha_attribution_service import generate_alpha_attribution_report
from intraday_scanner.services.alpha_cycle_service import alpha_report, alpha_status
from intraday_scanner.services.calendar_report_service import calendar_report
from intraday_scanner.services.daily_orchestrator_service import daily_orchestration_status
from intraday_scanner.services.outcome_gap_service import outcome_gap_report
from intraday_scanner.services.release_doctor_service import dashboard_doctor, probability_doctor
from intraday_scanner.services.return_attribution_service import (
    attribute_returns,
    historical_report,
)
from intraday_scanner.services.scenario_intelligence_service import (
    scenario_doctor,
    scenario_public_snapshot,
)
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from scripts.verify_daily_finalize_receipt import verify as verify_daily_finalize_receipt


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return directories, files


def _table_hash(path: Path, table: str) -> str:
    with connect_read_only(path) as connection:
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')]
        rows = connection.execute(
            f'SELECT * FROM "{table}" ORDER BY ' + ", ".join(f'"{column}"' for column in columns)
        ).fetchall()
    canonical = json.dumps(
        {"columns": columns, "rows": [list(row) for row in rows]},
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _all_table_hashes(path: Path) -> dict[str, str]:
    with connect_read_only(path) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
    return {table: _table_hash(path, table) for table in tables}


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


def test_connect_read_only_rejects_active_wal_before_connection_without_mutation(
    tmp_path: Path,
) -> None:
    db_root = tmp_path / "db-root"
    db_root.mkdir()
    path = db_root / "state.sqlite"
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        writer.execute("INSERT INTO evidence VALUES ('committed')")
        writer.commit()
        assert Path(f"{path}-wal").is_file()
        assert Path(f"{path}-shm").is_file()
        before = _tree(db_root)

        with pytest.raises(StorageError, match="active SQLite sidecar"):
            connect_read_only(path)

        assert _tree(db_root) == before
    finally:
        writer.close()


def test_connect_read_only_rejects_dormant_wal_header_without_mutation(tmp_path: Path) -> None:
    db_root = tmp_path / "db-root"
    db_root.mkdir()
    path = db_root / "state.sqlite"
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        writer.execute("INSERT INTO evidence VALUES ('committed')")
        writer.commit()
    finally:
        writer.close()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    before = _tree(db_root)

    with pytest.raises(StorageError, match="WAL-mode SQLite header"):
        connect_read_only(path)

    assert _tree(db_root) == before


def test_connect_read_only_rejects_existing_rollback_journal_before_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_root = tmp_path / "db-root"
    db_root.mkdir()
    path = db_root / "state.sqlite"
    with sqlite3.connect(path) as writer:
        writer.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
    Path(f"{path}-journal").write_bytes(b"active rollback journal fixture")
    before = _tree(db_root)

    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("sidecar preflight must run before sqlite3.connect")

    monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
    with pytest.raises(StorageError, match="state.sqlite-journal"):
        connect_read_only(path)

    assert _tree(db_root) == before


def test_connect_read_only_closes_connection_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "state.sqlite"
    path.touch()
    closed = False

    class FailingConnection:
        def execute(self, _sql: str) -> None:
            raise sqlite3.OperationalError("forced setup failure")

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(sqlite3, "connect", lambda *_args, **_kwargs: FailingConnection())
    with pytest.raises(StorageError, match="Could not open read-only SQLite database"):
        connect_read_only(path)
    assert closed


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
        assert (
            connection.execute("SELECT created_at FROM scenario_model_registry LIMIT 1").fetchone()[
                0
            ]
            == registry_before
            == "2026-08-09T12:30:16Z"
        )


def test_schema_26_observer_matrix_preserves_database_bytes_and_tables(tmp_path: Path) -> None:
    path = tmp_path / "schema26.sqlite"
    writer = SQLiteScanStore(path)
    writer.initialize()
    writer.persist_manual_outcomes(
        [
            {
                "outcome_key": "schema-26-observer:NOVA",
                "scan_id": "schema-26-observer",
                "ticker": "NOVA",
                "recommendation_timestamp": "2026-08-01T09:20:00-04:00",
                "uploaded_at": "2026-08-01T16:00:00-04:00",
                "date": "2026-08-01",
                "entry_time": "2026-08-01T09:30:00-04:00",
                "entry_price": 5.0,
                "close_price": 5.5,
                "high_after_entry": 5.6,
                "low_after_entry": 4.9,
                "halted": False,
                "source": "manual_outcome_upload",
                "notes": "schema 26 observer fixture",
            }
        ]
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO scenario_model_registry
            (model_id, created_at, policy_version, feature_schema_version,
             calibration_status, sample_count, promotion_state, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "schema-26-observer",
                "2026-08-09T12:30:16Z",
                "v1",
                "v1",
                "pending",
                0,
                "shadow",
                "{}",
            ),
        )
    before = {
        "file": _sha256(path),
        "schema_version": _table_hash(path, "schema_version"),
        "scenario_model_registry": _table_hash(path, "scenario_model_registry"),
        "tables": _table_count(path),
        "table_hashes": _all_table_hashes(path),
    }
    out_dir = tmp_path / "observer-output"
    scenario_doctor(db_path=path, config=ScannerConfig(database_path=path))
    alpha_status(db_path=path)
    alpha_report(db_path=path, out_dir=out_dir / "alpha")
    scenario_public_snapshot(db_path=path, limit=10)
    outcome_gap_report(db_path=path, out_path=out_dir / "outcome-gap.json")
    historical_report(db_path=path, out_dir=out_dir / "historical")
    calendar_report(db_path=path, out_dir=out_dir / "calendar", month="2026-08")
    generate_alpha_attribution_report(
        db_path=path,
        out_dir=out_dir / "alpha-attribution",
        paper_ops_root=tmp_path / "paper-ops",
    )
    attribute_returns(db_path=path, out_dir=out_dir / "return-attribution", persist=False)
    write_alpha_alert_replay_report(db_path=path, out_path=out_dir / "alert-replay.json")
    load_sqlite(path)
    probability_doctor(path)
    dashboard_doctor(path, tmp_path)
    status = daily_orchestration_status(
        SQLiteScanStore(path, read_only=True),
        market_date="2026-08-01",
        state_root=tmp_path / "state",
    )
    assert status["status"] == "STALE_HEARTBEAT"
    receipt = verify_daily_finalize_receipt(path, "2026-08-01", "schema26release")
    assert receipt["status"] == "BLOCKED"
    assert (
        cli._run_audit_manual_outcomes(
            SimpleNamespace(db_path=str(path), persist=False, out_dir=out_dir / "manual")
        )
        == 0
    )
    assert (
        cli._run_free_shadow_report(
            SimpleNamespace(db_path=str(path), persist=False, out_dir=out_dir / "shadow")
        )
        == 0
    )
    assert (
        cli._run_audit_latest(
            SimpleNamespace(
                db_path=str(path),
                persist=False,
                minute_bars="missing.csv",
                out_dir=out_dir / "latest",
                top_n=3,
                slippage_bps=None,
                entry_mode="open",
            )
        )
        == 1
    )
    assert cli._run_performance_report(SimpleNamespace(db_path=str(path), persist=False)) == 1
    after = {
        "file": _sha256(path),
        "schema_version": _table_hash(path, "schema_version"),
        "scenario_model_registry": _table_hash(path, "scenario_model_registry"),
        "tables": _table_count(path),
        "table_hashes": _all_table_hashes(path),
    }
    assert after == before


def test_attribute_returns_rejects_notification_without_persist_before_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "state.sqlite"
    SQLiteScanStore(path).initialize()
    before = _sha256(path)
    sent = False

    def forbidden_notification(**_kwargs: object) -> dict[str, int]:
        nonlocal sent
        sent = True
        return {"sent": 1}

    monkeypatch.setattr(
        return_attribution_service, "_send_accuracy_summary", forbidden_notification
    )
    with pytest.raises(ValueError, match="require persist=True"):
        attribute_returns(
            db_path=path,
            out_dir=tmp_path / "artifacts",
            persist=False,
            notify="console",
        )
    assert sent is False
    assert _sha256(path) == before


def _table_count(path: Path) -> int:
    with connect_read_only(path) as connection:
        return int(
            connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[
                0
            ]
        )
