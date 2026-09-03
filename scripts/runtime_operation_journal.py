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
COMPENSATED_SCHEMA = "dawnstrike.runtime_operation_journal.v2"
OPERATIONS = {
    "runtime_activation", "capture_task_rebind", "runtime_rollback",
    "capture_task_hardening", "state_preparation",
}
PHASES = {
    # PRE_QUIESCE is a durable intent written before the first scheduler
    # mutation.  It lets activation recovery distinguish an interrupted
    # quiescence operation from an unstarted activation.
    "runtime_activation": (
        "INIT", "PRE_QUIESCE", "PRE_SWAP", "POST_SWAP", "POST_SWAP_READY", "COMPLETE", "COMPENSATED"
    ),
    "capture_task_rebind": ("INIT", "PRE_ENABLE", "POST_ENABLE", "COMPLETE", "COMPENSATED"),
    "runtime_rollback": (
        "INIT",
        "PRE_SWAP",
        "POST_SWAP",
        "POST_SWAP_READY",
        "COMPLETE",
        "COMPENSATED",
    ),
    "capture_task_hardening": (
        "INIT", "PRE_TASK_UPDATE", "POST_TASK_UPDATE", "COMPLETE", "COMPENSATED"
    ),
    # State preparation has its own database receipt but shares the same
    # crash-safe global lock/adoption envelope. PREPARE is sealed after the
    # task baseline/proof is durable and before the Python database operation.
    "state_preparation": ("INIT", "PREPARE", "COMPLETE", "COMPENSATED"),
}
KEYS = {
    "schema_version", "operation", "phase", "sequence", "candidate_sha",
    "candidate_tree", "current_sha", "current_tree", "previous_sha",
    "previous_tree", "origin_identity", "origin_identity_sha256",
    "state_root_sha256", "lock_token", "lock_file_sha256",
    "prior_journal_file_sha256", "prepared_receipt_relative_path",
    "prepared_receipt_sha256", "complete_receipt_relative_path",
    "complete_receipt_sha256", "backup_contract_sha256", "task_contract_sha256",
    "runtime_stage_contract_sha256", "recorded_at_utc", "research_only",
    "broker_execution_enabled", "adoption_state", "old_lock_token",
    "old_lock_file_sha256", "next_lock_token", "next_lock_file_sha256",
    "old_lock_archive_relative_path", "next_lock_relative_path",
    "init_owner_process_id", "init_owner_started_at_utc", "journal_self_sha256",
    "compensation_receipt_relative_path", "compensation_receipt_sha256",
}
LEGACY_KEYS = KEYS - {"compensation_receipt_relative_path", "compensation_receipt_sha256"}
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
    if not isinstance(value, dict) or set(value) not in (LEGACY_KEYS, KEYS):
        raise ValueError("journal keys are not exact")
    operation = value["operation"]
    if value["schema_version"] not in (SCHEMA, COMPENSATED_SCHEMA) or operation not in OPERATIONS:
        raise ValueError("journal schema or operation is invalid")
    extended = set(value) == KEYS
    if value["schema_version"] == COMPENSATED_SCHEMA and not extended:
        raise ValueError("compensated journal keys are incomplete")
    if value["schema_version"] == SCHEMA and extended:
        raise ValueError("legacy journal carries compensation keys")
    phases = PHASES[operation]
    if value["phase"] not in phases or type(value["sequence"]) is not int:
        raise ValueError("journal phase or sequence is invalid")
    if value["sequence"] != phases.index(value["phase"]):
        raise ValueError("journal phase sequence is invalid")
    candidate_pair = (value["candidate_sha"], value["candidate_tree"])
    previous_pair = (value["previous_sha"], value["previous_tree"])
    current_pair = (value["current_sha"], value["current_tree"])
    if operation == "runtime_activation":
        expected_current = (
            previous_pair
            if value["phase"] in {"INIT", "PRE_QUIESCE", "PRE_SWAP", "COMPENSATED"}
            else candidate_pair
        )
    elif operation == "runtime_rollback":
        expected_current = (
            candidate_pair
            if value["phase"] in {"INIT", "PRE_SWAP", "COMPENSATED"}
            else previous_pair
        )
    elif operation == "capture_task_rebind":
        expected_current = candidate_pair
    else:
        expected_current = previous_pair
    if current_pair != expected_current:
        raise ValueError("current runtime identity is invalid for the phase")
    empty = hashlib.sha256(b"").hexdigest()
    if value["task_contract_sha256"] == empty:
        raise ValueError("journal lacks exact task contract proof")
    prepared_receipt = value["prepared_receipt_sha256"]
    complete_receipt = value["complete_receipt_sha256"]
    backup = value["backup_contract_sha256"]
    stage = value["runtime_stage_contract_sha256"]
    if value["phase"] == "INIT":
        if any(item != empty for item in (prepared_receipt, complete_receipt, backup, stage)):
            raise ValueError("INIT journal carries non-sentinel artifact hashes")
        if extended and (
            value["compensation_receipt_relative_path"] != "NONE"
            or value["compensation_receipt_sha256"] != empty
        ):
            raise ValueError("INIT journal carries compensation proof")
    elif value["phase"] == "PRE_QUIESCE":
        # The task backup and stage identity are sealed before scheduler
        # disablement.  The state/activation PREPARED receipt is intentionally
        # absent until quiescence and the locked state snapshot are complete.
        if (
            prepared_receipt != empty
            or complete_receipt != empty
            or backup == empty
            or stage == empty
        ):
            raise ValueError("PRE_QUIESCE journal artifact proof is invalid")
    elif operation == "state_preparation" and value["phase"] == "PREPARE":
        # The database receipt and backup are produced by the Python worker
        # after this intent is sealed.  A durable task baseline may already be
        # bound in prepared_receipt_sha256; either sentinel is valid here, but
        # a partial hash cannot be admitted.
        if complete_receipt != empty or stage != empty:
            raise ValueError("state-preparation PREPARE artifact proof is invalid")
        if prepared_receipt != empty and not HEX64.fullmatch(prepared_receipt):
            raise ValueError("state-preparation PREPARE baseline proof is invalid")
        if backup != empty and not HEX64.fullmatch(backup):
            raise ValueError("state-preparation PREPARE backup proof is invalid")
    elif value["phase"] == "COMPENSATED":
        # Compensation is legal from any recoverable task-operation phase,
        # including INIT when the failure occurred before PREPARED existed.
        if not extended or value["schema_version"] != COMPENSATED_SCHEMA:
            raise ValueError("compensated phase requires the v2 journal contract")
        if complete_receipt != empty or value["compensation_receipt_sha256"] == empty:
            raise ValueError("compensated receipt proof is invalid")
        if value["compensation_receipt_relative_path"] == "NONE":
            raise ValueError("compensated receipt path is invalid")
        if value["runtime_stage_contract_sha256"] != empty:
            raise ValueError("compensated task journal carries runtime stage proof")
    elif value["phase"] == "POST_SWAP_READY":
        # A COMPLETE receipt is sealed before scheduler enablement.  This
        # durable intermediate phase is the recovery boundary for a power loss
        # while tasks are being re-enabled: a Ready task set can never exist
        # without an exact receipt/journal pair to finish the commit.
        if (
            prepared_receipt == empty
            or complete_receipt == empty
            or backup == empty
            or stage == empty
        ):
            raise ValueError("POST_SWAP_READY journal artifact proof is invalid")
    else:
        if prepared_receipt == empty or backup == empty:
            raise ValueError("mutation phase lacks prepared receipt or backup proof")
        if (value["phase"] == "COMPLETE") != (complete_receipt != empty):
            raise ValueError("complete receipt proof sentinel is invalid")
        elif extended and (
            value["compensation_receipt_sha256"] != empty
            or value["compensation_receipt_relative_path"] != "NONE"
        ):
            raise ValueError("non-compensated journal carries compensation proof")
        runtime_operation = operation in {"runtime_activation", "runtime_rollback"}
        if runtime_operation != (stage != empty):
            raise ValueError("runtime stage proof sentinel is invalid")
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
        "prepared_receipt_sha256", "complete_receipt_sha256",
        "backup_contract_sha256", "task_contract_sha256",
        "runtime_stage_contract_sha256", "journal_self_sha256",
    ):
        if not isinstance(value[key], str) or not HEX64.fullmatch(value[key]):
            raise ValueError(f"{key} is invalid")
    if not isinstance(value["lock_token"], str) or not TOKEN.fullmatch(value["lock_token"]):
        raise ValueError("lock token is invalid")
    if type(value["init_owner_process_id"]) is not int or value["init_owner_process_id"] <= 0:
        raise ValueError("init_owner_process_id is invalid")
    _utc(value["init_owner_started_at_utc"])
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
    _safe_relative(value["prepared_receipt_relative_path"])
    _safe_relative(value["complete_receipt_relative_path"])
    if extended and value["compensation_receipt_relative_path"] != "NONE":
        _safe_relative(value["compensation_receipt_relative_path"])
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
    if set(value) not in (
        LEGACY_KEYS - {"journal_self_sha256"},
        KEYS - {"journal_self_sha256"},
    ):
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


def transition(source: Path, target: Path, previous: Path | None) -> dict[str, Any]:
    candidate = json.loads(_read_regular(source).decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(candidate, dict):
        raise ValueError("journal transition input must be an object")
    phase = candidate.get("phase")
    operation = candidate.get("operation")
    if operation not in OPERATIONS or phase not in PHASES[operation]:
        raise ValueError("journal transition operation or phase is invalid")
    if phase == "INIT":
        if previous is not None or target.exists():
            raise ValueError("INIT transition requires no prior journal")
        empty = hashlib.sha256(b"").hexdigest()
        if candidate.get("prior_journal_file_sha256") != empty:
            raise ValueError("INIT transition prior hash is not empty")
    else:
        if previous is None or previous != target:
            raise ValueError("non-INIT transition must replace its exact journal")
        prior_raw = _read_regular(previous)
        prior = validate(prior_raw)
        expected_index = PHASES[operation].index(phase) - 1
        if phase == "COMPENSATED":
            if prior["phase"] in {"COMPLETE", "COMPENSATED"}:
                raise ValueError("journal compensation transition is not recoverable")
        elif expected_index < 0 or prior["phase"] != PHASES[operation][expected_index]:
            raise ValueError("journal transition is not adjacent")
        if candidate.get("prior_journal_file_sha256") != hashlib.sha256(prior_raw).hexdigest():
            raise ValueError("journal prior raw hash mismatch")
        immutable = {
            "operation", "candidate_sha", "candidate_tree",
            "previous_sha", "previous_tree", "origin_identity",
            "origin_identity_sha256", "state_root_sha256",
            "prepared_receipt_relative_path",
            "init_owner_process_id", "init_owner_started_at_utc",
            "research_only", "broker_execution_enabled",
        }
        for key in immutable:
            if candidate.get(key) != prior[key]:
                raise ValueError(f"journal immutable field changed: {key}")
        prior_complete_path = str(prior["complete_receipt_relative_path"])
        next_complete_path = str(candidate.get("complete_receipt_relative_path") or "")
        if next_complete_path != prior_complete_path:
            ready_match = re.fullmatch(
                r"receipts/runtime-activation/"
                r"runtime-activation-([0-9a-f]{24})\.ready\.json",
                prior_complete_path,
            )
            exact_terminal_path = (
                "receipts/runtime-activation/"
                f"runtime-activation-{ready_match.group(1)}.json"
                if ready_match
                else ""
            )
            if not (
                operation == "runtime_activation"
                and prior["phase"] == "POST_SWAP_READY"
                and phase == "COMPLETE"
                and next_complete_path == exact_terminal_path
            ):
                raise ValueError(
                    "journal immutable field changed: complete_receipt_relative_path"
                )
        candidate_pair = (candidate["candidate_sha"], candidate["candidate_tree"])
        previous_pair = (candidate["previous_sha"], candidate["previous_tree"])
        current_pair = (candidate["current_sha"], candidate["current_tree"])
        if operation == "runtime_activation":
            expected = (
                previous_pair
                if phase in {"PRE_QUIESCE", "PRE_SWAP", "COMPENSATED"}
                else candidate_pair
            )
        elif operation == "runtime_rollback":
            expected = (
                candidate_pair
                if phase in {"PRE_SWAP", "COMPENSATED"}
                else previous_pair
            )
        elif operation == "capture_task_rebind":
            expected = (prior["current_sha"], prior["current_tree"])
        else:
            expected = previous_pair
        if current_pair != expected:
            raise ValueError("current runtime identity is invalid for the phase")
        if phase == "COMPENSATED":
            if prior["schema_version"] != COMPENSATED_SCHEMA:
                raise ValueError("compensation requires a v2 journal")
            if candidate.get("compensation_receipt_sha256") == hashlib.sha256(b"").hexdigest():
                raise ValueError("compensation requires a receipt hash")
    return seal(source, target)


COMPENSATION_KEYS_V1 = {
    "schema_version", "status", "operation", "candidate_sha", "candidate_tree",
    "prior_journal_file_sha256", "task_contract_sha256", "task_state",
    "task_xml_sha256", "task_action_contract_sha256", "task_definition_contract_sha256",
    "prior_receipt_relative_path", "prior_receipt_sha256", "failure_type",
    "research_only", "broker_execution_enabled", "receipt_self_sha256",
}
COMPENSATION_KEYS_V2 = COMPENSATION_KEYS_V1 | {"prior_receipt_archive_relative_path"}


def _validate_compensation(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode(), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid strict compensation JSON: {exc}") from exc
    value_keys = frozenset(value) if isinstance(value, dict) else frozenset()
    if not isinstance(value, dict) or value_keys not in {
        frozenset(COMPENSATION_KEYS_V1),
        frozenset(COMPENSATION_KEYS_V2),
    }:
        raise ValueError("compensation receipt keys are not exact")
    version = value["schema_version"]
    if (
        version == "dawnstrike.runtime_compensation_receipt.v1"
        and value_keys != frozenset(COMPENSATION_KEYS_V1)
    ) or (
        version == "dawnstrike.runtime_compensation_receipt.v2"
        and value_keys != frozenset(COMPENSATION_KEYS_V2)
    ) or version not in {
        "dawnstrike.runtime_compensation_receipt.v1",
        "dawnstrike.runtime_compensation_receipt.v2",
    }:
        raise ValueError("compensation receipt schema is invalid")
    if value["status"] != "COMPENSATED" or value["operation"] not in {
        "capture_task_rebind", "capture_task_hardening",
        "runtime_activation", "runtime_rollback", "state_preparation",
    }:
        raise ValueError("compensation receipt status or operation is invalid")
    for key in ("candidate_sha", "candidate_tree"):
        if not isinstance(value[key], str) or not HEX40.fullmatch(value[key]):
            raise ValueError("compensation candidate identity is invalid")
    for key in ("prior_journal_file_sha256", "task_contract_sha256", "task_xml_sha256",
                "task_action_contract_sha256", "task_definition_contract_sha256",
                "prior_receipt_sha256", "receipt_self_sha256"):
        if not isinstance(value[key], str) or not HEX64.fullmatch(value[key]):
            raise ValueError(f"compensation {key} is invalid")
    if value["prior_receipt_relative_path"] != "NONE":
        _safe_relative(value["prior_receipt_relative_path"])
    if version == "dawnstrike.runtime_compensation_receipt.v2":
        if value["prior_receipt_archive_relative_path"] != "NONE":
            _safe_relative(value["prior_receipt_archive_relative_path"])
        if (value["prior_receipt_relative_path"] == "NONE") != (
            value["prior_receipt_archive_relative_path"] == "NONE"
        ):
            raise ValueError("compensation prior receipt archive sentinel is inconsistent")
    if (value["prior_receipt_relative_path"] == "NONE") != (
        value["prior_receipt_sha256"] == hashlib.sha256(b"").hexdigest()
    ):
        raise ValueError("compensation prior receipt sentinel is inconsistent")
    allowed_task_states = {"Disabled", "Ready"}
    if value["operation"] == "state_preparation":
        allowed_task_states.add("ABSENT")
    if value["task_state"] not in allowed_task_states:
        raise ValueError("compensation task state is invalid")
    if (
        not isinstance(value["failure_type"], str)
        or not value["failure_type"]
        or len(value["failure_type"]) > 128
    ):
        raise ValueError("compensation failure type is invalid")
    if value["research_only"] is not True or value["broker_execution_enabled"] is not False:
        raise ValueError("compensation safety flags are invalid")
    unsigned = dict(value)
    claimed = unsigned.pop("receipt_self_sha256")
    if hashlib.sha256(_canonical(unsigned)).hexdigest() != claimed:
        raise ValueError("compensation receipt self hash mismatch")
    return value


def _validate_compensation_reference(
    value: dict[str, Any],
    state_root: Path,
    *,
    prior_receipt_fallback: Path | None = None,
) -> None:
    if value["prior_receipt_relative_path"] == "NONE":
        if prior_receipt_fallback is not None:
            raise ValueError("compensation prior receipt fallback is inconsistent")
        return
    prior = state_root / value["prior_receipt_relative_path"].replace("/", os.sep)
    _contained(prior, state_root)
    candidates = [prior]
    archive_relative = value.get("prior_receipt_archive_relative_path", "NONE")
    if archive_relative != "NONE":
        archive = state_root / archive_relative.replace("/", os.sep)
        _contained(archive, state_root)
        candidates.append(archive)
    if prior_receipt_fallback is not None:
        _contained(prior_receipt_fallback, state_root)
        candidates.append(prior_receipt_fallback)
    existing: list[Path] = []
    for candidate in candidates:
        try:
            if candidate.exists():
                existing.append(candidate)
        except OSError as exc:
            raise ValueError("compensation prior receipt changed or is missing") from exc
    if not existing:
        raise ValueError("compensation prior receipt changed or is missing")
    if len(existing) != 1:
        raise ValueError("compensation prior receipt source/archive state is not exact")
    try:
        raw = _read_regular(existing[0])
    except (FileNotFoundError, OSError) as exc:
        raise ValueError("compensation prior receipt changed or is missing") from exc
    if hashlib.sha256(raw).hexdigest() != value["prior_receipt_sha256"]:
        raise ValueError("compensation prior receipt changed or is missing")


def seal_compensation(
    source: Path,
    target: Path,
    state_root: Path | None = None,
    *,
    reuse_existing: bool = False,
) -> dict[str, Any]:
    value = json.loads(_read_regular(source).decode("utf-8"), object_pairs_hook=_pairs)
    input_keys = frozenset(value)
    if input_keys not in {
        frozenset(COMPENSATION_KEYS_V1 - {"receipt_self_sha256"}),
        frozenset(COMPENSATION_KEYS_V2 - {"receipt_self_sha256"}),
    }:
        raise ValueError("compensation input keys are not exact")
    value["receipt_self_sha256"] = hashlib.sha256(_canonical(value)).hexdigest()
    raw = _canonical(value)
    _validate_compensation(raw)
    if state_root is not None:
        _validate_compensation_reference(value, state_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and reuse_existing:
        # A worker can die after the immutable compensation receipt is sealed
        # but before the journal transition.  Retrying must converge on the
        # exact same bytes for the same prior-journal binding; accepting a
        # merely valid receipt (or replacing a foreign one) would launder the
        # failed attempt.  Return the existing proof only after byte-for-byte
        # equality with the newly derived payload and a second strict parse.
        try:
            existing_raw = _read_regular(target)
            existing = _validate_compensation(existing_raw)
            if state_root is not None:
                _validate_compensation_reference(existing, state_root)
        except (OSError, ValueError) as exc:
            raise ValueError(
                "compensation receipt already exists and is not exact reusable evidence"
            ) from exc
        if existing_raw != raw:
            raise ValueError(
                "compensation receipt already exists; immutable evidence cannot be replaced"
            )
        return {"payload": existing, "raw_file_sha256": hashlib.sha256(existing_raw).hexdigest()}
    if target.exists():
        raise ValueError(
            "compensation receipt already exists; immutable evidence cannot be replaced"
        )
    descriptor, temporary = tempfile.mkstemp(
        prefix=".compensation-", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Hard-link publication is an exclusive create on Windows/NTFS: it
        # cannot clobber a receipt that won the race after the preflight.
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ValueError(
                "compensation receipt already exists; immutable evidence cannot be replaced"
            ) from exc
        os.unlink(temporary)
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
    transition_parser = sub.add_parser("transition")
    transition_parser.add_argument("input", type=Path)
    transition_parser.add_argument("output", type=Path)
    transition_parser.add_argument("--previous", type=Path)
    transition_parser.add_argument("--state-root", required=True, type=Path)
    compensation = sub.add_parser("seal-compensation")
    compensation.add_argument("--input", required=True, type=Path)
    compensation.add_argument("--output", required=True, type=Path)
    compensation.add_argument("--state-root", required=True, type=Path)
    compensation.add_argument(
        "--reuse-existing",
        action="store_true",
        help="reuse an existing receipt only when its sealed bytes exactly match the input",
    )
    verify_compensation = sub.add_parser("verify-compensation")
    verify_compensation.add_argument("--receipt", required=True, type=Path)
    verify_compensation.add_argument("--state-root", required=True, type=Path)
    verify_compensation.add_argument("--prior-receipt-fallback", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        _contained(args.path, args.state_root)
        raw = _read_regular(args.path)
        result = {"payload": validate(raw), "raw_file_sha256": hashlib.sha256(raw).hexdigest()}
    elif args.command == "verify-compensation":
        _contained(args.receipt, args.state_root)
        raw = _read_regular(args.receipt)
        payload = _validate_compensation(raw)
        _validate_compensation_reference(
            payload,
            args.state_root,
            prior_receipt_fallback=args.prior_receipt_fallback,
        )
        result = {"payload": payload, "raw_file_sha256": hashlib.sha256(raw).hexdigest()}
    elif args.command == "seal-compensation":
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        result = seal_compensation(
            args.input,
            args.output,
            args.state_root,
            reuse_existing=args.reuse_existing,
        )
    elif args.command == "seal":
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        result = seal(args.input, args.output)
    else:
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        if args.previous is not None:
            _contained(args.previous, args.state_root)
        result = transition(args.input, args.output, args.previous)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
