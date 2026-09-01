from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.runtime_operation_journal import seal, transition, validate

EMPTY = hashlib.sha256(b"").hexdigest()


def _payload(operation: str = "runtime_activation", phase: str = "INIT") -> dict:
    phases = {
        "runtime_activation": ("INIT", "PRE_SWAP", "POST_SWAP", "COMPLETE"),
        "capture_task_rebind": ("INIT", "PRE_ENABLE", "POST_ENABLE", "COMPLETE"),
        "runtime_rollback": ("INIT", "PRE_SWAP", "POST_SWAP", "COMPLETE"),
        "capture_task_hardening": (
            "INIT", "PRE_TASK_UPDATE", "POST_TASK_UPDATE", "COMPLETE"
        ),
    }
    return {
        "schema_version": "dawnstrike.runtime_operation_journal.v1",
        "operation": operation,
        "phase": phase,
        "sequence": phases[operation].index(phase),
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "current_sha": (
            "e" * 40
            if operation == "runtime_activation" and phase in {"INIT", "PRE_SWAP"}
            else "e" * 40
            if operation == "capture_task_hardening"
            else "a" * 40
            if operation != "runtime_rollback" or phase in {"INIT", "PRE_SWAP"}
            else "e" * 40
        ),
        "current_tree": (
            "f" * 40
            if operation == "runtime_activation" and phase in {"INIT", "PRE_SWAP"}
            else "f" * 40
            if operation == "capture_task_hardening"
            else "b" * 40
            if operation != "runtime_rollback" or phase in {"INIT", "PRE_SWAP"}
            else "f" * 40
        ),
        "previous_sha": "e" * 40,
        "previous_tree": "f" * 40,
        "origin_identity": "github.com/mattfren/dawnstrike",
        "origin_identity_sha256": hashlib.sha256(
            b"github.com/mattfren/dawnstrike"
        ).hexdigest(),
        "state_root_sha256": "1" * 64,
        "lock_token": "2" * 32,
        "lock_file_sha256": "3" * 64,
        "prior_journal_file_sha256": EMPTY,
        "prepared_receipt_relative_path": "receipts/runtime-activation/prepared.json",
        "prepared_receipt_sha256": EMPTY if phase == "INIT" else "9" * 64,
        "complete_receipt_relative_path": "receipts/runtime-activation/complete.json",
        "complete_receipt_sha256": "8" * 64 if phase == "COMPLETE" else EMPTY,
        "backup_contract_sha256": EMPTY if phase == "INIT" else "4" * 64,
        "task_contract_sha256": "5" * 64,
        "runtime_stage_contract_sha256": (
            "6" * 64
            if phase != "INIT" and operation.startswith("runtime_")
            else EMPTY
        ),
        "adoption_state": "NONE",
        "old_lock_token": "2" * 32,
        "old_lock_file_sha256": "3" * 64,
        "next_lock_token": "2" * 32,
        "next_lock_file_sha256": "3" * 64,
        "old_lock_archive_relative_path": "NONE",
        "next_lock_relative_path": "NONE",
        "init_owner_process_id": 1234,
        "init_owner_started_at_utc": "2026-08-31T23:01:01.1234567Z",
        "recorded_at_utc": "2026-08-31T23:01:02.1234567Z",
        "research_only": True,
        "broker_execution_enabled": False,
    }


@pytest.mark.parametrize(
    ("operation", "phase"),
    [
        ("runtime_activation", "INIT"),
        ("runtime_activation", "POST_SWAP"),
        ("capture_task_rebind", "POST_ENABLE"),
        ("runtime_rollback", "COMPLETE"),
        ("capture_task_hardening", "POST_TASK_UPDATE"),
    ],
)
def test_journal_seals_and_validates_exact_phase(
    tmp_path: Path, operation: str, phase: str
) -> None:
    source = tmp_path / "input.json"
    target = tmp_path / "journal.json"
    source.write_text(json.dumps(_payload(operation, phase)), encoding="utf-8")
    result = seal(source, target)
    assert validate(target.read_bytes()) == result["payload"]
    assert result["raw_file_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        {"sequence": 2},
        {"origin_identity": "evil.invalid/repo"},
        {"lock_token": "0" * 31},
        {"prepared_receipt_relative_path": "../escape.json"},
        {"research_only": False},
        {"broker_execution_enabled": True},
        {"task_contract_sha256": EMPTY},
    ],
)
def test_journal_rejects_hostile_payload(tmp_path: Path, mutation: dict) -> None:
    payload = _payload()
    payload.update(mutation)
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        seal(source, tmp_path / "journal.json")


def test_journal_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate key"):
        validate(b'{"schema_version":"x","schema_version":"y"}')


@pytest.mark.parametrize("state", ["ADOPTION_PREPARED", "ADOPTED"])
def test_journal_seals_two_phase_adoption(tmp_path: Path, state: str) -> None:
    payload = _payload()
    payload.update(
        adoption_state=state,
        old_lock_token="2" * 32,
        old_lock_file_sha256="3" * 64,
        next_lock_token="7" * 32,
        next_lock_file_sha256="8" * 64,
        old_lock_archive_relative_path="locks/recovered-stale-3.lock",
        next_lock_relative_path=(
            "locks/next-runtime.lock" if state == "ADOPTION_PREPARED" else "NONE"
        ),
    )
    if state == "ADOPTED":
        payload["lock_token"] = payload["next_lock_token"]
        payload["lock_file_sha256"] = payload["next_lock_file_sha256"]
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    result = seal(source, tmp_path / "journal.json")
    assert result["payload"]["adoption_state"] == state


def test_prepared_adoption_rejects_same_old_and_next_identity(tmp_path: Path) -> None:
    payload = _payload()
    payload.update(
        adoption_state="ADOPTION_PREPARED",
        old_lock_archive_relative_path="locks/archive.lock",
        next_lock_relative_path="locks/next.lock",
    )
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="prepared adoption identities"):
        seal(source, tmp_path / "journal.json")


def test_transition_requires_adjacent_phase_and_exact_prior_raw_hash(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    journal = tmp_path / "journal.json"
    source.write_text(json.dumps(_payload()), encoding="utf-8")
    initial = transition(source, journal, None)
    next_payload = _payload(phase="PRE_SWAP")
    next_payload["prior_journal_file_sha256"] = initial["raw_file_sha256"]
    source.write_text(json.dumps(next_payload), encoding="utf-8")
    prepared = transition(source, journal, journal)
    assert prepared["payload"]["phase"] == "PRE_SWAP"

    skipped = _payload(phase="COMPLETE")
    skipped["prior_journal_file_sha256"] = prepared["raw_file_sha256"]
    source.write_text(json.dumps(skipped), encoding="utf-8")
    with pytest.raises(ValueError, match="not adjacent"):
        transition(source, journal, journal)

    wrong_operation = _payload("runtime_rollback", "POST_SWAP")
    wrong_operation["prior_journal_file_sha256"] = prepared["raw_file_sha256"]
    source.write_text(json.dumps(wrong_operation), encoding="utf-8")
    with pytest.raises(ValueError, match="not adjacent|immutable"):
        transition(source, journal, journal)


@pytest.mark.parametrize(
    ("operation", "phase", "bad_sha", "bad_tree"),
    [
        ("runtime_activation", "POST_SWAP", "e" * 40, "f" * 40),
        ("runtime_rollback", "POST_SWAP", "a" * 40, "b" * 40),
        ("capture_task_rebind", "PRE_ENABLE", "e" * 40, "f" * 40),
        ("capture_task_hardening", "PRE_TASK_UPDATE", "a" * 40, "b" * 40),
    ],
)
def test_phase_rejects_wrong_current_runtime_identity(
    operation: str, phase: str, bad_sha: str, bad_tree: str
) -> None:
    payload = _payload(operation, phase)
    payload["current_sha"] = bad_sha
    payload["current_tree"] = bad_tree
    payload["journal_self_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="current runtime identity"):
        validate(json.dumps(payload).encode())


@pytest.mark.parametrize("operation", ["runtime_activation", "runtime_rollback"])
def test_runtime_transition_allows_exact_pre_to_post_identity_change(
    tmp_path: Path, operation: str
) -> None:
    source = tmp_path / "input.json"
    journal = tmp_path / "journal.json"
    initial_payload = _payload(operation, "INIT")
    source.write_text(json.dumps(initial_payload), encoding="utf-8")
    initial = transition(source, journal, None)
    pre_payload = _payload(operation, "PRE_SWAP")
    pre_payload["prior_journal_file_sha256"] = initial["raw_file_sha256"]
    source.write_text(json.dumps(pre_payload), encoding="utf-8")
    prepared = transition(source, journal, journal)
    post_payload = _payload(operation, "POST_SWAP")
    post_payload["prior_journal_file_sha256"] = prepared["raw_file_sha256"]
    source.write_text(json.dumps(post_payload), encoding="utf-8")
    post = transition(source, journal, journal)
    assert post["payload"]["current_sha"] != prepared["payload"]["current_sha"]
