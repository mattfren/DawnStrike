from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

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


def test_rollback_snapshot_is_post_finalizer_and_waits_for_monday_weekly() -> None:
    script = rf"""
$ErrorActionPreference = 'Stop'
. {_ps_quote(ROLLBACK)}

function Convert-LocalTestTimeToUtc([datetime]$Value) {{
  return [DateTimeOffset]::new(
    $Value,
    [TimeZoneInfo]::Local.GetUtcOffset($Value)
  ).ToUniversalTime()
}}

function New-RollbackSnapshots([datetime]$Day) {{
  return @(
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps Morning'
      state='Ready'
      last_run_time=$Day.AddHours(9)
      next_run_time=$Day.AddDays(1).AddHours(9)
    }},
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps Monitor 5m'
      state='Disabled'
      last_run_time=$Day.AddHours(15)
      next_run_time=[datetime]::MinValue
    }},
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps EOD Full Report'
      state='Ready'
      last_run_time=$Day.AddHours(15).AddMinutes(15)
      next_run_time=$Day.AddDays(1).AddHours(15).AddMinutes(15)
    }},
    [pscustomobject]@{{
      name='Dawnstrike AlphaOps V6 Weekly Training'
      state='Ready'
      last_run_time=$Day.AddDays(-1).AddHours(21)
      next_run_time=$Day.AddDays(6).AddHours(21)
    }},
    [pscustomobject]@{{
      name='Dawnstrike 10of10 Daily Finalize'
      state='Ready'
      last_run_time=$Day.AddHours(17).AddMinutes(30)
      next_run_time=$Day.AddDays(1).AddHours(17).AddMinutes(30)
    }}
  )
}}

function Test-Rejected([scriptblock]$Action, [string]$Pattern) {{
  try {{ $null = & $Action; return $false }}
  catch {{ return [bool]($_.Exception.Message -match $Pattern) }}
}}

$tuesday = [datetime]::new(2026, 9, 8, 0, 0, 0, [DateTimeKind]::Unspecified)
$tuesdayNow = Convert-LocalTestTimeToUtc $tuesday.AddHours(18)
$base = New-RollbackSnapshots $tuesday
$basePass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc $tuesdayNow -TaskSnapshots @($base)

$running = New-RollbackSnapshots $tuesday
$running[1].state = 'Running'
$runningRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $tuesdayNow -TaskSnapshots @($running)
}} 'quiescent'

$duplicate = New-RollbackSnapshots $tuesday
$duplicate[0].name = 'Dawnstrike AlphaOps EOD Full Report'
$duplicateRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $tuesdayNow -TaskSnapshots @($duplicate)
}} 'unknown or duplicated'

$staleFinalizer = New-RollbackSnapshots $tuesday
$staleFinalizer[4].last_run_time = $tuesday.AddDays(-1).AddHours(17).AddMinutes(30)
$staleRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $tuesdayNow -TaskSnapshots @($staleFinalizer)
}} 'post-Finalizer window'

$pending = New-RollbackSnapshots $tuesday
$pending[0].next_run_time = $tuesday.AddHours(19)
$pendingRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $tuesdayNow -TaskSnapshots @($pending)
}} 'same-day canonical trigger'

$monday = [datetime]::new(2026, 9, 7, 0, 0, 0, [DateTimeKind]::Unspecified)
$mondayBeforeWeekly = New-RollbackSnapshots $monday
$mondayBeforeWeekly[3].last_run_time = $monday.AddDays(-7).AddHours(21)
$mondayBeforeWeekly[3].next_run_time = $monday.AddHours(21)
$monday2059 = Convert-LocalTestTimeToUtc $monday.AddHours(20).AddMinutes(59)
$mondayRejected = Test-Rejected {{
  Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
    -NowUtc $monday2059 -TaskSnapshots @($mondayBeforeWeekly)
}} 'same-day canonical trigger|same-day Weekly'

$mondayAfterWeekly = New-RollbackSnapshots $monday
$mondayAfterWeekly[3].last_run_time = $monday.AddHours(21).AddMinutes(30)
$mondayAfterWeekly[3].next_run_time = $monday.AddDays(7).AddHours(21)
$monday2200 = Convert-LocalTestTimeToUtc $monday.AddHours(22)
$mondayPass = Assert-DawnstrikeRollbackPostFinalizerBoundarySnapshot `
  -NowUtc $monday2200 -TaskSnapshots @($mondayAfterWeekly)

[pscustomobject]@{{
  base_pass = [bool]$basePass
  running_rejected = $runningRejected
  duplicate_rejected = $duplicateRejected
  stale_rejected = $staleRejected
  pending_rejected = $pendingRejected
  monday_2059_rejected = $mondayRejected
  monday_post_weekly_pass = [bool]$mondayPass
}} | ConvertTo-Json -Compress
"""
    completed = _run_powershell(script)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert all(result.values()), result


def test_rollback_live_snapshot_clock_override_is_explicitly_guarded() -> None:
    script = rf"""
$ErrorActionPreference = 'Stop'
. {_ps_quote(ROLLBACK)}

$day = [datetime]::new(2026, 9, 8, 0, 0, 0, [DateTimeKind]::Unspecified)
$nowUtc = [DateTimeOffset]::new(
  $day.AddHours(18),
  [TimeZoneInfo]::Local.GetUtcOffset($day.AddHours(18))
).ToUniversalTime()
$script:TaskRows = @{{
  'Dawnstrike AlphaOps Morning' = [pscustomobject]@{{
    state='Ready';last=$day.AddHours(9);next=$day.AddDays(1).AddHours(9)
  }}
  'Dawnstrike AlphaOps Monitor 5m' = [pscustomobject]@{{
    state='Disabled';last=$day.AddHours(15);next=[datetime]::MinValue
  }}
  'Dawnstrike AlphaOps EOD Full Report' = [pscustomobject]@{{
    state='Ready'
    last=$day.AddHours(15).AddMinutes(15)
    next=$day.AddDays(1).AddHours(15)
  }}
  'Dawnstrike AlphaOps V6 Weekly Training' = [pscustomobject]@{{
    state='Disabled'
    last=$day.AddDays(-1).AddHours(21)
    next=$day.AddDays(6).AddHours(21)
  }}
  'Dawnstrike 10of10 Daily Finalize' = [pscustomobject]@{{
    state='Ready'
    last=$day.AddHours(17).AddMinutes(30)
    next=$day.AddDays(1).AddHours(17).AddMinutes(30)
  }}
}}
function Get-ScheduledTask {{
  param([string]$TaskName, $ErrorAction)
  $row = $script:TaskRows[$TaskName]
  return [pscustomobject]@{{TaskName=$TaskName;TaskPath='\';State=$row.state}}
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
    -TestNowUtc $nowUtc.ToString('o')
}}
catch {{ $guarded = $_.Exception.Message -match 'test-only' }}

$env:DAWNSTRIKE_TEST_ROLLBACK_CLOCK = '1'
$accepted = Assert-DawnstrikeRollbackPostFinalizerMutationWindow `
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


def test_every_runtime_lock_admission_has_a_fresh_rollback_boundary() -> None:
    source = ROLLBACK.read_text(encoding="utf-8")
    guarded_adoptions = re.findall(
        r"Assert-DawnstrikeRollbackPostFinalizerMutationWindow\s+`\s*"
        r"-TestNowUtc \$TestNowUtc\s+"
        r"\$(?:completeLock|compensationLock|activationLock) = "
        r"Adopt-DawnstrikeGovernedRuntimeLockWithJournal",
        source,
    )
    guarded_creation = re.findall(
        r"Assert-DawnstrikeRollbackPostFinalizerMutationWindow\s+`\s*"
        r"-TestNowUtc \$TestNowUtc\s+"
        r"\$activationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal",
        source,
    )

    assert len(guarded_adoptions) == 3
    assert len(guarded_creation) == 1
    assert source.count("Adopt-DawnstrikeGovernedRuntimeLockWithJournal `") == 3
    assert source.count("Enter-DawnstrikeGovernedRuntimeLockWithJournal @enterJournalArgs") == 1
    assert "$env:DAWNSTRIKE_TEST_ROLLBACK_CLOCK -ne \"1\"" in source
    assert "[string]$TestNowUtc = \"\"" in source


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
