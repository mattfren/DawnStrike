"""Strict evidence and receipt contracts for governed runtime activation.

This module never changes the active runtime, Task Scheduler, or provider state.
It validates exact-SHA CI/SOL evidence, inspects SQLite read-only, and atomically
seals private activation/rollback receipts.  The Windows swap orchestration
lives in ``activate_dawnstrike_runtime.ps1`` and
``rollback_dawnstrike_runtime.ps1``.
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
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION

CI_SCHEMA = "dawnstrike.runtime_activation_ci_evidence.v1"
SOL_SCHEMA = "dawnstrike.runtime_activation_sol_evidence.v1"
ACTIVATION_SCHEMA = "dawnstrike.runtime_activation_receipt.v1"
ROLLBACK_SCHEMA = "dawnstrike.runtime_rollback_receipt.v1"

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVATION_ID = re.compile(r"^[0-9a-f]{24}$")
_MARKET_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GITHUB_RUN = re.compile(r"^https://github\.com/[^/?#]+/[^/?#]+/actions/runs/[1-9][0-9]*$")
_FORBIDDEN_KEY_PARTS = ("secret", "password", "credential", "private_key", "token")
_MAX_EVIDENCE_AGE = timedelta(days=30)
_REPARSE_POINT = 0x400

_CI_KEYS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "candidate_tree",
        "conclusion",
        "status",
        "head_branch",
        "run_url",
        "checks_total",
        "checks_succeeded",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "evidence_sha256",
    }
)
_SOL_KEYS = frozenset(
    {
        "schema_version",
        "candidate_sha",
        "candidate_tree",
        "auditor_model",
        "verdict",
        "critical_findings",
        "high_findings",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "evidence_sha256",
    }
)
_ACTIVATION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "activation_id",
        "market_date",
        "candidate_sha",
        "candidate_tree",
        "previous_sha",
        "previous_tree",
        "ci_evidence_sha256",
        "sol_evidence_sha256",
        "state_backup_id",
        "state_backup_db_sha256",
        "state_schema_version",
        "state_quick_check",
        "rollback_bundle_sha256",
        "task_count",
        "task_contract_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256",
        "task_paths_unchanged",
        "task_enablement_restored",
        "scheduler_backup_name",
        "scheduler_backup_manifest_sha256",
        "runtime_origin_sha256",
        "swap_contract",
        "stage_name",
        "rollback_checkout_name",
        "rollback_bundle_name",
        "prepared_at_utc",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "receipt_sha256",
    }
)
_ROLLBACK_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "activation_id",
        "market_date",
        "candidate_sha",
        "candidate_tree",
        "previous_sha",
        "previous_tree",
        "restored_sha",
        "ci_evidence_sha256",
        "sol_evidence_sha256",
        "state_backup_id",
        "state_backup_db_sha256",
        "state_schema_version",
        "state_quick_check",
        "rollback_bundle_sha256",
        "task_count",
        "task_contract_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256",
        "task_paths_unchanged",
        "task_enablement_restored",
        "scheduler_backup_name",
        "scheduler_backup_manifest_sha256",
        "runtime_origin_sha256",
        "swap_contract",
        "prepared_at_utc",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "receipt_sha256",
    }
)

# Receipts created for the one-percent sidecar carry an explicit state
# preparation proof and an auxiliary delayed-SIP task disposition.  Keep the
# historical key sets above accepted for older runtimes that do not declare
# the sidecar contract; activation itself requires the extended set whenever
# the candidate declaration is present.
_EXTENDED_RECEIPT_KEYS = frozenset(
    {
        "state_preparation_required",
        "state_preparation_contract",
        "state_preparation_receipt_sha256",
        "state_preparation_after_db_sha256",
        "state_preparation_after_wal_sha256",
        "state_preparation_after_shm_sha256",
        "state_preparation_after_logical_snapshot_sha256",
        "state_preparation_inventory_sha256",
        "state_preparation_backup_id",
        "state_preparation_backup_bundle_path",
        "state_preparation_backup_db_sha256",
        "state_preparation_backup_manifest_sha256",
        "state_preparation_backup_manifest_file_sha256",
        "state_backup_bundle_path",
        "state_backup_logical_snapshot_sha256",
        "state_backup_source_logical_snapshot_sha256",
        "state_backup_manifest_sha256",
        "auxiliary_capture_present",
        "auxiliary_capture_state_before",
        "auxiliary_capture_state_after",
        "auxiliary_capture_action",
        "auxiliary_capture_xml_sha256",
        "auxiliary_capture_xml_file_sha256",
        "auxiliary_capture_definition_contract_sha256",
        "auxiliary_capture_action_contract_sha256",
        "auxiliary_capture_backup_name",
        "auxiliary_capture_backup_manifest_sha256",
    }
)
_ACTIVATION_RECEIPT_KEYS_EXTENDED = _ACTIVATION_RECEIPT_KEYS | _EXTENDED_RECEIPT_KEYS
_ROLLBACK_RECEIPT_KEYS_EXTENDED = _ROLLBACK_RECEIPT_KEYS | _EXTENDED_RECEIPT_KEYS
_STATE_PREPARATION_DECLARATION_KEYS = frozenset(
    {
        "schema_version",
        "sidecar_contract",
        "sidecar_version",
        "legacy_schema_marker",
        "required_before_activation",
        "research_only",
        "broker_execution_enabled",
    }
)


class ActivationContractError(ValueError):
    """A supplied activation artifact is invalid or unsafe."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def self_hash(payload: Mapping[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != field}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _assert_no_reparse_components(path: str | Path) -> Path:
    """Reject links/junctions in the complete path before any I/O boundary."""

    absolute = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if (os.path.lexists(current) or current.is_symlink()) and _is_reparse_point(current):
            raise ActivationContractError(
                f"reparse-point path component is forbidden: {current}"
            )
    return absolute


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ActivationContractError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def seal_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a canonical self-hashed evidence object after strict validation."""

    sealed = dict(payload)
    sealed["evidence_sha256"] = self_hash(sealed, "evidence_sha256")
    validate_evidence(sealed, now=None, enforce_age=False)
    return sealed


def validate_evidence(
    payload: Mapping[str, Any],
    *,
    now: datetime | None = None,
    enforce_age: bool = True,
) -> dict[str, Any]:
    """Validate one CI or independent SOL evidence object."""

    _reject_sensitive_keys(payload)
    schema = payload.get("schema_version")
    if schema == CI_SCHEMA:
        _require_exact_keys(payload, _CI_KEYS, "CI evidence")
        _validate_common_evidence(payload, now=now, enforce_age=enforce_age)
        if payload.get("conclusion") != "SUCCESS" or payload.get("status") != "COMPLETED":
            raise ActivationContractError("CI evidence is not a completed success")
        if payload.get("head_branch") != "main":
            raise ActivationContractError("CI evidence is not bound to main")
        run_url = payload.get("run_url")
        if not isinstance(run_url, str) or not _GITHUB_RUN.fullmatch(run_url):
            raise ActivationContractError("CI run URL is invalid")
        total = payload.get("checks_total")
        succeeded = payload.get("checks_succeeded")
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or total != 19
            or not isinstance(succeeded, int)
            or isinstance(succeeded, bool)
            or succeeded != total
        ):
            raise ActivationContractError("CI check totals do not prove complete success")
    elif schema == SOL_SCHEMA:
        _require_exact_keys(payload, _SOL_KEYS, "SOL evidence")
        _validate_common_evidence(payload, now=now, enforce_age=enforce_age)
        if payload.get("auditor_model") != "gpt-5.6-sol":
            raise ActivationContractError("SOL evidence uses an unapproved auditor model")
        if payload.get("verdict") != "ZERO_CRITICAL_HIGH":
            raise ActivationContractError("SOL evidence verdict is not release-acceptable")
        if payload.get("critical_findings") != 0 or payload.get("high_findings") != 0:
            raise ActivationContractError("SOL evidence contains critical or high findings")
    else:
        raise ActivationContractError("unsupported activation evidence schema")
    return dict(payload)


def validate_evidence_pair(
    ci_path: str | Path,
    sol_path: str | Path,
    *,
    candidate_sha: str,
    candidate_tree: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate CI and SOL evidence against one exact commit and tree."""

    if not _GIT_SHA.fullmatch(candidate_sha):
        raise ActivationContractError("candidate SHA must be lowercase 40-hex")
    if not _GIT_SHA.fullmatch(candidate_tree):
        raise ActivationContractError("candidate tree must be lowercase 40-hex")
    ci = validate_evidence(_load_object(ci_path), now=now)
    sol = validate_evidence(_load_object(sol_path), now=now)
    if ci.get("schema_version") != CI_SCHEMA or sol.get("schema_version") != SOL_SCHEMA:
        raise ActivationContractError("both CI and SOL evidence are required")
    for label, value in (("CI", ci), ("SOL", sol)):
        if value.get("candidate_sha") != candidate_sha:
            raise ActivationContractError(f"{label} evidence candidate SHA mismatch")
        if value.get("candidate_tree") != candidate_tree:
            raise ActivationContractError(f"{label} evidence candidate tree mismatch")
    return {
        "status": "PASS",
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "ci_evidence_sha256": ci["evidence_sha256"],
        "sol_evidence_sha256": sol["evidence_sha256"],
        "research_only": True,
        "broker_execution_enabled": False,
    }


def inspect_state(db_path: str | Path) -> dict[str, Any]:
    """Inspect the durable database without creating or migrating it."""

    path = _assert_no_reparse_components(db_path)
    try:
        before = path.lstat()
    except OSError as exc:
        raise ActivationContractError("durable state database is missing or unsafe") from exc
    if _is_reparse_point(path) or path.name != "shadow_real.sqlite" or not path.is_file():
        raise ActivationContractError("durable state database is missing or unsafe")
    uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
        try:
            connection.execute("PRAGMA query_only = ON")
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if table is None:
                raise ActivationContractError("Dawnstrike schema_version table is missing")
            row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
            if row is None or row[0] is None:
                raise ActivationContractError("Dawnstrike schema_version table is empty")
            schema_version = int(row[0])
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ActivationContractError("SQLite state inspection failed") from exc
    if quick_check != "ok":
        raise ActivationContractError("SQLite quick_check is not ok")
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise ActivationContractError(
            "durable state schema does not exactly match the candidate runtime"
        )
    _assert_no_reparse_components(path)
    try:
        after = path.lstat()
    except OSError as exc:
        raise ActivationContractError("durable state database changed during inspection") from exc
    if (
        _is_reparse_point(path)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
    ):
        raise ActivationContractError("durable state database changed during inspection")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _assert_no_reparse_components(path)
    try:
        final = path.lstat()
    except OSError as exc:
        raise ActivationContractError("durable state database changed during inspection") from exc
    if (
        _is_reparse_point(path)
        or final.st_size != before.st_size
        or final.st_mtime_ns != before.st_mtime_ns
    ):
        raise ActivationContractError("durable state database changed during inspection")
    return {
        "status": "PASS",
        "database_name": path.name,
        "main_file_sha256": digest,
        "main_file_hash_semantics": "observational_main_database_only_wal_may_be_pending",
        "schema_version": schema_version,
        "candidate_schema_version": CURRENT_SCHEMA_VERSION,
        "quick_check": quick_check,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def seal_receipt(payload: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    """Validate and atomically write one activation or rollback receipt."""

    sealed = dict(payload)
    sealed["receipt_sha256"] = self_hash(sealed, "receipt_sha256")
    validate_receipt(sealed)
    _atomic_write(Path(output_path), sealed)
    return sealed


def validate_receipt(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a self-hashed private activation/rollback receipt."""

    _reject_sensitive_keys(payload)
    schema = payload.get("schema_version")
    if schema not in {ACTIVATION_SCHEMA, ROLLBACK_SCHEMA}:
        raise ActivationContractError("unsupported runtime receipt schema")
    extended = "state_preparation_contract" in payload
    expected_keys = (
        _ACTIVATION_RECEIPT_KEYS_EXTENDED
        if schema == ACTIVATION_SCHEMA and extended
        else _ROLLBACK_RECEIPT_KEYS_EXTENDED
        if schema == ROLLBACK_SCHEMA and extended
        else _ACTIVATION_RECEIPT_KEYS
        if schema == ACTIVATION_SCHEMA
        else _ROLLBACK_RECEIPT_KEYS
    )
    _require_exact_keys(payload, expected_keys, "runtime receipt")
    if payload.get("receipt_sha256") != self_hash(payload, "receipt_sha256"):
        raise ActivationContractError("runtime receipt self-hash mismatch")
    if not _ACTIVATION_ID.fullmatch(str(payload.get("activation_id") or "")):
        raise ActivationContractError("runtime receipt activation id is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_sha") or "")):
        raise ActivationContractError("runtime receipt candidate SHA is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_tree") or "")):
        raise ActivationContractError("runtime receipt candidate tree is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("previous_sha") or "")):
        raise ActivationContractError("runtime receipt previous SHA is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("previous_tree") or "")):
        raise ActivationContractError("runtime receipt previous tree is invalid")
    market_date = str(payload.get("market_date") or "")
    if not _MARKET_DATE.fullmatch(market_date):
        raise ActivationContractError("runtime receipt market date is invalid")
    try:
        if date.fromisoformat(market_date).isoformat() != market_date:
            raise ValueError
    except ValueError as exc:
        raise ActivationContractError("runtime receipt market date is invalid") from exc
    for field in (
        "ci_evidence_sha256",
        "sol_evidence_sha256",
        "state_backup_db_sha256",
        "rollback_bundle_sha256",
        "task_contract_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256",
        "runtime_origin_sha256",
        "scheduler_backup_manifest_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise ActivationContractError(f"runtime receipt {field} is invalid")
    if payload.get("state_quick_check") != "ok":
        raise ActivationContractError("runtime receipt state quick_check is invalid")
    schema_version = payload.get("state_schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != CURRENT_SCHEMA_VERSION
    ):
        raise ActivationContractError("runtime receipt state schema is incompatible")
    if payload.get("task_count") != 5:
        raise ActivationContractError("runtime receipt task count is invalid")
    if payload.get("task_paths_unchanged") is not True:
        raise ActivationContractError("runtime receipt does not preserve task paths")
    if payload.get("research_only") is not True:
        raise ActivationContractError("runtime receipt is not research-only")
    if payload.get("broker_execution_enabled") is not False:
        raise ActivationContractError("runtime receipt enables broker execution")
    if extended:
        _validate_extended_receipt(payload)
    backup_id = payload.get("state_backup_id")
    expected_backup_id = "runtime-activation-" + str(payload.get("activation_id"))
    if not isinstance(backup_id, str) or backup_id != expected_backup_id:
        raise ActivationContractError("runtime receipt backup id is invalid")
    scheduler_backup_name = payload.get("scheduler_backup_name")
    if not isinstance(scheduler_backup_name, str) or not re.fullmatch(
        r"runtime-(?:activation|rollback)-[0-9a-f]{24}", scheduler_backup_name
    ):
        raise ActivationContractError("runtime receipt scheduler backup name is invalid")
    activation_id = str(payload.get("activation_id"))
    if not scheduler_backup_name.endswith("-" + activation_id):
        raise ActivationContractError("runtime receipt scheduler backup id is invalid")
    if schema == ACTIVATION_SCHEMA and scheduler_backup_name != (
        "runtime-activation-" + activation_id
    ):
        raise ActivationContractError("activation scheduler backup name is invalid")
    prepared_at = _parse_utc(payload.get("prepared_at_utc"))
    completed_at = payload.get("completed_at_utc")
    if schema == ACTIVATION_SCHEMA:
        if payload.get("status") not in {"PREPARED", "COMPLETE"}:
            raise ActivationContractError("activation receipt status is invalid")
        if payload.get("swap_contract") != "same_volume_two_rename_with_immediate_restore":
            raise ActivationContractError("activation swap contract is invalid")
        if payload.get("stage_name") != (
            "dawnstrike-runtime.stage-" + str(payload.get("activation_id"))
        ):
            raise ActivationContractError("activation stage name is invalid")
        if payload.get("rollback_checkout_name") != "previous-runtime":
            raise ActivationContractError("activation rollback checkout name is invalid")
        if payload.get("rollback_bundle_name") != "previous-runtime.bundle":
            raise ActivationContractError("activation rollback bundle name is invalid")
        if payload.get("status") == "PREPARED" and completed_at is not None:
            raise ActivationContractError("prepared activation receipt has a completion time")
        if (
            payload.get("status") == "PREPARED"
            and payload.get("task_enablement_restored") is not False
        ):
            raise ActivationContractError("prepared activation receipt has invalid task state")
        if payload.get("status") == "COMPLETE":
            if _parse_utc(completed_at) < prepared_at:
                raise ActivationContractError("activation completion predates preparation")
            if payload.get("task_enablement_restored") is not True:
                raise ActivationContractError("complete activation did not restore task enablement")
    else:
        if payload.get("status") != "ROLLED_BACK":
            raise ActivationContractError("rollback receipt status is invalid")
        if payload.get("restored_sha") != payload.get("previous_sha"):
            raise ActivationContractError("rollback receipt restored SHA mismatch")
        if payload.get("swap_contract") != "same_volume_two_rename_with_immediate_restore":
            raise ActivationContractError("rollback swap contract is invalid")
        if payload.get("task_enablement_restored") is not True:
            raise ActivationContractError("rollback did not restore task enablement")
        if _parse_utc(completed_at) < prepared_at:
            raise ActivationContractError("rollback completion predates preparation")
    return dict(payload)


def _validate_extended_receipt(payload: Mapping[str, Any]) -> None:
    """Validate the sidecar and auxiliary-task portion of a runtime receipt."""

    if payload.get("state_preparation_required") is not True:
        raise ActivationContractError("sidecar runtime receipt does not require state preparation")
    if payload.get("state_preparation_contract") != ("dawnstrike.account_capture_trial_sidecar.v1"):
        raise ActivationContractError("runtime state-preparation contract is invalid")
    for field in (
        "state_preparation_receipt_sha256",
        "state_preparation_after_db_sha256",
        "state_preparation_after_wal_sha256",
        "state_preparation_after_shm_sha256",
        "state_preparation_after_logical_snapshot_sha256",
        "state_preparation_inventory_sha256",
        "auxiliary_capture_xml_sha256",
        "auxiliary_capture_xml_file_sha256",
        "auxiliary_capture_definition_contract_sha256",
        "auxiliary_capture_action_contract_sha256",
        "auxiliary_capture_backup_manifest_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise ActivationContractError(f"runtime receipt {field} is invalid")
    for field in (
        "state_preparation_backup_db_sha256",
        "state_preparation_backup_manifest_sha256",
        "state_preparation_backup_manifest_file_sha256",
        "state_backup_logical_snapshot_sha256",
        "state_backup_source_logical_snapshot_sha256",
        "state_backup_manifest_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise ActivationContractError(f"runtime receipt {field} is invalid")
    backup_id = payload.get("state_preparation_backup_id")
    backup_path = payload.get("state_preparation_backup_bundle_path")
    if (
        not isinstance(backup_id, str)
        or not re.fullmatch(r"state-preparation-[0-9a-f]{16}-[0-9a-f]{16}", backup_id)
        or not isinstance(backup_path, str)
        or not Path(backup_path).is_absolute()
        or Path(backup_path).name != backup_id
    ):
        raise ActivationContractError(
            "runtime receipt state-preparation backup identity is invalid"
        )
    direct_backup_id = payload.get("state_backup_id")
    direct_backup_path = payload.get("state_backup_bundle_path")
    if (
        not isinstance(direct_backup_id, str)
        or not re.fullmatch(r"runtime-activation-[0-9a-f]{24}", direct_backup_id)
        or not isinstance(direct_backup_path, str)
        or not Path(direct_backup_path).is_absolute()
        or Path(direct_backup_path).name != direct_backup_id
    ):
        raise ActivationContractError("runtime receipt durable-state backup identity is invalid")
    if payload.get("auxiliary_capture_present") not in {True, False}:
        raise ActivationContractError("runtime receipt auxiliary capture presence is invalid")
    before = payload.get("auxiliary_capture_state_before")
    after = payload.get("auxiliary_capture_state_after")
    action = payload.get("auxiliary_capture_action")
    if payload.get("auxiliary_capture_present") is True:
        if before not in {"Ready", "Disabled"}:
            raise ActivationContractError("runtime receipt auxiliary capture state is invalid")
        if payload.get("schema_version") == ACTIVATION_SCHEMA:
            if after != "Disabled" or action != "DISABLED_UNTIL_EXACT_SHA_REBIND":
                raise ActivationContractError("activation auxiliary capture disposition is invalid")
        elif after not in {"Ready", "Disabled"} or action != "RESTORED_EXACT":
            raise ActivationContractError("rollback auxiliary capture disposition is invalid")
        backup_name = payload.get("auxiliary_capture_backup_name")
        if not isinstance(backup_name, str) or not re.fullmatch(
            r"runtime-(?:activation|rollback)-[0-9a-f]{24}", backup_name
        ):
            raise ActivationContractError("runtime receipt auxiliary backup name is invalid")
    else:
        if before != "ABSENT" or after != "ABSENT" or action != "ABSENT_ALLOWED":
            raise ActivationContractError(
                "runtime receipt has an inconsistent absent auxiliary task"
            )
        if payload.get("auxiliary_capture_backup_name") != "NONE":
            raise ActivationContractError("absent auxiliary task must not have a backup name")


def load_receipt(path: str | Path) -> dict[str, Any]:
    return validate_receipt(_load_object(path))


def _validate_common_evidence(
    payload: Mapping[str, Any],
    *,
    now: datetime | None,
    enforce_age: bool,
) -> None:
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_sha") or "")):
        raise ActivationContractError("evidence candidate SHA is invalid")
    if not _GIT_SHA.fullmatch(str(payload.get("candidate_tree") or "")):
        raise ActivationContractError("evidence candidate tree is invalid")
    if payload.get("research_only") is not True:
        raise ActivationContractError("activation evidence is not research-only")
    if payload.get("broker_execution_enabled") is not False:
        raise ActivationContractError("activation evidence enables broker execution")
    if payload.get("evidence_sha256") != self_hash(payload, "evidence_sha256"):
        raise ActivationContractError("activation evidence self-hash mismatch")
    completed = _parse_utc(payload.get("completed_at_utc"))
    if enforce_age:
        reference = (now or datetime.now(UTC)).astimezone(UTC)
        if completed > reference + timedelta(minutes=5):
            raise ActivationContractError("activation evidence completion is in the future")
        if reference - completed > _MAX_EVIDENCE_AGE:
            raise ActivationContractError("activation evidence is older than 30 days")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ActivationContractError("evidence completion must be RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ActivationContractError("evidence completion is invalid") from exc
    return parsed.astimezone(UTC)


def _require_exact_keys(payload: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(payload)
    if actual != expected:
        raise ActivationContractError(f"{label} fields do not match the strict contract")


def validate_state_preparation_declaration(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the candidate's exact additive sidecar declaration.

    PowerShell's ``ConvertFrom-Json`` keeps the last duplicate property.  The
    activation boundary therefore validates the raw JSON through this strict
    Python loader before PowerShell consumes any fields.
    """

    _require_exact_keys(
        payload,
        _STATE_PREPARATION_DECLARATION_KEYS,
        "state preparation declaration",
    )
    if (
        payload.get("schema_version") != "dawnstrike.state_preparation_contract.v1"
        or payload.get("sidecar_contract")
        != "dawnstrike.account_capture_trial_sidecar.v1"
        or type(payload.get("sidecar_version")) is not int
        or payload.get("sidecar_version") != 1
        or type(payload.get("legacy_schema_marker")) is not int
        or payload.get("legacy_schema_marker") != 30
        or type(payload.get("required_before_activation")) is not bool
        or payload.get("required_before_activation") is not True
        or type(payload.get("research_only")) is not bool
        or payload.get("research_only") is not True
        or type(payload.get("broker_execution_enabled")) is not bool
        or payload.get("broker_execution_enabled") is not False
    ):
        raise ActivationContractError(
            "state preparation declaration violates the sidecar contract"
        )
    return dict(payload)


def _reject_sensitive_keys(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                raise ActivationContractError(f"sensitive field is forbidden at {path}")
            _reject_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, f"{path}[{index}]")


def _load_object(path: str | Path) -> dict[str, Any]:
    source = _assert_no_reparse_components(path)
    try:
        before = source.lstat()
    except OSError as exc:
        raise ActivationContractError("activation JSON input is missing or unsafe") from exc
    if _is_reparse_point(source) or not source.is_file():
        raise ActivationContractError("activation JSON input is missing or unsafe")
    try:
        raw = source.read_bytes()
        _assert_no_reparse_components(source)
        after = source.lstat()
        if (
            _is_reparse_point(source)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise ActivationContractError("activation JSON input changed during read")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except ActivationContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ActivationContractError("activation JSON input is invalid") from exc
    if not isinstance(value, dict):
        raise ActivationContractError("activation JSON input must be an object")
    return value


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path = _assert_no_reparse_components(path)
    _assert_no_reparse_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(path.parent)
    if os.path.lexists(path):
        raise ActivationContractError("activation output already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        _assert_no_reparse_components(temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_reparse_components(path.parent)
        _assert_no_reparse_components(path)
        os.link(temporary, path)
        _assert_no_reparse_components(path)
    finally:
        temporary.unlink(missing_ok=True)


def _json_summary(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    evidence = subparsers.add_parser("validate-evidence")
    evidence.add_argument("--ci", required=True)
    evidence.add_argument("--sol", required=True)
    evidence.add_argument("--candidate-sha", required=True)
    evidence.add_argument("--candidate-tree", required=True)

    evidence_seal = subparsers.add_parser("seal-evidence")
    evidence_seal.add_argument("--input", required=True)
    evidence_seal.add_argument("--output", required=True)

    state = subparsers.add_parser("inspect-state")
    state.add_argument("--db-path", required=True)

    seal = subparsers.add_parser("seal-receipt")
    seal.add_argument("--input", required=True)
    seal.add_argument("--output", required=True)

    verify = subparsers.add_parser("verify-receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--expected-status", choices=("PREPARED", "COMPLETE", "ROLLED_BACK"))

    declaration = subparsers.add_parser("validate-state-preparation-declaration")
    declaration.add_argument("--input", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate-evidence":
            result = validate_evidence_pair(
                args.ci,
                args.sol,
                candidate_sha=args.candidate_sha,
                candidate_tree=args.candidate_tree,
            )
        elif args.command == "seal-evidence":
            result = seal_evidence(_load_object(args.input))
            _atomic_write(Path(args.output), result)
        elif args.command == "inspect-state":
            result = inspect_state(args.db_path)
        elif args.command == "seal-receipt":
            result = seal_receipt(_load_object(args.input), args.output)
        elif args.command == "validate-state-preparation-declaration":
            result = validate_state_preparation_declaration(_load_object(args.input))
        else:
            result = load_receipt(args.receipt)
            if args.expected_status and result.get("status") != args.expected_status:
                raise ActivationContractError("runtime receipt status mismatch")
    except (ActivationContractError, OSError) as exc:
        print(_json_summary({"status": "FAIL", "error": str(exc)}))
        return 2
    print(_json_summary(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
