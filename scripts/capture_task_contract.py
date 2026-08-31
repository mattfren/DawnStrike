"""Strict receipt contract for the governed delayed-SIP task rebind."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

CAPTURE_TASK_RECEIPT_SCHEMA = "dawnstrike.capture_task_rebind_receipt.v1"
CAPTURE_TASK_NAME = "Dawnstrike Delayed SIP Capture"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEY_PARTS = ("secret", "password", "credential", "private_key", "token")


class CaptureTaskContractError(ValueError):
    """The task or rebind receipt is unsafe or ambiguous."""


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
    if candidate_sha is not None and payload.get("candidate_sha") != candidate_sha:
        raise CaptureTaskContractError("capture-task candidate SHA mismatch")
    if candidate_tree is not None and payload.get("candidate_tree") != candidate_tree:
        raise CaptureTaskContractError("capture-task candidate tree mismatch")
    for field in (
        "runtime_origin_sha256",
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
    if payload.get("changed_field") != "candidate_sha":
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


def seal_receipt(payload: Mapping[str, Any], output_path: str | Path) -> dict[str, Any]:
    value = dict(payload)
    value["receipt_sha256"] = self_hash(value, "receipt_sha256")
    validated = validate_receipt(value)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise CaptureTaskContractError("capture-task receipt already exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        import os

        os.close(descriptor)
        Path(temporary_name).write_bytes(canonical_json(validated))
        Path(temporary_name).replace(path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)
    return validated


def load_receipt(
    path: str | Path, *, candidate_sha: str | None = None, candidate_tree: str | None = None
) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureTaskContractError("capture-task receipt is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CaptureTaskContractError("capture-task receipt must be an object")
    return validate_receipt(value, candidate_sha=candidate_sha, candidate_tree=candidate_tree)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("seal-receipt")
    seal.add_argument("--input", required=True)
    seal.add_argument("--output", required=True)
    verify = sub.add_parser("verify-receipt")
    verify.add_argument("--receipt", required=True)
    verify.add_argument("--candidate-sha")
    verify.add_argument("--candidate-tree")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "seal-receipt":
            value = json.loads(Path(args.input).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise CaptureTaskContractError("capture-task receipt input must be an object")
            result = seal_receipt(value, args.output)
        else:
            result = load_receipt(
                args.receipt, candidate_sha=args.candidate_sha, candidate_tree=args.candidate_tree
            )
    except (OSError, CaptureTaskContractError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
