"""Strict, atomic crash journal for governed Dawnstrike runtime operations."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "dawnstrike.runtime_operation_journal.v1"
OPERATIONS = {
    "runtime_activation", "capture_task_rebind", "runtime_rollback",
    "capture_task_hardening",
}
PHASES = {
    "runtime_activation": ("INIT", "PRE_SWAP", "POST_SWAP", "COMPLETE"),
    "capture_task_rebind": ("INIT", "PRE_ENABLE", "POST_ENABLE", "COMPLETE"),
    "runtime_rollback": ("INIT", "PRE_SWAP", "POST_SWAP", "COMPLETE"),
    "capture_task_hardening": (
        "INIT", "PRE_TASK_UPDATE", "POST_TASK_UPDATE", "COMPLETE"
    ),
}
KEYS = {
    "schema_version", "operation", "phase", "sequence", "candidate_sha",
    "candidate_tree", "current_sha", "current_tree", "previous_sha",
    "previous_tree", "origin_identity", "origin_identity_sha256",
    "state_root_sha256", "lock_token", "lock_file_sha256",
    "prior_journal_file_sha256", "receipt_relative_path",
    "receipt_sha256", "backup_contract_sha256", "task_contract_sha256",
    "runtime_stage_contract_sha256", "recorded_at_utc", "research_only",
    "broker_execution_enabled", "adoption_state", "old_lock_token",
    "old_lock_file_sha256", "next_lock_token", "next_lock_file_sha256",
    "old_lock_archive_relative_path", "next_lock_relative_path",
    "journal_self_sha256",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _utc(value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("recorded_at_utc must be canonical UTC")
    text = value[:-1]
    if "." in text:
        head, fraction = text.rsplit(".", 1)
        text = f"{head}.{fraction[:6]}"
    try:
        datetime.fromisoformat(text + "+00:00")
    except ValueError as exc:
        raise ValueError("recorded_at_utc is invalid") from exc


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _safe_relative(value: Any) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("receipt_relative_path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("receipt_relative_path is unsafe")


def _optional_relative(value: Any) -> None:
    if value == "NONE":
        return
    _safe_relative(value)


def validate(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode(), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid strict JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError("journal keys are not exact")
    operation = value["operation"]
    if value["schema_version"] != SCHEMA or operation not in OPERATIONS:
        raise ValueError("journal schema or operation is invalid")
    phases = PHASES[operation]
    if value["phase"] not in phases or type(value["sequence"]) is not int:
        raise ValueError("journal phase or sequence is invalid")
    if value["sequence"] != phases.index(value["phase"]):
        raise ValueError("journal phase sequence is invalid")
    empty = hashlib.sha256(b"").hexdigest()
    if value["phase"] == "INIT" and any(
        value[key] != empty
        for key in (
            "receipt_sha256", "backup_contract_sha256",
            "runtime_stage_contract_sha256",
        )
    ):
        raise ValueError("INIT journal carries non-sentinel artifact hashes")
    for key in ("candidate_sha", "candidate_tree", "current_sha", "current_tree",
                "previous_sha", "previous_tree"):
        if not isinstance(value[key], str) or not HEX40.fullmatch(value[key]):
            raise ValueError(f"{key} is invalid")
    if value["origin_identity"] != "github.com/mattfren/dawnstrike":
        raise ValueError("origin identity is invalid")
    if value["origin_identity_sha256"] != hashlib.sha256(
        value["origin_identity"].encode()
    ).hexdigest():
        raise ValueError("origin identity hash mismatch")
    for key in (
        "state_root_sha256", "lock_file_sha256", "prior_journal_file_sha256",
        "receipt_sha256", "backup_contract_sha256", "task_contract_sha256",
        "runtime_stage_contract_sha256", "journal_self_sha256",
    ):
        if not isinstance(value[key], str) or not HEX64.fullmatch(value[key]):
            raise ValueError(f"{key} is invalid")
    if not isinstance(value["lock_token"], str) or not TOKEN.fullmatch(value["lock_token"]):
        raise ValueError("lock token is invalid")
    adoption = value["adoption_state"]
    if adoption not in {"NONE", "ADOPTION_PREPARED", "ADOPTED"}:
        raise ValueError("adoption_state is invalid")
    for key in ("old_lock_token", "next_lock_token"):
        if not isinstance(value[key], str) or not TOKEN.fullmatch(value[key]):
            raise ValueError(f"{key} is invalid")
    for key in ("old_lock_file_sha256", "next_lock_file_sha256"):
        if not isinstance(value[key], str) or not HEX64.fullmatch(value[key]):
            raise ValueError(f"{key} is invalid")
    _optional_relative(value["old_lock_archive_relative_path"])
    _optional_relative(value["next_lock_relative_path"])
    if adoption == "NONE":
        if not (
            value["old_lock_token"] == value["lock_token"]
            and value["next_lock_token"] == value["lock_token"]
            and value["old_lock_file_sha256"] == value["lock_file_sha256"]
            and value["next_lock_file_sha256"] == value["lock_file_sha256"]
            and value["old_lock_archive_relative_path"] == "NONE"
            and value["next_lock_relative_path"] == "NONE"
        ):
            raise ValueError("non-adoption journal has inconsistent lock identities")
    elif adoption == "ADOPTION_PREPARED":
        if (
            value["old_lock_token"] == value["next_lock_token"]
            or value["old_lock_file_sha256"] == value["next_lock_file_sha256"]
            or value["lock_token"] != value["old_lock_token"]
            or value["lock_file_sha256"] != value["old_lock_file_sha256"]
            or value["old_lock_archive_relative_path"] == "NONE"
            or value["next_lock_relative_path"] == "NONE"
        ):
            raise ValueError("prepared adoption identities are inconsistent")
    elif not (
        value["lock_token"] == value["next_lock_token"]
        and value["lock_file_sha256"] == value["next_lock_file_sha256"]
        and value["old_lock_token"] != value["next_lock_token"]
        and value["old_lock_archive_relative_path"] != "NONE"
        and value["next_lock_relative_path"] == "NONE"
    ):
        raise ValueError("adopted journal identities are inconsistent")
    _safe_relative(value["receipt_relative_path"])
    _utc(value["recorded_at_utc"])
    if value["research_only"] is not True or value["broker_execution_enabled"] is not False:
        raise ValueError("journal safety flags are invalid")
    unsigned = dict(value)
    claimed = unsigned.pop("journal_self_sha256")
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != claimed:
        raise ValueError("journal self hash mismatch")
    return value


def _read_regular(path: Path) -> bytes:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("journal path must be a regular non-reparse leaf")
    if before.st_size > 65536:
        raise ValueError("journal path is too large")
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ):
        raise ValueError("journal path changed while reading")
    return raw


def _contained(path: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("journal path escapes StateRoot")
    cursor = path.parent
    while cursor != root.parent and cursor.exists():
        if cursor.is_symlink():
            raise ValueError("journal path contains a reparse component")
        if cursor == root:
            break
        cursor = cursor.parent


def seal(source: Path, target: Path) -> dict[str, Any]:
    value = json.loads(_read_regular(source).decode("utf-8"), object_pairs_hook=_pairs)
    if set(value) != KEYS - {"journal_self_sha256"}:
        raise ValueError("journal input keys are not exact")
    value["journal_self_sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
    raw = _canonical(value)
    validate(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".journal-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"payload": value, "raw_file_sha256": hashlib.sha256(raw).hexdigest()}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("path", type=Path)
    verify.add_argument("--state-root", required=True, type=Path)
    seal_parser = sub.add_parser("seal")
    seal_parser.add_argument("input", type=Path)
    seal_parser.add_argument("output", type=Path)
    seal_parser.add_argument("--state-root", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        _contained(args.path, args.state_root)
        raw = _read_regular(args.path)
        result = {"payload": validate(raw), "raw_file_sha256": hashlib.sha256(raw).hexdigest()}
    else:
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        result = seal(args.input, args.output)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
