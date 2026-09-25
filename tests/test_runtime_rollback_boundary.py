from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROLLBACK = ROOT / "scripts" / "rollback_dawnstrike_runtime.ps1"
ACTIVATION = ROOT / "scripts" / "activate_dawnstrike_runtime.ps1"
POWERSHELL = Path(
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)

def _ps_quote(value: Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL.is_file(),
    reason="requires Windows PowerShell 5.1",
)
def test_rollback_snapshot_enforces_progress_and_protected_recovery_modes() -> None:
    script = rf"""
$ErrorActionPreference = 'Stop'
. {_ps_quote(ROLLBACK)}

function Convert-LocalTestTimeToUtc([datetime]$Value) {{
  return [DateTimeOffset]::new(
    $Value,
    [TimeZoneInfo]::Local.GetUtcOffset($Value)
  ).ToUniversalTime()
}}

function New-RollbackSnapshots(
  [datetime]$PriorDay,
  [datetime]$NextDay,
  [datetime]$WeeklyLast,
  [string]$State = 'Ready'
) {{
  return @(
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps Morning'
      state=$State
      last_run_time=$PriorDay.AddHours(9)
      last_task_result=0
      next_run_time=if ($State -eq 'Disabled') {{
        [datetime]::MinValue
      }} else {{
        $NextDay.AddHours(8)
      }}
      start_when_available=$true
    }},
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps Monitor 5m'
      state=$State
      last_run_time=$PriorDay.AddHours(14)
      last_task_result=0
      next_run_time=if ($State -eq 'Disabled') {{
        [datetime]::MinValue
      }} else {{
        $NextDay.AddHours(8).AddMinutes(35)
      }}
      start_when_available=$true
    }},
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps EOD Full Report'
      state=$State
      last_run_time=$PriorDay.AddHours(15).AddMinutes(15)
      last_task_result=0
      next_run_time=if ($State -eq 'Disabled') {{
        [datetime]::MinValue
      }} else {{
        $NextDay.AddHours(15).AddMinutes(15)
      }}
      start_when_available=$true
    }},
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps V6 Weekly Training'
      state=$State
      last_run_time=$WeeklyLast
      last_task_result=0
      next_run_time=if ($State -eq 'Disabled') {{
        [datetime]::MinValue
      }} else {{
        $WeeklyLast.AddDays(7)
      }}
      start_when_available=$true
    }},
    [pscustomobject]@{{
      name='Dawnstrike 10of10 Daily Finalize'
      state=$State
      last_run_time=$PriorDay.AddHours(17).AddMinutes(30)
      last_task_result=0
      next_run_time=if ($State -eq 'Disabled') {{
        [datetime]::MinValue
      }} else {{
        $NextDay.AddHours(17).AddMinutes(30)
      }}
      start_when_available=$true
    }}
  )
}}

function Test-Rejected([scriptblock]$Action, [string]$Pattern) {{
  try {{ $null = & $Action; return $false }}
  catch {{ return [bool]($_.Exception.Message -match $Pattern) }}
}}

$target = [datetime]::new(2026, 9, 9, 0, 0, 0, [DateTimeKind]::Unspecified)
$prior = $target.AddDays(-1)
$now = Convert-LocalTestTimeToUtc $target.AddHours(7)
$base = New-RollbackSnapshots $prior $target $target.AddDays(-2).AddHours(21)
$basePass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc $now -RollbackTargetMarketDate '2026-09-09' `
  -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($base)

$running = New-RollbackSnapshots $prior $target $target.AddDays(-2).AddHours(21)
$running[1].state = 'Running'
$runningRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $now -RollbackTargetMarketDate '2026-09-09' `
    -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($running)
}} 'quiescent'

$targetProgress = New-RollbackSnapshots $prior $target $target.AddDays(-2).AddHours(21)
$targetProgress[0].last_run_time = $target.AddHours(6)
$targetProgressRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $now -RollbackTargetMarketDate '2026-09-09' `
    -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($targetProgress)
}} 'target-date task progress'

$failedCompletion = New-RollbackSnapshots `
  $prior $target $target.AddDays(-2).AddHours(21)
$failedCompletion[2].last_task_result = 1
$failedCompletionRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $now -RollbackTargetMarketDate '2026-09-09' `
    -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($failedCompletion)
}} 'successful ordered'

$expiredNow = Convert-LocalTestTimeToUtc $target.AddDays(1).AddHours(12)
$expired = New-RollbackSnapshots $prior $target $target.AddDays(-2).AddHours(21) 'Disabled'
$expiredPass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc $expiredNow -RollbackTargetMarketDate '2026-09-09' `
  -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($expired) `
  -BoundaryMode EXPIRED_NO_RUN -ProtectedInFlight

$prefix = New-RollbackSnapshots `
  $prior $target.AddDays(2) $target.AddDays(-2).AddHours(21) 'Disabled'
$prefix[0].state = 'Ready'
$prefix[1].state = 'Ready'
$prefix[0].next_run_time = $target.AddDays(2).AddHours(8)
$prefix[1].next_run_time = $target.AddDays(2).AddHours(8).AddMinutes(35)
$prefixPass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc $expiredNow -RollbackTargetMarketDate '2026-09-09' `
  -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($prefix) `
  -BoundaryMode EXPIRED_NO_RUN -ProtectedInFlight -AllowRecoveryEnablePrefix
$badPrefix = @($prefix)
$badPrefix[1].state = 'Disabled'
$badPrefix[2].state = 'Ready'
$badPrefixRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $expiredNow -RollbackTargetMarketDate '2026-09-09' `
    -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($badPrefix) `
    -BoundaryMode EXPIRED_NO_RUN -ProtectedInFlight -AllowRecoveryEnablePrefix
}} 'exact recovery prefix'

$recoveryWithRun = New-RollbackSnapshots `
  $target $target.AddDays(2) $target.AddDays(-2).AddHours(21)
$recoveryWithRun[0].last_run_time = $target.AddHours(9)
$recoveryWithRun[1].last_run_time = $target.AddHours(14)
$recoveryWithRun[2].last_run_time = $target.AddHours(15).AddMinutes(15)
$recoveryWithRun[4].last_run_time = $target.AddHours(17).AddMinutes(30)
$recoveryWithRunPass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc $expiredNow -RollbackTargetMarketDate '2026-09-09' `
  -RequiredCompletedMarketDate '2026-09-08' -TaskSnapshots @($recoveryWithRun) `
  -BoundaryMode RECOVERY_WITH_RUN -ProtectedInFlight -AllowRecoveryEnablePrefix
$recoveryUnknownTrigger = New-RollbackSnapshots `
  $target $target.AddDays(2) $target.AddDays(-2).AddHours(21) 'Disabled'
$recoveryUnknownTrigger[0].last_run_time = $target.AddHours(9)
$recoveryUnknownTrigger[1].last_run_time = $target.AddHours(14)
$recoveryUnknownTrigger[2].last_run_time = $target.AddHours(15).AddMinutes(15)
$recoveryUnknownTrigger[4].last_run_time = $target.AddHours(17).AddMinutes(30)
$recoveryUnknownTriggerRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $expiredNow -RollbackTargetMarketDate '2026-09-09' `
    -RequiredCompletedMarketDate '2026-09-08' `
    -TaskSnapshots @($recoveryUnknownTrigger) `
    -BoundaryMode RECOVERY_WITH_RUN -ProtectedInFlight
}} 'trigger to have advanced'

$monday = [datetime]::new(2026, 9, 7, 0, 0, 0, [DateTimeKind]::Unspecified)
$tuesday = $monday.AddDays(1)
$mondayBeforeWeekly = New-RollbackSnapshots $monday $tuesday $monday.AddDays(-7).AddHours(21)
$mondayBeforeWeekly[3].next_run_time = $monday.AddHours(21)
$monday2059 = Convert-LocalTestTimeToUtc $monday.AddHours(20).AddMinutes(59)
$mondayRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $monday2059 -RollbackTargetMarketDate '2026-09-08' `
    -RequiredCompletedMarketDate '2026-09-07' -TaskSnapshots @($mondayBeforeWeekly)
}} 'pending pre-target|pending Weekly'

$mondayAfterWeekly = New-RollbackSnapshots $monday $tuesday $monday.AddHours(21).AddMinutes(5)
$mondayAfterWeekly[3].next_run_time = $monday.AddDays(7).AddHours(21)
$monday2200 = Convert-LocalTestTimeToUtc $monday.AddHours(22)
$mondayPass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc $monday2200 -RollbackTargetMarketDate '2026-09-08' `
  -RequiredCompletedMarketDate '2026-09-07' -TaskSnapshots @($mondayAfterWeekly)

$sunday = [datetime]::new(2026, 9, 13, 0, 0, 0, [DateTimeKind]::Unspecified)
$nextMonday = $sunday.AddDays(1)
$sundaySnapshots = New-RollbackSnapshots $sunday $nextMonday $sunday.AddDays(-6).AddHours(21)
$sundayPass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc (Convert-LocalTestTimeToUtc $sunday.AddHours(22)) `
  -RollbackTargetMarketDate '2026-09-14' `
  -RequiredCompletedMarketDate '2026-09-11' -TaskSnapshots @($sundaySnapshots)

[pscustomobject]@{{
  base_pass = [bool]$basePass
  running_rejected = $runningRejected
  target_progress_rejected = $targetProgressRejected
  failed_completion_rejected = $failedCompletionRejected
  expired_no_run_pass = [bool]$expiredPass
  exact_enable_prefix_pass = [bool]$prefixPass
  non_prefix_rejected = $badPrefixRejected
  recovery_with_run_pass = [bool]$recoveryWithRunPass
  recovery_unknown_trigger_rejected = $recoveryUnknownTriggerRejected
  monday_2059_rejected = $mondayRejected
  monday_post_weekly_pass = [bool]$mondayPass
  sunday_to_monday_pass = [bool]$sundayPass
}} | ConvertTo-Json -Compress
"""
    completed = _run_powershell(script)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert all(result.values()), result


@pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL.is_file(),
    reason="requires Windows PowerShell 5.1",
)
def test_rollback_live_snapshot_clock_override_is_explicitly_guarded() -> None:
    script = rf"""
$ErrorActionPreference = 'Stop'
. {_ps_quote(ROLLBACK)}

$day = [datetime]::new(2026, 9, 9, 0, 0, 0, [DateTimeKind]::Unspecified)
$nowUtc = [DateTimeOffset]::new(
  $day.AddHours(7),
  [TimeZoneInfo]::Local.GetUtcOffset($day.AddHours(7))
).ToUniversalTime()
$script:TaskRows = @{{
  'Dawnstrike AlphaOps Morning' = [pscustomobject]@{{
    state='Ready';last=$day.AddDays(-1).AddHours(8);next=$day.AddHours(8)
  }}
  'Dawnstrike AlphaOps Monitor 5m' = [pscustomobject]@{{
    state='Ready';last=$day.AddDays(-1).AddHours(14);next=$day.AddHours(8).AddMinutes(35)
  }}
  'Dawnstrike AlphaOps EOD Full Report' = [pscustomobject]@{{
    state='Ready'
    last=$day.AddDays(-1).AddHours(15).AddMinutes(15)
    next=$day.AddHours(15).AddMinutes(15)
  }}
  'Dawnstrike AlphaOps V6 Weekly Training' = [pscustomobject]@{{
    state='Ready'
    last=$day.AddDays(-2).AddHours(21)
    next=$day.AddDays(5).AddHours(21)
  }}
  'Dawnstrike 10of10 Daily Finalize' = [pscustomobject]@{{
    state='Ready'
    last=$day.AddDays(-1).AddHours(17).AddMinutes(30)
    next=$day.AddHours(17).AddMinutes(30)
  }}
}}
function Get-ScheduledTask {{
  param([string]$TaskName, $ErrorAction)
  $row = $script:TaskRows[$TaskName]
  return [pscustomobject]@{{
    TaskName=$TaskName;TaskPath='\';State=$row.state
    Settings=[pscustomobject]@{{StartWhenAvailable=$true}}
  }}
}}
function Get-ScheduledTaskInfo {{
  param([string]$TaskName, [string]$TaskPath, $ErrorAction)
  $row = $script:TaskRows[$TaskName]
  return [pscustomobject]@{{LastRunTime=$row.last;NextRunTime=$row.next}}
}}

Remove-Item Env:DAWNSTRIKE_TEST_ROLLBACK_CLOCK -ErrorAction SilentlyContinue
$guarded = $false
try {{
  $null = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
    -RollbackTargetMarketDate '2026-09-09' `
    -RequiredCompletedMarketDate '2026-09-08' `
    -TestNowUtc $nowUtc.ToString('o')
}}
catch {{ $guarded = $_.Exception.Message -match 'test-only' }}

$env:DAWNSTRIKE_TEST_ROLLBACK_CLOCK = '1'
$accepted = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
  -RollbackTargetMarketDate '2026-09-09' `
  -RequiredCompletedMarketDate '2026-09-08' `
  -TestNowUtc $nowUtc.ToString('o')
Remove-Item Env:DAWNSTRIKE_TEST_ROLLBACK_CLOCK -ErrorAction SilentlyContinue

[pscustomobject]@{{guarded=$guarded;accepted=[bool]$accepted}} |
  ConvertTo-Json -Compress
"""
    completed = _run_powershell(script)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        "guarded": True,
        "accepted": True,
    }


@pytest.mark.skipif(
    os.name != "nt" or not POWERSHELL.is_file(),
    reason="requires Windows PowerShell 5.1",
)
def test_protected_enable_prefix_requires_exact_order_and_sealed_hashes() -> None:
    script = rf"""
$ErrorActionPreference = 'Stop'
. {_ps_quote(ROLLBACK)}
$global:States = @{{}}
for ($index = 0; $index -lt $script:DawnstrikeCanonicalTaskNames.Count; $index++) {{
  $global:States[$script:DawnstrikeCanonicalTaskNames[$index]] =
    if ($index -lt 2) {{ 'Ready' }} else {{ 'Disabled' }}
}}
function Get-ScheduledTask {{
  param([string]$TaskName, $ErrorAction)
  [pscustomobject]@{{
    State=$global:States[$TaskName]
    TaskPath='\'
    Actions=@([pscustomobject]@{{
      Execute='powershell.exe'
      Arguments=('run "C:\runtime" "C:\state" ' + $TaskName)
      WorkingDirectory='C:\runtime'
    }})
  }}
}}
function Export-ScheduledTask {{
  param([string]$TaskName,[string]$TaskPath,$ErrorAction)
  $enabled = if ($global:States[$TaskName] -eq 'Ready') {{ 'true' }} else {{ 'false' }}
  $safe = [Security.SecurityElement]::Escape($TaskName)
  "<Task><Settings><Enabled>$enabled</Enabled></Settings>" +
    "<Actions><Exec><Arguments>C:\runtime C:\state $safe</Arguments>" +
    "</Exec></Actions></Task>"
}}
function Test-Rejected([scriptblock]$Action, [string]$Pattern) {{
  try {{ $null = & $Action; return $false }}
  catch {{ return [bool]($_.Exception.Message -match $Pattern) }}
}}
$contract = Get-DawnstrikeTaskContract 'C:\runtime' 'C:\state' -AllowDisabled
$accepted = Assert-DawnstrikeProtectedRollbackEnablePrefix `
  -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' `
  -ExpectedTaskDefinitionContractSha256 $contract.task_definition_contract_sha256 `
  -ExpectedTaskActionContractSha256 $contract.task_action_contract_sha256
$global:States[$script:DawnstrikeCanonicalTaskNames[1]] = 'Disabled'
$global:States[$script:DawnstrikeCanonicalTaskNames[2]] = 'Ready'
$nonPrefix = Test-Rejected {{
  Assert-DawnstrikeProtectedRollbackEnablePrefix `
    -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' `
    -ExpectedTaskDefinitionContractSha256 $contract.task_definition_contract_sha256 `
    -ExpectedTaskActionContractSha256 $contract.task_action_contract_sha256
}} 'exact canonical Ready prefix'
$global:States[$script:DawnstrikeCanonicalTaskNames[1]] = 'Ready'
$global:States[$script:DawnstrikeCanonicalTaskNames[2]] = 'Disabled'
$hashDrift = Test-Rejected {{
  Assert-DawnstrikeProtectedRollbackEnablePrefix `
    -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' `
    -ExpectedTaskDefinitionContractSha256 ('0' * 64) `
    -ExpectedTaskActionContractSha256 $contract.task_action_contract_sha256
}} 'sealed task inventory'
[pscustomobject]@{{
  accepted=([int]$accepted.ready_count -eq 2 -and [int]$accepted.disabled_count -eq 3)
  non_prefix_rejected=$nonPrefix
  hash_drift_rejected=$hashDrift
}} | ConvertTo-Json -Compress
"""
    completed = _run_powershell(script)
    assert completed.returncode == 0, completed.stderr
    assert all(json.loads(completed.stdout.strip().splitlines()[-1]).values())


def test_runtime_lock_admission_and_every_enable_are_boundary_guarded() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    prefix_admission = source.split(
        "if ($entryRollbackPhase -ceq 'POST_SWAP_READY')", 1
    )[1].split("elseif ($entryRollbackPhase -ceq 'POST_SWAP')", 1)[0]
    for evidence in (
        "prepared_receipt_relative_path",
        "prepared_receipt_sha256",
        "complete_receipt_relative_path",
        "complete_receipt_sha256",
        "scheduler_backup_manifest_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256",
        "Assert-DawnstrikeProtectedRollbackEnablePrefix `",
    ):
        assert evidence in prefix_admission
    assert prefix_admission.index(
        "Assert-DawnstrikeProtectedRollbackEnablePrefix `"
    ) < prefix_admission.index("$protectedRecoveryEnablePrefix = $true")

    main_admission = source.split(
        "$rollbackBoundaryMode = if ($hasJournal)", 1
    )[1].split("$operationJournal = Get-DawnstrikeStrictRuntimeOperationJournal", 1)[0]
    assert main_admission.index(
        "Assert-DawnstrikeRollbackPostFinalizerMutationWindow `"
    ) < main_admission.index("Adopt-DawnstrikeGovernedRuntimeLockWithJournal `")
    assert main_admission.index(
        "Assert-DawnstrikeRollbackPostFinalizerMutationWindow `"
    ) < main_admission.index("Enter-DawnstrikeGovernedRuntimeLockWithJournal @enterJournalArgs")

    retained_boundary = source.split(
        "$dailyLock = Enter-DawnstrikeDailyRunLock `", 1
    )[1].split("$taskLocked = Get-DawnstrikeTaskContract", 1)[0]
    assert "Confirm-DawnstrikeActivationDailyLockHandshake `" in retained_boundary
    assert "Assert-DawnstrikeRollbackPostFinalizerMutationWindow `" in retained_boundary
    assert "-AllowRecoveryEnablePrefix:$protectedRecoveryEnablePrefix" in retained_boundary
    assert "-AllowRecoveryDisablePrefix:$protectedRecoveryDisablePrefix" in retained_boundary

    for callback_name in (
        "$terminalEnableBoundary = {",
        "$rollbackEnableBoundary = {",
        "$compensationEnableBoundary = {",
    ):
        callback = source.split(callback_name, 1)[1].split("}.GetNewClosure()", 1)[0]
        assert "Confirm-DawnstrikeActivationDailyLockHandshake `" in callback
        assert "Assert-DawnstrikeRollbackPostFinalizerMutationWindow `" in callback
        assert "-AllowRecoveryEnablePrefix `" in callback

    # COMPLETE/COMPENSATED terminal cleanup can adopt retained evidence without
    # reopening a schedule window because those paths do not enable tasks.
    assert source.count("Adopt-DawnstrikeGovernedRuntimeLockWithJournal `") == 4
    assert source.count("Enter-DawnstrikeGovernedRuntimeLockWithJournal @enterJournalArgs") == 1
    assert "$env:DAWNSTRIKE_TEST_ROLLBACK_CLOCK -ne \"1\"" in source
    assert "[string]$TestNowUtc = \"\"" in source


def test_expired_no_run_is_a_retained_disabled_safe_stop() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    safe_stop = source.split(
        "if ($rollbackBoundaryMode -ceq 'EXPIRED_NO_RUN')", 1
    )[1].split("$null = Assert-DawnstrikeTaskXmlBackup", 1)[0]
    assert "Confirm-DawnstrikeActivationDailyLockHandshake `" in safe_stop
    assert "-BoundaryMode EXPIRED_NO_RUN -ProtectedInFlight" in safe_stop
    assert "$expiredNoRunSafeStop = $true" in safe_stop
    assert "$preserveLocks = $true" in safe_stop
    assert "catch-up-neutralized enablement is not certified" in safe_stop

    catch_prefix = source.split("catch {\n        $failure = $_", 1)[1].split(
        "# COMPLETE is an irreversible commit", 1
    )[0]
    assert "if ($expiredNoRunSafeStop)" in catch_prefix
    assert "throw $failure" in catch_prefix

    terminal_recovery = source.split(
        "if ($disabledTerminalRecovery -or $rollbackTerminalRecoveryJournalPending)", 1
    )[1].split("# A failed rollback may restore the activated candidate", 1)[0]
    prefix_proof = terminal_recovery.index(
        "Assert-DawnstrikeProtectedRollbackEnablePrefix `"
    )
    normalization = terminal_recovery.index(
        "Set-DawnstrikeTasksFailClosedDisabled", prefix_proof
    )
    terminal_expired = terminal_recovery.index(
        "if ($terminalRollbackBoundaryMode -ceq 'EXPIRED_NO_RUN')", normalization
    )
    terminal_enable = terminal_recovery.index(
        "$terminalEnableBoundary = {", terminal_expired
    )
    assert prefix_proof < normalization < terminal_expired < terminal_enable
    terminal_safe_stop = terminal_recovery[terminal_expired:terminal_enable]
    assert "Confirm-DawnstrikeActivationDailyLockHandshake `" in terminal_safe_stop
    assert "-BoundaryMode EXPIRED_NO_RUN -ProtectedInFlight" in terminal_safe_stop
    assert "governed operator recovery is required" in terminal_safe_stop

    terminal_failure = terminal_recovery.split(
        "catch {\n                $terminalRecoveryFailure = $_", 1
    )[1].split("finally {", 1)[0]
    assert "Set-DawnstrikeTasksFailClosedDisabled" in terminal_failure
    assert "Disable-DawnstrikeAuxiliaryCaptureTask" in terminal_failure
    assert "$releaseTerminalRecoveryLocks = $true" not in terminal_failure


def test_failure_mutation_requires_retained_pair_and_cleanup_uses_sealed_backup() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    failure_path = source.split("catch {\n        $failure = $_", 1)[1].split(
        "finally {\n        if (-not $preserveLocks)", 1
    )[0]
    pair_guard = failure_path.index("$null -eq $activationLock")
    handshake = failure_path.index(
        "Confirm-DawnstrikeActivationDailyLockHandshake `", pair_guard
    )
    fail_closed = failure_path.index(
        "Set-DawnstrikeTasksFailClosedDisabled", handshake
    )
    assert pair_guard < handshake < fail_closed
    assert "$preserveLocks = $true" in failure_path[pair_guard:handshake]
    assert "no further task mutation is permitted" in failure_path[pair_guard:fail_closed]

    compensated_cleanup = source.split(
        'if ([string]$compensatedJournal.payload.phase -eq "COMPENSATED")', 1
    )[1].split("return Invoke-DawnstrikeRuntimeRollback @PSBoundParameters", 1)[0]
    allow_missing = compensated_cleanup.index("$archiveAttemptArgs.AllowMissing = $true")
    assert "if (-not $tasksInitiallyEnabled)" not in compensated_cleanup[:allow_missing]
    assert (
        "[string]$compensatedJournal.payload.backup_contract_sha256 -ceq"
        in compensated_cleanup[:allow_missing]
    )
    assert (
        "[string]$activation.scheduler_backup_manifest_sha256"
        in compensated_cleanup[:allow_missing]
    )


def test_post_swap_ready_adopts_only_exact_prelinked_terminal_receipt() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    existing_receipt = source.split(
        'if (Test-Path -LiteralPath $rollbackReceipt -PathType Leaf)', 1
    )[1].split('if ([string]$existingJournal.payload.phase -eq "COMPLETE")', 1)[0]
    assert (
        '[string]$existingJournal.payload.phase -cne "POST_SWAP_READY"'
        in existing_receipt
    )
    assert '@("POST_SWAP", "POST_SWAP_READY")' not in existing_receipt

    terminal = source.split(
        '$payload.schema_version = "dawnstrike.runtime_rollback_receipt.v1"', 1
    )[1].split('$completeReceiptHash = Get-DawnstrikeSha256File', 1)[0]
    exact_adoption = terminal.index("if ($null -ne $existingRollbackReceipt)")
    fresh_seal = terminal.index("else {", exact_adoption)
    assert "$existingNames.Count -ne ($payload.Keys.Count + 1)" in terminal
    assert "[string]$existingRollbackReceipt.$field -cne [string]$payload[$field]" in terminal
    assert (
        "Existing rollback terminal receipt is not the exact ready-receipt derivation"
        in terminal
    )
    assert "Rollback terminal receipt sealing" in terminal[fresh_seal:]
    assert "after_complete_receipt" in terminal[fresh_seal:]

    no_mutation = source.split("$preserveReadyPostSwap = (", 1)[1].split(
        "$payload.schema_version = \"dawnstrike.runtime_rollback_receipt.v1\"", 1
    )[0]
    assert "$protectedRecoveryAllReady" in no_mutation
    assert "$rollbackBoundaryMode -cne 'EXPIRED_NO_RUN'" in no_mutation
    assert "-and -not $preserveReadyPostSwap" in no_mutation
    assert "POST_SWAP_READY no-mutation recovery lost its exact Ready task contract" in no_mutation
    assert "if ($preserveReadyPostSwap)" in no_mutation
    enable_guard = no_mutation.rindex("if (-not $preserveReadyPostSwap)")
    enable_call = no_mutation.index("Enable-DawnstrikeCanonicalTasks", enable_guard)
    final_boundary = no_mutation.index("& $rollbackEnableBoundary", enable_call)
    assert enable_guard < enable_call < final_boundary


def test_rollback_compensation_python_calls_are_bounded_and_preserve_receipts() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    activation = ACTIVATION.read_text(encoding="utf-8")
    verification = source.split(
        '$compensationCheck = Invoke-DawnstrikeActivationProcess `', 1
    )[1].split("$compensatedRuntime =", 1)[0]
    sealing = source.split(
        '$null = Invoke-DawnstrikeActivationProcess `\n'
        '                        -FilePath $lockInterpreter.path',
        1,
    )[1].split("$compensationHash =", 1)[0]

    assert '"verify-compensation"' in verification
    assert "-TimeoutSeconds $ProcessTimeoutSeconds" in verification
    assert '"seal-compensation"' in sealing
    assert "-TimeoutSeconds $ProcessTimeoutSeconds" in sealing
    assert "Remove-Item -LiteralPath $compensationInput -Force" in sealing
    assert "Remove-Item -LiteralPath $compensationReceipt" not in sealing
    assert "& $approvedJournalInterpreter.path" not in source
    assert "& $lockInterpreter.path -I -B -S" not in source

    bounded_runner = activation.split(
        "function Invoke-DawnstrikeActivationProcess", 1
    )[1].split("function Get-DawnstrikeActivationNowUtc", 1)[0]
    assert "Invoke-DawnstrikeJobProcess `" in bounded_runner
    assert "-TimeoutSeconds $TimeoutSeconds `" in bounded_runner
    assert "-OutputDrainTimeoutSeconds 5 `" in bounded_runner
