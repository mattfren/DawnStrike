from __future__ import annotations

import hashlib
import shutil
import sqlite3
from pathlib import Path

from intraday_scanner.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    get_schema_version,
    run_migrations,
    set_schema_version,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def test_current_schema_26_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite"
    SQLiteScanStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO scan_runs VALUES (?, ?, ?, ?, ?)",
            ("daily", "2026-08-07T20:00:00+00:00", "fixture", "{}", "{}"),
        )
        connection.commit()
        before = connection.execute("SELECT * FROM scan_runs").fetchall()
        assert get_schema_version(connection) == CURRENT_SCHEMA_VERSION == 26
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        tables = _table_names(connection)
        assert {
            "intraday_provider_capability_receipts",
            "intraday_artifact_manifests",
            "intraday_coverage_receipts",
            "legacy_policy_classifications",
            "alpha_path_replays",
            "paper_position_excursion_reconciliations",
            "catalyst_evidence_events",
            "catalyst_claim_extractions",
            "alpha_v6_evidence_lineage",
            "trade_attribution_cases",
            "trade_attribution_factors",
        } <= tables
        assert run_migrations(connection) == 26
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(alpha_v6_labels)").fetchall()
        }
        assert {
            "source_artifact_hash_sha256",
            "path_replay_id",
            "benchmark_hash_sha256",
            "observed_cost_model_identity",
            "modeled_cost_model_identity",
            "evidence_cohort",
            "retrospective_research_eligible",
            "prospective_promotion_eligible",
            "evidence_lineage_hash_sha256",
        } <= columns
        assert connection.execute("SELECT * FROM scan_runs").fetchall() == before


def test_two_disposable_21_to_26_rehearsals_preserve_daily_rows_and_are_repeatable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (21, '2026-08-07T00:00:00+00:00')")
        connection.execute(
            "CREATE TABLE daily_truth (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO daily_truth VALUES (1, 'unchanged')")
        connection.commit()

    source_daily_hash = hashlib.sha256(
        b"1|unchanged"
    ).hexdigest()
    for index in (1, 2):
        rehearsal = tmp_path / f"rehearsal-{index}.sqlite"
        shutil.copy2(source, rehearsal)
        with sqlite3.connect(rehearsal) as connection:
            assert run_migrations(connection) == 26
            assert run_migrations(connection) == 26
            assert get_schema_version(connection) == 26
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            row = connection.execute("SELECT id, value FROM daily_truth").fetchone()
            assert hashlib.sha256(f"{row[0]}|{row[1]}".encode()).hexdigest() == source_daily_hash


def test_migration_rollback_leaves_disposable_database_at_21(tmp_path: Path) -> None:
    database = tmp_path / "rollback.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        set_schema_version(connection, 21)
        connection.commit()
        connection.execute("BEGIN")
        connection.execute("CREATE TABLE rollback_probe (value TEXT NOT NULL)")
        connection.rollback()
        assert get_schema_version(connection) == 21
        assert "rollback_probe" not in _table_names(connection)
