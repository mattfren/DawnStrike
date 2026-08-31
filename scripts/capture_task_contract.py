"""Strict receipt contract for the governed delayed-SIP task rebind."""

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

CAPTURE_TASK_RECEIPT_SCHEMA = "dawnstrike.capture_task_rebind_receipt.v1"
CAPTURE_TASK_PREPARED_SCHEMA = "dawnstrike.capture_task_rebind_prepared.v1"
CAPTURE_TASK_NAME = "Dawnstrike Delayed SIP Capture"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ACTIVATION_ID = re.compile(r"^[0-9a-f]{24}$")
_FORBIDDEN_KEY_PARTS = ("secret", "password", "credential", "private_key", "token")
_REPARSE_POINT = 0x400


class CaptureTaskContractError(ValueError):
    """The task or rebind receipt is unsafe or ambiguous."""


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
        # ``exists`` is false for a broken link.  ``lexists`` plus the direct
        # symlink check keeps a hostile/broken reparse component from being
        # mistaken for a missing ordinary path.
        if (os.path.lexists(current) or current.is_symlink()) and _is_reparse_point(current):
            raise CaptureTaskContractError(
                f"reparse-point path component is forbidden: {current}"
            )
    return absolute


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys instead of silently accepting last-write-wins."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CaptureTaskContractError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def _load_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    supplied = _assert_no_reparse_components(path)
    try:
        item = supplied.lstat()
    except OSError as exc:
        raise CaptureTaskContractError(f"{label} is missing or unsafe") from exc
    if _is_reparse_point(supplied) or not supplied.is_file():
        raise CaptureTaskContractError(f"{label} is missing or unsafe")
    try:
        raw = supplied.read_bytes()
        # Re-check both the file and every parent after the read.  A junction
        # swap between the preflight and the read must fail closed even on
        # platforms where the first lstat looked ordinary.
        _assert_no_reparse_components(supplied)
        after = supplied.lstat()
        if (
            after.st_size != item.st_size
            or after.st_mtime_ns != item.st_mtime_ns
            or _is_reparse_point(supplied)
        ):
            raise CaptureTaskContractError(f"{label} changed during read")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except CaptureTaskContractError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureTaskContractError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CaptureTaskContractError(f"{label} must be an object")
    return value


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def self_hash(payload: Mapping[str, Any], field: str) -> str:
    return hashlib.sha256(
        canonical_json({key: value for key, value in payload.items() if key != field})
    ).hexdigest()


def _reject_sensitive_keys(value: object, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if any(part in str(key).lower() for part in _FORBIDDEN_KEY_PARTS):
                raise CaptureTaskContractError(f"sensitive field is forbidden at {path}")
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
        "candidate_sha",
        "candidate_tree",
        "activation_id",
        "activation_receipt_name",
        "activation_receipt_sha256",
        "runtime_origin_sha256",
        "previous_candidate_sha",
        "xml_before_sha256",
        "xml_after_sha256",
        "action_before_sha256",
        "action_after_sha256",
        "definition_before_sha256",
        "definition_after_sha256",
        "principal_sha256",
        "trigger_sha256",
        "settings_sha256",
        "symbols_manifest_sha256",
        "entitlement_receipt_sha256",
        "source_config_sha256",
        "enablement_before",
        "enablement_after",
        "changed_field",
        "preserved_contract",
        "completed_at_utc",
        "research_only",
        "broker_execution_enabled",
        "receipt_sha256",
    }
    if set(payload) != expected:
        raise CaptureTaskContractError(
            "capture-task receipt fields do not match the strict contract"
        )
    if payload.get("receipt_sha256") != self_hash(payload, "receipt_sha256"):
        raise CaptureTaskContractError("capture-task receipt self-hash mismatch")
    if (
        payload.get("schema_version") != CAPTURE_TASK_RECEIPT_SCHEMA
        or payload.get("status") != "COMPLETE"
    ):
        raise CaptureTaskContractError("capture-task receipt is not COMPLETE")
    if payload.get("task_name") != CAPTURE_TASK_NAME:
        raise CaptureTaskContractError("capture-task receipt task name is invalid")
    for field in ("candidate_sha", "candidate_tree", "previous_candidate_sha"):
        if not _GIT_SHA.fullmatch(str(payload.get(field) or "")):
            raise CaptureTaskContractError(f"capture-task {field} is invalid")
    if not _ACTIVATION_ID.fullmatch(str(payload.get("activation_id") or "")):
        raise CaptureTaskContractError("capture-task activation id is invalid")
    if payload.get("activation_receipt_name") != (
        "runtime-activation-" + str(payload.get("activation_id")) + ".json"
    ):
        raise CaptureTaskContractError("capture-task activation receipt name is invalid")
    if candidate_sha is not None and payload.get("candidate_sha") != candidate_sha:
        raise CaptureTaskContractError("capture-task candidate SHA mismatch")
    if candidate_tree is not None and payload.get("candidate_tree") != candidate_tree:
        raise CaptureTaskContractError("capture-task candidate tree mismatch")
    for field in (
        "runtime_origin_sha256",
        "activation_receipt_sha256",
        "xml_before_sha256",
        "xml_after_sha256",
        "action_before_sha256",
        "action_after_sha256",
        "definition_before_sha256",
        "definition_after_sha256",
        "principal_sha256",
        "trigger_sha256",
        "settings_sha256",
        "symbols_manifest_sha256",
        "entitlement_receipt_sha256",
        "source_config_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise CaptureTaskContractError(f"capture-task {field} is invalid")
    if payload.get("enablement_before") != "Disabled" or payload.get("enablement_after") != "Ready":
        raise CaptureTaskContractError(
            "capture task was not disabled before rebind and Ready after rebind"
        )
    if payload.get("changed_field") not in {
        "candidate_sha",
        "candidate_sha_and_input_bindings",
    }:
        raise CaptureTaskContractError("capture-task rebind changed more than candidate SHA")
    if payload.get("preserved_contract") is not True:
        raise CaptureTaskContractError(
            "capture-task principal/triggers/settings were not preserved"
        )
    if (
        payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        raise CaptureTaskContractError("capture-task receipt safety flags are invalid")
    completed = payload.get("completed_at_utc")
    if not isinstance(completed, str) or not completed.endswith("Z"):
        raise CaptureTaskContractError("capture-task completion timestamp must be UTC")
    try:
        datetime.fromisoformat(completed[:-1] + "+00:00")
    except ValueError as exc:
        raise CaptureTaskContractError("capture-task completion timestamp is invalid") from exc
    return dict(payload)


def validate_prepared(
    payload: Mapping[str, Any],
    *,
    candidate_sha: str | None = None,
    candidate_tree: str | None = None,
) -> dict[str, Any]:
    """Validate the durable PREPARED capture-rebind recovery record."""

    _reject_sensitive_keys(payload)
    expected = {
        "schema_version",
        "status",
        "task_name",
        "candidate_sha",
        "candidate_tree",
        "activation_id",
        "activation_receipt_name",
        "activation_receipt_sha256",
        "previous_candidate_sha",
        "xml_before_sha256",
        "action_before_sha256",
        "definition_before_sha256",
        "normalized_definition_before_sha256",
        "principal_sha256",
        "trigger_sha256",
        "settings_sha256",
        "symbols_manifest_path",
        "symbols_manifest_sha256",
        "entitlement_receipt_path",
        "entitlement_receipt_sha256",
        "source_config_path",
        "source_config_sha256",
        "enablement_before",
        "compensation",
        "prepared_at_utc",
        "research_only",
        "broker_execution_enabled",
        "prepared_sha256",
    }
    if set(payload) != expected:
        raise CaptureTaskContractError(
            "capture-task PREPARED fields do not match the strict contract"
        )
    if payload.get("prepared_sha256") != self_hash(payload, "prepared_sha256"):
        raise CaptureTaskContractError("capture-task PREPARED self-hash mismatch")
    if (
        payload.get("schema_version") != CAPTURE_TASK_PREPARED_SCHEMA
        or payload.get("status") != "PREPARED"
        or payload.get("task_name") != CAPTURE_TASK_NAME
    ):
        raise CaptureTaskContractError("capture-task PREPARED record is invalid")
    for field in ("candidate_sha", "candidate_tree", "previous_candidate_sha"):
        if not _GIT_SHA.fullmatch(str(payload.get(field) or "")):
            raise CaptureTaskContractError(f"capture-task PREPARED {field} is invalid")
    if candidate_sha is not None and payload.get("candidate_sha") != candidate_sha:
        raise CaptureTaskContractError("capture-task PREPARED candidate SHA mismatch")
    if candidate_tree is not None and payload.get("candidate_tree") != candidate_tree:
        raise CaptureTaskContractError("capture-task PREPARED candidate tree mismatch")
    if not _ACTIVATION_ID.fullmatch(str(payload.get("activation_id") or "")):
        raise CaptureTaskContractError("capture-task PREPARED activation id is invalid")
    if payload.get("activation_receipt_name") != (
        "runtime-activation-" + str(payload.get("activation_id")) + ".json"
    ):
        raise CaptureTaskContractError("capture-task PREPARED activation receipt name is invalid")
    for field in (
        "activation_receipt_sha256",
        "xml_before_sha256",
        "action_before_sha256",
        "definition_before_sha256",
        "normalized_definition_before_sha256",
        "principal_sha256",
        "trigger_sha256",
        "settings_sha256",
        "symbols_manifest_sha256",
        "entitlement_receipt_sha256",
        "source_config_sha256",
    ):
        if not _SHA256.fullmatch(str(payload.get(field) or "")):
            raise CaptureTaskContractError(f"capture-task PREPARED {field} is invalid")
    for field in (
        "symbols_manifest_path",
        "entitlement_receipt_path",
        "source_config_path",
    ):
        value = payload.get(field)
        if not isinstance(value, str) or not os.path.isabs(value):
            raise CaptureTaskContractError(f"capture-task PREPARED {field} is invalid")
    if payload.get("enablement_before") != "Disabled":
        raise CaptureTaskContractError("capture-task PREPARED boundary is not Disabled")
    if payload.get("compensation") != "RESTORE_EXACT_XML_AND_DISABLED":
        raise CaptureTaskContractError("capture-task PREPARED compensation is invalid")
    completed = payload.get("prepared_at_utc")
    if not isinstance(completed, str) or not completed.endswith("Z"):
        raise CaptureTaskContractError("capture-task PREPARED timestamp must be UTC")
    try:
        datetime.fromisoformat(completed[:-1] + "+00:00")
    except ValueError as exc:
        raise CaptureTaskContractError("capture-task PREPARED timestamp is invalid") from exc
    if (
        payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        raise CaptureTaskContractError("capture-task PREPARED safety flags are invalid")
    return dict(payload)


def seal_receipt(payload: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    value = dict(payload)
    value["receipt_sha256"] = self_hash(value, "receipt_sha256")
    validated = validate_receipt(value)
    path = _assert_no_reparse_components(output_path)
    _assert_no_reparse_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(path.parent)
    if os.path.lexists(path):
        raise CaptureTaskContractError("capture-task receipt already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.close(descriptor)
        _assert_no_reparse_components(temporary)
        with temporary.open("wb") as handle:
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
    """Self-hash, validate, and atomically write a PREPARED recovery record."""

    value = dict(payload)
    value["prepared_sha256"] = self_hash(value, "prepared_sha256")
    validated = validate_prepared(value)
    path = _assert_no_reparse_components(output_path)
    _assert_no_reparse_components(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(path.parent)
    if os.path.lexists(path):
        raise CaptureTaskContractError("capture-task PREPARED record already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        _assert_no_reparse_components(temporary)
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


def load_receipt(
    path: str | Path, *, candidate_sha: str | None = None, candidate_tree: str | None = None
) -> dict[str, Any]:
    value = _load_json_object(path, label="capture-task receipt")
    return validate_receipt(value, candidate_sha=candidate_sha, candidate_tree=candidate_tree)


def load_prepared(
    path: str | Path, *, candidate_sha: str | None = None, candidate_tree: str | None = None
) -> dict[str, Any]:
    value = _load_json_object(path, label="capture-task PREPARED record")
    return validate_prepared(value, candidate_sha=candidate_sha, candidate_tree=candidate_tree)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("seal-receipt")
    seal.add_argument("--input", required=True)
    seal.add_argument("--output", required=True)
    prepared = sub.add_parser("seal-prepared")
    prepared.add_argument("--input", required=True)
    prepared.add_argument("--output", required=True)
    verify = sub.add_parser("verify-receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--candidate-sha")
    verify.add_argument("--candidate-tree")
    verify_prepared = sub.add_parser("verify-prepared")
    verify_prepared.add_argument("--prepared", required=True)
    verify_prepared.add_argument("--candidate-sha")
    verify_prepared.add_argument("--candidate-tree")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "seal-receipt":
            value = _load_json_object(args.input, label="capture-task receipt input")
            result = seal_receipt(value, args.output)
        elif args.command == "seal-prepared":
            value = _load_json_object(args.input, label="capture-task PREPARED input")
            result = seal_prepared(value, args.output)
        elif args.command == "verify-receipt":
            result = load_receipt(
                args.receipt, candidate_sha=args.candidate_sha, candidate_tree=args.candidate_tree
            )
        else:
            result = load_prepared(
                args.prepared, candidate_sha=args.candidate_sha, candidate_tree=args.candidate_tree
            )
    except (OSError, CaptureTaskContractError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
