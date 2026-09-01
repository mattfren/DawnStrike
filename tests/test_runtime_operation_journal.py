from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.runtime_operation_journal import (
    _validate_compensation,
    seal,
    seal_compensation,
    transition,
    validate,
)

EMPTY = hashlib.sha256(b"").hexdigest()


def _payload(operation: str = "runtime_activation", phase: str = "INIT") -> dict:
    phases = {
        "runtime_activation": (
            "INIT", "PRE_QUIESCE", "PRE_SWAP", "POST_SWAP", "COMPLETE", "COMPENSATED"
        ),
        "capture_task_rebind": ("INIT", "PRE_ENABLE", "POST_ENABLE", "COMPLETE", "COMPENSATED"),
        "runtime_rollback": (
            "INIT", "PRE_SWAP", "POST_SWAP", "COMPLETE", "COMPENSATED"
        ),
        "capture_task_hardening": (
            "INIT", "PRE_TASK_UPDATE", "POST_TASK_UPDATE", "COMPLETE", "COMPENSATED"
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
            if operation == "runtime_activation"
            and phase in {"INIT", "PRE_QUIESCE", "PRE_SWAP", "COMPENSATED"}
            else "e" * 40
            if operation == "capture_task_hardening"
            else "a" * 40
            if operation != "runtime_rollback" or phase in {"INIT", "PRE_SWAP", "COMPENSATED"}
            else "e" * 40
        ),
        "current_tree": (
            "f" * 40
            if operation == "runtime_activation"
            and phase in {"INIT", "PRE_QUIESCE", "PRE_SWAP", "COMPENSATED"}
            else "f" * 40
            if operation == "capture_task_hardening"
            else "b" * 40
            if operation != "runtime_rollback" or phase in {"INIT", "PRE_SWAP", "COMPENSATED"}
            else "f" * 40
        ),
        "previous_sha": "e" * 40,
        "previous_tree": "f" * 40,
        "origin_identity": "github.com/mattfren/dawnstrike",
        "origin_identity_sha256": hashlib.sha256(b"github.com/mattfren/dawnstrike").hexdigest(),
        "state_root_sha256": "1" * 64,
        "lock_token": "2" * 32,
        "lock_file_sha256": "3" * 64,
        "prior_journal_file_sha256": EMPTY,
        "prepared_receipt_relative_path": "receipts/runtime-activation/prepared.json",
        "prepared_receipt_sha256": EMPTY if phase in {"INIT", "PRE_QUIESCE"} else "9" * 64,
        "complete_receipt_relative_path": "receipts/runtime-activation/complete.json",
        "complete_receipt_sha256": "8" * 64 if phase == "COMPLETE" else EMPTY,
        "backup_contract_sha256": EMPTY if phase == "INIT" else "4" * 64,
        "task_contract_sha256": "5" * 64,
        "runtime_stage_contract_sha256": (
            "6" * 64 if phase != "INIT" and operation.startswith("runtime_") else EMPTY
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
    quiesce_payload = _payload(phase="PRE_QUIESCE")
    quiesce_payload["prior_journal_file_sha256"] = initial["raw_file_sha256"]
    source.write_text(json.dumps(quiesce_payload), encoding="utf-8")
    quiesced = transition(source, journal, journal)
    next_payload = _payload(phase="PRE_SWAP")
    next_payload["prior_journal_file_sha256"] = quiesced["raw_file_sha256"]
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


def test_capture_rebind_journal_cannot_skip_enablement_phases(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    journal = tmp_path / "journal.json"
    source.write_text(json.dumps(_payload("capture_task_rebind", "INIT")), encoding="utf-8")
    initial = transition(source, journal, None)

    prepared = _payload("capture_task_rebind", "PRE_ENABLE")
    prepared["prior_journal_file_sha256"] = initial["raw_file_sha256"]
    source.write_text(json.dumps(prepared), encoding="utf-8")
    pre_enable = transition(source, journal, journal)

    skipped = _payload("capture_task_rebind", "COMPLETE")
    skipped["prior_journal_file_sha256"] = pre_enable["raw_file_sha256"]
    source.write_text(json.dumps(skipped), encoding="utf-8")
    with pytest.raises(ValueError, match="not adjacent"):
        transition(source, journal, journal)

    post_enable = _payload("capture_task_rebind", "POST_ENABLE")
    post_enable["prior_journal_file_sha256"] = pre_enable["raw_file_sha256"]
    source.write_text(json.dumps(post_enable), encoding="utf-8")
    post = transition(source, journal, journal)

    complete = _payload("capture_task_rebind", "COMPLETE")
    complete["prior_journal_file_sha256"] = post["raw_file_sha256"]
    source.write_text(json.dumps(complete), encoding="utf-8")
    sealed = transition(source, journal, journal)
    assert sealed["payload"]["phase"] == "COMPLETE"


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
    prior = initial
    if operation == "runtime_activation":
        quiesce_payload = _payload(operation, "PRE_QUIESCE")
        quiesce_payload["prior_journal_file_sha256"] = prior["raw_file_sha256"]
        source.write_text(json.dumps(quiesce_payload), encoding="utf-8")
        prior = transition(source, journal, journal)
    pre_payload = _payload(operation, "PRE_SWAP")
    pre_payload["prior_journal_file_sha256"] = prior["raw_file_sha256"]
    source.write_text(json.dumps(pre_payload), encoding="utf-8")
    prepared = transition(source, journal, journal)
    post_payload = _payload(operation, "POST_SWAP")
    post_payload["prior_journal_file_sha256"] = prepared["raw_file_sha256"]
    source.write_text(json.dumps(post_payload), encoding="utf-8")
    post = transition(source, journal, journal)
    assert post["payload"]["current_sha"] != prepared["payload"]["current_sha"]


@pytest.mark.parametrize(
    ("operation", "prior_phase"),
    [
        ("runtime_activation", "PRE_SWAP"),
        ("runtime_activation", "POST_SWAP"),
        ("runtime_rollback", "POST_SWAP"),
    ],
)
def test_runtime_compensation_converges_from_recoverable_swap_phases(
    tmp_path: Path, operation: str, prior_phase: str
) -> None:
    """A restored Ready boundary is a terminal compensation, not a retry phase."""

    source = tmp_path / "input.json"
    journal = tmp_path / "journal.json"
    initial = _payload(operation, "INIT")
    initial.update(
        schema_version="dawnstrike.runtime_operation_journal.v2",
        compensation_receipt_relative_path="NONE",
        compensation_receipt_sha256=EMPTY,
    )
    source.write_text(json.dumps(initial), encoding="utf-8")
    prior = transition(source, journal, None)
    if operation == "runtime_activation":
        quiesce = _payload(operation, "PRE_QUIESCE")
        quiesce.update(
            schema_version="dawnstrike.runtime_operation_journal.v2",
            compensation_receipt_relative_path="NONE",
            compensation_receipt_sha256=EMPTY,
            prior_journal_file_sha256=prior["raw_file_sha256"],
        )
        source.write_text(json.dumps(quiesce), encoding="utf-8")
        prior = transition(source, journal, journal)
    if prior_phase == "POST_SWAP":
        pre_swap = _payload(operation, "PRE_SWAP")
        pre_swap.update(
            schema_version="dawnstrike.runtime_operation_journal.v2",
            compensation_receipt_relative_path="NONE",
            compensation_receipt_sha256=EMPTY,
            prior_journal_file_sha256=prior["raw_file_sha256"],
        )
        source.write_text(json.dumps(pre_swap), encoding="utf-8")
        prior = transition(source, journal, journal)
    if prior_phase == "POST_SWAP":
        post_swap = _payload(operation, "POST_SWAP")
        post_swap.update(
            schema_version="dawnstrike.runtime_operation_journal.v2",
            compensation_receipt_relative_path="NONE",
            compensation_receipt_sha256=EMPTY,
            prior_journal_file_sha256=prior["raw_file_sha256"],
        )
        source.write_text(json.dumps(post_swap), encoding="utf-8")
        prior = transition(source, journal, journal)
    compensated = _payload(operation, "COMPENSATED")
    compensated.update(
        schema_version="dawnstrike.runtime_operation_journal.v2",
        prior_journal_file_sha256=prior["raw_file_sha256"],
        compensation_receipt_relative_path="receipts/compensated.json",
        compensation_receipt_sha256="9" * 64,
        runtime_stage_contract_sha256=EMPTY,
    )
    source.write_text(json.dumps(compensated), encoding="utf-8")
    terminal = transition(source, journal, journal)
    assert terminal["payload"]["phase"] == "COMPENSATED"
    assert validate(journal.read_bytes())["current_sha"] == (
        "e" * 40 if operation == "runtime_activation" else "a" * 40
    )


def test_runtime_compensation_cannot_replace_complete_journal(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    journal = tmp_path / "journal.json"
    initial = _payload("runtime_activation", "INIT")
    initial.update(
        schema_version="dawnstrike.runtime_operation_journal.v2",
        compensation_receipt_relative_path="NONE",
        compensation_receipt_sha256=EMPTY,
    )
    source.write_text(json.dumps(initial), encoding="utf-8")
    prior = transition(source, journal, None)
    for phase in ("PRE_QUIESCE", "PRE_SWAP", "POST_SWAP", "COMPLETE"):
        payload = _payload("runtime_activation", phase)
        payload.update(
            schema_version="dawnstrike.runtime_operation_journal.v2",
            compensation_receipt_relative_path="NONE",
            compensation_receipt_sha256=EMPTY,
            prior_journal_file_sha256=prior["raw_file_sha256"],
        )
        source.write_text(json.dumps(payload), encoding="utf-8")
        prior = transition(source, journal, journal)
    compensated = _payload("runtime_activation", "COMPENSATED")
    compensated.update(
        schema_version="dawnstrike.runtime_operation_journal.v2",
        prior_journal_file_sha256=prior["raw_file_sha256"],
        compensation_receipt_relative_path="receipts/compensated.json",
        compensation_receipt_sha256="9" * 64,
        runtime_stage_contract_sha256=EMPTY,
    )
    source.write_text(json.dumps(compensated), encoding="utf-8")
    with pytest.raises(ValueError, match="not recoverable"):
        transition(source, journal, journal)


def test_hardening_journal_rejects_cross_candidate_and_self_hash_tamper() -> None:
    payload = _payload("capture_task_hardening", "POST_TASK_UPDATE")
    payload["candidate_sha"] = "c" * 40
    payload["journal_self_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="current runtime identity|self hash"):
        validate(json.dumps(payload).encode())


def _compensation_payload(tmp_path: Path) -> dict:
    prior = tmp_path / "prior-receipt.json"
    prior.write_text("immutable prior receipt", encoding="utf-8")
    prior_hash = hashlib.sha256(prior.read_bytes()).hexdigest()
    return {
        "schema_version": "dawnstrike.runtime_compensation_receipt.v1",
        "status": "COMPENSATED",
        "operation": "capture_task_rebind",
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "prior_journal_file_sha256": "c" * 64,
        "task_contract_sha256": "d" * 64,
        "task_state": "Disabled",
        "task_xml_sha256": "e" * 64,
        "task_action_contract_sha256": "f" * 64,
        "task_definition_contract_sha256": "1" * 64,
        "prior_receipt_relative_path": prior.name,
        "prior_receipt_sha256": prior_hash,
        "failure_type": "RuntimeError",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_compensation_receipt_is_strict_self_hashed_and_prior_bound(tmp_path: Path) -> None:
    source = tmp_path / "compensation.input.json"
    target = tmp_path / "compensation.json"
    source.write_text(json.dumps(_compensation_payload(tmp_path)), encoding="utf-8")
    sealed = seal_compensation(source, target, tmp_path)
    assert sealed["payload"]["status"] == "COMPENSATED"
    assert sealed["raw_file_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text(
        target.read_text(encoding="utf-8").replace("RuntimeError", "TamperedError"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="self hash"):
        _validate_compensation(target.read_bytes())


def test_compensation_rejects_missing_or_tampered_prior_receipt(tmp_path: Path) -> None:
    payload = _compensation_payload(tmp_path)
    source = tmp_path / "input.json"
    target = tmp_path / "output.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "prior-receipt.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="prior receipt changed"):
        seal_compensation(source, target, tmp_path)
    (tmp_path / "prior-receipt.json").unlink()
    with pytest.raises(ValueError, match="prior receipt changed|missing"):
        seal_compensation(source, target, tmp_path)


def test_compensation_rejects_inconsistent_prior_receipt_sentinel(tmp_path: Path) -> None:
    payload = _compensation_payload(tmp_path)
    payload["prior_receipt_relative_path"] = "NONE"
    source = tmp_path / "input.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="prior receipt sentinel"):
        seal_compensation(source, tmp_path / "output.json", tmp_path)


def test_compensation_receipt_cannot_be_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    target = tmp_path / "output.json"
    source.write_text(json.dumps(_compensation_payload(tmp_path)), encoding="utf-8")
    seal_compensation(source, target, tmp_path)
    with pytest.raises(ValueError, match="already exists"):
        seal_compensation(source, target, tmp_path)


def test_v2_compensated_journal_is_terminal_and_requires_compensation_proof(tmp_path: Path) -> None:
    initial = _payload("capture_task_rebind", "INIT")
    initial.update(
        schema_version="dawnstrike.runtime_operation_journal.v2",
        compensation_receipt_relative_path="NONE",
        compensation_receipt_sha256=EMPTY,
    )
    source = tmp_path / "input.json"
    journal = tmp_path / "journal.json"
    source.write_text(json.dumps(initial), encoding="utf-8")
    first = transition(source, journal, None)
    compensated = _payload("capture_task_rebind", "COMPENSATED")
    compensated.update(
        schema_version="dawnstrike.runtime_operation_journal.v2",
        prior_journal_file_sha256=first["raw_file_sha256"],
        compensation_receipt_relative_path="receipts/compensated.json",
        compensation_receipt_sha256="9" * 64,
    )
    source.write_text(json.dumps(compensated), encoding="utf-8")
    terminal = transition(source, journal, journal)
    assert terminal["payload"]["phase"] == "COMPENSATED"


def test_consumers_keep_nonterminal_journal_until_compensation_or_completion() -> None:
    for script_name, crash_marker, complete_marker in (
        (
            "scripts/rebind_intraday_capture_task.ps1",
            "after_receipt_seal_before_complete",
            "-Phase COMPLETE",
        ),
        (
            "scripts/harden_intraday_capture_task.ps1",
            "after_receipt_seal_before_complete",
            "-Phase COMPLETE",
        ),
    ):
        script = Path(script_name).read_text(encoding="utf-8")
        assert crash_marker in script
        assert "-Phase COMPENSATED" in script
        assert "retained for governed recovery" in script
        assert script.index(crash_marker) < script.rindex(complete_marker)
        complete_transition = script.rindex(complete_marker)
        local_terminal = script.index('$journalPhase = "COMPLETE"', complete_transition)
        assert complete_transition < local_terminal
        compensated = script.index("after_compensated_before_release")
        assert compensated < script.index("Exit-DawnstrikeGovernedRuntimeLock", compensated)


def test_consumers_reconcile_complete_before_any_compensation_or_restore() -> None:
    """A post-commit cleanup/output fault must not execute a stale rollback path."""

    cases = (
        (
            "scripts/rebind_intraday_capture_task.ps1",
            "Restore-DawnstrikeAuxiliaryCaptureTask",
            "Capture-task terminal evidence requires governed recovery",
        ),
        (
            "scripts/harden_intraday_capture_task.ps1",
            "Restore-HardeningExactTask",
            "Hardening terminal evidence requires governed recovery",
        ),
        (
            "scripts/activate_dawnstrike_runtime.ps1",
            "Set-DawnstrikeTasksFailClosedDisabled",
            "Complete activation evidence could not be reconciled",
        ),
        (
            "scripts/rollback_dawnstrike_runtime.ps1",
            "Set-DawnstrikeTasksFailClosedDisabled",
            "Complete rollback evidence could not be reconciled",
        ),
    )
    for script_name, compensation_marker, recovery_marker in cases:
        script = Path(script_name).read_text(encoding="utf-8")
        comment = script.find("COMPLETE journal is an irreversible commit")
        if comment < 0:
            comment = script.find("COMPLETE is an irreversible commit")
        assert comment >= 0
        terminal_guard = script.index('if ($journalPhase -eq "COMPLETE")', comment)
        compensation = script.index(compensation_marker, terminal_guard)
        assert terminal_guard < compensation
        assert recovery_marker in script
        assert "journal phase could not be reconciled" in script
