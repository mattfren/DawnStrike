"""Exact schema-28 validation for governed opportunity outcome objects."""

from __future__ import annotations

import sqlite3

from intraday_scanner.storage.opportunity_outcome_errors import (
    OpportunityOutcomeIntegrityError,
)
from intraday_scanner.storage.opportunity_store import _sql_fingerprint

OUTCOME_DATABASE_SCHEMA_VERSION = 28
PREVIOUS_STORAGE_SCHEMA_VERSION = 29
CURRENT_STORAGE_SCHEMA_VERSION = 30

RECEIPT_COLUMNS = frozenset(
    {
        "outcome_receipt_id", "receipt_content_hash_sha256", "receipt_kind",
        "batch_id", "batch_content_hash_sha256", "batch_schema_version",
        "batch_json", "run_id", "run_content_hash_sha256",
        "run_persistence_receipt_id",
        "run_persistence_receipt_content_hash_sha256", "source_dataset_id",
        "source_dataset_content_hash_sha256", "policy_id",
        "policy_content_hash_sha256", "decision_at", "batch_recorded_at",
        "persisted_at", "supersedes_outcome_receipt_id",
        "supersedes_outcome_receipt_content_hash_sha256", "record_count",
        "artifact_count", "artifact_inventory_hash_sha256",
        "receipt_schema_version", "receipt_json", "research_only",
        "database_schema_version",
    }
)
RECORD_COLUMNS = frozenset(
    {
        "outcome_receipt_id", "record_ordinal", "run_id", "evaluation_id",
        "horizon_id", "decision_id", "outcome_id",
        "outcome_content_hash_sha256", "outcome_schema_version", "outcome_json",
        "completeness", "entry_status", "path_status",
        "supersedes_outcome_receipt_id", "supersedes_outcome_id",
        "supersedes_outcome_content_hash_sha256", "first_persisted_at",
    }
)
EXPECTED_INDEX_COLUMNS = {
    "uq_opportunity_outcome_receipt_root": ("run_id",),
    "uq_opportunity_outcome_receipt_successor": ("supersedes_outcome_receipt_id",),
    "idx_opportunity_outcome_receipts_run": (
        "run_id", "persisted_at", "outcome_receipt_id",
    ),
    "idx_opportunity_outcome_receipts_batch": (
        "batch_id", "batch_content_hash_sha256",
    ),
    "uq_opportunity_outcome_record_root": (
        "run_id", "evaluation_id", "horizon_id",
    ),
    "uq_opportunity_outcome_record_successor": (
        "run_id", "supersedes_outcome_receipt_id", "supersedes_outcome_id",
        "supersedes_outcome_content_hash_sha256",
    ),
    "idx_opportunity_outcome_records_pair": (
        "run_id", "evaluation_id", "horizon_id", "outcome_receipt_id",
        "record_ordinal",
    ),
    "idx_opportunity_outcome_records_identity": (
        "outcome_id", "outcome_content_hash_sha256", "outcome_schema_version",
    ),
    "idx_opportunity_outcome_records_decision": (
        "run_id", "decision_id", "outcome_receipt_id",
    ),
    "idx_opportunity_outcome_records_status": (
        "run_id", "completeness", "path_status",
    ),
}
EXPECTED_TRIGGERS = frozenset(
    {
        "opportunity_outcome_receipts_no_update",
        "opportunity_outcome_receipts_no_delete",
        "opportunity_outcome_records_no_update",
        "opportunity_outcome_records_no_delete",
    }
)
RECEIPT_INSERT_ORDER = (
    "outcome_receipt_id", "receipt_content_hash_sha256", "receipt_kind",
    "batch_id", "batch_content_hash_sha256", "batch_schema_version", "batch_json",
    "run_id", "run_content_hash_sha256", "run_persistence_receipt_id",
    "run_persistence_receipt_content_hash_sha256", "source_dataset_id",
    "source_dataset_content_hash_sha256", "policy_id", "policy_content_hash_sha256",
    "decision_at", "batch_recorded_at", "persisted_at",
    "supersedes_outcome_receipt_id",
    "supersedes_outcome_receipt_content_hash_sha256", "record_count",
    "artifact_count", "artifact_inventory_hash_sha256", "receipt_schema_version",
    "receipt_json", "research_only", "database_schema_version",
)
RECORD_INSERT_ORDER = (
    "outcome_receipt_id", "record_ordinal", "run_id", "evaluation_id",
    "horizon_id", "decision_id", "outcome_id", "outcome_content_hash_sha256",
    "outcome_schema_version", "outcome_json", "completeness", "entry_status",
    "path_status", "supersedes_outcome_receipt_id", "supersedes_outcome_id",
    "supersedes_outcome_content_hash_sha256", "first_persisted_at",
)


def validate_outcome_schema(
    connection: sqlite3.Connection,
    *,
    require_current: bool = False,
) -> None:
    try:
        row = connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise OpportunityOutcomeIntegrityError(
            "outcome database schema is absent; call initialize explicitly"
        ) from exc
    try:
        version = int(row[0]) if row is not None else 0
    except (TypeError, ValueError) as exc:
        raise OpportunityOutcomeIntegrityError(
            "outcome database schema version is malformed"
        ) from exc
    if version not in {
        OUTCOME_DATABASE_SCHEMA_VERSION,
        PREVIOUS_STORAGE_SCHEMA_VERSION,
        CURRENT_STORAGE_SCHEMA_VERSION,
    } or (
        require_current and version != CURRENT_STORAGE_SCHEMA_VERSION
    ):
        raise OpportunityOutcomeIntegrityError(
            "outcome store requires schema 28, 29, or 30"
            f"{' (30 for writes)' if require_current else ''}, found {version}"
        )
    for table, expected in (
        ("opportunity_outcome_receipts", RECEIPT_COLUMNS),
        ("opportunity_outcome_records", RECORD_COLUMNS),
    ):
        columns = {
            str(item[1])
            for item in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if columns != expected:
            raise OpportunityOutcomeIntegrityError(
                f"outcome store table {table} has an invalid schema"
            )
    objects = {
        str(item[0]): str(item[1])
        for item in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND tbl_name IN ('opportunity_outcome_receipts', "
            "'opportunity_outcome_records')"
        ).fetchall()
    }
    expected_objects = expected_outcome_schema_sql()
    if set(objects) != set(expected_objects):
        raise OpportunityOutcomeIntegrityError(
            "outcome store schema objects are not canonical"
        )
    indexes = {
        str(item[0])
        for item in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
            "AND tbl_name IN ('opportunity_outcome_receipts', "
            "'opportunity_outcome_records')"
        ).fetchall()
    }
    triggers = {
        str(item[0])
        for item in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND sql IS NOT NULL "
            "AND tbl_name IN ('opportunity_outcome_receipts', "
            "'opportunity_outcome_records')"
        ).fetchall()
    }
    if indexes != set(EXPECTED_INDEX_COLUMNS) or triggers != EXPECTED_TRIGGERS:
        raise OpportunityOutcomeIntegrityError(
            "outcome store governed indexes or triggers are not canonical"
        )
    for name, expected_sql in expected_objects.items():
        if _sql_fingerprint(objects[name]) != _sql_fingerprint(expected_sql):
            raise OpportunityOutcomeIntegrityError(
                f"outcome store schema object {name} is missing or invalid"
            )
    _validate_key_structures(connection)
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise OpportunityOutcomeIntegrityError(
            "outcome store requires foreign key enforcement"
        )
    for table in ("opportunity_outcome_receipts", "opportunity_outcome_records"):
        if connection.execute(f"PRAGMA foreign_key_check({table})").fetchall():
            raise OpportunityOutcomeIntegrityError(
                f"outcome store table {table} contains foreign key violations"
            )


def _validate_key_structures(connection: sqlite3.Connection) -> None:
    primary_keys = {
        "opportunity_outcome_receipts": ("outcome_receipt_id",),
        "opportunity_outcome_records": ("outcome_receipt_id", "record_ordinal"),
    }
    unique_keys = {
        "opportunity_outcome_receipts": {
            (("outcome_receipt_id",), False), (("batch_id",), False),
            (("run_id", "outcome_receipt_id"), False),
            (("run_id", "outcome_receipt_id", "receipt_content_hash_sha256"), False),
            (("run_id",), True), (("supersedes_outcome_receipt_id",), True),
        },
        "opportunity_outcome_records": {
            (("outcome_receipt_id", "record_ordinal"), False),
            (("outcome_receipt_id", "evaluation_id", "horizon_id"), False),
            (("outcome_receipt_id", "outcome_id"), False),
            (("run_id", "outcome_receipt_id", "outcome_id",
              "outcome_content_hash_sha256"), False),
            (("run_id", "evaluation_id", "horizon_id"), True),
            (("run_id", "supersedes_outcome_receipt_id", "supersedes_outcome_id",
              "supersedes_outcome_content_hash_sha256"), True),
        },
    }
    for table, expected_pk in primary_keys.items():
        info = connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual_pk = tuple(
            str(item[1])
            for item in sorted(
                (item for item in info if int(item[5]) > 0),
                key=lambda item: int(item[5]),
            )
        )
        if actual_pk != expected_pk:
            raise OpportunityOutcomeIntegrityError(
                f"outcome store primary key for {table} is invalid"
            )
        actual_unique = set()
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if int(index[2]) == 1:
                name = str(index[1])
                columns = tuple(
                    str(item[2])
                    for item in connection.execute(
                        f"PRAGMA index_info({name})"
                    ).fetchall()
                )
                actual_unique.add((columns, bool(index[4])))
        if actual_unique != unique_keys[table]:
            raise OpportunityOutcomeIntegrityError(
                f"outcome store unique constraints for {table} are invalid"
            )
    for name, expected in EXPECTED_INDEX_COLUMNS.items():
        actual = tuple(
            str(item[2])
            for item in connection.execute(f"PRAGMA index_info({name})").fetchall()
        )
        if actual != expected:
            raise OpportunityOutcomeIntegrityError(
                f"outcome store index {name} has invalid columns"
            )
    _validate_foreign_keys(connection)


def _validate_foreign_keys(connection: sqlite3.Connection) -> None:
    expected = {
        "opportunity_outcome_receipts": {
            ("opportunity_pipeline_runs", ("run_id",), ("run_id",)),
            ("opportunity_outcome_receipts",
             ("run_id", "supersedes_outcome_receipt_id",
              "supersedes_outcome_receipt_content_hash_sha256"),
             ("run_id", "outcome_receipt_id", "receipt_content_hash_sha256")),
        },
        "opportunity_outcome_records": {
            ("opportunity_outcome_receipts",
             ("run_id", "outcome_receipt_id"),
             ("run_id", "outcome_receipt_id")),
            ("opportunity_outcome_records",
             ("run_id", "supersedes_outcome_receipt_id", "supersedes_outcome_id",
              "supersedes_outcome_content_hash_sha256"),
             ("run_id", "outcome_receipt_id", "outcome_id",
              "outcome_content_hash_sha256")),
        },
    }
    for table, expected_keys in expected.items():
        groups: dict[int, list[sqlite3.Row]] = {}
        for item in connection.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            groups.setdefault(int(item[0]), []).append(item)
        actual = set()
        for items in groups.values():
            ordered = sorted(items, key=lambda item: int(item[1]))
            if any(
                str(item[5]) != "NO ACTION"
                or str(item[6]) != "NO ACTION"
                or str(item[7]) != "NONE"
                for item in ordered
            ):
                raise OpportunityOutcomeIntegrityError(
                    f"outcome store foreign key actions for {table} are invalid"
                )
            actual.add(
                (
                    str(ordered[0][2]),
                    tuple(str(item[3]) for item in ordered),
                    tuple(str(item[4]) for item in ordered),
                )
            )
        if actual != expected_keys:
            raise OpportunityOutcomeIntegrityError(
                f"outcome store foreign keys for {table} are invalid"
            )


def expected_outcome_schema_sql() -> dict[str, str]:
    """Return exact migration-created SQL for governed outcome objects."""

    with sqlite3.connect(":memory:") as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE opportunity_pipeline_runs (run_id TEXT PRIMARY KEY);
            CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL);
            INSERT INTO schema_version VALUES (27, 'fixture');
            """
        )
        from intraday_scanner.storage.migrations import _migration_028_opportunity_outcomes

        _migration_028_opportunity_outcomes(connection)
        return {
            str(item[0]): str(item[1])
            for item in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL "
                "AND tbl_name IN ('opportunity_outcome_receipts', "
                "'opportunity_outcome_records')"
            ).fetchall()
        }
