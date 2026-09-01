"""Strict receipt contract for delayed-SIP task hardening."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA = "dawnstrike.capture_task_hardening_receipt.v1"
CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED = "dawnstrike.capture_task_hardening_receipt.v2"
CAPTURE_TASK_HARDENING_PREPARED_SCHEMA = "dawnstrike.capture_task_hardening_prepared.v2"
CAPTURE_TASK_NAME = "Dawnstrike Delayed SIP Capture"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.xml$")
_FORBIDDEN_KEY_PARTS = ("secret", "password", "credential", "private_key", "token")
_REPARSE_POINT = 0x400


class CaptureTaskHardeningContractError(ValueError):
    """The hardening receipt is unsafe or ambiguous."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def self_hash(payload: Mapping[str, Any], field: str) -> str:
    return hashlib.sha256(
        canonical_json({key: value for key, value in payload.items() if key != field})
    ).hexdigest()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _assert_no_reparse_components(path: str | Path) -> Path:
    absolute = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if (os.path.lexists(current) or current.is_symlink()) and _is_reparse_point(current):
            raise CaptureTaskHardeningContractError(
                f"reparse-point path component is forbidden: {current}"
            )
    return absolute


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CaptureTaskHardeningContractError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def _load_json_object(path: str | Path) -> dict[str, Any]:
    supplied = _assert_no_reparse_components(path)
    try:
        item = supplied.lstat()
    except OSError as exc:
        raise CaptureTaskHardeningContractError("hardening receipt is missing or unsafe") from exc
    if _is_reparse_point(supplied) or not supplied.is_file():
        raise CaptureTaskHardeningContractError("hardening receipt is missing or unsafe")
    try:
        raw = supplied.read_bytes()
        _assert_no_reparse_components(supplied)
        after = supplied.lstat()
        if (
            after.st_size != item.st_size
            or after.st_mtime_ns != item.st_mtime_ns
            or _is_reparse_point(supplied)
        ):
            raise CaptureTaskHardeningContractError("hardening receipt changed during read")
        if len(raw) > 1_048_576:
            raise CaptureTaskHardeningContractError("contract JSON exceeds the bounded size")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except CaptureTaskHardeningContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureTaskHardeningContractError("hardening receipt is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CaptureTaskHardeningContractError("hardening receipt must be an object")
    return value


def _reject_sensitive_keys(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if any(part in str(key).lower() for part in _FORBIDDEN_KEY_PARTS):
                raise CaptureTaskHardeningContractError(f"sensitive field is forbidden at {path}")
            _reject_sensitive_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_keys(item, f"{path}[{index}]")


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
        "task_name",
        "task_path",
        "candidate_sha",
        "candidate_tree",
        "original_state",
        "final_state",
        "backup_name",
        "backup_relative_path",
        "prepared_relative_path",
        "backup_xml_sha256",
        "backup_xml_file_sha256",
        "xml_before_sha256",
        "xml_after_sha256",
        "action_sha256",
        "trigger_sha256",
        "principal_before_sha256",
        "principal_after_sha256",
        "settings_before_sha256",
        "settings_after_sha256",
        "prepared_record_sha256",
        "origin_main_refreshed_at_utc",
        "origin_url",
        "origin_url_sha256",
        "old_last_task_result",
        "old_last_run_time",
        "new_last_task_result",
        "new_last_run_time",
        "history_reset_proven",
        "changed_fields",
        "preserved_action",
        "preserved_trigger",
        "preserved_input_bindings",
        "logon_type",
        "network_capable",
        "start_when_available",
        "wake_to_run",
        "battery_safe",
        "restart_count",
        "restart_interval",
        "execution_time_limit",
        "multiple_instances",
        "rollback_contract",
        "research_only",
        "broker_execution_enabled",
        "completed_at_utc",
        "receipt_sha256",
    }
    attested = {
        "receipt_relative_path",
        "interpreter_path",
        "interpreter_sha256",
        "interpreter_version",
        "interpreter_signer_subject",
        "interpreter_signer_thumbprint",
        "runner_path",
        "runner_before_sha256",
        "runner_sha256",
        "action_bindings",
        "previous_candidate_sha",
        "action_before_sha256",
        "action_after_sha256",
        "action_migrated",
        "history_evidence_preserved",
        "history_disposition",
        "input_stage",
    }
    schema = payload.get("schema_version")
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED:
        if payload.get("input_stage") not in {"LEGACY_MIGRATION", "CANONICAL_REPIN"}:
            raise CaptureTaskHardeningContractError("hardening input stage is invalid")
        expected |= attested
    if set(payload) != expected:
        raise CaptureTaskHardeningContractError(
            "hardening receipt fields do not match the strict contract"
        )
    if payload.get("receipt_sha256") != self_hash(payload, "receipt_sha256"):
        raise CaptureTaskHardeningContractError("hardening receipt self-hash mismatch")
    if (
        schema
        not in {
            CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA,
            CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED,
        }
        or payload.get("status") != "COMPLETE"
        or payload.get("task_name") != CAPTURE_TASK_NAME
        or payload.get("task_path") != "\\"
    ):
        raise CaptureTaskHardeningContractError("hardening receipt identity is invalid")
    for field in ("candidate_sha", "candidate_tree"):
        if not re.fullmatch(r"^[0-9a-f]{40}$", str(payload.get(field) or "")):
            raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
    if candidate_sha is not None and payload.get("candidate_sha") != candidate_sha:
        raise CaptureTaskHardeningContractError("hardening candidate SHA mismatch")
    if candidate_tree is not None and payload.get("candidate_tree") != candidate_tree:
        raise CaptureTaskHardeningContractError("hardening candidate tree mismatch")
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED:
        if not re.fullmatch(r"^[0-9a-f]{40}$", str(payload.get("previous_candidate_sha") or "")):
            raise CaptureTaskHardeningContractError("hardening previous candidate SHA is invalid")
        for field in ("action_before_sha256", "action_after_sha256"):
            if not _SHA256.fullmatch(str(payload.get(field) or "")):
                raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
    if payload.get("original_state") not in {"Ready", "Disabled"}:
        raise CaptureTaskHardeningContractError("hardening receipt original state is invalid")
    if payload.get("final_state") != "Disabled":
        raise CaptureTaskHardeningContractError(
            "replacement must remain Disabled until exact rebind"
        )
    if not isinstance(payload.get("backup_name"), str) or not _SAFE_NAME.fullmatch(
        payload["backup_name"]
    ):
        raise CaptureTaskHardeningContractError("hardening backup name is invalid")
    for field in ("backup_relative_path", "prepared_relative_path"):
        value = payload.get(field)
        if (
            not isinstance(value, str)
            or value.startswith(("/", "\\"))
            or ":" in value
            or any(part in {"", ".", ".."} for part in re.split(r"[\\/]", value))
        ):
            raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
    for field in (
        "backup_xml_sha256",
        "backup_xml_file_sha256",
        "xml_before_sha256",
        "xml_after_sha256",
        "action_sha256",
        "trigger_sha256",
        "principal_before_sha256",
        "principal_after_sha256",
        "settings_before_sha256",
        "settings_after_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
    if not _SHA256.fullmatch(str(payload.get("prepared_record_sha256") or "")):
        raise CaptureTaskHardeningContractError("hardening prepared record is invalid")
    refreshed = payload.get("origin_main_refreshed_at_utc")
    if not isinstance(refreshed, str) or not refreshed.endswith("Z"):
        raise CaptureTaskHardeningContractError("origin/main refresh timestamp is invalid")
    try:
        datetime.fromisoformat(refreshed[:-1] + "+00:00")
    except ValueError as exc:
        raise CaptureTaskHardeningContractError("origin/main refresh timestamp is invalid") from exc
    origin = payload.get("origin_url")
    if (
        not isinstance(origin, str)
        or not origin.strip()
        or "\n" in origin
        or "\r" in origin
        or re.search(r"(gh[pousr]_|oauth|password|access[_-]?token|private[_-]?key)", origin, re.I)
        or "?" in origin
        or "#" in origin
        or re.search(r"^https?://[^/]*@", origin, re.I)
    ):
        raise CaptureTaskHardeningContractError("origin URL is invalid")
    if not _SHA256.fullmatch(str(payload.get("origin_url_sha256") or "")):
        raise CaptureTaskHardeningContractError("origin URL hash is invalid")
    if payload["origin_url_sha256"] != hashlib.sha256(origin.encode("utf-8")).hexdigest():
        raise CaptureTaskHardeningContractError("origin URL hash does not match")
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED and origin not in {
        "https://github.com/mattfren/DawnStrike.git",
        "git@github.com:mattfren/DawnStrike.git",
    }:
        raise CaptureTaskHardeningContractError("origin URL is not the approved canonical origin")
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED:
        relative = payload.get("receipt_relative_path")
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith(("/", "\\"))
            or ":" in relative
            or any(part in {"", ".", ".."} for part in re.split(r"[\\/]", relative))
            or not relative.lower().endswith(".json")
        ):
            raise CaptureTaskHardeningContractError("hardening receipt relative path is invalid")
        if (
            relative.replace("\\", "/")
            != f"receipts/capture-task/capture-task-hardening-{payload['candidate_sha']}.json"
        ):
            raise CaptureTaskHardeningContractError(
                "hardening receipt path is not the candidate-bound path"
            )
        for field in ("interpreter_sha256", "runner_before_sha256", "runner_sha256"):
            if not _SHA256.fullmatch(str(payload.get(field) or "")):
                raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
        for field in ("interpreter_path", "runner_path"):
            value = payload.get(field)
            if (
                not isinstance(value, str)
                or not os.path.isabs(value)
                or "\n" in value
                or "\r" in value
            ):
                raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
        if not isinstance(payload.get("interpreter_version"), str) or not re.fullmatch(
            r"3\.13\.\d+", payload["interpreter_version"]
        ):
            raise CaptureTaskHardeningContractError("hardening interpreter version is invalid")
        if payload.get("interpreter_signer_subject") != (
            "CN=Python Software Foundation, O=Python Software Foundation, "
            "L=Beaverton, S=Oregon, C=US"
        ) or not re.fullmatch(
            r"[0-9A-F]{40}", str(payload.get("interpreter_signer_thumbprint") or "")
        ):
            raise CaptureTaskHardeningContractError(
                "hardening interpreter signer identity is invalid"
            )
        bindings = payload.get("action_bindings")
        if not isinstance(bindings, dict) or set(bindings) != {
            "candidate_sha",
            "bytecode_prefix",
            "runner_path",
            "runner_sha256",
            "symbols_manifest_path",
            "symbols_manifest_sha256",
            "entitlement_receipt_path",
            "entitlement_receipt_sha256",
            "source_config_path",
            "source_config_sha256",
        }:
            raise CaptureTaskHardeningContractError("hardening action bindings are not exact")
        if (
            bindings.get("candidate_sha") != payload.get("candidate_sha")
            or not isinstance(bindings.get("bytecode_prefix"), str)
            or bindings.get("runner_path") != payload.get("runner_path")
            or bindings.get("runner_sha256") != payload.get("runner_sha256")
        ):
            raise CaptureTaskHardeningContractError(
                "hardening action bindings do not match identity"
            )
        for field in (
            "symbols_manifest_sha256",
            "entitlement_receipt_sha256",
            "source_config_sha256",
        ):
            if not _SHA256.fullmatch(str(bindings.get(field) or "")):
                raise CaptureTaskHardeningContractError("hardening action input hash is invalid")
        for field in ("symbols_manifest_path", "entitlement_receipt_path", "source_config_path"):
            if not isinstance(bindings.get(field), str) or not os.path.isabs(bindings[field]):
                raise CaptureTaskHardeningContractError("hardening action input path is invalid")
        prefix = bindings["bytecode_prefix"]
        if not os.path.isabs(prefix) or "\n" in prefix or "\r" in prefix:
            raise CaptureTaskHardeningContractError("hardening bytecode prefix path is invalid")
        if not re.search(rf"[\\/]capture-bytecode[\\/]{payload['candidate_sha']}$", prefix, re.I):
            raise CaptureTaskHardeningContractError(
                "hardening bytecode prefix is not candidate-bound"
            )
    for field in ("old_last_task_result", "new_last_task_result"):
        if not isinstance(payload.get(field), int) or isinstance(payload.get(field), bool):
            raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA and payload.get(
        "new_last_task_result"
    ) not in {0, 267011}:
        raise CaptureTaskHardeningContractError(
            "replacement task must have a fresh acceptable initial result"
        )
    for field in ("old_last_run_time", "new_last_run_time"):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.endswith("Z"):
                raise CaptureTaskHardeningContractError(f"hardening {field} must be UTC or null")
            try:
                datetime.fromisoformat(value[:-1] + "+00:00")
            except ValueError as exc:
                raise CaptureTaskHardeningContractError(f"hardening {field} is invalid") from exc
    if not isinstance(payload.get("history_reset_proven"), bool):
        raise CaptureTaskHardeningContractError("hardening history reset evidence is invalid")
    if (
        schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA
        and payload.get("history_reset_proven") is not True
    ):
        raise CaptureTaskHardeningContractError("hardening history reset is unproven")
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED:
        action_changed = payload.get("action_before_sha256") != payload.get("action_after_sha256")
        if payload.get("action_migrated") is not action_changed or payload.get(
            "preserved_action"
        ) is not (not action_changed):
            raise CaptureTaskHardeningContractError("hardening action migration is invalid")
        if payload.get("history_evidence_preserved") is not True or payload.get(
            "history_disposition"
        ) not in {"PRESERVED", "RESET_AS_UPDATE_SIDE_EFFECT"}:
            raise CaptureTaskHardeningContractError("hardening history evidence is invalid")
        if payload.get("history_disposition") == "PRESERVED":
            if (
                payload.get("new_last_task_result") != payload.get("old_last_task_result")
                or payload.get("new_last_run_time") != payload.get("old_last_run_time")
                or payload.get("history_reset_proven") is not False
            ):
                raise CaptureTaskHardeningContractError(
                    "preserved scheduler history does not match the receipt"
                )
        else:
            if (
                payload.get("new_last_task_result") not in {0, 267011}
                or payload.get("new_last_run_time") is not None
                or payload.get("history_reset_proven") is not True
            ):
                raise CaptureTaskHardeningContractError(
                    "scheduler update-side-effect history is invalid"
                )
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED:
        expected_changed_fields = []
        if payload.get("principal_before_sha256") != payload.get("principal_after_sha256"):
            expected_changed_fields.append("principal")
        if payload.get("settings_before_sha256") != payload.get("settings_after_sha256"):
            expected_changed_fields.append("settings")
        if payload.get("action_before_sha256") != payload.get("action_after_sha256"):
            expected_changed_fields.append("action")
    else:
        expected_changed_fields = ["principal", "settings"]
    if payload.get("changed_fields") != expected_changed_fields:
        raise CaptureTaskHardeningContractError("hardening changed-field contract is invalid")
    if (
        payload.get("preserved_trigger") is not True
        or payload.get("preserved_input_bindings") is not True
    ):
        raise CaptureTaskHardeningContractError("hardening trigger/input preservation is invalid")
    if (
        schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA
        and payload.get("preserved_action") is not True
    ):
        raise CaptureTaskHardeningContractError("hardening action preservation is invalid")
    if schema == CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA_ATTESTED:
        action_changed = payload.get("action_before_sha256") != payload.get("action_after_sha256")
        if payload.get("preserved_action") is not (not action_changed):
            raise CaptureTaskHardeningContractError(
                "attested hardening action migration is invalid"
            )
    if payload.get("logon_type") != "Password" or payload.get("network_capable") is not True:
        raise CaptureTaskHardeningContractError("hardening logon contract is invalid")
    if payload.get("start_when_available") is not True or payload.get("wake_to_run") is not True:
        raise CaptureTaskHardeningContractError("hardening availability contract is invalid")
    if payload.get("battery_safe") is not True:
        raise CaptureTaskHardeningContractError("hardening battery contract is invalid")
    if payload.get("restart_count") != 3 or payload.get("restart_interval") != "PT15M":
        raise CaptureTaskHardeningContractError("hardening restart contract is invalid")
    if payload.get("execution_time_limit") != "PT3H":
        raise CaptureTaskHardeningContractError("hardening execution-limit contract is invalid")
    if payload.get("multiple_instances") != "IgnoreNew":
        raise CaptureTaskHardeningContractError("hardening multiple-instance contract is invalid")
    if payload.get("rollback_contract") != "RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED":
        raise CaptureTaskHardeningContractError("hardening rollback contract is invalid")
    if (
        payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        raise CaptureTaskHardeningContractError("hardening safety flags are invalid")
    completed = payload.get("completed_at_utc")
    if not isinstance(completed, str) or not completed.endswith("Z"):
        raise CaptureTaskHardeningContractError("hardening timestamp must be UTC")
    try:
        datetime.fromisoformat(completed[:-1] + "+00:00")
    except ValueError as exc:
        raise CaptureTaskHardeningContractError("hardening timestamp is invalid") from exc
    return dict(payload)


_PREPARED_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "task_name",
        "task_path",
        "candidate_sha",
        "candidate_tree",
        "original_state",
        "backup_path",
        "backup_xml_sha256",
        "backup_xml_file_sha256",
        "xml_before_sha256",
        "xml_after_sha256",
        "action_sha256",
        "trigger_sha256",
        "principal_sha256",
        "settings_sha256",
        "action_before_sha256",
        "action_after_sha256",
        "runtime_head",
        "runtime_tree",
        "runtime_origin",
        "runtime_origin_sha256",
        "lock_token",
        "lock_bytes_sha256",
        "interpreter_path",
        "interpreter_sha256",
        "interpreter_version",
        "interpreter_signer_subject",
        "interpreter_signer_thumbprint",
        "runner_before_sha256",
        "runner_target_sha256",
        "old_last_task_result",
        "old_last_run_time",
        "intended_receipt_path",
        "rollback_contract",
        "research_only",
        "broker_execution_enabled",
        "prepared_at_utc",
        "input_stage",
        "prepared_record_sha256",
    }
)


def validate_prepared(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) != _PREPARED_KEYS:
        raise CaptureTaskHardeningContractError("prepared fields do not match the strict contract")
    if payload.get("prepared_record_sha256") != self_hash(payload, "prepared_record_sha256"):
        raise CaptureTaskHardeningContractError("prepared self-hash mismatch")
    if (
        payload.get("schema_version") != CAPTURE_TASK_HARDENING_PREPARED_SCHEMA
        or payload.get("status") != "PREPARED"
    ):
        raise CaptureTaskHardeningContractError("prepared identity is invalid")
    if payload.get("task_name") != CAPTURE_TASK_NAME or payload.get("task_path") != "\\":
        raise CaptureTaskHardeningContractError("prepared task identity is invalid")
    for field in ("candidate_sha", "candidate_tree", "runtime_head", "runtime_tree"):
        if not re.fullmatch(r"^[0-9a-f]{40}$", str(payload.get(field) or "")):
            raise CaptureTaskHardeningContractError(f"prepared {field} is invalid")
    for field in (
        "backup_xml_sha256",
        "backup_xml_file_sha256",
        "xml_before_sha256",
        "xml_after_sha256",
        "action_sha256",
        "trigger_sha256",
        "principal_sha256",
        "settings_sha256",
        "action_before_sha256",
        "action_after_sha256",
        "runtime_origin_sha256",
        "lock_bytes_sha256",
        "interpreter_sha256",
        "runner_before_sha256",
        "runner_target_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise CaptureTaskHardeningContractError(f"prepared {field} is invalid")
    if not re.fullmatch(r"^[0-9a-f]{32}$", str(payload.get("lock_token") or "")):
        raise CaptureTaskHardeningContractError("prepared lock token is invalid")
    if payload.get("original_state") not in {"Ready", "Disabled"}:
        raise CaptureTaskHardeningContractError("prepared original state is invalid")
    if payload.get("input_stage") not in {"LEGACY_MIGRATION", "CANONICAL_REPIN"}:
        raise CaptureTaskHardeningContractError("prepared input stage is invalid")
    backup_path = payload.get("backup_path")
    if (
        not isinstance(backup_path, str)
        or not backup_path
        or not re.match(r"^[A-Za-z]:[\\/].*scheduler-backups[\\/].+$", backup_path, re.I)
    ):
        raise CaptureTaskHardeningContractError("prepared backup path is invalid")
    intended_receipt_path = payload.get("intended_receipt_path")
    if not isinstance(intended_receipt_path, str) or not re.match(
        rf"^[A-Za-z]:[\\/].*[\\/]receipts[\\/]capture-task[\\/]capture-task-hardening-{payload['candidate_sha']}\.json$",
        intended_receipt_path,
        re.I,
    ):
        raise CaptureTaskHardeningContractError("prepared receipt path is not candidate-bound")
    if (
        payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        raise CaptureTaskHardeningContractError("prepared safety flags are invalid")
    origin = payload.get("runtime_origin")
    if origin not in {
        "https://github.com/mattfren/DawnStrike.git",
        "git@github.com:mattfren/DawnStrike.git",
    }:
        raise CaptureTaskHardeningContractError("prepared runtime origin is invalid")
    if payload.get("runtime_origin_sha256") != hashlib.sha256(origin.encode("utf-8")).hexdigest():
        raise CaptureTaskHardeningContractError("prepared runtime origin hash does not match")
    if not isinstance(payload.get("interpreter_path"), str) or not payload[
        "interpreter_path"
    ].lower().endswith("\\python.exe"):
        raise CaptureTaskHardeningContractError("prepared interpreter path is invalid")
    if not re.fullmatch(r"^3\.13\.\d+$", str(payload.get("interpreter_version") or "")):
        raise CaptureTaskHardeningContractError("prepared interpreter version is invalid")
    if payload.get("interpreter_signer_subject") != (
        "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US"
    ):
        raise CaptureTaskHardeningContractError("prepared interpreter signer subject is invalid")
    if not re.fullmatch(r"^[0-9A-F]{40}$", str(payload.get("interpreter_signer_thumbprint") or "")):
        raise CaptureTaskHardeningContractError("prepared interpreter signer thumbprint is invalid")
    if payload.get("rollback_contract") != "RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED":
        raise CaptureTaskHardeningContractError("prepared rollback contract is invalid")
    timestamp = payload.get("prepared_at_utc")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise CaptureTaskHardeningContractError("prepared timestamp is invalid")
    try:
        datetime.fromisoformat(timestamp[:-1] + "+00:00")
    except ValueError as exc:
        raise CaptureTaskHardeningContractError("prepared timestamp is invalid") from exc
    return dict(payload)


def load_prepared(path: str | Path) -> dict[str, Any]:
    return validate_prepared(_load_json_object(path))


def reseal_prepared_lock(path: str | Path, token: str, raw_sha256: str) -> dict[str, Any]:
    value = load_prepared(path)
    if not re.fullmatch(r"^[0-9a-f]{32}$", token) or not _SHA256.fullmatch(raw_sha256):
        raise CaptureTaskHardeningContractError("replacement lock identity is invalid")
    value["lock_token"] = token
    value["lock_bytes_sha256"] = raw_sha256
    value["prepared_record_sha256"] = self_hash(value, "prepared_record_sha256")
    validated = validate_prepared(value)
    destination = _assert_no_reparse_components(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(validated))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return validated


def seal_receipt(payload: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    value = dict(payload)
    value["receipt_sha256"] = self_hash(value, "receipt_sha256")
    validated = validate_receipt(value)
    path = _assert_no_reparse_components(output_path)
    _assert_no_reparse_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(path.parent)
    if os.path.lexists(path):
        existing = validate_receipt(_load_json_object(path))
        if existing != validated:
            raise CaptureTaskHardeningContractError(
                "hardening receipt already exists with different content"
            )
        return existing
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(validated))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_reparse_components(path.parent)
        _assert_no_reparse_components(path)
        os.link(temporary, path)
        _assert_no_reparse_components(path)
    finally:
        temporary.unlink(missing_ok=True)
    return validated


def seal_prepared(payload: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    value = dict(payload)
    value["prepared_record_sha256"] = self_hash(value, "prepared_record_sha256")
    validated = validate_prepared(value)
    path = _assert_no_reparse_components(output_path)
    _assert_no_reparse_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(path.parent)
    if os.path.lexists(path):
        existing = load_prepared(path)
        if existing != validated:
            raise CaptureTaskHardeningContractError(
                "prepared record already exists with different content"
            )
        return existing
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(validated))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_reparse_components(path.parent)
        os.link(temporary, path)
        _assert_no_reparse_components(path)
    finally:
        temporary.unlink(missing_ok=True)
    return validated


def load_receipt(
    path: str | Path,
    *,
    candidate_sha: str | None = None,
    candidate_tree: str | None = None,
) -> dict[str, Any]:
    return validate_receipt(
        _load_json_object(path), candidate_sha=candidate_sha, candidate_tree=candidate_tree
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("seal-hardening")
    seal.add_argument("--input", required=True)
    seal.add_argument("--output", required=True)
    verify = sub.add_parser("verify-hardening")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--candidate-sha")
    verify.add_argument("--candidate-tree")
    prepared = sub.add_parser("seal-prepared")
    prepared.add_argument("--input", required=True)
    prepared.add_argument("--output", required=True)
    verify_prepared = sub.add_parser("verify-prepared")
    verify_prepared.add_argument("--prepared", required=True)
    reseal = sub.add_parser("reseal-prepared-lock")
    reseal.add_argument("--prepared", required=True)
    reseal.add_argument("--lock-token", required=True)
    reseal.add_argument("--lock-bytes-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "seal-hardening":
            result = seal_receipt(_load_json_object(args.input), args.output)
        elif args.command == "seal-prepared":
            result = seal_prepared(_load_json_object(args.input), args.output)
        elif args.command == "verify-prepared":
            result = load_prepared(args.prepared)
        elif args.command == "reseal-prepared-lock":
            result = reseal_prepared_lock(args.prepared, args.lock_token, args.lock_bytes_sha256)
        else:
            result = load_receipt(
                args.receipt,
                candidate_sha=args.candidate_sha,
                candidate_tree=args.candidate_tree,
            )
    except (OSError, CaptureTaskHardeningContractError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
