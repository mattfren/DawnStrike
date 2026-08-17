"""Exact schema-29 validation for governed discovery-metric objects."""

from __future__ import annotations

import sqlite3
from typing import Any

from intraday_scanner.storage.migrations import _migration_029_opportunity_research
from intraday_scanner.storage.opportunity_metric_errors import OpportunityMetricIntegrityError
from intraday_scanner.storage.opportunity_store import _sql_fingerprint

METRIC_TABLES = ("opportunity_metric_receipts", "opportunity_metric_session_bindings")
METRIC_INDEXES = frozenset(
    {
        "uq_opportunity_metric_receipt_root",
        "uq_opportunity_metric_receipt_successor",
        "idx_opportunity_metric_receipts_scope",
        "idx_opportunity_metric_receipts_policy_kind",
        "idx_opportunity_metric_receipts_parent_miss",
        "idx_opportunity_metric_receipts_cohort",
        "idx_opportunity_metric_bindings_child_metric",
        "idx_opportunity_metric_bindings_child_miss",
        "idx_opportunity_metric_bindings_order",
    }
)
METRIC_TRIGGERS = frozenset(
    {
        "opportunity_metric_receipts_no_update",
        "opportunity_metric_receipts_no_delete",
        "opportunity_metric_session_bindings_no_update",
        "opportunity_metric_session_bindings_no_delete",
    }
)
RECEIPT_INSERT_ORDER = (
    "metric_receipt_id", "receipt_content_hash_sha256", "receipt_kind", "report_kind",
    "scope_key", "report_id", "report_content_hash_sha256", "report_schema_version",
    "report_json", "metric_policy_id", "metric_policy_content_hash_sha256",
    "exchange_session_id", "session_open_at", "session_close_at",
    "parent_miss_receipt_id", "parent_miss_receipt_content_hash_sha256", "cohort_id",
    "report_recorded_at", "persisted_at", "supersedes_metric_receipt_id",
    "supersedes_metric_receipt_content_hash_sha256", "session_binding_count",
    "metric_value_count", "artifact_count", "artifact_inventory_hash_sha256",
    "receipt_schema_version", "receipt_json", "research_only", "promotion_eligible",
    "database_schema_version",
)
BINDING_INSERT_ORDER = (
    "metric_receipt_id", "session_ordinal", "binding_id",
    "binding_content_hash_sha256", "binding_schema_version", "binding_json",
    "exchange_session_id", "child_metric_receipt_id",
    "child_metric_receipt_content_hash_sha256", "child_metric_scope_key",
    "child_session_report_id", "child_session_report_content_hash_sha256",
    "child_miss_receipt_id", "child_miss_receipt_content_hash_sha256",
)


def validate_metric_schema(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        version = int(row[0]) if row is not None else 0
        if version not in {29, 30}:
            raise OpportunityMetricIntegrityError(
                f"metric store requires schema 29 or 30, found {version}"
            )
        expected_sql, expected_structures = _canonical_schema()
        actual_sql = _governed_sql(connection)
        if set(actual_sql) != set(expected_sql):
            raise OpportunityMetricIntegrityError("metric governed schema set is invalid")
        for name, sql in expected_sql.items():
            if _sql_fingerprint(actual_sql[name]) != _sql_fingerprint(sql):
                raise OpportunityMetricIntegrityError(f"metric schema object {name} is invalid")
        indexes = {
            name
            for name in actual_sql
            if name.startswith("idx_") or name.startswith("uq_")
        }
        if indexes != METRIC_INDEXES:
            raise OpportunityMetricIntegrityError("metric governed index set is invalid")
        triggers = {
            name
            for name in actual_sql
            if name.endswith("_no_update") or name.endswith("_no_delete")
        }
        if triggers != METRIC_TRIGGERS:
            raise OpportunityMetricIntegrityError("metric governed trigger set is invalid")
        for table, expected in expected_structures.items():
            if _structure(connection, table) != expected:
                raise OpportunityMetricIntegrityError(f"metric structure for {table} is invalid")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise OpportunityMetricIntegrityError("metric store requires foreign keys")
        for table in METRIC_TABLES:
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchall():
                raise OpportunityMetricIntegrityError(f"metric table {table} has FK violations")
    except OpportunityMetricIntegrityError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise OpportunityMetricIntegrityError("metric schema metadata is invalid") from exc


def _governed_sql(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND tbl_name IN (?,?)",
            METRIC_TABLES,
        ).fetchall()
    }


def _canonical_schema():
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            """
            CREATE TABLE opportunity_pipeline_runs(run_id TEXT PRIMARY KEY);
            CREATE TABLE opportunity_outcome_receipts(
              run_id TEXT, outcome_receipt_id TEXT, receipt_content_hash_sha256 TEXT,
              PRIMARY KEY(outcome_receipt_id),
              UNIQUE(run_id,outcome_receipt_id,receipt_content_hash_sha256));
            """
        )
        _migration_029_opportunity_research(connection)
        return _governed_sql(connection), {
            table: _structure(connection, table) for table in METRIC_TABLES
        }


def _structure(connection: sqlite3.Connection, table: str):
    info = connection.execute(f"PRAGMA table_info({table})").fetchall()
    columns = tuple((str(row[1]), int(row[3]), int(row[5])) for row in info)
    uniques = set()
    for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if int(index[2]) == 1:
            uniques.add(
                (
                    tuple(
                        str(row[2])
                        for row in connection.execute(
                            f"PRAGMA index_info({str(index[1])})"
                        ).fetchall()
                    ),
                    bool(index[4]),
                )
            )
    groups: dict[int, list[Any]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        groups.setdefault(int(row[0]), []).append(row)
    foreign = set()
    for rows in groups.values():
        ordered = sorted(rows, key=lambda row: int(row[1]))
        foreign.add(
            (
                str(ordered[0][2]),
                tuple(str(row[3]) for row in ordered),
                tuple(str(row[4]) for row in ordered),
                str(ordered[0][5]), str(ordered[0][6]), str(ordered[0][7]),
            )
        )
    return columns, frozenset(uniques), frozenset(foreign)


__all__ = ["BINDING_INSERT_ORDER", "RECEIPT_INSERT_ORDER", "validate_metric_schema"]
