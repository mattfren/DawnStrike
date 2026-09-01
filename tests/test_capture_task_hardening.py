"""Hostile and source-contract coverage for delayed-SIP task hardening."""

import hashlib
import json
import marshal
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.capture_task_hardening_contract import (
    CAPTURE_TASK_HARDENING_RECEIPT_SCHEMA,
    CaptureTaskHardeningContractError,
    load_prepared,
    seal_prepared,
    seal_receipt,
    self_hash,
    validate_receipt,
)


def _prepared() -> dict[str, object]:
    sha = "a" * 64
    payload: dict[str, object] = {
        "schema_version": "dawnstrike.capture_task_hardening_prepared.v2",
        "status": "PREPARED",
        "task_name": "Dawnstrike Delayed SIP Capture",
        "task_path": "\\",
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "original_state": "Ready",
        "backup_path": r"C:\state\scheduler-backups\capture-hardening-aaaaaaaa\task.xml",
        "backup_xml_sha256": sha,
        "backup_xml_file_sha256": sha,
        "xml_before_sha256": sha,
        "xml_after_sha256": "c" * 64,
        "action_sha256": sha,
        "trigger_sha256": sha,
        "principal_sha256": sha,
        "settings_sha256": sha,
        "action_before_sha256": sha,
        "action_after_sha256": "c" * 64,
        "runtime_head": "a" * 40,
        "runtime_tree": "b" * 40,
        "runtime_origin": "https://github.com/mattfren/DawnStrike.git",
        "runtime_origin_sha256": hashlib.sha256(
            b"https://github.com/mattfren/DawnStrike.git"
        ).hexdigest(),
        "lock_token": "d" * 32,
        "lock_bytes_sha256": sha,
        "interpreter_path": r"C:\Python313\python.exe",
        "interpreter_sha256": sha,
        "interpreter_version": "3.13.7",
        "interpreter_signer_subject": (
            "CN=Python Software Foundation, O=Python Software Foundation, "
            "L=Beaverton, S=Oregon, C=US"
        ),
        "interpreter_signer_thumbprint": "E" * 40,
        "runner_before_sha256": sha,
        "runner_target_sha256": "c" * 64,
        "old_last_task_result": 2147942402,
        "old_last_run_time": "2026-08-31T15:20:00.0000000Z",
        "intended_receipt_path": (
            r"C:\state\receipts\capture-task\capture-task-hardening-"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
        ),
        "rollback_contract": "RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED",
        "research_only": True,
        "broker_execution_enabled": False,
        "prepared_at_utc": "2026-08-31T23:59:00.0000000Z",
        "input_stage": "LEGACY_MIGRATION",
    }
    payload["prepared_record_sha256"] = self_hash(payload, "prepared_record_sha256")
    return payload


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
            "scheduler-backups/capture-hardening/Dawnstrike_Delayed_SIP_Capture.xml"
        ),
        "prepared_relative_path": (
            "scheduler-backups/capture-hardening/capture-task-hardening-prepared.json"
        ),
        **hashes,
        "changed_fields": ["principal", "settings"],
        "preserved_action": True,
        "preserved_trigger": True,
        "preserved_input_bindings": True,
        "prepared_record_sha256": hashlib.sha256(b"prepared").hexdigest(),
        "origin_main_refreshed_at_utc": "2026-08-31T23:59:00.0000000Z",
        "origin_url": "https://example.invalid/dawnstrike.git",
        "origin_url_sha256": hashlib.sha256(b"https://example.invalid/dawnstrike.git").hexdigest(),
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


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:password@example.invalid/repo.git",
        "https://example.invalid/repo.git?access_token=secret",
    ],
)
def test_hardening_receipt_rejects_credentialed_origin(origin: str) -> None:
    payload = _receipt()
    payload["origin_url"] = origin
    payload["origin_url_sha256"] = hashlib.sha256(origin.encode()).hexdigest()
    payload["receipt_sha256"] = self_hash(payload, "receipt_sha256")
    with pytest.raises(CaptureTaskHardeningContractError, match="origin"):
        validate_receipt(payload)


def test_hardening_receipt_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
    from scripts.capture_task_hardening_contract import load_receipt

    with pytest.raises(CaptureTaskHardeningContractError, match="duplicate JSON field"):
        load_receipt(path)


def test_prepared_v2_is_strict_self_hashed_and_tamper_evident(tmp_path: Path) -> None:
    path = tmp_path / "prepared.json"
    payload = _prepared()
    assert seal_prepared(payload, path) == payload
    assert load_prepared(path) == payload
    path.write_text(
        path.read_text(encoding="utf-8").replace('"status":"PREPARED"', '"status":"BROKEN"'),
        encoding="utf-8",
    )
    with pytest.raises(CaptureTaskHardeningContractError):
        load_prepared(path)


def test_prepared_v2_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / "prepared-duplicate.json"
    path.write_text('{"status":"PREPARED","status":"BROKEN"}', encoding="utf-8")
    with pytest.raises(CaptureTaskHardeningContractError, match="duplicate JSON field"):
        load_prepared(path)


def test_direct_capture_python_isolation_bypasses_hostile_local_pyc(tmp_path: Path) -> None:
    """The scheduled action must not import stale bytecode from RuntimeRoot."""
    module_root = tmp_path / "runtime"
    module_root.mkdir()
    source = module_root / "hostile_capture_module.py"
    source.write_text("VALUE = 'source'\n", encoding="utf-8")
    pycache = module_root / "__pycache__"
    pycache.mkdir()
    pyc_path = (
        pycache
        / f"hostile_capture_module.cpython-{sys.version_info.major}{sys.version_info.minor}.pyc"
    )
    py_compile.compile(str(source), cfile=str(pyc_path), doraise=True)
    original_pyc = pyc_path.read_bytes()
    # Keep the valid source-metadata header while replacing only the code object.
    malicious_code = compile("VALUE = 'pycache'\n", str(source), "exec")
    pyc_path.write_bytes(original_pyc[:16] + marshal.dumps(malicious_code))

    ordinary = subprocess.run(
        [
            sys.executable,
            "-c",
            "import hostile_capture_module; print(hostile_capture_module.VALUE)",
        ],
        cwd=module_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert ordinary.stdout.strip() == "pycache"

    governed_prefix = tmp_path / "capture-bytecode" / ("a" * 40)
    governed_prefix.mkdir(parents=True)
    isolated = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-X",
            f"pycache_prefix={governed_prefix}",
            "-u",
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(module_root)!r}); "
                "import hostile_capture_module; print(hostile_capture_module.VALUE)"
            ),
        ],
        cwd=module_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert isolated.stdout.strip() == "source"
    assert list(governed_prefix.rglob("*")) == []


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_capture_bytecode_prefix_rejects_nonempty_and_reparse_directories(
    tmp_path: Path,
) -> None:
    safety = Path("scripts/capture_task_safety.ps1").resolve()
    prefix = tmp_path / "prefix"
    prefix.mkdir()

    def ps_quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    nonempty = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                f". {ps_quote(safety)}; New-Item -ItemType File -Path "
                f"{ps_quote(prefix / 'stale.pyc')} | Out-Null; "
                f"try {{ Assert-DawnstrikeCaptureBytecodePrefix {ps_quote(prefix)}; exit 11 }} "
                "catch { exit 0 }"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert nonempty.returncode == 0, (nonempty.stdout, nonempty.stderr)

    (prefix / "stale.pyc").unlink()
    target = tmp_path / "target"
    target.mkdir()
    junction = prefix / "junction"
    junction_result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                f". {ps_quote(safety)}; New-Item -ItemType Junction -Path "
                f"{ps_quote(junction)} -Target {ps_quote(target)} | Out-Null; "
                f"try {{ Assert-DawnstrikeCaptureBytecodePrefix {ps_quote(junction)}; exit 12 }} "
                "catch { exit 0 }"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction_result.returncode not in (0, 12):
        pytest.skip("junction creation is unavailable on this host")
    assert junction_result.returncode == 0, (
        junction_result.stdout,
        junction_result.stderr,
    )


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_hardening_recovery_child_process_crash_boundaries(tmp_path: Path) -> None:
    """PREPARED, scheduler update, and COMPLETE crashes recover idempotently."""
    helper = Path("scripts/capture_task_hardening_recovery.ps1").resolve()
    backup = tmp_path / "backup.xml"
    prepared_path = tmp_path / "capture-task-hardening.prepared.json"
    current_path = tmp_path / "current-task.json"
    receipt_path = tmp_path / "capture-task-hardening.json"
    before_xml = '<Task xmlns="urn:test"><Settings><Enabled>true</Enabled></Settings></Task>'
    after_xml = '<Task xmlns="urn:test"><Settings><Enabled>false</Enabled></Settings></Task>'
    backup.write_text(before_xml, encoding="utf-8")

    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    candidate_sha = "a" * 40
    candidate_tree = "b" * 40
    runtime = {
        "head": "c" * 40,
        "tree": "d" * 40,
        "origin": "github.com/mattfren/dawnstrike",
        "runner_sha256": "e" * 64,
    }
    interpreter = {
        "path": r"C:\Python313\python.exe",
        "sha256": "f" * 64,
        "version": "3.13.7",
        "signer_subject": "CN=Python Software Foundation",
        "signer_thumbprint": "1" * 40,
    }
    prepared = {
        "schema_version": "dawnstrike.capture_task_hardening_prepared.v2",
        "status": "PREPARED",
        "task_name": "Dawnstrike Delayed SIP Capture",
        "task_path": "\\",
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "runtime_head": runtime["head"],
        "runtime_tree": runtime["tree"],
        "runtime_origin": runtime["origin"],
        "runner_before_sha256": runtime["runner_sha256"],
        "interpreter_path": interpreter["path"],
        "interpreter_sha256": interpreter["sha256"],
        "interpreter_version": interpreter["version"],
        "interpreter_signer_subject": interpreter["signer_subject"],
        "interpreter_signer_thumbprint": interpreter["signer_thumbprint"],
        "intended_receipt_path": str(receipt_path),
        "original_state": "Ready",
        "xml_before_sha256": digest(before_xml),
        "xml_after_sha256": digest(after_xml),
        "backup_path": str(backup),
        "backup_xml_sha256": digest(before_xml),
        "backup_xml_file_sha256": digest(before_xml),
    }
    prepared_path.write_text(json.dumps(prepared), encoding="utf-8")

    def ps_quote(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    common = (
        f". {ps_quote(helper)}\n"
        f"$prepared = Get-Content -LiteralPath {ps_quote(prepared_path)} -Raw "
        "| ConvertFrom-Json\n"
        f"$current = Get-Content -LiteralPath {ps_quote(current_path)} -Raw "
        "| ConvertFrom-Json\n"
        "$runtime = [pscustomobject]@{ "
        f"head='{runtime['head']}'; tree='{runtime['tree']}'; "
        f"origin='{runtime['origin']}'; "
        f"runner_sha256='{runtime['runner_sha256']}' }}\n"
        "$interpreter = [pscustomobject]@{ "
        f"path='{interpreter['path']}'; sha256='{interpreter['sha256']}'; "
        f"version='{interpreter['version']}'; "
        f"signer_subject='{interpreter['signer_subject']}'; "
        f"signer_thumbprint='{interpreter['signer_thumbprint']}' }}\n"
        "$recoveryArgs = @{"
        f" Prepared=$prepared; CurrentTask=$current; PreparedPath="
        f"{ps_quote(prepared_path)}; ExpectedTaskName='Dawnstrike Delayed SIP Capture';"
        " ExpectedTaskPath='\\';"
        f" ExpectedCandidateSha='{candidate_sha}'; ExpectedCandidateTree='{candidate_tree}';"
        " RuntimeIdentity=$runtime; InterpreterIdentity=$interpreter;"
        f" ExpectedReceiptPath={ps_quote(receipt_path)} }}\n"
        "$null = Assert-HardeningPreparedRecoveryState @recoveryArgs\n"
    )

    def run_child(body: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", body],
            text=True,
            capture_output=True,
            check=False,
        )

    crashed = run_child(
        f"$prepared = Get-Content -LiteralPath {ps_quote(prepared_path)} -Raw; "
        f"[IO.File]::WriteAllText({ps_quote(prepared_path)}, $prepared); exit 17"
    )
    assert crashed.returncode == 17
    current_path.write_text(
        json.dumps({"state": "Disabled", "task_path": "\\", "xml_sha256": digest(after_xml)}),
        encoding="utf-8",
    )
    crashed = run_child("Start-Sleep -Milliseconds 1; exit 23")
    assert crashed.returncode == 23
    checked = run_child(
        common
        + "[IO.File]::WriteAllText("
        + ps_quote(receipt_path)
        + ', \'{"status":"COMPLETE"}\'); exit 31'
    )
    assert checked.returncode == 31, (checked.stdout, checked.stderr)
    retry = run_child(common + "Write-Output 'IDEMPOTENT'")
    assert retry.returncode == 0, (retry.stdout, retry.stderr)
    assert retry.stdout.strip() == "IDEMPOTENT"


def test_hardening_script_is_explicit_and_preserves_capture_bindings() -> None:
    script = Path("scripts/harden_intraday_capture_task.ps1").read_text(encoding="utf-8")
    registration = Path("scripts/register_intraday_capture_task.ps1").read_text(encoding="utf-8")
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
        '"-I", "-B", "-X"',
        "pycache_prefix=",
        "if (-not $recoveringPrepared)",
    ):
        assert marker in script
    assert "goto hardening_payload" not in script
    assert "Assert-DawnstrikeCaptureBytecodePrefix" in Path(
        "scripts/capture_task_safety.ps1"
    ).read_text(encoding="utf-8")
    assert "[switch]$ReplaceExisting" in registration
    assert "harden_intraday_capture_task.ps1" in registration
    assert "InteractiveCurrentUser" in registration


def test_standalone_entrypoints_forward_credential_without_serializing_it() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    for script in (activation, rollback):
        assert "[pscredential]$RunAsCredential" in script
        assert "-RunAsCredential $RunAsCredential" in script
        assert "ConvertTo-Json $RunAsCredential" not in script
        assert "Write-Output $RunAsCredential" not in script
    assert "$rollbackRunAsCredential = $RunAsCredential" in rollback


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
        marker in (result.stdout + result.stderr) for marker in ("RunAsCredential", "CandidateSha")
    )
    assert "password" not in (result.stdout + result.stderr).lower().replace("runascredential", "")
