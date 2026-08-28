from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import (
    CURRENT_SCHEMA_VERSION,
    _migration_033_research_episode_outcome_bridges,
    _migration_034_research_episode_outcome_bridge_logical_key,
    get_schema_version,
    run_migrations,
    set_schema_version,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeCompleteness,
    OutcomeEntryStatus,
    OutcomePathStatus,
)


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _foreign_key_mappings(
    connection: sqlite3.Connection,
    table: str,
) -> set[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    grouped: dict[int, list[tuple[int, str, str, str]]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        grouped.setdefault(int(row[0]), []).append(
            (int(row[1]), str(row[2]), str(row[3]), str(row[4]))
        )
    return {
        (
            sorted(rows)[0][1],
            tuple(item[2] for item in sorted(rows)),
            tuple(item[3] for item in sorted(rows)),
        )
        for rows in grouped.values()
    }


def _insert_minimal_miss_receipt(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    analysis_key: str,
    membership_as_of_at: str = "2026-08-10T13:30:00+00:00",
    requested_query_start_at: str = "2026-08-11T13:30:00+00:00",
    requested_symbols_json: str = '["ABC"]',
    requested_symbol_count: int = 1,
    empty_eligible_universe: int = 0,
    authority_claim: str = "market_complete",
    source_scope_status: str = "complete_market",
    inventory_status: str = "complete_authoritative",
) -> tuple[str, str]:
    receipt_id = f"miss-receipt-{suffix}"
    receipt_hash = hashlib.sha256(f"miss-receipt-{suffix}".encode()).hexdigest()
    connection.execute(
        """
        INSERT INTO opportunity_miss_receipts (
            miss_receipt_id, receipt_content_hash_sha256, receipt_kind,
            analysis_key, batch_id, batch_content_hash_sha256,
            batch_schema_version, batch_json, exchange_session_id,
            session_open_at, session_close_at, membership_as_of_at,
            requested_query_start_at, requested_through_at,
            requested_symbols_json, requested_symbol_count,
            empty_eligible_universe, authority_claim,
            source_scope_status, inventory_status,
            qualification_policy_id, qualification_policy_content_hash_sha256,
            qualification_batch_id, qualification_batch_content_hash_sha256,
            session_replay_id, session_replay_content_hash_sha256,
            session_disposition, batch_recorded_at, persisted_at,
            supersedes_miss_receipt_id,
            supersedes_miss_receipt_content_hash_sha256,
            record_count, run_binding_count, artifact_count,
            artifact_inventory_hash_sha256, receipt_schema_version,
            receipt_json, research_only, promotion_eligible,
            database_schema_version
        ) VALUES (
            :receipt_id, :receipt_hash, 'initial', :analysis_key,
            :batch_id, :batch_hash,
            'v2.opportunity.miss_reconciliation_batch.v1', '{}',
            'XNYS-2026-08-11', '2026-08-11T13:30:00+00:00',
            '2026-08-11T20:00:00+00:00', :membership_as_of_at,
            :requested_query_start_at, '2026-08-11T20:00:00+00:00',
            :requested_symbols_json, :requested_symbol_count,
            :empty_eligible_universe, :authority_claim,
            :source_scope_status, :inventory_status,
            'policy-1', :policy_hash, 'qualification-batch-1',
            :qualification_hash, 'session-replay-1', :replay_hash,
            'correct_no_trade', '2026-08-11T20:01:00+00:00',
            '2026-08-11T20:02:00+00:00', NULL, NULL, 0, 0, 1,
            :inventory_hash, 'v2.opportunity.miss_persistence_receipt.v1',
            '{}', 1, 0, 29
        )
        """,
        {
            "receipt_id": receipt_id,
            "receipt_hash": receipt_hash,
            "analysis_key": analysis_key,
            "batch_id": f"miss-batch-{suffix}",
            "batch_hash": hashlib.sha256(f"batch-{suffix}".encode()).hexdigest(),
            "membership_as_of_at": membership_as_of_at,
            "requested_query_start_at": requested_query_start_at,
            "requested_symbols_json": requested_symbols_json,
            "requested_symbol_count": requested_symbol_count,
            "empty_eligible_universe": empty_eligible_universe,
            "authority_claim": authority_claim,
            "source_scope_status": source_scope_status,
            "inventory_status": inventory_status,
            "policy_hash": hashlib.sha256(b"policy").hexdigest(),
            "qualification_hash": hashlib.sha256(b"qualification").hexdigest(),
            "replay_hash": hashlib.sha256(b"replay").hexdigest(),
            "inventory_hash": hashlib.sha256(f"inventory-{suffix}".encode()).hexdigest(),
        },
    )
    return receipt_id, receipt_hash


def _insert_minimal_metric_receipt(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    report_kind: str,
    parent_miss: tuple[str, str] | None,
    session_open_at: str | None = "2026-08-11T13:30:00+00:00",
    session_close_at: str | None = "2026-08-11T20:00:00+00:00",
) -> tuple[str, str, str, str]:
    receipt_id = f"metric-receipt-{suffix}"
    receipt_hash = hashlib.sha256(f"metric-receipt-{suffix}".encode()).hexdigest()
    report_id = f"metric-report-{suffix}"
    report_hash = hashlib.sha256(f"metric-report-{suffix}".encode()).hexdigest()
    is_session = report_kind == "session"
    connection.execute(
        """
        INSERT INTO opportunity_metric_receipts (
            metric_receipt_id, receipt_content_hash_sha256, receipt_kind,
            report_kind, scope_key, report_id, report_content_hash_sha256,
            report_schema_version, report_json, metric_policy_id,
            metric_policy_content_hash_sha256, exchange_session_id,
            session_open_at, session_close_at, parent_miss_receipt_id,
            parent_miss_receipt_content_hash_sha256, cohort_id,
            report_recorded_at, persisted_at, supersedes_metric_receipt_id,
            supersedes_metric_receipt_content_hash_sha256,
            session_binding_count, metric_value_count, artifact_count,
            artifact_inventory_hash_sha256, receipt_schema_version,
            receipt_json, research_only, promotion_eligible,
            database_schema_version
        ) VALUES (
            :receipt_id, :receipt_hash, 'initial', :report_kind, :scope_key,
            :report_id, :report_hash, :report_schema, '{}', 'metric-policy-1',
            :policy_hash, :exchange_session_id, :session_open_at,
            :session_close_at, :parent_miss_id, :parent_miss_hash, :cohort_id,
            :report_recorded_at, '2026-08-11T20:03:00+00:00', NULL, NULL,
            :binding_count, 9, :artifact_count, :inventory_hash,
            'v2.opportunity.metric_persistence_receipt.v1', '{}', 1, 0, 29
        )
        """,
        {
            "receipt_id": receipt_id,
            "receipt_hash": receipt_hash,
            "report_kind": report_kind,
            "scope_key": f"metric-scope-{suffix}",
            "report_id": report_id,
            "report_hash": report_hash,
            "report_schema": (
                "v2.opportunity.session_discovery_metric_report.v1"
                if is_session
                else "v2.opportunity.discovery_metric_report.v1"
            ),
            "policy_hash": hashlib.sha256(b"metric-policy").hexdigest(),
            "exchange_session_id": "XNYS-2026-08-11" if is_session else None,
            "session_open_at": session_open_at if is_session else None,
            "session_close_at": session_close_at if is_session else None,
            "parent_miss_id": parent_miss[0] if parent_miss else None,
            "parent_miss_hash": parent_miss[1] if parent_miss else None,
            "cohort_id": None if is_session else f"metric-cohort-{suffix}",
            "report_recorded_at": (
                "2026-08-11T20:01:00+00:00" if is_session else "2026-08-11T20:02:00+00:00"
            ),
            "binding_count": 0 if is_session else 1,
            "artifact_count": 1 if is_session else 2,
            "inventory_hash": hashlib.sha256(f"metric-inventory-{suffix}".encode()).hexdigest(),
        },
    )
    return receipt_id, receipt_hash, report_id, report_hash


def test_outcome_schema_uses_exact_contract_enums_and_versions(tmp_path: Path) -> None:
    database = tmp_path / "outcome-schema.sqlite"
    SQLiteScanStore(database).initialize()
    with sqlite3.connect(database) as connection:
        receipt_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'opportunity_outcome_receipts'"
            ).fetchone()[0]
        )
        record_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'opportunity_outcome_records'"
            ).fetchone()[0]
        )
    for column, enum_type in (
        ("completeness", OutcomeCompleteness),
        ("entry_status", OutcomeEntryStatus),
        ("path_status", OutcomePathStatus),
    ):
        check = re.search(
            rf"{column}\s+TEXT\s+NOT\s+NULL\s+CHECK\s*"
            rf"\({column}\s+IN\s*\((.*?)\)\)",
            record_sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        assert check is not None
        assert set(re.findall(r"'([^']+)'", check.group(1))) == {
            item.value for item in enum_type
        }
    assert "v2.opportunity.outcome_label_batch.v2" in receipt_sql
    assert "v2.opportunity.outcome_persistence_receipt.v1" in receipt_sql
    assert "v2.opportunity.outcome_record.v3" in record_sql


def test_schema_29_research_tables_bind_exact_versions_guards_and_foreign_keys(
    tmp_path: Path,
) -> None:
    database = tmp_path / "research-schema.sqlite"
    SQLiteScanStore(database).initialize()
    tables = {
        "opportunity_miss_receipts",
        "opportunity_miss_records",
        "opportunity_miss_run_bindings",
        "opportunity_metric_receipts",
        "opportunity_metric_session_bindings",
    }
    with sqlite3.connect(database) as connection:
        objects = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
        }
        assert tables <= _table_names(connection)
        assert "v2.opportunity.miss_reconciliation_batch.v1" in objects[
            "opportunity_miss_receipts"
        ]
        assert "v2.opportunity.missed_opportunity_record.v1" in objects[
            "opportunity_miss_records"
        ]
        assert "v2.opportunity.session_run_binding.v1" in objects[
            "opportunity_miss_run_bindings"
        ]
        assert "v2.opportunity.session_discovery_metric_report.v1" in objects[
            "opportunity_metric_receipts"
        ]
        assert "v2.opportunity.discovery_metric_report.v1" in objects[
            "opportunity_metric_receipts"
        ]
        assert "v2.opportunity.metric_session_report_binding.v1" in objects[
            "opportunity_metric_session_bindings"
        ]
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN (?, ?, ?, ?, ?)",
                tuple(sorted(tables)),
            ).fetchall()
        }
        assert triggers == {
            f"{table}_{operation}"
            for table in tables
            for operation in ("no_update", "no_delete")
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND sql IS NOT NULL AND tbl_name IN (?, ?, ?, ?, ?)",
                tuple(sorted(tables)),
            ).fetchall()
        }
        assert {
            "uq_opportunity_miss_receipt_root",
            "uq_opportunity_miss_receipt_successor",
            "idx_opportunity_miss_records_stable_key",
            "idx_opportunity_miss_run_bindings_parent",
            "uq_opportunity_metric_receipt_root",
            "uq_opportunity_metric_receipt_successor",
            "idx_opportunity_metric_bindings_child_metric",
            "idx_opportunity_metric_bindings_child_miss",
        } <= indexes
        assert _foreign_key_mappings(connection, "opportunity_miss_run_bindings") == {
            (
                "opportunity_outcome_receipts",
                (
                    "run_id",
                    "outcome_head_receipt_id",
                    "outcome_head_receipt_content_hash_sha256",
                ),
                ("run_id", "outcome_receipt_id", "receipt_content_hash_sha256"),
            ),
            ("opportunity_pipeline_runs", ("run_id",), ("run_id",)),
            (
                "opportunity_miss_receipts",
                ("miss_receipt_id",),
                ("miss_receipt_id",),
            ),
        }
        assert _foreign_key_mappings(connection, "opportunity_miss_records") == {
            (
                "opportunity_miss_receipts",
                ("analysis_key", "miss_receipt_id"),
                ("analysis_key", "miss_receipt_id"),
            ),
        }
        assert _foreign_key_mappings(connection, "opportunity_metric_receipts") == {
            (
                "opportunity_miss_receipts",
                (
                    "parent_miss_receipt_id",
                    "parent_miss_receipt_content_hash_sha256",
                ),
                ("miss_receipt_id", "receipt_content_hash_sha256"),
            ),
            (
                "opportunity_metric_receipts",
                (
                    "scope_key",
                    "supersedes_metric_receipt_id",
                    "supersedes_metric_receipt_content_hash_sha256",
                ),
                ("scope_key", "metric_receipt_id", "receipt_content_hash_sha256"),
            ),
        }
        assert _foreign_key_mappings(
            connection,
            "opportunity_metric_session_bindings",
        ) == {
            (
                "opportunity_miss_receipts",
                (
                    "child_miss_receipt_id",
                    "child_miss_receipt_content_hash_sha256",
                ),
                ("miss_receipt_id", "receipt_content_hash_sha256"),
            ),
            (
                "opportunity_metric_receipts",
                (
                    "child_metric_receipt_id",
                    "child_metric_receipt_content_hash_sha256",
                    "child_metric_scope_key",
                    "child_session_report_id",
                    "child_session_report_content_hash_sha256",
                ),
                (
                    "metric_receipt_id",
                    "receipt_content_hash_sha256",
                    "scope_key",
                    "report_id",
                    "report_content_hash_sha256",
                ),
            ),
            (
                "opportunity_metric_receipts",
                ("metric_receipt_id",),
                ("metric_receipt_id",),
            ),
        }


def test_schema_29_rejects_cross_scope_status_parent_and_chronology_forgery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "research-schema-forgery.sqlite"
    SQLiteScanStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        miss_parent = _insert_minimal_miss_receipt(
            connection,
            suffix="parent",
            analysis_key="analysis-parent",
        )
        record_values = (
            miss_parent[0],
            0,
            "analysis-parent",
            "session-opportunity-1",
            "ABC",
            "long",
            "horizon-1",
            "opportunity-1",
            hashlib.sha256(b"opportunity").hexdigest(),
            "miss-record-1",
            hashlib.sha256(b"miss-record").hexdigest(),
            "v2.opportunity.missed_opportunity_record.v1",
            "{}",
            "caught",
            None,
            "2026-08-11T20:02:00+00:00",
        )
        record_sql = """
            INSERT INTO opportunity_miss_records (
                miss_receipt_id, record_ordinal, analysis_key,
                session_opportunity_key, symbol, direction, horizon_id,
                opportunity_id, opportunity_content_hash_sha256,
                miss_record_id, miss_record_content_hash_sha256,
                miss_record_schema_version, miss_record_json, disposition,
                category, first_persisted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                record_sql,
                (*record_values[:2], "analysis-wrong", *record_values[3:]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                record_sql,
                (*record_values[:14], "quality_gate_miss", record_values[15]),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                record_sql,
                (*record_values[:13], "missed", None, record_values[15]),
            )

        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert_minimal_miss_receipt(
                connection,
                suffix="late-membership",
                analysis_key="analysis-late-membership",
                membership_as_of_at="2026-08-11T13:30:00.000001+00:00",
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            _insert_minimal_miss_receipt(
                connection,
                suffix="late-query-start",
                analysis_key="analysis-late-query-start",
                requested_query_start_at="2026-08-11T13:30:00.000001+00:00",
            )
        for suffix, kwargs in (
            ("nonempty-empty-flag", {"empty_eligible_universe": 1}),
            (
                "empty-bounded-authority",
                {
                    "requested_symbols_json": "[]",
                    "requested_symbol_count": 0,
                    "empty_eligible_universe": 1,
                    "authority_claim": "bounded_cohort",
                },
            ),
            (
                "empty-partial-scope",
                {
                    "requested_symbols_json": "[]",
                    "requested_symbol_count": 0,
                    "empty_eligible_universe": 1,
                    "source_scope_status": "partial",
                },
            ),
            (
                "empty-bounded-inventory",
                {
                    "requested_symbols_json": "[]",
                    "requested_symbol_count": 0,
                    "empty_eligible_universe": 1,
                    "inventory_status": "complete_bounded",
                },
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                _insert_minimal_miss_receipt(
                    connection,
                    suffix=suffix,
                    analysis_key=f"analysis-{suffix}",
                    **kwargs,
                )
        empty_receipt = _insert_minimal_miss_receipt(
            connection,
            suffix="empty-authoritative",
            analysis_key="analysis-empty-authoritative",
            requested_symbols_json="[]",
            requested_symbol_count=0,
            empty_eligible_universe=1,
        )
        assert empty_receipt[0] == "miss-receipt-empty-authoritative"

        for suffix, session_open_at, session_close_at in (
            (
                "equal-session",
                "2026-08-11T13:30:00+00:00",
                "2026-08-11T13:30:00+00:00",
            ),
            (
                "reversed-session",
                "2026-08-11T13:31:00+00:00",
                "2026-08-11T13:30:00+00:00",
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
                _insert_minimal_metric_receipt(
                    connection,
                    suffix=suffix,
                    report_kind="session",
                    parent_miss=miss_parent,
                    session_open_at=session_open_at,
                    session_close_at=session_close_at,
                )

        child = _insert_minimal_metric_receipt(
            connection,
            suffix="child",
            report_kind="session",
            parent_miss=miss_parent,
        )
        cohort = _insert_minimal_metric_receipt(
            connection,
            suffix="cohort",
            report_kind="multi_session",
            parent_miss=None,
        )
        binding_sql = """
            INSERT INTO opportunity_metric_session_bindings (
                metric_receipt_id, session_ordinal, binding_id,
                binding_content_hash_sha256, binding_schema_version,
                binding_json, exchange_session_id, child_metric_receipt_id,
                child_metric_receipt_content_hash_sha256,
                child_metric_scope_key,
                child_session_report_id,
                child_session_report_content_hash_sha256,
                child_miss_receipt_id,
                child_miss_receipt_content_hash_sha256
            ) VALUES (?, 0, ?, ?,
                'v2.opportunity.metric_session_report_binding.v1', '{}',
                'XNYS-2026-08-11', ?, ?, ?, ?, ?, ?, ?)
        """
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                binding_sql,
                (
                    cohort[0],
                    "binding-wrong-report",
                    hashlib.sha256(b"binding-wrong-report").hexdigest(),
                    child[0],
                    child[1],
                    "metric-scope-child",
                    "metric-report-wrong",
                    child[3],
                    miss_parent[0],
                    miss_parent[1],
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            connection.execute(
                binding_sql,
                (
                    cohort[0],
                    "binding-self",
                    hashlib.sha256(b"binding-self").hexdigest(),
                    cohort[0],
                    cohort[1],
                    "metric-scope-cohort",
                    cohort[2],
                    cohort[3],
                    miss_parent[0],
                    miss_parent[1],
                ),
            )


def test_current_schema_30_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite"
    SQLiteScanStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO scan_runs VALUES (?, ?, ?, ?, ?)",
            ("daily", "2026-08-07T20:00:00+00:00", "fixture", "{}", "{}"),
        )
        connection.commit()
        before = connection.execute("SELECT * FROM scan_runs").fetchall()
        assert get_schema_version(connection) == CURRENT_SCHEMA_VERSION == 30
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
            "opportunity_pipeline_runs",
            "opportunity_run_artifacts",
            "opportunity_outcome_receipts",
            "opportunity_outcome_records",
            "opportunity_miss_receipts",
            "opportunity_miss_records",
            "opportunity_miss_run_bindings",
            "opportunity_metric_receipts",
            "opportunity_metric_session_bindings",
            "opportunity_validation_receipts",
            "opportunity_validation_oos_sessions",
        } <= tables
        assert run_migrations(connection) == 30
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


def test_two_disposable_21_to_30_rehearsals_preserve_daily_rows_and_are_repeatable(
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
            assert run_migrations(connection) == 30
            assert run_migrations(connection) == 30
            assert get_schema_version(connection) == 30
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            row = connection.execute("SELECT id, value FROM daily_truth").fetchone()
            assert hashlib.sha256(f"{row[0]}|{row[1]}".encode()).hexdigest() == source_daily_hash


def test_two_disposable_26_to_30_rehearsals_preserve_rows_and_hashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "schema-26.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (26, '2026-08-10T00:00:00+00:00')")
        connection.execute(
            "CREATE TABLE daily_truth (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO daily_truth VALUES (1, 'unchanged-through-27')")
        connection.commit()

    source_bytes_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    source_row_hash = hashlib.sha256(b"1|unchanged-through-27").hexdigest()
    for index in (1, 2):
        rehearsal = tmp_path / f"schema-27-rehearsal-{index}.sqlite"
        shutil.copy2(source, rehearsal)
        assert hashlib.sha256(rehearsal.read_bytes()).hexdigest() == source_bytes_hash
        with sqlite3.connect(rehearsal) as connection:
            assert run_migrations(connection) == 30
            assert run_migrations(connection) == 30
            assert get_schema_version(connection) == 30
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            row = connection.execute("SELECT id, value FROM daily_truth").fetchone()
            assert hashlib.sha256(f"{row[0]}|{row[1]}".encode()).hexdigest() == source_row_hash
            assert {
                "opportunity_pipeline_runs",
                "opportunity_run_artifacts",
            } <= _table_names(connection)
            triggers = {
                str(item[0])
                for item in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            assert {
                "opportunity_pipeline_runs_no_update",
                "opportunity_pipeline_runs_no_delete",
                "opportunity_run_artifacts_no_update",
                "opportunity_run_artifacts_no_delete",
            } <= triggers


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


def _insert_duplicate_research_bridge_rows(connection: sqlite3.Connection) -> None:
    values = []
    for bridge_id, bridge_hash in (("rep-a", "a" * 64), ("rep-b", "b" * 64)):
        values.append(
            (
                bridge_id,
                bridge_hash,
                "selection:nova",
                "slate:nova",
                "c" * 64,
                "episode:nova",
                "NOVA",
                "2026-08-28",
                "2026-08-28T14:00:00+00:00",
                "strategy-one",
                "v1",
                "receipt-one",
                "d" * 64,
                "MISSING",
                0,
                None,
                None,
                None,
                None,
                "2026-08-28T20:00:00+00:00",
                None,
                None,
                "{}",
                "2026-08-28T20:00:00+00:00",
            )
        )
    connection.executemany(
        """INSERT INTO research_episode_outcome_bridges (
        bridge_id, bridge_hash_sha256, selection_id, slate_id,
        slate_content_hash_sha256, episode_id, ticker, market_date, selected_at,
        strategy_id, strategy_version, receipt_id, receipt_hash_sha256,
        outcome_status, learning_eligible, source_observation_id,
        source_observation_hash_sha256, source_path_id, source_path_hash_sha256,
        source_cutoff, outcome_artifact_id, outcome_artifact_hash_sha256,
        payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values,
    )


def test_migration_34_assigns_stable_revisions_to_populated_duplicate_keys() -> None:
    with sqlite3.connect(":memory:") as connection:
        _migration_033_research_episode_outcome_bridges(connection)
        _insert_duplicate_research_bridge_rows(connection)
        connection.commit()
        _migration_034_research_episode_outcome_bridge_logical_key(connection)
        rows = connection.execute(
            "SELECT bridge_id, logical_key FROM research_episode_outcome_bridges "
            "ORDER BY bridge_id"
        ).fetchall()
        assert rows[0][1].startswith("research-bridge-logical-v1-")
        assert rows[1][1] == rows[0][1] + "-r2"
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' "
            "AND name='idx_research_episode_outcome_bridges_logical_key'"
        ).fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE research_episode_outcome_bridges SET ticker='EVIL' "
                "WHERE bridge_id='rep-a'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM research_episode_outcome_bridges WHERE bridge_id='rep-a'"
            )


def test_migration_34_failure_rolls_back_column_rows_index_and_restores_triggers() -> None:
    with sqlite3.connect(":memory:") as connection:
        _migration_033_research_episode_outcome_bridges(connection)
        _insert_duplicate_research_bridge_rows(connection)
        connection.commit()
        before = connection.execute(
            "SELECT bridge_id, payload_json FROM research_episode_outcome_bridges "
            "ORDER BY bridge_id"
        ).fetchall()

        def deny_logical_index(action, arg1, _arg2, _database, _source):
            if (
                action == sqlite3.SQLITE_CREATE_INDEX
                and arg1 == "idx_research_episode_outcome_bridges_logical_key"
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(deny_logical_index)
        with pytest.raises(sqlite3.DatabaseError):
            _migration_034_research_episode_outcome_bridge_logical_key(connection)
        connection.set_authorizer(None)
        assert "logical_key" not in {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(research_episode_outcome_bridges)"
            ).fetchall()
        }
        assert connection.execute(
            "SELECT bridge_id, payload_json FROM research_episode_outcome_bridges "
            "ORDER BY bridge_id"
        ).fetchall() == before
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        assert {
            "research_episode_outcome_bridges_no_update",
            "research_episode_outcome_bridges_no_delete",
        } <= triggers
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='index' "
            "AND name='idx_research_episode_outcome_bridges_logical_key'"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE research_episode_outcome_bridges SET ticker='EVIL' "
                "WHERE bridge_id='rep-a'"
            )
