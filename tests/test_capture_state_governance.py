from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import run_migrations
from scripts.capture_task_contract import (
    CAPTURE_TASK_NAME,
    CaptureTaskContractError,
)
from scripts.capture_task_contract import (
    self_hash as capture_self_hash,
)
from scripts.capture_task_contract import (
    validate_receipt as validate_capture_receipt,
)
from scripts.state_preparation import (
    StatePreparationError,
    inspect_task_proof,
    inventory,
    prepare_state,
)
from scripts.state_preparation import (
    self_hash as state_self_hash,
)
from scripts.state_preparation import (
    validate_receipt as validate_state_receipt,
)

CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _task_proof(path: Path, *, capture_present: bool = True) -> Path:
    value = {
        "schema_version": "dawnstrike.task_preparation_proof.v1",
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "task_count": 5,
        "canonical_running_count": 0,
        "capture_present": capture_present,
        "capture_running": False if capture_present else None,
        "capture_state": "Disabled" if capture_present else "ABSENT",
        "capture_action": "DISABLED_UNTIL_EXACT_SHA_REBIND"
        if capture_present
        else "ABSENT_ALLOWED",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _state_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    state = tmp_path / "state"
    state.mkdir()
    db = state / "shadow_real.sqlite"
    with sqlite3.connect(db) as connection:
        run_migrations(connection)
    proof = _task_proof(tmp_path / "task-proof.json")
    backup = tmp_path / "backups"
    return db, state, proof, backup


def _prepare(tmp_path: Path) -> tuple[dict[str, object], Path, Path, Path, Path]:
    db, state, proof, backup = _state_fixture(tmp_path)
    receipt = prepare_state(
        db,
        state_root=state,
        backup_root=backup,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        task_proof=proof,
    )
    return receipt, db, state, proof, backup


def test_state_preparation_is_additive_complete_and_idempotent(tmp_path: Path) -> None:
    receipt, db, state, proof, backup = _prepare(tmp_path)

    assert receipt["status"] == "COMPLETE"
    assert receipt["state_schema_version"] == 30
    assert receipt["initialization_idempotent"] is True
    assert receipt["before_db_sha256"] != receipt["after_db_sha256"]
    assert receipt["backup_db_sha256"] == receipt["before_db_sha256"]
    assert backup != state and (backup / str(receipt["backup_id"])).is_dir()

    second = prepare_state(
        db,
        state_root=state,
        backup_root=backup,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        task_proof=proof,
    )
    assert second == receipt
    with sqlite3.connect(db) as connection:
        current = inventory(connection)
    assert current["inventory_contract_sha256"] == receipt["inventory_sha256"]


def test_state_preparation_fails_closed_on_wal_drift(tmp_path: Path) -> None:
    receipt, db, state, proof, backup = _prepare(tmp_path)
    wal = db.with_name("shadow_real.sqlite-wal")
    wal.write_bytes(b"unexpected wal drift")

    with pytest.raises(StatePreparationError, match="database hashes"):
        prepare_state(
            db,
            state_root=state,
            backup_root=backup,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            task_proof=proof,
        )
    assert receipt["status"] == "COMPLETE"


def test_state_preparation_rejects_partial_inventory_then_repairs_idempotently(
    tmp_path: Path,
) -> None:
    receipt, db, state, proof, backup = _prepare(tmp_path)
    (state / "receipts" / "state-preparation" / f"state-preparation-{CANDIDATE_SHA}.json").unlink()
    with sqlite3.connect(db) as connection:
        connection.execute("DROP TRIGGER expected_market_sessions_no_update")
        connection.commit()
        with pytest.raises(StatePreparationError, match="triggers are missing"):
            inventory(connection)

    repaired = prepare_state(
        db,
        state_root=state,
        backup_root=backup,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        task_proof=proof,
    )
    assert repaired["status"] == "COMPLETE"
    assert repaired["inventory_sha256"] == receipt["inventory_sha256"]


def test_state_preparation_retains_recovery_evidence_on_injected_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, state, proof, backup = _state_fixture(tmp_path)
    import scripts.state_disaster_recovery as recovery

    def fail_restore(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected restore verification failure")

    monkeypatch.setattr(recovery, "restore_verify", fail_restore)
    with pytest.raises(StatePreparationError, match="recovery evidence"):
        prepare_state(
            db,
            state_root=state,
            backup_root=backup,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            task_proof=proof,
        )
    failed = (
        state / "receipts" / "state-preparation" / f"state-preparation-{CANDIDATE_SHA}.failed.json"
    )
    evidence = json.loads(failed.read_text(encoding="utf-8"))
    assert evidence["status"] == "FAILED_BEFORE_COMPLETE"
    assert evidence["recovery_evidence"] == "online_backup_restore_verify_required"
    assert list(backup.glob("state-preparation-*"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("canonical_running_count", 1, "not be running"),
        ("capture_state", "Ready", "present and Disabled"),
        ("capture_running", True, "present and Disabled"),
        ("research_only", False, "safety flags"),
        ("broker_execution_enabled", True, "safety flags"),
    ],
)
def test_task_proof_hostile_mutations_fail_closed(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    proof = _task_proof(tmp_path / "task-proof.json")
    payload = json.loads(proof.read_text(encoding="utf-8"))
    payload[field] = value
    proof.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StatePreparationError, match=message):
        inspect_task_proof(proof)


def _capture_receipt() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "dawnstrike.capture_task_rebind_receipt.v1",
        "status": "COMPLETE",
        "task_name": CAPTURE_TASK_NAME,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "runtime_origin_sha256": _sha("origin"),
        "previous_candidate_sha": "c" * 40,
        "xml_before_sha256": _sha("xml-before"),
        "xml_after_sha256": _sha("xml-after"),
        "action_before_sha256": _sha("action-before"),
        "action_after_sha256": _sha("action-after"),
        "definition_before_sha256": _sha("definition-before"),
        "definition_after_sha256": _sha("definition-after"),
        "principal_sha256": _sha("principal"),
        "trigger_sha256": _sha("trigger"),
        "settings_sha256": _sha("settings"),
        "symbols_manifest_sha256": _sha("symbols"),
        "entitlement_receipt_sha256": _sha("entitlement"),
        "source_config_sha256": _sha("source"),
        "enablement_before": "Disabled",
        "enablement_after": "Ready",
        "changed_field": "candidate_sha",
        "preserved_contract": True,
        "completed_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    value["receipt_sha256"] = capture_self_hash(value, "receipt_sha256")
    return value


def test_capture_receipt_rejects_xml_action_and_safety_tamper() -> None:
    receipt = _capture_receipt()
    assert validate_capture_receipt(receipt, candidate_sha=CANDIDATE_SHA)
    for field, value in (
        ("xml_after_sha256", "f" * 64),
        ("action_after_sha256", "e" * 64),
        ("principal_sha256", "d" * 64),
        ("trigger_sha256", "c" * 64),
        ("settings_sha256", "b" * 64),
    ):
        tampered = dict(receipt)
        tampered[field] = value
        with pytest.raises(CaptureTaskContractError, match="self-hash"):
            validate_capture_receipt(tampered)
    for field, value, message in (
        ("research_only", False, "safety flags"),
        ("broker_execution_enabled", True, "safety flags"),
        ("enablement_before", "Ready", "disabled before"),
        ("changed_field", "principal", "more than candidate"),
    ):
        tampered = dict(receipt)
        tampered[field] = value
        tampered["receipt_sha256"] = capture_self_hash(tampered, "receipt_sha256")
        with pytest.raises(CaptureTaskContractError, match=message):
            validate_capture_receipt(tampered)


def test_state_receipt_candidate_and_broker_tamper_fail_closed(tmp_path: Path) -> None:
    receipt, _db, _state, _proof, _backup = _prepare(tmp_path)
    with pytest.raises(StatePreparationError, match="candidate SHA"):
        validate_state_receipt(receipt, candidate_sha="f" * 40, candidate_tree=CANDIDATE_TREE)
    tampered = dict(receipt)
    tampered["broker_execution_enabled"] = True
    tampered["receipt_sha256"] = state_self_hash(tampered, "receipt_sha256")
    with pytest.raises(StatePreparationError, match="safety flags"):
        validate_state_receipt(tampered)
