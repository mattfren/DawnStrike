# ruff: noqa: E501
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
LAUNCHER = ROOT / "scripts" / "dawnstrike_release_launcher.ps1"
ACTIVATE = ROOT / "scripts" / "activate_dawnstrike_runtime.ps1"
REBIND = ROOT / "scripts" / "rebind_intraday_capture_task.ps1"
ROLLBACK = ROOT / "scripts" / "rollback_dawnstrike_runtime.ps1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_launcher_routes_terminal_reconciliation_only_to_supported_modes() -> None:
    launcher = _text(LAUNCHER)
    harden = _between(
        launcher,
        "elseif ($Mode -eq 'HardenCapture') {",
        "elseif ($Mode -in @('BootstrapBaseline', 'Activate')) {",
    )
    activate = _between(
        launcher,
        "elseif ($Mode -in @('BootstrapBaseline', 'Activate')) {",
        "elseif ($Mode -eq 'RebindCapture') {",
    )
    rebind = _between(
        launcher,
        "elseif ($Mode -eq 'RebindCapture') {",
        "elseif ($Mode -eq 'Rollback') {",
    )
    rollback = _between(
        launcher,
        "elseif ($Mode -eq 'Rollback') {",
        "elseif ($Mode -eq 'BootstrapUniverse') {",
    )

    assert "StateBoundaryTerminalReconciliationRequired" not in harden
    for block in (activate, rebind, rollback):
        assert "-StateBoundaryTaskMutationOperationId" in block
        assert "-StateBoundaryTerminalReconciliationRequired:" in block

    enter = launcher.index("Enter-DawnstrikeStateBoundaryTaskMutation")
    derive = launcher.index("$stateBoundaryTerminalReconciliationRequired = (", enter)
    dispatch = launcher.index("elseif ($Mode -in @('BootstrapBaseline', 'Activate')) {", derive)
    assert enter < derive < dispatch


def test_rebind_disabled_terminal_requires_exact_protected_intent_and_predecessor() -> None:
    rebind = _text(REBIND)
    authorization = _between(
        rebind,
        "function Assert-DawnstrikeRebindStateBoundaryTerminalRecoveryAuthorization {",
        "function Get-DawnstrikeAuxiliarySectionHash {",
    )
    for marker in (
        "-AllowedTaskMutationOperationId $OperationId",
        "Get-DawnstrikeStateBoundaryTaskMutationIntent",
        "-Intent $intent -StateRoot $StateRoot -Mode RebindCapture",
        "-ExpectedSha $CandidateSha -ExpectedTree $CandidateTree",
        "old_current_receipt_sha256",
        "old_task_binding_sha256",
        "predecessor_task_path",
        "predecessor_definition_sha256",
        "predecessor_definition_contract_sha256",
        "predecessor_action_contract_sha256",
    ):
        assert marker in authorization

    auth_call = rebind.index(
        "$stateBoundaryTerminalRecoveryAuthorization = Assert-DawnstrikeRebindStateBoundaryTerminalRecoveryAuthorization"
    )
    backup = rebind.index(
        "$original = Get-DawnstrikeCaptureOriginalFromActivationBackup", auth_call
    )
    predecessor_check = rebind.index(
        'throw "Disabled terminal rebind recovery activation backup is not the protected predecessor task."',
        backup,
    )
    receipt_fast_path = rebind.index("$existingDisabledTerminal = (", predecessor_check)
    assert auth_call < backup < predecessor_check < receipt_fast_path


def test_rebind_proves_terminal_before_enable_and_redisables_on_any_failure() -> None:
    rebind = _text(REBIND)
    recovery = _between(
        rebind,
        "if ($existingDisabledTerminal) {",
        "else {\n                # The no-lock COMPLETE fast path",
    )
    pre_enable_proof = recovery.index(
        "Assert-DawnstrikeCaptureRebindCompleteTerminal `"
    )
    enable = recovery.index("Enable-ScheduledTask `")
    ready_proof = recovery.index(
        "Assert-DawnstrikeCaptureRebindCompleteTerminal @terminalValidationArguments",
        enable,
    )
    redisable = recovery.index("Disable-DawnstrikeAuxiliaryCaptureTask", ready_proof)
    disabled_proof = recovery.index("state -cne 'Disabled'", redisable)
    assert pre_enable_proof < enable < ready_proof < redisable < disabled_proof
    terminal_validator = _between(
        rebind,
        "function Assert-DawnstrikeCaptureRebindCompleteTerminal {",
        "function Assert-DawnstrikeCaptureHardeningBoundary {",
    )
    assert "task path is not the activation-bound path" in terminal_validator

    cleanup = _between(
        rebind,
        "if ($null -ne $adoptedCompleteLock) {",
        "Write-Output ([string]$existingReceipt.Stdout).Trim()",
    )
    assert "Remove-DawnstrikeCapturePrepared" in cleanup
    assert "catch {" in cleanup
    assert "Disable-DawnstrikeAuxiliaryCaptureTask" in cleanup
    assert "state -cne 'Disabled'" in cleanup


def test_activate_terminal_recovery_retains_runtime_and_daily_exclusion() -> None:
    activate = _text(ACTIVATE)
    recovery = _between(
        activate,
        "function Complete-DawnstrikeProtectedTerminalCanonicalRecovery {",
        "function Write-DawnstrikeActivationJson {",
    )
    journal_path = recovery.index("Get-DawnstrikeTerminalRecoveryJournalPath `")
    runtime_lock = recovery.index("Enter-DawnstrikeGovernedRuntimeLockWithJournal `")
    daily_lock = recovery.index("Enter-DawnstrikeDailyRunLock `", runtime_lock)
    handshake = recovery.index(
        "Confirm-DawnstrikeActivationDailyLockHandshake `", daily_lock
    )
    locked_proof = recovery.index("$lockedTasks = Get-DawnstrikeTaskContract", handshake)
    normalize = recovery.index("Set-DawnstrikeTasksFailClosedDisabled", locked_proof)
    recovery_boundary = recovery.index(
        "Assert-DawnstrikePostFinalizerMutationWindow `", normalize
    )
    enable = recovery.index("Enable-DawnstrikeCanonicalTasks", recovery_boundary)
    final_proof = recovery.index(
        "$ready = Get-DawnstrikeTaskContract", enable
    )
    fail_closed = recovery.index("Set-DawnstrikeTasksFailClosedDisabled", final_proof)
    release_gate = recovery.index("if ($releaseRecoveryLocks", fail_closed)
    release_runtime = recovery.index(
        "Exit-DawnstrikeGovernedTerminalRecoveryLockWithJournal", release_gate
    )
    assert (
        journal_path
        < runtime_lock
        < daily_lock
        < handshake
        < locked_proof
        < normalize
        < recovery_boundary
        < enable
        < final_proof
        < fail_closed
        < release_gate
        < release_runtime
    )


def test_rebind_terminal_recovery_retains_runtime_exclusion() -> None:
    rebind = _text(REBIND)
    terminal = _between(
        rebind,
        "$terminalRecoveryJournalPath = if ($stateBoundaryTerminalRecoveryAuthorized) {",
        'if (-not $compensatedReceiptRecovered) { throw "Existing capture-task receipt',
    )
    journal_path = terminal.index("Get-DawnstrikeTerminalRecoveryJournalPath `")
    fresh_lock = terminal.index("Enter-DawnstrikeGovernedRuntimeLockWithJournal `")
    confirm = terminal.index("Confirm-DawnstrikeGovernedRuntimeLock", fresh_lock)
    disabled_proof = terminal.index(
        "Assert-DawnstrikeCaptureRebindCompleteTerminal `", confirm
    )
    enable = terminal.index("Enable-ScheduledTask `", disabled_proof)
    final_proof = terminal.index(
        "Assert-DawnstrikeCaptureRebindCompleteTerminal @terminalValidationArguments",
        enable,
    )
    fail_closed = terminal.index("Disable-DawnstrikeAuxiliaryCaptureTask", final_proof)
    release = terminal.index(
        "Exit-DawnstrikeGovernedTerminalRecoveryLockWithJournal", fail_closed
    )
    assert (
        journal_path
        < fresh_lock
        < confirm
        < disabled_proof
        < enable
        < final_proof
        < fail_closed
        < release
    )


def test_rollback_disabled_terminal_requires_exact_protected_intent_and_predecessor() -> None:
    rollback = _text(ROLLBACK)
    authorization = _between(
        rollback,
        "function Assert-DawnstrikeRollbackStateBoundaryTerminalRecoveryAuthorization {",
        "function Get-DawnstrikeActivationAuxiliaryRecoveryContract {",
    )
    for marker in (
        "-AllowedTaskMutationOperationId $OperationId",
        "Get-DawnstrikeStateBoundaryTaskMutationIntent",
        "-Intent $intent -StateRoot $StateRoot -Mode Rollback",
        "-ExpectedSha $CandidateSha -ExpectedTree $CandidateTree",
        "old_current_receipt_sha256",
        "old_task_binding_sha256",
        "predecessor canonical inventory is not exact Ready",
        "canonical_task_contract_sha256",
        "canonical_task_definition_contract_sha256",
        "canonical_task_action_contract_sha256",
    ):
        assert marker in authorization

    auth_call = rollback.index(
        "$stateBoundaryTerminalRecoveryAuthorization = Assert-DawnstrikeRollbackStateBoundaryTerminalRecoveryAuthorization"
    )
    activation_anchor = rollback.index(
        'throw "Disabled terminal rollback recovery activation task contract is not the protected predecessor contract."',
        auth_call,
    )
    receipt_fast_path = rollback.index("$disabledTerminalRecovery = (", activation_anchor)
    assert auth_call < activation_anchor < receipt_fast_path


def test_rollback_proves_runtime_tasks_and_auxiliary_before_exact_enable() -> None:
    rollback = _text(ROLLBACK)
    terminal = _between(
        rollback,
        'if ([string]$existingJournal.payload.phase -eq "COMPLETE") {',
        "# A failed rollback may restore the activated candidate",
    )
    auth = terminal.index("$stateBoundaryTerminalRecoveryAuthorized -and")
    stable_definition = terminal.index("task_definition_contract_sha256", auth)
    stable_action = terminal.index("task_action_contract_sha256", stable_definition)
    aux_proof = terminal.index("Get-DawnstrikeRollbackTerminalAuxiliaryRecovery", stable_action)
    deep_proof = terminal.index("Assert-DawnstrikeRollbackCompleteTerminal `", aux_proof)
    assert auth < stable_definition < stable_action < aux_proof < deep_proof
    recovery_journal = terminal.index("Get-DawnstrikeTerminalRecoveryJournalPath `")
    recovery_runtime_lock = terminal.index(
        "Enter-DawnstrikeGovernedRuntimeLockWithJournal `", recovery_journal
    )
    recovery_daily_lock = terminal.index(
        "Enter-DawnstrikeDailyRunLock `", recovery_runtime_lock
    )
    recovery_handshake = terminal.index(
        "Confirm-DawnstrikeActivationDailyLockHandshake `", recovery_daily_lock
    )
    locked_membership = terminal.index(
        "$lockedTasks = Get-DawnstrikeTaskContract", recovery_handshake
    )
    exact_prefix = terminal.index(
        "Assert-DawnstrikeProtectedRollbackEnablePrefix `", locked_membership
    )
    normalize = terminal.index("Set-DawnstrikeTasksFailClosedDisabled", exact_prefix)
    recovery_boundary = terminal.index(
        "$terminalRollbackBoundaryMode = Resolve-DawnstrikeProtectedRollbackBoundaryMode",
        normalize,
    )
    locked_deep_proof = terminal.index(
        "Assert-DawnstrikeRollbackCompleteTerminal `", recovery_boundary
    )
    reread_disabled = terminal.index(
        "$disabledTasks = Get-DawnstrikeTaskContract", locked_deep_proof
    )
    expired_safe_stop = terminal.index(
        "if ($terminalRollbackBoundaryMode -ceq 'EXPIRED_NO_RUN')", reread_disabled
    )
    enable = terminal.index("Enable-DawnstrikeCanonicalTasks", expired_safe_stop)
    aux_enable = terminal.index("Enable-ScheduledTask `", enable)
    ready_proof = terminal.index(
        "Assert-DawnstrikeRollbackCompleteTerminal @terminalValidationArguments", aux_enable
    )
    redisable_canonical = terminal.index("Set-DawnstrikeTasksFailClosedDisabled", ready_proof)
    redisable_auxiliary = terminal.index(
        "Disable-DawnstrikeAuxiliaryCaptureTask", redisable_canonical
    )
    release_gate = terminal.index(
        "if ($releaseTerminalRecoveryLocks", redisable_auxiliary
    )
    release_runtime = terminal.index(
        "Exit-DawnstrikeGovernedTerminalRecoveryLockWithJournal", release_gate
    )
    assert (
        recovery_journal
        < recovery_runtime_lock
        < recovery_daily_lock
        < recovery_handshake
        < locked_membership
        < exact_prefix
        < normalize
        < recovery_boundary
        < locked_deep_proof
        < reread_disabled
        < expired_safe_stop
        < enable
        < ready_proof
        < redisable_canonical
        < release_gate
        < release_runtime
    )

    auxiliary = _between(
        rollback,
        "function Get-DawnstrikeRollbackTerminalAuxiliaryRecovery {",
        "function Assert-DawnstrikeCapturePreparedRecovery {",
    )
    for marker in (
        "Get-DawnstrikeActivationAuxiliaryRecoveryContract",
        "auxiliary_capture_xml_sha256",
        "auxiliary_capture_definition_contract_sha256",
        "auxiliary_capture_action_contract_sha256",
        "definition_contract_sha256",
        "action_contract_sha256",
        "$AllowStateBoundaryDisabledTerminal",
        "requires_enable = $requiresEnable",
    ):
        assert marker in auxiliary


@pytest.mark.skipif(not POWERSHELL.exists(), reason="Windows PowerShell is unavailable")
def test_terminal_recovery_scripts_parse_in_windows_powershell_51() -> None:
    for path in (LAUNCHER, REBIND, ROLLBACK):
        quoted = str(path).replace("'", "''")
        command = (
            "$tokens=$null;$errors=$null;"
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{quoted}',"
            "[ref]$tokens,[ref]$errors);"
            "if(@($errors).Count -ne 0){$errors|ForEach-Object{$_.Message};exit 1}"
        )
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
