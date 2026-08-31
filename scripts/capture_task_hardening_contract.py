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

CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA = (
    "dawnstrike.capture_task_hardening_receipt.v1"
)
CAPTURE_TASK_NAME = "Dawnstrike Delayed SIP Capture"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+\.xml$")
_FORBIDDEN_KEY_PARTS = ("secret", "password", "credential", "private_key", "token")
_REPARSE_POINT = 0x400


class CaptureTaskHardeningContractError(ValueError):
    """The hardening receipt is unsafe or ambiguous."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


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
        if (os.path.lexists(current) or current.is_symlink()) and _is_reparse_point(
            current
        ):
            raise CaptureTaskHardeningContractError(
                f"reparse-point path component is forbidden: {current}"
            )
    return absolute


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CaptureTaskHardeningContractError(
                f"duplicate JSON field is forbidden: {key}"
            )
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
                raise CaptureTaskHardeningContractError(
                    f"sensitive field is forbidden at {path}"
                )
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
    if set(payload) != expected:
        raise CaptureTaskHardeningContractError(
            "hardening receipt fields do not match the strict contract"
        )
    if payload.get("receipt_sha256") != self_hash(payload, "receipt_sha256"):
        raise CaptureTaskHardeningContractError("hardening receipt self-hash mismatch")
    if (
        payload.get("schema_version") != CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA
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
    for field in ("old_last_task_result", "new_last_task_result"):
        if not isinstance(payload.get(field), int) or isinstance(payload.get(field), bool):
            raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
    if payload.get("new_last_task_result") not in {0, 267011}:
        raise CaptureTaskHardeningContractError(
            "replacement task must have a fresh acceptable initial result"
        )
    for field in ("old_last_run_time", "new_last_run_time"):
        value = payload.get(field)
        if value is not None:
            if not isinstance(value, str) or not value.endswith("Z"):
                raise CaptureTaskHardeningContractError(
                    f"hardening {field} must be UTC or null"
                )
            try:
                datetime.fromisoformat(value[:-1] + "+00:00")
            except ValueError as exc:
                raise CaptureTaskHardeningContractError(
                    f"hardening {field} is invalid"
                ) from exc
    if payload.get("new_last_run_time") is not None:
        raise CaptureTaskHardeningContractError(
            "replacement task LastRunTime must be unset before first run"
        )
    if payload.get("history_reset_proven") is not True:
        raise CaptureTaskHardeningContractError("hardening history reset is unproven")
    if payload.get("changed_fields") != ["principal", "settings"]:
        raise CaptureTaskHardeningContractError("hardening changed-field contract is invalid")
    for field in ("preserved_action", "preserved_trigger", "preserved_input_bindings"):
        if payload.get(field) is not True:
            raise CaptureTaskHardeningContractError(f"hardening {field} is invalid")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "seal-hardening":
            result = seal_receipt(_load_json_object(args.input), args.output)
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
