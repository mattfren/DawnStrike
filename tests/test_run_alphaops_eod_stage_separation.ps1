<#
Orchestration-level test for scripts/run_alphaops_eod.ps1 (DS-08).

This drives the REAL run_alphaops_eod.ps1 script end-to-end against a fully
isolated, disposable fake "runtime" (its own throwaway git repo) and a fresh
temp state/backup root for every scenario. Every py.exe/git.exe invocation
the script makes is answered by tiny stub scripts placed inside that fake
runtime, which log their own invocation and return a caller-controlled exit
code -- nothing here calls the real intraday_scanner package, the real
broker, or touches C:\r\dawnstrike-runtime / C:\r\dawnstrike-state.

It proves the stage-separation repair behaviourally, at the PowerShell
orchestration layer: which stages get *attempted* (a stub invocation is
logged), what each stage is *recorded* as (via the record_daily_stage.py
stub's structured log), and what the script's own process exit code is.

Run:  pwsh -NoProfile -File tests\test_run_alphaops_eod_stage_separation.ps1
Exits 0 on all-pass, 1 if any assertion failed.
#>

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$scriptUnderTest = Join-Path $repoRoot "scripts\run_alphaops_eod.ps1"
if (-not (Test-Path -LiteralPath $scriptUnderTest)) {
    throw "Cannot find script under test at $scriptUnderTest"
}

$script:failures = @()
$script:passCount = 0

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if ($Condition) {
        $script:passCount++
        Write-Host "  PASS: $Message"
    } else {
        $script:failures += $Message
        Write-Host "  FAIL: $Message" -ForegroundColor Red
    }
}

function New-FakeRuntime {
    # Build a disposable git repo standing in for the deployed runtime.
    # Resolve-DawnstrikeReleaseSha (unmodified infra, reused as-is) requires a
    # real, clean git worktree, so this really is git-backed, not a plain folder.
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ("ds08-fake-runtime-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root "scripts") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root "intraday_scanner\services") -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $root "intraday_scanner\v2") -Force | Out-Null

    Set-Content -LiteralPath (Join-Path $root "stub_common.py") -Encoding UTF8 -Value @'
import os, sys, json, datetime

def log_invocation(key, argv=None):
    log_path = os.environ.get("STUB_INVOCATION_LOG")
    if not log_path:
        return
    argv = argv if argv is not None else sys.argv[1:]
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"key": key, "argv": argv, "ts": datetime.datetime.utcnow().isoformat()}) + "\n")

def exit_code_for(key, default=0):
    val = os.environ.get("STUB_EXIT_" + key)
    if val is None:
        return default
    return int(val)
'@

    Set-Content -LiteralPath (Join-Path $root "intraday_scanner\__init__.py") -Encoding UTF8 -Value ""
    Set-Content -LiteralPath (Join-Path $root "intraday_scanner\services\__init__.py") -Encoding UTF8 -Value ""
    Set-Content -LiteralPath (Join-Path $root "intraday_scanner\v2\__init__.py") -Encoding UTF8 -Value ""

    # `python -m pkg.module` prepends the CWD to sys.path, so these import
    # stub_common straight from the fake runtime root with no path surgery.
    Set-Content -LiteralPath (Join-Path $root "intraday_scanner\services\market_calendar.py") -Encoding UTF8 -Value @'
import sys
import stub_common as sc
sc.log_invocation("MARKET_CALENDAR", sys.argv[1:])
sys.exit(sc.exit_code_for("MARKET_CALENDAR", 0))
'@

    Set-Content -LiteralPath (Join-Path $root "intraday_scanner\v2\paper_ops.py") -Encoding UTF8 -Value @'
import sys
import stub_common as sc
args = sys.argv[1:]
sub = args[0] if args else "UNKNOWN"
key = "PAPEROPS_" + sub.upper().replace("-", "_")
sc.log_invocation(key, args)
sys.exit(sc.exit_code_for(key, 0))
'@

    Set-Content -LiteralPath (Join-Path $root "intraday_scanner\cli.py") -Encoding UTF8 -Value @'
import sys, os, json
import stub_common as sc

def get_arg(args, flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None

def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(2)
    sub = args[0]
    key = "CLI_" + sub.upper().replace("-", "_")
    sc.log_invocation(key, args)
    code = sc.exit_code_for(key, 0)
    if sub == "alpha-eod-gate" and code == 0:
        out = get_arg(args, "--out")
        if out:
            official_required = os.environ.get("STUB_OFFICIAL_OUTCOMES_REQUIRED", "true").lower() == "true"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump({"official_outcomes_required": official_required}, f)
    sys.exit(code)

if __name__ == "__main__":
    main()
'@

    # Direct script-path invocations get sys.path[0] == their own directory,
    # not the runtime root, so these add the runtime root manually.
    Set-Content -LiteralPath (Join-Path $root "scripts\state_disaster_recovery.py") -Encoding UTF8 -Value @'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stub_common as sc
args = sys.argv[1:]
sub = args[0] if args else "UNKNOWN"
key = "BACKUP_" + sub.upper()
sc.log_invocation(key, args)
sys.exit(sc.exit_code_for(key, 0))
'@

    Set-Content -LiteralPath (Join-Path $root "scripts\build_paperops_universe_handoff.py") -Encoding UTF8 -Value @'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stub_common as sc
sc.log_invocation("HANDOFF_VALIDATE", sys.argv[1:])
sys.exit(sc.exit_code_for("HANDOFF_VALIDATE", 0))
'@

    Set-Content -LiteralPath (Join-Path $root "scripts\record_daily_stage.py") -Encoding UTF8 -Value @'
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stub_common as sc

args = sys.argv[1:]

def get_arg(flag):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return None

stage = get_arg("--stage")
status = get_arg("--status")
error_code = get_arg("--error-code")
exit_code_seen = get_arg("--exit-code")
key = "RECORD_STAGE_" + (stage or "UNKNOWN").upper()
sc.log_invocation(key, args)

record_log = os.environ.get("STUB_STAGE_RECORD_LOG")
if record_log:
    with open(record_log, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "stage": stage,
            "status": status,
            "error_code": error_code,
            "exit_code_seen": exit_code_seen,
        }) + "\n")

sys.exit(sc.exit_code_for(key, 0))
'@

    Set-Content -LiteralPath (Join-Path $root "scripts\send_stage_failure_notification.py") -Encoding UTF8 -Value @'
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stub_common as sc
sc.log_invocation("FAILURE_NOTIFICATION", sys.argv[1:])
sys.exit(sc.exit_code_for("FAILURE_NOTIFICATION", 0))
'@

    # Resolve-DawnstrikeReleaseSha (unmodified infra) fails closed on a dirty
    # worktree. Every stub invocation below runs the fake runtime's Python
    # files, which writes __pycache__/*.pyc as an untracked side effect --
    # gitignore it up front so repeated scenario runs stay clean.
    Set-Content -LiteralPath (Join-Path $root ".gitignore") -Encoding UTF8 -Value "__pycache__/`n*.pyc`n"

    Push-Location $root
    try {
        git init -q .
        git config user.email "ds08-test@example.invalid" | Out-Null
        git config user.name "ds08-test" | Out-Null
        git add -A | Out-Null
        git commit -q -m "fake runtime for DS-08 orchestration test" | Out-Null
    } finally {
        Pop-Location
    }
    return $root
}

function Invoke-EodScript {
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][hashtable]$StubExit,
        [string]$OfficialOutcomesRequired = "true",
        [string]$MarketDate = "2026-09-18"
    )

    $stateRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ds08-fake-state-" + [guid]::NewGuid().ToString("N"))
    $backupRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ds08-fake-backup-" + [guid]::NewGuid().ToString("N"))
    $invocationLog = Join-Path ([System.IO.Path]::GetTempPath()) ("ds08-invocations-" + [guid]::NewGuid().ToString("N") + ".jsonl")
    $stageRecordLog = Join-Path ([System.IO.Path]::GetTempPath()) ("ds08-stage-records-" + [guid]::NewGuid().ToString("N") + ".jsonl")
    New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

    # Every scenario must start from a completely clean STUB_EXIT_* slate: a
    # value left over from a previous scenario (even restored via
    # SetEnvironmentVariable($k, $null), which can round-trip as an empty
    # string rather than a truly absent variable) would silently change this
    # scenario's stub behaviour. Remove-Item on the Env: drive is the one
    # operation that reliably deletes rather than blanks a variable.
    Get-ChildItem Env: | Where-Object { $_.Name -like "STUB_EXIT_*" } | ForEach-Object {
        Remove-Item -Path "Env:\$($_.Name)" -ErrorAction SilentlyContinue
    }

    try {
        foreach ($k in $StubExit.Keys) { [Environment]::SetEnvironmentVariable("STUB_EXIT_$k", [string]$StubExit[$k]) }
        [Environment]::SetEnvironmentVariable("STUB_INVOCATION_LOG", $invocationLog)
        [Environment]::SetEnvironmentVariable("STUB_STAGE_RECORD_LOG", $stageRecordLog)
        [Environment]::SetEnvironmentVariable("STUB_OFFICIAL_OUTCOMES_REQUIRED", $OfficialOutcomesRequired)
        [Environment]::SetEnvironmentVariable("DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED", "false")
        [Environment]::SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1")

        $pwshExe = (Get-Command pwsh -ErrorAction SilentlyContinue)
        if (-not $pwshExe) { $pwshExe = (Get-Command powershell -ErrorAction Stop) }
        $psi = @{
            FilePath = $pwshExe.Source
            ArgumentList = @(
                "-NoProfile", "-NonInteractive", "-File", $scriptUnderTest,
                "-RuntimeRoot", $RuntimeRoot,
                "-StateRoot", $stateRoot,
                "-MarketDate", $MarketDate,
                "-BackupRoot", $backupRoot,
                "-PaperOpsRetryLimit", "1",
                "-PaperOpsRetryDelaySeconds", "1"
            )
            NoNewWindow = $true
            Wait = $true
            PassThru = $true
            RedirectStandardOutput = "$stageRecordLog.stdout.txt"
            RedirectStandardError = "$stageRecordLog.stderr.txt"
        }
        $proc = Start-Process @psi
        $exitCode = $proc.ExitCode
    }
    finally {
        Get-ChildItem Env: | Where-Object { $_.Name -like "STUB_EXIT_*" } | ForEach-Object {
            Remove-Item -Path "Env:\$($_.Name)" -ErrorAction SilentlyContinue
        }
    }

    $invocations = @()
    if (Test-Path -LiteralPath $invocationLog) {
        $invocations = Get-Content -LiteralPath $invocationLog | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json }
    }
    $stageRecords = @()
    if (Test-Path -LiteralPath $stageRecordLog) {
        $stageRecords = Get-Content -LiteralPath $stageRecordLog | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json }
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Invocations = $invocations
        StageRecords = $stageRecords
        StateRoot = $stateRoot
        BackupRoot = $backupRoot
        StdoutPath = "$stageRecordLog.stdout.txt"
        StderrPath = "$stageRecordLog.stderr.txt"
    }
}

function Get-StageRecord {
    param($Result, [string]$Stage)
    return $Result.StageRecords | Where-Object { $_.stage -eq $Stage } | Select-Object -Last 1
}

function Test-InvocationPresent {
    param($Result, [string]$Key)
    return [bool]($Result.Invocations | Where-Object { $_.key -eq $Key })
}

Write-Host "Building fake runtime..."
$fakeRuntime = New-FakeRuntime
Write-Host "Fake runtime: $fakeRuntime"

try {
    # ---------------------------------------------------------------
    # Scenario 1: invalid universe handoff, existing-position stages
    # (protective/reconciliation) must still be attempted; new entries
    # (paperops run-day) must stay blocked.
    # ---------------------------------------------------------------
    Write-Host "`nScenario 1: invalid universe handoff + existing position"
    $r1 = Invoke-EodScript -RuntimeRoot $fakeRuntime -StubExit @{ HANDOFF_VALIDATE = 7 }

    $captureRec = Get-StageRecord $r1 "eod_outcome_capture"
    Assert-True ($captureRec -and $captureRec.status -eq "FAILED" -and $captureRec.error_code -eq "eod_precondition_universe_handoff_invalid") `
        "eod_outcome_capture recorded FAILED/eod_precondition_universe_handoff_invalid on invalid handoff"

    $reconcileRec = Get-StageRecord $r1 "paper_reconciliation"
    Assert-True (Test-InvocationPresent $r1 "CLI_ALPHA_PAPER_RECONCILE") `
        "paper_reconciliation stage IS attempted (alpha-paper-reconcile invoked) despite invalid universe handoff"
    Assert-True ($reconcileRec -and $reconcileRec.status -eq "COMPLETE") `
        "paper_reconciliation recorded COMPLETE (protective stage not blocked by universe failure)"

    Assert-True (-not (Test-InvocationPresent $r1 "PAPEROPS_RUN_DAY")) `
        "paperops_forward run-day (new entries) is NOT invoked when universe handoff is invalid"
    $paperRec = Get-StageRecord $r1 "paperops_forward"
    Assert-True ($paperRec -and $paperRec.status -eq "FAILED" -and $paperRec.error_code -eq "blocked_by_eod_precondition") `
        "paperops_forward recorded FAILED/blocked_by_eod_precondition (new entries blocked)"

    $learnRec = Get-StageRecord $r1 "alpha_learning"
    Assert-True (-not (Test-InvocationPresent $r1 "CLI_ALPHA_LEARN")) `
        "alpha-learn is NOT invoked (optional learning not forced to run) when universe handoff is invalid"
    Assert-True ($learnRec -and $learnRec.status -eq "FAILED" -and $learnRec.error_code -eq "blocked_by_eod_precondition") `
        "alpha_learning recorded FAILED/blocked_by_eod_precondition"

    Assert-True ($r1.ExitCode -ne 0) "script exit code is non-zero on invalid universe handoff (overall run is not a success)"

    # ---------------------------------------------------------------
    # Scenario 2: optional learning failure must not block the
    # protective/accounting stages.
    # ---------------------------------------------------------------
    Write-Host "`nScenario 2: healthy universe handoff, alpha-learn fails"
    $r2 = Invoke-EodScript -RuntimeRoot $fakeRuntime -StubExit @{ HANDOFF_VALIDATE = 0; CLI_ALPHA_LEARN = 5 }

    $reconcileRec2 = Get-StageRecord $r2 "paper_reconciliation"
    Assert-True ($reconcileRec2 -and $reconcileRec2.status -eq "COMPLETE") `
        "paper_reconciliation still COMPLETE when only alpha-learn fails"
    $paperRec2 = Get-StageRecord $r2 "paperops_forward"
    Assert-True ($paperRec2 -and $paperRec2.status -eq "COMPLETE") `
        "paperops_forward still COMPLETE when only alpha-learn fails"
    $learnRec2 = Get-StageRecord $r2 "alpha_learning"
    Assert-True ($learnRec2 -and $learnRec2.status -eq "FAILED" -and $learnRec2.error_code -eq "alpha_learning_failed") `
        "alpha_learning recorded FAILED/alpha_learning_failed (its own genuine failure is still visible)"

    # ---------------------------------------------------------------
    # Scenario 3: a genuine reconciliation failure must stay visible as
    # a failed critical-stage outcome, never a successful EOD result.
    # ---------------------------------------------------------------
    Write-Host "`nScenario 3: healthy universe handoff, alpha-paper-reconcile fails"
    $r3 = Invoke-EodScript -RuntimeRoot $fakeRuntime -StubExit @{ HANDOFF_VALIDATE = 0; CLI_ALPHA_PAPER_RECONCILE = 4 }

    $reconcileRec3 = Get-StageRecord $r3 "paper_reconciliation"
    Assert-True ($reconcileRec3 -and $reconcileRec3.status -eq "FAILED" -and $reconcileRec3.error_code -eq "paper_reconciliation_failed") `
        "paper_reconciliation recorded FAILED/paper_reconciliation_failed on a genuine reconcile failure"
    Assert-True ($r3.ExitCode -ne 0) "script exit code is non-zero when reconciliation genuinely fails (never a successful EOD result)"

    # ---------------------------------------------------------------
    # Scenario 4: identity/writer failure (record_daily_stage itself
    # fails) must still be enforced as an overall failure.
    # ---------------------------------------------------------------
    Write-Host "`nScenario 4: healthy universe handoff, single-writer stage-record call fails"
    $r4 = Invoke-EodScript -RuntimeRoot $fakeRuntime -StubExit @{ HANDOFF_VALIDATE = 0; RECORD_STAGE_PAPER_RECONCILIATION = 9 }
    Assert-True ($r4.ExitCode -ne 0) "script exit code is non-zero when the single stage-record writer itself fails"

    # ---------------------------------------------------------------
    # Scenario 5: healthy session behaviour is unchanged -- every
    # required stage attempted and recorded COMPLETE, new entries run.
    # ---------------------------------------------------------------
    Write-Host "`nScenario 5: fully healthy session"
    $r5 = Invoke-EodScript -RuntimeRoot $fakeRuntime -StubExit @{ HANDOFF_VALIDATE = 0 }

    foreach ($stage in @("eod_outcome_capture", "paper_reconciliation", "alpha_learning", "paperops_forward")) {
        $rec = Get-StageRecord $r5 $stage
        Assert-True ($rec -and $rec.status -eq "COMPLETE") "$stage recorded COMPLETE on a healthy run"
    }
    Assert-True (Test-InvocationPresent $r5 "PAPEROPS_RUN_DAY") "paperops run-day (new entries) IS invoked on a healthy run"
    Assert-True ($r5.ExitCode -eq 0) "script exit code is 0 on a fully healthy run"

    foreach ($r in @($r1, $r2, $r3, $r4, $r5)) {
        Remove-Item -LiteralPath $r.StateRoot -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $r.BackupRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
finally {
    Remove-Item -LiteralPath $fakeRuntime -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "`n----------------------------------------"
Write-Host "Passed: $script:passCount   Failed: $($script:failures.Count)"
if ($script:failures.Count -gt 0) {
    Write-Host "Failures:" -ForegroundColor Red
    foreach ($f in $script:failures) { Write-Host "  - $f" -ForegroundColor Red }
    exit 1
}
exit 0
