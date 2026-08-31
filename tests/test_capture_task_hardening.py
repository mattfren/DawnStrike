"""Hostile and source-contract coverage for delayed-SIP task hardening."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.capture_task_hardening_contract import (
    CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA,
    CaptureTaskHardeningContractError,
    seal_receipt,
    self_hash,
    validate_receipt,
)


def _receipt() -> dict[str, object]:
    hashes = {
        field: hashlib.sha256(field.encode()).hexdigest()
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
        )
    }
    payload: dict[str, object] = {
        "schema_version": CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA,
        "status": "COMPLETE",
        "task_name": "Dawnstrike Delayed SIP Capture",
        "task_path": "\\",
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "original_state": "Ready",
        "final_state": "Disabled",
        "backup_name": "Dawnstrike_Delayed_SIP_Capture.xml",
        "backup_relative_path": (
            "scheduler-backups/capture-hardening/"
            "Dawnstrike_Delayed_SIP_Capture.xml"
        ),
        "prepared_relative_path": (
            "scheduler-backups/capture-hardening/"
            "capture-task-hardening-prepared.json"
        ),
        **hashes,
        "changed_fields": ["principal", "settings"],
        "preserved_action": True,
        "preserved_trigger": True,
        "preserved_input_bindings": True,
        "prepared_record_sha256": hashlib.sha256(b"prepared").hexdigest(),
        "origin_main_refreshed_at_utc": "2026-08-31T23:59:00.0000000Z",
        "origin_url": "https://example.invalid/dawnstrike.git",
        "origin_url_sha256": hashlib.sha256(
            b"https://example.invalid/dawnstrike.git"
        ).hexdigest(),
        "old_last_task_result": 2147942402,
        "old_last_run_time": "2026-08-31T15:20:00.0000000Z",
        "new_last_task_result": 267011,
        "new_last_run_time": None,
        "history_reset_proven": True,
        "logon_type": "Password",
        "network_capable": True,
        "start_when_available": True,
        "wake_to_run": True,
        "battery_safe": True,
        "restart_count": 3,
        "restart_interval": "PT15M",
        "execution_time_limit": "PT3H",
        "multiple_instances": "IgnoreNew",
        "rollback_contract": "RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED",
        "research_only": True,
        "broker_execution_enabled": False,
        "completed_at_utc": "2026-08-31T23:59:59.0000000Z",
    }
    payload["receipt_sha256"] = self_hash(payload, "receipt_sha256")
    return payload


def test_hardening_receipt_round_trip_and_idempotent_seal(tmp_path: Path) -> None:
    receipt_path = tmp_path / "capture-task-hardening.json"
    payload = _receipt()
    assert seal_receipt(payload, receipt_path) == payload
    assert seal_receipt(payload, receipt_path) == payload
    assert validate_receipt(json.loads(receipt_path.read_text(encoding="utf-8"))) == payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("changed_fields", ["settings"]),
        ("preserved_action", False),
        ("wake_to_run", False),
        ("restart_count", 2),
        ("logon_type", "InteractiveToken"),
        ("broker_execution_enabled", True),
    ],
)
def test_hardening_receipt_rejects_safety_drift(field: str, value: object) -> None:
    payload = _receipt()
    payload[field] = value
    payload["receipt_sha256"] = self_hash(payload, "receipt_sha256")
    with pytest.raises(CaptureTaskHardeningContractError):
        validate_receipt(payload)


def test_hardening_receipt_rejects_sensitive_keys() -> None:
    payload = _receipt()
    payload["password_value"] = "must never persist"
    payload["receipt_sha256"] = self_hash(payload, "receipt_sha256")
    with pytest.raises(CaptureTaskHardeningContractError, match="sensitive field"):
        validate_receipt(payload)


@pytest.mark.parametrize("origin", [
    "https://user:password@example.invalid/repo.git",
    "https://example.invalid/repo.git?access_token=secret",
])
def test_hardening_receipt_rejects_credentialed_origin(origin: str) -> None:
    payload = _receipt()
    payload["origin_url"] = origin
    payload["origin_url_sha256"] = hashlib.sha256(origin.encode()).hexdigest()
    payload["receipt_sha256"] = self_hash(payload, "receipt_sha256")
    with pytest.raises(CaptureTaskHardeningContractError, match="origin"):
        validate_receipt(payload)


def test_hardening_receipt_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"schema_version":"x","schema_version":"y"}', encoding="utf-8"
    )
    from scripts.capture_task_hardening_contract import load_receipt

    with pytest.raises(CaptureTaskHardeningContractError, match="duplicate JSON field"):
        load_receipt(path)


def test_hardening_script_is_explicit_and_preserves_capture_bindings() -> None:
    script = Path("scripts/harden_intraday_capture_task.ps1").read_text(encoding="utf-8")
    registration = Path("scripts/register_intraday_capture_task.ps1").read_text(
        encoding="utf-8"
    )
    for setting in (
        '"Password"',
        '"LeastPrivilege"',
        '"StartWhenAvailable"',
        '"WakeToRun"',
        '"AllowStartIfOnBatteries"',
        '"DisallowStartIfOnBatteries"',
        '"DontStopIfGoingOnBatteries"',
        '"StopIfGoingOnBatteries"',
        '"ExecutionTimeLimit"',
        '"RestartOnFailure"',
        '"PT15M"',
        '"RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED"',
    ):
        assert setting in script
    for marker in (
        '"Actions"',
        '"Triggers"',
        "Hardening changed the capture action or input bindings.",
        "Register-ScheduledTask",
        "Unregister-ScheduledTask",
        "Get-ScheduledTaskInfo",
        "dawnstrike-runtime-activation.lock",
        "Restore-HardeningExactTask",
        "Get-Credential",
        "broker_execution_enabled = $false",
    ):
        assert marker in script
    assert "[switch]$ReplaceExisting" in registration
    assert "harden_intraday_capture_task.ps1" in registration
    assert "InteractiveCurrentUser" in registration


@pytest.mark.skipif(
    subprocess.run(["where", "powershell"], capture_output=True).returncode != 0,
    reason="Windows PowerShell unavailable",
)
def test_hardening_script_fails_closed_without_credential() -> None:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            "scripts/harden_intraday_capture_task.ps1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert any(
        marker in (result.stdout + result.stderr)
        for marker in ("RunAsCredential", "CandidateSha")
    )
    assert "password" not in (result.stdout + result.stderr).lower().replace(
        "runascredential", ""
    )
