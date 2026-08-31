"""Governed, additive pre-activation state preparation.

The one-percent account/capture/trial stores are an additive sidecar over the
legacy schema marker (30).  This command is deliberately separate from normal
scheduled work: it takes an online backup *before* applying the idempotent
initialization, proves the resulting inventory twice, and seals a receipt that
can be consumed by runtime activation.  It never restores or overwrites the
live database automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, run_migrations

# Canonical SQL fragments and receipt argument vectors remain readable as
# literals; their exact text is part of the governed contract.
# ruff: noqa: E501

STATE_PREPARATION_SCHEMA = "dawnstrike.state_preparation_receipt.v1"
TASK_PROOF_SCHEMA = "dawnstrike.state_preparation_task_proof.v1"
STATE_SIDECAR_CONTRACT = "dawnstrike.account_capture_trial_sidecar.v1"
STATE_SIDECAR_VERSION = 1
DB_NAME = "shadow_real.sqlite"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEY_PARTS = ("secret", "password", "credential", "private_key", "token")
_REPARSE_POINT = 0x400

# These are the intentionally additive account/capture/trial stores.  The
# order is part of the portable inventory hash and must not be changed.
SIDECAR_TABLES = (
    "expected_market_sessions",
    "intraday_capture_runs",
    "committed_fill_truth_receipts",
    "no_trade_session_receipts",
    "experiment_trial_ledger",
)
SIDECAR_TRIGGERS = (
    "expected_market_sessions_no_update",
    "expected_market_sessions_no_delete",
    "intraday_capture_runs_no_update",
    "intraday_capture_runs_no_delete",
    "committed_fill_truth_receipts_no_update",
    "committed_fill_truth_receipts_no_delete",
    "no_trade_session_receipts_no_update",
    "no_trade_session_receipts_no_delete",
    "experiment_trial_ledger_no_update",
    "experiment_trial_ledger_no_delete",
)
SIDECAR_COLUMNS = {
    "expected_market_sessions": (
        "session_id",
        "market_date",
        "exchange",
        "session_open_utc",
        "session_close_utc",
        "status",
        "calendar_source",
        "calendar_source_hash_sha256",
        "created_at",
        "research_only",
        "broker_execution_enabled",
        "payload_json",
    ),
    "intraday_capture_runs": (
        "capture_run_id",
        "session_id",
        "market_date",
        "evidence_mode",
        "provider",
        "feed",
        "source_identity",
        "requested_start_utc",
        "requested_end_utc",
        "started_at",
        "completed_at",
        "status",
        "coverage_status",
        "code_sha",
        "source_config_hash_sha256",
        "raw_artifact_hash_sha256",
        "normalized_artifact_hash_sha256",
        "receipt_hash_sha256",
        "payload_json",
        "created_at",
        "research_only",
        "broker_execution_enabled",
    ),
    "committed_fill_truth_receipts": (
        "receipt_id",
        "receipt_hash_sha256",
        "account_id",
        "strategy_id",
        "strategy_version",
        "experiment_id",
        "arm_id",
        "decision_id",
        "selection_id",
        "intent_id",
        "position_id",
        "order_id",
        "side",
        "market_date",
        "execution_status",
        "entry_at",
        "exit_at",
        "quantity",
        "entry_price",
        "exit_price",
        "spread_cost_cents",
        "slippage_cost_cents",
        "fees_cents",
        "regulatory_cost_cents",
        "borrow_cost_cents",
        "source_artifact_hash_sha256",
        "code_sha",
        "frozen_window",
        "payload_json",
        "created_at",
        "research_only",
        "broker_execution_enabled",
    ),
    "no_trade_session_receipts": (
        "receipt_id",
        "receipt_hash_sha256",
        "account_id",
        "strategy_id",
        "strategy_version",
        "experiment_id",
        "arm_id",
        "market_date",
        "session_id",
        "run_id",
        "status",
        "decision",
        "no_entry",
        "source_artifact_hash_sha256",
        "source_config_hash_sha256",
        "calendar_source_hash_sha256",
        "code_sha",
        "payload_json",
        "created_at",
        "research_only",
        "broker_execution_enabled",
    ),
    "experiment_trial_ledger": (
        "trial_id",
        "trial_number",
        "experiment_id",
        "arm_id",
        "strategy_id",
        "strategy_version",
        "configuration_hash_sha256",
        "feature_set_hash_sha256",
        "cost_model_version",
        "validation_window",
        "status",
        "code_sha",
        "source_hash_sha256",
        "attempted_at",
        "payload_json",
        "research_only",
        "broker_execution_enabled",
    ),
}
# The migration is intentionally immutable after publication.  These
# fragments are checked against sqlite_master, while PRAGMA checks below
# prove the actual primary-key, uniqueness, NOT NULL, CHECK, and FK shape.
SIDECAR_TABLE_SQL_FRAGMENTS = {
    "expected_market_sessions": (
        "session_id text primary key",
        "market_date text not null unique",
        "status text not null check (status in ('expected', 'open', 'closed', 'cancelled', 'quarantined'))",
        "research_only integer not null check (research_only = 1)",
        "broker_execution_enabled integer not null check ( broker_execution_enabled = 0 )",
        "payload_json text not null",
    ),
    "intraday_capture_runs": (
        "capture_run_id text primary key",
        "session_id text not null",
        "evidence_mode text not null check (evidence_mode in ('forward_observed', 'retrospective_research'))",
        "status text not null check (status in ('pending', 'running', 'complete', 'partial', 'failed', 'source_conflict', 'quarantined'))",
        "receipt_hash_sha256 text not null unique",
        "research_only integer not null check (research_only = 1)",
        "broker_execution_enabled integer not null check ( broker_execution_enabled = 0 )",
        "foreign key (session_id) references expected_market_sessions(session_id)",
    ),
    "committed_fill_truth_receipts": (
        "receipt_id text primary key",
        "receipt_hash_sha256 text not null unique",
        "account_id text not null",
        "market_date text not null",
        "execution_status text not null check (execution_status in ('pending', 'partial', 'filled', 'rejected', 'cancelled', 'closed', 'quarantined'))",
        "source_artifact_hash_sha256 text not null",
        "code_sha text not null",
        "frozen_window text not null",
        "payload_json text not null",
        "research_only integer not null check (research_only = 1)",
        "broker_execution_enabled integer not null check ( broker_execution_enabled = 0 )",
    ),
    "no_trade_session_receipts": (
        "receipt_id text primary key",
        "receipt_hash_sha256 text not null unique",
        "account_id text not null",
        "market_date text not null",
        "session_id text not null",
        "run_id text not null",
        "status text not null check (status = 'finalized')",
        "decision text not null check (decision = 'no_trade')",
        "no_entry integer not null check (no_entry = 1)",
        "source_artifact_hash_sha256 text not null",
        "payload_json text not null",
        "research_only integer not null check (research_only = 1)",
        "broker_execution_enabled integer not null check ( broker_execution_enabled = 0 )",
    ),
    "experiment_trial_ledger": (
        "trial_id text primary key",
        "trial_number integer not null unique check (trial_number > 0)",
        "experiment_id text not null",
        "arm_id text not null",
        "status text not null check (status in ('attempted', 'complete', 'failed', 'quarantined'))",
        "code_sha text not null",
        "source_hash_sha256 text not null",
        "payload_json text not null",
        "research_only integer not null check (research_only = 1)",
        "broker_execution_enabled integer not null check ( broker_execution_enabled = 0 )",
    ),
}
SIDECAR_INDEXES = {
    "idx_expected_market_sessions_date": ("expected_market_sessions", False, ("market_date", "status")),
    "idx_intraday_capture_runs_session": ("intraday_capture_runs", False, ("market_date", "session_id", "status")),
    "idx_committed_fill_truth_account_day": ("committed_fill_truth_receipts", False, ("account_id", "market_date", "receipt_id")),
    "idx_committed_fill_truth_experiment": ("committed_fill_truth_receipts", False, ("experiment_id", "arm_id", "market_date")),
    "idx_no_trade_session_receipts_identity": ("no_trade_session_receipts", False, ("account_id", "market_date", "session_id", "run_id")),
    "idx_experiment_trial_ledger_experiment": ("experiment_trial_ledger", False, ("experiment_id", "arm_id", "trial_number")),
}
SIDECAR_EXPRESSION_INDEX = "idx_experiment_trial_ledger_attempt"
PAPER_LEDGER_COLUMNS = (
    "target_return_pct",
    "target_status",
    "expected_session_id",
    "experiment_id",
    "arm_id",
    "evidence_mode",
    "lineage_sha256",
    "target_shortfall_pct",
    "target_excess_pct",
    "spread_cost_cents",
    "slippage_cost_cents",
    "fees_cents",
    "regulatory_cost_cents",
    "borrow_cost_cents",
)


class StatePreparationError(ValueError):
    """A state-preparation input, inventory, or receipt is unsafe."""


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _assert_no_reparse_components(path: str | Path) -> Path:
    """Reject symlink/junction components, including descendants of safe roots."""

    resolved = Path(os.path.abspath(Path(path)))
    current = Path(resolved.anchor)
    for component in resolved.parts[1:]:
        current /= component
        if current.exists() and _is_reparse_point(current):
            raise StatePreparationError(f"reparse-point path component is forbidden: {current}")
    return resolved


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def self_hash(payload: Mapping[str, Any], field: str) -> str:
    return hashlib.sha256(
        canonical_json({key: value for key, value in payload.items() if key != field})
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_hash_or_empty(path: Path) -> str:
    return _sha256_file(path) if path.is_file() else hashlib.sha256(b"").hexdigest()


def _reject_sensitive_keys(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise StatePreparationError(f"sensitive field is forbidden at {path}")
            _reject_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, f"{path}[{index}]")


def _safe_database(path: str | Path) -> Path:
    supplied = _assert_no_reparse_components(path)
    if _is_reparse_point(supplied):
        raise StatePreparationError("state database must be a regular file")
    resolved = supplied.resolve()
    _assert_no_reparse_components(resolved)
    if resolved.name != DB_NAME or not resolved.is_file():
        raise StatePreparationError(f"state database must be an existing {DB_NAME} file")
    return resolved


def _safe_state_root(path: str | Path) -> Path:
    root = _assert_no_reparse_components(path).resolve()
    _assert_no_reparse_components(root)
    if not root.is_dir() or _is_reparse_point(root):
        raise StatePreparationError("state root must be an existing regular directory")
    return root


def _safe_backup_root(path: str | Path, state_root: Path) -> Path:
    root = _assert_no_reparse_components(path).resolve()
    _assert_no_reparse_components(root)
    if root == state_root or state_root in root.parents or root in state_root.parents:
        raise StatePreparationError("backup root must be outside and separate from state root")
    if root.exists() and (_is_reparse_point(root) or not root.is_dir()):
        raise StatePreparationError("backup root must be a regular directory")
    return root


def _validate_preparation_lock(path: str | Path, state_root: Path) -> Path:
    lock = _assert_no_reparse_components(path).resolve()
    expected = state_root / "locks" / "dawnstrike-runtime-activation.lock"
    if lock != expected or not lock.is_file() or _is_reparse_point(lock):
        raise StatePreparationError("state preparation lock is missing or unsafe")
    try:
        payload = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatePreparationError("state preparation lock is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != "dawnstrike.runtime_activation_lock.v1"
        or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("lock_token") or ""))
        or payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        raise StatePreparationError("state preparation lock is invalid")
    return lock


def _hashes(db: Path) -> dict[str, str]:
    return {
        "db_sha256": _sha256_file(db),
        "wal_sha256": _file_hash_or_empty(db.with_name(db.name + "-wal")),
        "shm_sha256": _file_hash_or_empty(db.with_name(db.name + "-shm")),
    }


def _connect_rw(db: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _online_snapshot_sha256(connection: sqlite3.Connection) -> str:
    """Hash an SQLite online-backup snapshot without touching StateRoot."""

    descriptor, temporary_name = tempfile.mkstemp(prefix="dawnstrike-state-snapshot-", suffix=".sqlite")
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as destination:
            connection.backup(destination)
            destination.commit()
        return _sha256_file(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def _logical_snapshot_for_path(db: Path) -> str:
    with closing(sqlite3.connect(f"file:{quote(db.as_posix(), safe='/:')}?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only = ON")
        return _online_snapshot_sha256(connection)


def _row_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))


def _normalize_sql(value: str | None) -> str:
    return " ".join((value or "").split()).lower()


def _compact_sql(value: str | None) -> str:
    return re.sub(r"\s+", "", _normalize_sql(value))


def inventory(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return and validate the exact sidecar inventory and invariants."""

    tables = {
        str(row[0]): str(row[1] or "")
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = [name for name in SIDECAR_TABLES if name not in tables]
    if missing_tables:
        raise StatePreparationError("sidecar tables are missing: " + ", ".join(missing_tables))
    missing_columns: dict[str, list[str]] = {}
    for table, expected in SIDECAR_COLUMNS.items():
        actual = set(_row_columns(connection, table))
        missing = [column for column in expected if column not in actual]
        if missing:
            missing_columns[table] = missing
    paper_columns = set(_row_columns(connection, "paper_account_daily_ledger"))
    missing_paper = [column for column in PAPER_LEDGER_COLUMNS if column not in paper_columns]
    if missing_paper:
        missing_columns["paper_account_daily_ledger"] = missing_paper
    if missing_columns:
        raise StatePreparationError(
            "sidecar columns are missing: " + json.dumps(missing_columns, sort_keys=True)
        )

    structural_errors: list[str] = []
    for table, fragments in SIDECAR_TABLE_SQL_FRAGMENTS.items():
        sql = _compact_sql(tables[table])
        structural_errors.extend(
            f"{table}: missing canonical SQL fragment {fragment}"
            for fragment in fragments
            if _compact_sql(fragment) not in sql
        )
    if structural_errors:
        raise StatePreparationError("sidecar table constraints are invalid: " + "; ".join(structural_errors))

    for index_name, (table, unique, columns) in SIDECAR_INDEXES.items():
        row = connection.execute(
            "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
        if row is None or str(row[0]) != table or not row[1]:
            raise StatePreparationError(f"sidecar index is missing or invalid: {index_name}")
        index_rows = connection.execute(f"PRAGMA index_list({table})").fetchall()
        index_meta = next((item for item in index_rows if str(item[1]) == index_name), None)
        if index_meta is None or bool(index_meta[2]) != unique:
            raise StatePreparationError(f"sidecar index uniqueness is invalid: {index_name}")
        actual_index_columns = tuple(
            str(item[2])
            for item in connection.execute(f"PRAGMA index_info({index_name})").fetchall()
        )
        if actual_index_columns != columns:
            raise StatePreparationError(f"sidecar index columns are invalid: {index_name}")
    expression_index = connection.execute(
        "SELECT tbl_name, sql FROM sqlite_master WHERE type = 'index' AND name = ?",
        (SIDECAR_EXPRESSION_INDEX,),
    ).fetchone()
    if (
        expression_index is None
        or str(expression_index[0]) != "experiment_trial_ledger"
        or _compact_sql(str(expression_index[1] or ""))
        != "createuniqueindexidx_experiment_trial_ledger_attemptonexperiment_trial_ledger(json_extract(payload_json,'$.attempt_id'))"
    ):
        raise StatePreparationError("sidecar trial-attempt expression index is invalid")
    foreign_keys = connection.execute("PRAGMA foreign_key_list(intraday_capture_runs)").fetchall()
    if not any(
        str(row[2]) == "expected_market_sessions"
        and str(row[3]) == "session_id"
        and str(row[4]) == "session_id"
        for row in foreign_keys
    ):
        raise StatePreparationError("sidecar capture/session foreign key is invalid")

    trigger_sql: dict[str, str] = {}
    missing_triggers: list[str] = []
    for name in SIDECAR_TRIGGERS:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (name,)
        ).fetchone()
        if row is None:
            missing_triggers.append(name)
            continue
        sql = str(row[0] or "")
        normalized = _normalize_sql(sql)
        expected_trigger = _normalize_sql(
            f"CREATE TRIGGER {name} BEFORE {'UPDATE' if name.endswith('_no_update') else 'DELETE'} ON "
            f"{name.rsplit('_no_', 1)[0]} BEGIN SELECT RAISE(ABORT, "
            f"'{name.rsplit('_no_', 1)[0]} is append-only'); END"
        )
        if normalized != expected_trigger:
            raise StatePreparationError(f"sidecar trigger is not append-only: {name}")
        trigger_sql[name] = sql
    if missing_triggers:
        raise StatePreparationError("sidecar triggers are missing: " + ", ".join(missing_triggers))

    row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
    if row is None or row[0] is None or int(row[0]) != CURRENT_SCHEMA_VERSION:
        raise StatePreparationError("state schema marker is not exactly 30")
    quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if quick_check != "ok":
        raise StatePreparationError("SQLite quick_check is not ok")

    # Hash the expected contract itself, not row counts.  Existing account
    # evidence is intentionally preserved and does not alter the contract.
    table_sql = {name: _normalize_sql(tables[name]) for name in SIDECAR_TABLES}
    compact_table_sql = {name: _compact_sql(sql) for name, sql in tables.items()}
    research_only_checks = all(
        "research_onlyintegernotnullcheck(research_only=1)" in compact_table_sql[name]
        for name in SIDECAR_TABLES
    )
    broker_execution_disabled_checks = all(
        "broker_execution_enabledintegernotnullcheck(broker_execution_enabled=0)"
        in compact_table_sql[name]
        for name in SIDECAR_TABLES
    )
    if not research_only_checks or not broker_execution_disabled_checks:
        raise StatePreparationError("sidecar safety constraints are not proven by sqlite_master")
    contract = {
        "schema_version": STATE_SIDECAR_CONTRACT,
        "sidecar_version": STATE_SIDECAR_VERSION,
        "schema_marker": CURRENT_SCHEMA_VERSION,
        "tables": {
            name: {
                "columns": list(SIDECAR_COLUMNS[name]),
                "sql": table_sql[name],
            }
            for name in SIDECAR_TABLES
        },
        "paper_account_daily_ledger_columns": list(PAPER_LEDGER_COLUMNS),
        "triggers": {name: _normalize_sql(trigger_sql[name]) for name in SIDECAR_TRIGGERS},
        "invariants": {
            "quick_check": quick_check,
            "research_only_checks": research_only_checks,
            "broker_execution_disabled_checks": broker_execution_disabled_checks,
            "append_only_triggers": True,
            "current_schema_version_unchanged": True,
        },
    }
    encoded = canonical_json(contract)
    return {
        "schema_version": STATE_SIDECAR_CONTRACT,
        "sidecar_version": STATE_SIDECAR_VERSION,
        "schema_marker": CURRENT_SCHEMA_VERSION,
        "table_names": list(SIDECAR_TABLES),
        "trigger_names": list(SIDECAR_TRIGGERS),
        "table_columns": {name: list(SIDECAR_COLUMNS[name]) for name in SIDECAR_TABLES},
        "paper_account_daily_ledger_columns": list(PAPER_LEDGER_COLUMNS),
        "invariants": contract["invariants"],
        "inventory_contract_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def inspect_task_proof(
    path: str | Path | None,
    *,
    candidate_sha: str | None = None,
    candidate_tree: str | None = None,
) -> dict[str, Any]:
    if path is None:
        raise StatePreparationError(
            "task proof is required; prepare state before any scheduled task starts"
        )
    source = _assert_no_reparse_components(path)
    if _is_reparse_point(source) or not source.is_file():
        raise StatePreparationError("task proof is missing or unsafe")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatePreparationError("task proof is invalid JSON") from exc
    if not isinstance(value, dict):
        raise StatePreparationError("task proof must be an object")
    _reject_sensitive_keys(value)
    expected = {
        "schema_version",
        "candidate_sha",
        "candidate_tree",
        "task_count",
        "canonical_running_count",
        "canonical_enabled_count",
        "capture_present",
        "capture_running",
        "capture_state",
        "capture_action",
        "capture_xml_sha256",
        "capture_action_contract_sha256",
        "research_only",
        "broker_execution_enabled",
    }
    if set(value) != expected:
        raise StatePreparationError("task proof fields do not match the strict contract")
    if value.get("schema_version") != TASK_PROOF_SCHEMA:
        raise StatePreparationError("task proof schema is invalid")
    for field, expected_value in (("candidate_sha", candidate_sha), ("candidate_tree", candidate_tree)):
        actual = value.get(field)
        if not isinstance(actual, str) or not _GIT_SHA.fullmatch(actual):
            raise StatePreparationError(f"task proof {field} is invalid")
        if expected_value is not None and actual != expected_value:
            raise StatePreparationError(f"task proof {field} does not match the candidate")
    if type(value.get("task_count")) is not int or value.get("task_count") != 5:
        raise StatePreparationError("task proof must preserve exactly five canonical tasks")
    if type(value.get("canonical_running_count")) is not int or value.get("canonical_running_count") != 0:
        raise StatePreparationError("canonical tasks must not be running")
    if type(value.get("canonical_enabled_count")) is not int or value.get("canonical_enabled_count") != 5:
        raise StatePreparationError("task proof must attest all five canonical tasks are enabled and quiesced")
    if type(value.get("capture_present")) is not bool:
        raise StatePreparationError("task proof must explicitly attest auxiliary capture presence")
    if value.get("capture_present") is True:
        if (
            value.get("capture_running") is not False
            or value.get("capture_state") != "Disabled"
            or value.get("capture_action") != "DISABLED_UNTIL_EXACT_SHA_REBIND"
        ):
            raise StatePreparationError("auxiliary capture task must be present and Disabled")
    else:
        if (
            value.get("capture_running") is not False
            or value.get("capture_state") != "ABSENT"
            or value.get("capture_action") != "ABSENT_ALLOWED"
        ):
            raise StatePreparationError("absent auxiliary capture task must be explicitly allowed")
    for field in ("capture_xml_sha256", "capture_action_contract_sha256"):
        if not _SHA256.fullmatch(str(value.get(field) or "")):
            raise StatePreparationError(f"task proof {field} is invalid")
    empty_hash = hashlib.sha256(b"").hexdigest()
    if value["capture_present"] is False and any(
        value[field] != empty_hash
        for field in ("capture_xml_sha256", "capture_action_contract_sha256")
    ):
        raise StatePreparationError("absent auxiliary capture hashes must attest empty values")
    if value.get("research_only") is not True or value.get("broker_execution_enabled") is not False:
        raise StatePreparationError("task proof safety flags are invalid")
    return value


def inspect_live(db_path: str | Path) -> dict[str, Any]:
    """Read live hashes and sidecar inventory without writing the database."""

    db = _safe_database(db_path)
    hashes = _hashes(db)
    with closing(
        sqlite3.connect(f"file:{quote(db.as_posix(), safe='/:')}?mode=ro", uri=True)
    ) as connection:
        connection.execute("PRAGMA query_only = ON")
        current = inventory(connection)
        logical_snapshot = _online_snapshot_sha256(connection)
    return {
        **hashes,
        "inventory_sha256": current["inventory_contract_sha256"],
        "schema_marker": CURRENT_SCHEMA_VERSION,
        "quick_check": "ok",
        "logical_snapshot_sha256": logical_snapshot,
    }


def _load_receipt(path: Path) -> dict[str, Any]:
    path = _assert_no_reparse_components(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StatePreparationError("state-preparation receipt is invalid JSON") from exc
    if not isinstance(value, dict):
        raise StatePreparationError("state-preparation receipt must be an object")
    return value


def validate_receipt(
    payload: Mapping[str, Any],
    *,
    candidate_sha: str | None = None,
    candidate_tree: str | None = None,
) -> dict[str, Any]:
    _reject_sensitive_keys(payload)
    expected = {
        "schema_version",
        "status",
        "contract",
        "candidate_sha",
        "candidate_tree",
        "state_schema_version",
        "before_db_sha256",
        "before_wal_sha256",
        "before_shm_sha256",
        "before_logical_snapshot_sha256",
        "after_db_sha256",
        "after_wal_sha256",
        "after_shm_sha256",
        "after_logical_snapshot_sha256",
        "backup_id",
        "backup_bundle_path",
        "backup_db_sha256",
        "backup_manifest_sha256",
        "backup_manifest_file_sha256",
        "inventory_before_sha256",
        "inventory_after_sha256",
        "inventory_sha256",
        "initialization_idempotent",
        "task_proof_sha256",
        "prepared_at_utc",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "receipt_sha256",
    }
    if set(payload) != expected:
        raise StatePreparationError(
            "state-preparation receipt fields do not match the strict contract"
        )
    if payload.get("receipt_sha256") != self_hash(payload, "receipt_sha256"):
        raise StatePreparationError("state-preparation receipt self-hash mismatch")
    if (
        payload.get("schema_version") != STATE_PREPARATION_SCHEMA
        or payload.get("status") != "COMPLETE"
    ):
        raise StatePreparationError("state-preparation receipt is not COMPLETE")
    if payload.get("contract") != STATE_SIDECAR_CONTRACT:
        raise StatePreparationError("state-preparation sidecar contract mismatch")
    for field in ("candidate_sha", "candidate_tree"):
        if not _GIT_SHA.fullmatch(str(payload.get(field) or "")):
            raise StatePreparationError(f"state-preparation {field} is invalid")
    if candidate_sha is not None and payload.get("candidate_sha") != candidate_sha:
        raise StatePreparationError("state-preparation candidate SHA mismatch")
    if candidate_tree is not None and payload.get("candidate_tree") != candidate_tree:
        raise StatePreparationError("state-preparation candidate tree mismatch")
    if payload.get("state_schema_version") != CURRENT_SCHEMA_VERSION:
        raise StatePreparationError("state-preparation schema marker is incompatible")
    for field in (
        "before_db_sha256",
        "before_wal_sha256",
        "before_shm_sha256",
        "before_logical_snapshot_sha256",
        "after_db_sha256",
        "after_wal_sha256",
        "after_shm_sha256",
        "after_logical_snapshot_sha256",
        "backup_db_sha256",
        "backup_manifest_sha256",
        "backup_manifest_file_sha256",
        "inventory_before_sha256",
        "inventory_after_sha256",
        "inventory_sha256",
        "task_proof_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise StatePreparationError(f"state-preparation {field} is invalid")
    bundle_path = payload.get("backup_bundle_path")
    if not isinstance(bundle_path, str) or not Path(bundle_path).is_absolute():
        raise StatePreparationError("state-preparation backup bundle path is invalid")
    bundle = _assert_no_reparse_components(bundle_path)
    if bundle.name != payload.get("backup_id") or not bundle.is_dir():
        raise StatePreparationError("state-preparation backup bundle path is invalid")
    if payload.get("initialization_idempotent") is not True:
        raise StatePreparationError("state-preparation did not prove idempotence")
    if (
        payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        raise StatePreparationError("state-preparation safety flags are invalid")
    for field in ("prepared_at_utc", "completed_at_utc"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.endswith("Z"):
            raise StatePreparationError("state-preparation timestamps must be UTC")
        try:
            datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise StatePreparationError("state-preparation timestamp is invalid") from exc
    return dict(payload)


def _atomic_json(path: Path, payload: Mapping[str, Any], *, refuse_existing: bool = True) -> None:
    _assert_no_reparse_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(path.parent)
    if refuse_existing and (path.exists() or path.is_symlink()):
        raise StatePreparationError("state-preparation receipt already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _assert_no_reparse_components(temporary)
        temporary.write_bytes(canonical_json(payload))
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if refuse_existing:
            _assert_no_reparse_components(path)
            os.link(temporary, path)
        else:
            _assert_no_reparse_components(path)
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _seal_failure_evidence(
    state: Path,
    candidate_sha: str,
    candidate_tree: str,
    before: Mapping[str, str],
    backup_result: Mapping[str, Any] | None,
    error: BaseException,
) -> None:
    failure_path = (
        state
        / "receipts"
        / "state-preparation"
        / f"state-preparation-{candidate_sha}.failed.json"
    )
    recovery: dict[str, Any] = {
        "schema_version": "dawnstrike.state_preparation_failure.v1",
        "status": "FAILED_BEFORE_COMPLETE",
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "before_db_sha256": before.get("db_sha256"),
        "backup_id": backup_result.get("backup_id") if backup_result else None,
        "backup_db_sha256": backup_result.get("backup_db_sha256") if backup_result else None,
        "backup_manifest_sha256": backup_result.get("manifest_sha256") if backup_result else None,
        "recovery_evidence": (
            "online_backup_restore_verify_required"
            if backup_result
            else "no_complete_online_backup_was_sealed"
        ),
        "error_type": type(error).__name__,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    try:
        _atomic_json(failure_path, recovery)
    except Exception:
        # The caller remains fail-closed even if evidence persistence itself
        # is unavailable; never claim a receipt was sealed in that case.
        pass


def prepare_state(
    db_path: str | Path,
    *,
    state_root: str | Path,
    backup_root: str | Path,
    candidate_sha: str,
    candidate_tree: str,
    task_proof: str | Path | None,
    preparation_lock: str | Path | None = None,
    receipt_path: str | Path | None = None,
    retention: int = 5,
) -> dict[str, Any]:
    """Prepare the sidecar and return an immutable COMPLETE receipt."""

    if not _GIT_SHA.fullmatch(candidate_sha) or not _GIT_SHA.fullmatch(candidate_tree):
        raise StatePreparationError("candidate SHA and tree must be lowercase 40-hex")
    state = _safe_state_root(state_root)
    db = _safe_database(db_path)
    if state not in db.parents:
        raise StatePreparationError("state database must be contained by state root")
    backup = _safe_backup_root(backup_root, state)
    inspect_task_proof(
        task_proof,
        candidate_sha=candidate_sha,
        candidate_tree=candidate_tree,
    )
    proof_path = _assert_no_reparse_components(Path(task_proof)).resolve()
    proof_hash = _sha256_file(proof_path)
    target_receipt = (
        _assert_no_reparse_components(receipt_path)
        if receipt_path is not None
        else _assert_no_reparse_components(
            state / "receipts" / "state-preparation" / f"state-preparation-{candidate_sha}.json"
        )
    )
    target_receipt = target_receipt.resolve()
    if state not in target_receipt.parents:
        raise StatePreparationError("state-preparation receipt must remain inside state root")
    if target_receipt.exists() or target_receipt.is_symlink():
        existing = validate_receipt(
            _load_receipt(target_receipt),
            candidate_sha=candidate_sha,
            candidate_tree=candidate_tree,
        )
        existing_bundle = _assert_no_reparse_components(existing["backup_bundle_path"]).resolve()
        if existing_bundle.parent != backup:
            raise StatePreparationError(
                "existing COMPLETE preparation receipt backup is outside the supplied backup root"
            )
        live = _hashes(db)
        live["logical_snapshot_sha256"] = _logical_snapshot_for_path(db)
        if (
            live["db_sha256"] != existing["after_db_sha256"]
            or live["wal_sha256"] != existing["after_wal_sha256"]
            or live["shm_sha256"] != existing["after_shm_sha256"]
            or live["logical_snapshot_sha256"] != existing["after_logical_snapshot_sha256"]
        ):
            raise StatePreparationError(
                "existing COMPLETE preparation receipt does not match live database hashes"
            )
        with closing(sqlite3.connect(db)) as connection:
            current = inventory(connection)
        if current["inventory_contract_sha256"] != existing["inventory_sha256"]:
            raise StatePreparationError(
                "existing COMPLETE preparation receipt does not match live inventory"
            )
        if existing["task_proof_sha256"] != proof_hash:
            raise StatePreparationError(
                "existing COMPLETE preparation receipt does not match task proof"
            )
        return existing

    locks = state / "locks"
    owned_lock = None
    if preparation_lock is not None:
        owned_lock = _validate_preparation_lock(preparation_lock, state)
    active_locks = (
        [item for item in locks.iterdir() if item.is_file() and item != owned_lock]
        if locks.is_dir()
        else []
    )
    if active_locks:
        raise StatePreparationError("state preparation requires no active locks")

    before = _hashes(db)
    before["logical_snapshot_sha256"] = _logical_snapshot_for_path(db)
    # Import lazily so read-only contract importers do not gain a write path.
    # Runtime invokes this file by absolute path (rather than as a package), so
    # the sibling fallback is required for the real PowerShell entry point.
    try:
        from scripts.state_disaster_recovery import create_backup, restore_verify
    except ModuleNotFoundError as exc:
        if exc.name != "scripts":
            raise
        from state_disaster_recovery import create_backup, restore_verify

    backup_id = f"state-preparation-{candidate_sha[:16]}-{before['db_sha256'][:16]}"
    try:
        backup_result = create_backup(
            db,
            backup,
            state_root=state,
            retention=retention,
            source_sha=candidate_sha,
            backup_id=backup_id,
        )
        backup_bundle = _assert_no_reparse_components(backup / backup_result["backup_id"])
        for backup_file in (DB_NAME, "manifest.json", "receipt.json"):
            _assert_no_reparse_components(backup_bundle / backup_file)
        after_backup = _hashes(db)
        after_backup["logical_snapshot_sha256"] = _logical_snapshot_for_path(db)
        if after_backup != before:
            raise StatePreparationError("state database/WAL changed while creating the online backup")
    except Exception as exc:
        _seal_failure_evidence(
            state, candidate_sha, candidate_tree, before,
            locals().get("backup_result"), exc,
        )
        if isinstance(exc, StatePreparationError):
            raise
        raise StatePreparationError(
            "state preparation backup failed; recovery evidence was retained"
        ) from exc

    prepared_at = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    try:
        with _connect_rw(db) as connection:
            run_migrations(connection)
            connection.commit()
            first = inventory(connection)
            first_hash = first["inventory_contract_sha256"]
            # A second complete initialization proves the operation is truly
            # idempotent rather than merely passing after one DDL application.
            run_migrations(connection)
            connection.commit()
            second = inventory(connection)
            second_hash = second["inventory_contract_sha256"]
            if first_hash != second_hash:
                raise StatePreparationError("state preparation is not idempotent")
        after = _hashes(db)
        after["logical_snapshot_sha256"] = _logical_snapshot_for_path(db)
        with closing(sqlite3.connect(db)) as connection:
            final = inventory(connection)
        if final["inventory_contract_sha256"] != second_hash:
            raise StatePreparationError("live inventory changed after preparation")
        # Verify the backup non-mutating before sealing COMPLETE.  This is
        # recovery evidence, not an automatic overwrite of the live database.
        verified_backup = restore_verify(
            backup_bundle,
            db,
            backup_root=backup,
            state_root=state,
        )
        if verified_backup["backup_db_sha256"] != backup_result["backup_db_sha256"]:
            raise StatePreparationError("online backup hash changed during preparation")
        payload: dict[str, Any] = {
            "schema_version": STATE_PREPARATION_SCHEMA,
            "status": "COMPLETE",
            "contract": STATE_SIDECAR_CONTRACT,
            "candidate_sha": candidate_sha,
            "candidate_tree": candidate_tree,
            "state_schema_version": CURRENT_SCHEMA_VERSION,
            "before_db_sha256": before["db_sha256"],
            "before_wal_sha256": before["wal_sha256"],
            "before_shm_sha256": before["shm_sha256"],
            "before_logical_snapshot_sha256": before["logical_snapshot_sha256"],
            "after_db_sha256": after["db_sha256"],
            "after_wal_sha256": after["wal_sha256"],
            "after_shm_sha256": after["shm_sha256"],
            "after_logical_snapshot_sha256": after["logical_snapshot_sha256"],
            "backup_id": str(backup_result["backup_id"]),
            "backup_bundle_path": str(backup_bundle.resolve()),
            "backup_db_sha256": str(backup_result["backup_db_sha256"]),
            "backup_manifest_sha256": str(backup_result["manifest_sha256"]),
            "backup_manifest_file_sha256": _sha256_file(backup_bundle / "manifest.json"),
            "inventory_before_sha256": first_hash,
            "inventory_after_sha256": second_hash,
            "inventory_sha256": final["inventory_contract_sha256"],
            "initialization_idempotent": True,
            "task_proof_sha256": proof_hash,
            "prepared_at_utc": prepared_at,
            "completed_at_utc": datetime.now(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z"),
            "research_only": True,
            "broker_execution_enabled": False,
        }
        payload["receipt_sha256"] = self_hash(payload, "receipt_sha256")
        validated = validate_receipt(
            payload, candidate_sha=candidate_sha, candidate_tree=candidate_tree
        )
        _atomic_json(target_receipt, validated)
        return validated
    except Exception as exc:
        # The bundle has already been sealed and verified.  Preserve explicit
        # recovery evidence; never guess whether an operator wants overwrite.
        _seal_failure_evidence(
            state, candidate_sha, candidate_tree, before, backup_result, exc
        )
        if isinstance(exc, StatePreparationError):
            raise
        raise StatePreparationError(
            "state preparation failed; recovery evidence was retained"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path")
    parser.add_argument("--state-root")
    parser.add_argument("--backup-root")
    parser.add_argument("--candidate-sha")
    parser.add_argument("--candidate-tree")
    parser.add_argument("--task-proof")
    parser.add_argument("--preparation-lock")
    parser.add_argument("--receipt-path")
    parser.add_argument("--retention", type=int, default=5)
    parser.add_argument("--verify-receipt")
    parser.add_argument("--inspect-live", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.inspect_live:
            if not args.db_path:
                raise StatePreparationError("--db-path is required for --inspect-live")
            result = inspect_live(args.db_path)
        elif args.verify_receipt:
            if not args.candidate_sha or not args.candidate_tree:
                raise StatePreparationError(
                    "--candidate-sha and --candidate-tree are required for --verify-receipt"
                )
            result = validate_receipt(
                _load_receipt(Path(args.verify_receipt)),
                candidate_sha=args.candidate_sha,
                candidate_tree=args.candidate_tree,
            )
        else:
            required = {
                "--state-root": args.state_root,
                "--backup-root": args.backup_root,
                "--candidate-sha": args.candidate_sha,
                "--candidate-tree": args.candidate_tree,
                "--task-proof": args.task_proof,
            }
            missing = [name for name, value in required.items() if not value]
            if not args.db_path or missing:
                raise StatePreparationError(
                    "state preparation arguments are missing: " + ", ".join(missing or ["--db-path"])
                )
            result = prepare_state(
                args.db_path,
                state_root=args.state_root,
                backup_root=args.backup_root,
                candidate_sha=args.candidate_sha,
                candidate_tree=args.candidate_tree,
                task_proof=args.task_proof,
                preparation_lock=args.preparation_lock,
                receipt_path=args.receipt_path,
                retention=args.retention,
            )
    except (OSError, sqlite3.Error, StatePreparationError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
