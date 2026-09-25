from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_LOCK = ROOT / "scripts" / "runtime_activation_lock.ps1"
DAILY_LOCK = ROOT / "scripts" / "invoke_dawnstrike_stage.ps1"
POWERSHELL = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")


def _ps_quote(value: Path) -> str:
    return str(value.resolve()).replace("'", "''")


def _run_powershell(
    command: str,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
        env=environment,
    )


@pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL.is_file(),
    reason="terminal recovery lock execution requires Windows PowerShell 5.1",
)
@pytest.mark.parametrize(
    ("crash_marker", "enabled_count", "disabled_count"),
    [
        pytest.param("PARTIAL_ENABLE", 2, 3, id="partial-canonical-enable"),
        pytest.param("ALL_READY", 5, 0, id="all-ready-before-cleanup"),
    ],
)
def test_terminal_recovery_hard_kill_adopts_daily_lock_and_cleans_up(
    tmp_path: Path,
    crash_marker: str,
    enabled_count: int,
    disabled_count: int,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    marker = state / "terminal-task-state.json"
    operation_id = "1" * 32
    market_date = "2026-09-03"
    journal = (
        state
        / "receipts"
        / "runtime-operation"
        / f"terminal-recovery-{operation_id}.json"
    )
    runtime_path = state / "locks" / "dawnstrike-runtime-activation.lock"
    daily_path = state / "locks" / f"dawnstrike-daily-{market_date}.lock"

    module_q = _ps_quote(RUNTIME_LOCK)
    daily_module_q = _ps_quote(DAILY_LOCK)
    state_q = _ps_quote(state)
    marker_q = _ps_quote(marker)
    python_q = _ps_quote(Path(sys.executable))

    common = rf"""
. '{module_q}'
. '{daily_module_q}'
$script:DawnstrikeApprovedPythonPath = '{python_q}'
$approved = Get-DawnstrikeApprovedLockInterpreter
$origin = 'github.com/mattfren/dawnstrike'
$journal = Get-DawnstrikeTerminalRecoveryJournalPath `
    -StateRoot '{state_q}' -OperationId '{operation_id}'
"""
    acquire_and_crash = (
        common
        + rf"""
$runtimeLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal `
    -StateRoot '{state_q}' -JournalPath $journal `
    -Operation runtime_activation `
    -CandidateSha ('a' * 40) -CandidateTree ('b' * 40) `
    -CurrentSha ('a' * 40) -CurrentTree ('b' * 40) `
    -PreviousSha ('a' * 40) -PreviousTree ('b' * 40) `
    -OriginIdentity $origin `
    -PreparedReceiptRelativePath 'receipts/runtime-activation/complete.json' `
    -CompleteReceiptRelativePath 'receipts/runtime-activation/complete.json' `
    -TaskContractSha256 ('5' * 64) `
    -PythonPath $approved.path -PythonSha256 $approved.sha256
$dailyLock = Enter-DawnstrikeDailyRunLock `
    -StateRoot '{state_q}' -MarketDate '{market_date}' `
    -Owner runtime_activation -RetainHandle
if (-not $dailyLock.acquired) {{ throw 'Initial retained daily lock was not acquired.' }}
$null = Confirm-DawnstrikeActivationDailyLockHandshake `
    -StateRoot '{state_q}' -ActivationLock $runtimeLock -DailyLock $dailyLock
$marker = [ordered]@{{
    phase = '{crash_marker}'
    enabled_count = {enabled_count}
    disabled_count = {disabled_count}
}}
[IO.File]::WriteAllText(
    '{marker_q}',
    ($marker | ConvertTo-Json -Compress),
    [Text.UTF8Encoding]::new($false)
)
# Deliberately bypass every finally/Exit path. Windows closes the retained
# handles but leaves both exact lock files and the INIT recovery journal.
Stop-Process -Id $PID -Force
"""
    )
    environment = dict(os.environ)
    environment["DAWNSTRIKE_TEST_LOCK_JOURNAL"] = "1"
    crashed = _run_powershell(acquire_and_crash, environment=environment)
    assert crashed.returncode != 0, (crashed.stdout, crashed.stderr)
    assert marker.is_file()
    assert journal.is_file()
    assert runtime_path.is_file()
    assert daily_path.is_file()

    recover = (
        common
        + rf"""
if (-not (Test-Path -LiteralPath $journal -PathType Leaf)) {{
    throw 'Terminal recovery journal was not retained across the hard kill.'
}}
$runtimeLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
    -StateRoot '{state_q}' -JournalPath $journal `
    -CandidateSha ('a' * 40) -CandidateTree ('b' * 40) `
    -OriginIdentity $origin `
    -PythonPath $approved.path -PythonSha256 $approved.sha256
$dailyLock = Enter-DawnstrikeDailyRunLock `
    -StateRoot '{state_q}' -MarketDate '{market_date}' `
    -Owner runtime_activation -RetainHandle
if (-not $dailyLock.acquired) {{ throw 'Dead retained daily lock did not converge.' }}
$null = Confirm-DawnstrikeActivationDailyLockHandshake `
    -StateRoot '{state_q}' -ActivationLock $runtimeLock -DailyLock $dailyLock
$initial = Get-Content -LiteralPath '{marker_q}' -Raw | ConvertFrom-Json
if ([string]$initial.phase -eq 'PARTIAL_ENABLE') {{
    # This models the caller's locked fail-close normalization before it
    # resumes exact enablement after a mid-loop process death.
    $disabled = [ordered]@{{ phase = 'FAIL_CLOSED'; enabled_count = 0; disabled_count = 5 }}
    [IO.File]::WriteAllText(
        '{marker_q}',
        ($disabled | ConvertTo-Json -Compress),
        [Text.UTF8Encoding]::new($false)
    )
}}
elseif ([string]$initial.phase -ne 'ALL_READY') {{
    throw 'Unexpected retained terminal task marker.'
}}
$null = Confirm-DawnstrikeActivationDailyLockHandshake `
    -StateRoot '{state_q}' -ActivationLock $runtimeLock -DailyLock $dailyLock
$ready = [ordered]@{{ phase = 'ALL_READY'; enabled_count = 5; disabled_count = 0 }}
[IO.File]::WriteAllText(
    '{marker_q}',
    ($ready | ConvertTo-Json -Compress),
    [Text.UTF8Encoding]::new($false)
)
$null = Confirm-DawnstrikeActivationDailyLockHandshake `
    -StateRoot '{state_q}' -ActivationLock $runtimeLock -DailyLock $dailyLock
Exit-DawnstrikeDailyRunLock -Lock $dailyLock
Exit-DawnstrikeGovernedTerminalRecoveryLockWithJournal `
    -StateRoot '{state_q}' -JournalPath $journal -Lock $runtimeLock `
    -Operation runtime_activation `
    -CandidateSha ('a' * 40) -CandidateTree ('b' * 40) `
    -OriginIdentity $origin `
    -PythonPath $approved.path -PythonSha256 $approved.sha256
$final = Get-Content -LiteralPath '{marker_q}' -Raw | ConvertFrom-Json
$archives = @(Get-ChildItem -LiteralPath (Join-Path '{state_q}' 'locks') `
    -Filter 'dawnstrike-daily-*.lock.stale-dead-*' -File -Force)
[pscustomobject]@{{
    initial_phase = [string]$initial.phase
    final_phase = [string]$final.phase
    final_enabled_count = [int]$final.enabled_count
    final_disabled_count = [int]$final.disabled_count
    runtime_lock_present = Test-Path -LiteralPath '{_ps_quote(runtime_path)}'
    daily_lock_present = Test-Path -LiteralPath '{_ps_quote(daily_path)}'
    journal_present = Test-Path -LiteralPath $journal
    archived_dead_daily_count = $archives.Count
}} | ConvertTo-Json -Compress
"""
    )
    recovered = _run_powershell(recover, environment=environment)
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    proof = json.loads(recovered.stdout.strip().splitlines()[-1])
    assert proof == {
        "initial_phase": crash_marker,
        "final_phase": "ALL_READY",
        "final_enabled_count": 5,
        "final_disabled_count": 0,
        "runtime_lock_present": False,
        "daily_lock_present": False,
        "journal_present": False,
        "archived_dead_daily_count": 1,
    }
    assert not runtime_path.exists()
    assert not daily_path.exists()
    assert not journal.exists()
