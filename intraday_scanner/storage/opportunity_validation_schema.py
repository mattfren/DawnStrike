"""Exact schema-30 verification for durable validation evidence."""

from __future__ import annotations

import sqlite3
from typing import Any

from intraday_scanner.storage.migrations import _migration_030_opportunity_validation
from intraday_scanner.storage.opportunity_store import _sql_fingerprint
from intraday_scanner.storage.opportunity_validation_errors import (
    OpportunityValidationIntegrityError,
)

VALIDATION_TABLES = (
    "opportunity_validation_receipts",
    "opportunity_validation_oos_sessions",
)
VALIDATION_INDEXES = frozenset(
    {
        "uq_opportunity_validation_consumed_lock",
        "uq_opportunity_validation_consumed_authority",
        "uq_opportunity_validation_consumed_inventory",
        "idx_opportunity_validation_receipts_preparation",
        "idx_opportunity_validation_receipts_result",
        "idx_opportunity_validation_receipts_policy",
        "idx_opportunity_validation_oos_inventory",
        "idx_opportunity_validation_oos_order",
    }
)
VALIDATION_TRIGGERS = frozenset(
    {
        "opportunity_validation_receipts_no_update",
        "opportunity_validation_receipts_no_delete",
        "opportunity_validation_oos_sessions_no_update",
        "opportunity_validation_oos_sessions_no_delete",
    }
)
RECEIPT_INSERT_ORDER = (
    "validation_receipt_id",
    "receipt_content_hash_sha256",
    "semantic_lock_key",
    "lock_authority_key",
    "holdout_inventory_key",
    "status",
    "fresh_lock_eligible",
    "preparation_id",
    "preparation_content_hash_sha256",
    "preparation_schema_version",
    "preparation_json",
    "metric_report_id",
    "metric_report_content_hash_sha256",
    "metric_report_schema_version",
    "metric_report_json",
    "robustness_report_id",
    "robustness_report_content_hash_sha256",
    "robustness_report_schema_version",
    "robustness_report_json",
    "holdout_access_evidence_id",
    "holdout_access_content_hash_sha256",
    "holdout_access_schema_version",
    "holdout_access_json",
    "corpus_id",
    "split_plan_id",
    "split_policy_id",
    "split_policy_content_hash_sha256",
    "split_policy_declared_at",
    "code_identity",
    "code_content_hash_sha256",
    "strategy_id",
    "strategy_version",
    "confirmatory_unit_id",
    "confirmatory_unit_content_hash_sha256",
    "corpus_policy_id",
    "corpus_policy_content_hash_sha256",
    "metric_policy_id",
    "metric_policy_content_hash_sha256",
    "robustness_policy_id",
    "robustness_policy_content_hash_sha256",
    "oos_session_count",
    "oos_session_inventory_hash_sha256",
    "result_set_hash_sha256",
    "persisted_at",
    "lifecycle_mutation_count",
    "take_authorization",
    "research_only",
    "promotion_eligible",
    "database_schema_version",
    "receipt_schema_version",
    "receipt_json",
)
SESSION_INSERT_ORDER = (
    "validation_receipt_id",
    "session_ordinal",
    "session_source_id",
    "session_content_hash_sha256",
    "exchange_session_id",
    "session_open_at",
    "session_close_at",
    "role",
)


def validate_validation_schema(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        version = int(row[0]) if row is not None else 0
        if version != 30:
            raise OpportunityValidationIntegrityError(
                f"validation store requires schema 30, found {version}"
            )
        expected_sql, expected_structures = _canonical_schema()
        actual_sql = _governed_sql(connection)
        if set(actual_sql) != set(expected_sql):
            raise OpportunityValidationIntegrityError(
                "validation governed schema object set is invalid"
            )
        for name, sql in expected_sql.items():
            if _sql_fingerprint(actual_sql[name]) != _sql_fingerprint(sql):
                raise OpportunityValidationIntegrityError(
                    f"validation schema object {name} is invalid"
                )
        indexes = {
            name
            for name in actual_sql
            if name.startswith("idx_") or name.startswith("uq_")
        }
        if indexes != VALIDATION_INDEXES:
            raise OpportunityValidationIntegrityError(
                "validation governed index set is invalid"
            )
        triggers = {
            name
            for name in actual_sql
            if name.endswith("_no_update") or name.endswith("_no_delete")
        }
        if triggers != VALIDATION_TRIGGERS:
            raise OpportunityValidationIntegrityError(
                "validation governed trigger set is invalid"
            )
        for table, expected in expected_structures.items():
            if _structure(connection, table) != expected:
                raise OpportunityValidationIntegrityError(
                    f"validation structure for {table} is invalid"
                )
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise OpportunityValidationIntegrityError(
                "validation store requires foreign keys"
            )
        for table in VALIDATION_TABLES:
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchall():
                raise OpportunityValidationIntegrityError(
                    f"validation table {table} has FK violations"
                )
    except OpportunityValidationIntegrityError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise OpportunityValidationIntegrityError(
            "validation schema metadata is invalid"
        ) from exc


def validation_schema_inventory(connection: sqlite3.Connection) -> dict[str, str]:
    """Return canonical normalized fingerprints for durable migration evidence."""

    validate_validation_schema(connection)
    return {
        name: _sql_fingerprint(sql)
        for name, sql in sorted(_governed_sql(connection).items())
    }


def expected_validation_schema_inventory() -> dict[str, str]:
    sql, _ = _canonical_schema()
    return {name: _sql_fingerprint(value) for name, value in sorted(sql.items())}


def _governed_sql(connection: sqlite3.Connection) -> dict[str, str]:
    placeholders = ",".join("?" for _ in VALIDATION_TABLES)
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL "
            f"AND tbl_name IN ({placeholders})",  # nosec B608 -- placeholder count derives only from fixed VALIDATION_TABLES
            VALIDATION_TABLES,
        ).fetchall()
    }


def _canonical_schema() -> tuple[dict[str, str], dict[str, Any]]:
    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _migration_030_opportunity_validation(connection)
        return _governed_sql(connection), {
            table: _structure(connection, table) for table in VALIDATION_TABLES
        }


def _structure(connection: sqlite3.Connection, table: str) -> tuple[object, ...]:
    columns = tuple(
        (str(row[1]), str(row[2]), int(row[3]), int(row[5]))
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )
    unique_keys = set()
    for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if int(index[2]) == 1:
            unique_keys.add(
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
    foreign_keys = set()
    for rows in groups.values():
        ordered = sorted(rows, key=lambda item: int(item[1]))
        foreign_keys.add(
            (
                str(ordered[0][2]),
                tuple(str(item[3]) for item in ordered),
                tuple(str(item[4]) for item in ordered),
                str(ordered[0][5]),
                str(ordered[0][6]),
                str(ordered[0][7]),
            )
        )
    return columns, frozenset(unique_keys), frozenset(foreign_keys)


__all__ = [
    "RECEIPT_INSERT_ORDER",
    "SESSION_INSERT_ORDER",
    "VALIDATION_INDEXES",
    "VALIDATION_TABLES",
    "VALIDATION_TRIGGERS",
    "expected_validation_schema_inventory",
    "validate_validation_schema",
    "validation_schema_inventory",
]
