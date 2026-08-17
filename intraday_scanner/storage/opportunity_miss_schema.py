"""Exact schema-29 validation for governed missed-opportunity objects."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from intraday_scanner.storage.opportunity_miss_errors import (
    OpportunityMissIntegrityError,
)
from intraday_scanner.storage.opportunity_store import _sql_fingerprint

MISS_DATABASE_SCHEMA_VERSION = 29
CURRENT_STORAGE_SCHEMA_VERSION = 30
MISS_TABLES = (
    "opportunity_miss_receipts",
    "opportunity_miss_records",
    "opportunity_miss_run_bindings",
)
MISS_INDEXES = frozenset(
    {
        "uq_opportunity_miss_receipt_root",
        "uq_opportunity_miss_receipt_successor",
        "idx_opportunity_miss_receipts_scope",
        "idx_opportunity_miss_receipts_batch",
        "idx_opportunity_miss_receipts_session_policy",
        "idx_opportunity_miss_records_stable_key",
        "idx_opportunity_miss_records_identity",
        "idx_opportunity_miss_records_status",
        "idx_opportunity_miss_run_bindings_parent",
        "idx_opportunity_miss_run_bindings_order",
        "idx_opportunity_miss_run_bindings_identity",
    }
)
MISS_TRIGGERS = frozenset(
    {
        "opportunity_miss_receipts_no_update",
        "opportunity_miss_receipts_no_delete",
        "opportunity_miss_records_no_update",
        "opportunity_miss_records_no_delete",
        "opportunity_miss_run_bindings_no_update",
        "opportunity_miss_run_bindings_no_delete",
    }
)

RECEIPT_INSERT_ORDER = (
    "miss_receipt_id", "receipt_content_hash_sha256", "receipt_kind",
    "analysis_key", "batch_id", "batch_content_hash_sha256",
    "batch_schema_version", "batch_json", "exchange_session_id",
    "session_open_at", "session_close_at", "membership_as_of_at",
    "requested_query_start_at", "requested_through_at", "requested_symbols_json",
    "requested_symbol_count", "empty_eligible_universe", "authority_claim",
    "source_scope_status", "inventory_status",
    "qualification_policy_id", "qualification_policy_content_hash_sha256",
    "qualification_batch_id", "qualification_batch_content_hash_sha256",
    "session_replay_id", "session_replay_content_hash_sha256",
    "session_disposition", "batch_recorded_at", "persisted_at",
    "supersedes_miss_receipt_id", "supersedes_miss_receipt_content_hash_sha256",
    "record_count", "run_binding_count", "artifact_count",
    "artifact_inventory_hash_sha256", "receipt_schema_version", "receipt_json",
    "research_only", "promotion_eligible", "database_schema_version",
)
RECORD_INSERT_ORDER = (
    "miss_receipt_id", "record_ordinal", "analysis_key",
    "session_opportunity_key", "symbol", "direction", "horizon_id",
    "opportunity_id", "opportunity_content_hash_sha256", "miss_record_id",
    "miss_record_content_hash_sha256", "miss_record_schema_version",
    "miss_record_json", "disposition", "category", "first_persisted_at",
)
BINDING_INSERT_ORDER = (
    "miss_receipt_id", "binding_ordinal", "binding_id",
    "binding_content_hash_sha256", "binding_schema_version", "binding_json",
    "run_id", "run_content_hash_sha256", "run_persistence_receipt_id",
    "run_persistence_receipt_content_hash_sha256", "outcome_replay_id",
    "outcome_replay_content_hash_sha256", "outcome_head_receipt_id",
    "outcome_head_receipt_content_hash_sha256", "decision_at",
)


@dataclass(frozen=True)
class _TableStructure:
    primary_key: tuple[str, ...]
    unique_keys: frozenset[tuple[tuple[str, ...], bool]]
    foreign_keys: frozenset[
        tuple[str, tuple[str, ...], tuple[str, ...], str, str, str]
    ]


def validate_miss_schema(connection: sqlite3.Connection) -> None:
    """Fail closed unless all governed schema-29 objects are canonical."""

    try:
        row = connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
        version = int(row[0]) if row is not None else 0
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise OpportunityMissIntegrityError(
            "miss database schema is absent or malformed; initialize explicitly"
        ) from exc
    if version not in {MISS_DATABASE_SCHEMA_VERSION, CURRENT_STORAGE_SCHEMA_VERSION}:
        raise OpportunityMissIntegrityError(
            f"miss store requires schema 29 or 30, found {version}"
        )
    try:
        expected_sql, expected_structures = _canonical_schema()
        actual_sql = _governed_sql(connection)
        if set(actual_sql) != set(expected_sql):
            raise OpportunityMissIntegrityError(
                "miss store governed schema object set is not canonical"
            )
        for name, sql in expected_sql.items():
            if _sql_fingerprint(actual_sql[name]) != _sql_fingerprint(sql):
                raise OpportunityMissIntegrityError(
                    f"miss store schema object {name} is invalid"
                )
        for table, expected in expected_structures.items():
            if _table_structure(connection, table) != expected:
                raise OpportunityMissIntegrityError(
                    f"miss store key structure for {table} is invalid"
                )
        explicit_indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
                "AND tbl_name IN (?,?,?)",
                MISS_TABLES,
            ).fetchall()
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL "
                "AND tbl_name IN (?,?,?)",
                MISS_TABLES,
            ).fetchall()
        }
        if explicit_indexes != MISS_INDEXES or triggers != MISS_TRIGGERS:
            raise OpportunityMissIntegrityError(
                "miss store governed indexes or triggers are not canonical"
            )
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise OpportunityMissIntegrityError(
                "miss store requires foreign key enforcement"
            )
        for table in MISS_TABLES:
            if connection.execute(f"PRAGMA foreign_key_check({table})").fetchall():
                raise OpportunityMissIntegrityError(
                    f"miss store table {table} contains foreign key violations"
                )
    except OpportunityMissIntegrityError:
        raise
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise OpportunityMissIntegrityError("miss schema metadata is malformed") from exc


def _governed_sql(connection: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND tbl_name IN (?,?,?)",
            MISS_TABLES,
        ).fetchall()
    }


def _canonical_schema() -> tuple[dict[str, str], dict[str, _TableStructure]]:
    from intraday_scanner.storage.migrations import _migration_029_opportunity_research

    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE opportunity_pipeline_runs (run_id TEXT PRIMARY KEY);
            CREATE TABLE opportunity_outcome_receipts (
                run_id TEXT NOT NULL,
                outcome_receipt_id TEXT NOT NULL,
                receipt_content_hash_sha256 TEXT NOT NULL,
                PRIMARY KEY (outcome_receipt_id),
                UNIQUE (run_id, outcome_receipt_id, receipt_content_hash_sha256)
            );
            """
        )
        _migration_029_opportunity_research(connection)
        sql = _governed_sql(connection)
        structures = {table: _table_structure(connection, table) for table in MISS_TABLES}
        return sql, structures


def _table_structure(connection: sqlite3.Connection, table: str) -> _TableStructure:
    table_info = connection.execute(f"PRAGMA table_info({table})").fetchall()
    primary_key = tuple(
        str(row[1])
        for row in sorted(
            (row for row in table_info if int(row[5]) > 0),
            key=lambda row: int(row[5]),
        )
    )
    unique_keys: set[tuple[tuple[str, ...], bool]] = set()
    for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
        if int(index[2]) != 1:
            continue
        name = str(index[1])
        columns = tuple(
            str(row[2])
            for row in connection.execute(f"PRAGMA index_info({name})").fetchall()
        )
        unique_keys.add((columns, bool(index[4])))
    groups: dict[int, list[Any]] = {}
    for row in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        groups.setdefault(int(row[0]), []).append(row)
    foreign_keys = set()
    for rows in groups.values():
        ordered = sorted(rows, key=lambda row: int(row[1]))
        foreign_keys.add(
            (
                str(ordered[0][2]),
                tuple(str(row[3]) for row in ordered),
                tuple(str(row[4]) for row in ordered),
                str(ordered[0][5]),
                str(ordered[0][6]),
                str(ordered[0][7]),
            )
        )
    return _TableStructure(
        primary_key=primary_key,
        unique_keys=frozenset(unique_keys),
        foreign_keys=frozenset(foreign_keys),
    )


__all__ = [
    "BINDING_INSERT_ORDER",
    "MISS_DATABASE_SCHEMA_VERSION",
    "RECEIPT_INSERT_ORDER",
    "RECORD_INSERT_ORDER",
    "validate_miss_schema",
]
