[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [string]$Notify = "telegram"
)

$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path $RuntimeRoot).Path
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
. (Join-Path $PSScriptRoot "import_dawnstrike_environment.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_process_runner.ps1")
. (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")
. (Join-Path $PSScriptRoot "alpha_cycle_artifact.ps1")
Import-DawnstrikeEnvironment -StateRoot $state
$dbPath = Join-Path $state "shadow_real.sqlite"
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$releaseSha = Resolve-DawnstrikeReleaseSha -RuntimeRoot $runtime -LogRoot $logRoot
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$exitCode = 0
$errorCode = ""
$scenarioCandidateCount = 0
$scenarioSymbols = ""
$scenarioExitCode = $null
$scenarioStageRecordFailed = $false
$scenarioWatermarkPath = Join-Path $state "scenario_monitor_watermark.json"
$alphaCyclePath = Join-Path $state "outputs\alpha_cycle\$MarketDate\alpha_cycle.json"

function Get-ScenarioMonitorWatermark {
    # The watermark is deliberately durable and only advances after a successful
    # Scenario cycle.  A short overlap is safe because provider/article and
    # extraction identities are idempotent; a clock-only window is not safe
    # because a delayed task or provider page can otherwise create a gap.
    if (Test-Path -LiteralPath $scenarioWatermarkPath) {
        try {
            $saved = Get-Content -LiteralPath $scenarioWatermarkPath -Raw | ConvertFrom-Json
            $parsed = [DateTimeOffset]::Parse([string]$saved.watermark_utc)
            return $parsed.ToUniversalTime().ToString("o")
        }
        catch {
            throw "Scenario monitor watermark is invalid; refusing to infer a narrower news window. Repair $scenarioWatermarkPath before rerunning."
        }
    }
    # First monitor after deployment has no cursor.  The wide initial window is
    # intentional and safe: downstream ids deduplicate it, while an artificially
    # short window could silently omit premarket news.
    return (Get-Date).ToUniversalTime().AddHours(-12).ToString("o")
}

function Save-ScenarioMonitorWatermark {
    param([string]$WatermarkUtc)
    $payload = [ordered]@{
        schema_version = 1
        watermark_utc = $WatermarkUtc
        recorded_at_utc = (Get-Date).ToUniversalTime().ToString("o")
        producer = "run_alphaops_monitor.ps1"
    } | ConvertTo-Json -Compress
    $temporary = "$scenarioWatermarkPath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, $payload, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $scenarioWatermarkPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}
function Write-MonitorStage {
    param([string]$Status, [int]$ExitCode, [string]$ErrorCode = "")
    $arguments = @(
        "scripts\record_daily_stage.py",
        "--db-path", $dbPath,
        "--market-date", $MarketDate,
        "--stage", "intraday_monitor",
        "--status", $Status,
        "--runtime-root", $runtime,
        "--state-root", $state,
        "--release-sha", $releaseSha,
        "--exit-code", "$ExitCode",
        "--started-at", $startedAt
    )
    if ($ErrorCode) { $arguments += @("--error-code", $ErrorCode) }
    return Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList $arguments `
        -LogRoot $logRoot `
        -LogName "record_stage-intraday_monitor-$MarketDate"
}
function Write-ScenarioStage {
    param([string]$Status, [int]$ExitCode, [string]$ErrorCode = "")
    $arguments = @(
        "scripts\record_daily_stage.py",
        "--db-path", $dbPath,
        "--market-date", $MarketDate,
        "--stage", "scenario_intelligence",
        "--status", $Status,
        "--runtime-root", $runtime,
        "--state-root", $state,
        "--release-sha", $releaseSha,
        "--exit-code", "$ExitCode",
        "--started-at", $startedAt,
        "--not-required"
    )
    if ($ErrorCode) { $arguments += @("--error-code", $ErrorCode) }
    return Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList $arguments `
        -LogRoot $logRoot `
        -LogName "record_stage-scenario_intelligence-$MarketDate"
}
$dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "alphaops_monitor"
if (-not $dailyLock.acquired) {
    $lockReceiptWritten = Write-DawnstrikeLockDenialReceipt -StateRoot $state -MarketDate $MarketDate -Owner "alphaops_monitor" -Lock $dailyLock
    $record = Write-MonitorStage `
        -Status FAILED `
        -ExitCode 3 `
        -ErrorCode "daily_lock_unavailable"
    Write-Error "Required AlphaOps monitor run blocked by daily lock: $($dailyLock.reason)"
    exit $(if ($record.exit_code -ne 0 -or -not $lockReceiptWritten) { 2 } else { 3 })
}
$heartbeat = Invoke-DawnstrikeNativeProcess `
    -FilePath "py.exe" `
    -ArgumentList @("-m", "intraday_scanner.cli", "daily-heartbeat", "--state-root", $state, "--runtime-root", $runtime, "--market-date", $MarketDate, "--stage", "intraday_monitor", "--status", "RUNNING", "--release-sha", $releaseSha) `
    -LogRoot $logRoot `
    -LogName "alpha_monitor_heartbeat-$MarketDate"
if ($heartbeat.exit_code -ne 0) {
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
    throw "Could not persist monitor heartbeat."
}

Push-Location $runtime
try {
    $calendar = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.services.market_calendar", "--date", $MarketDate) `
        -LogRoot $logRoot `
        -LogName "alpha_monitor_calendar-$MarketDate"
    $calendarExit = $calendar.exit_code
    if ($calendarExit -eq 10) {
        $record = Write-MonitorStage -Status SKIPPED_NOT_APPLICABLE -ExitCode 0
        exit $(if ($record.exit_code -eq 0) { 0 } else { 2 })
    }
    if ($calendarExit -ne 0) {
        $exitCode = $calendarExit
        $errorCode = "market_calendar_failed"
    }

    if ($exitCode -eq 0) {
        try {
            $alphaArtifact = Test-DawnstrikeAlphaCycleArtifact `
                -ArtifactPath $alphaCyclePath `
                -MarketDate $MarketDate `
                -ReleaseSha $releaseSha
            $scenarioCandidateCount = [int64]$alphaArtifact.research_candidate_count
            $scenarioSymbols = [string]::Join(",", @($alphaArtifact.research_symbols))
        }
        catch {
            $exitCode = 2
            $errorCode = "alpha_cycle_artifact_invalid"
        }
    }

    if ($exitCode -eq 0) {
        $monitor = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "alpha-monitor", "--db-path", $dbPath, "--notify", $Notify) `
            -LogRoot $logRoot `
            -LogName "alpha_monitor-$MarketDate"
        if ($monitor.exit_code -ne 0) {
            $exitCode = $monitor.exit_code
            $errorCode = "alpha_monitor_failed"
        }
    }
    if ($exitCode -eq 0) {
        $scenarioWatchArgs = @()
        if ($env:DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED -match '^(?i:true|1|yes|y)$') {
            $scenarioWatchArgs += "--include-scenarios"
        }
        $watch = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList (@("-m", "intraday_scanner.cli", "trade-watch", "--db-path", $dbPath, "--market-date", $MarketDate, "--mode", "paper_execute", "--source", "alpaca", "--notify", $Notify, "--simulated-equity", "100000", "--max-open-positions", "3", "--max-daily-entries", "10", "--min-reward-risk", "1.5", "--expected-code-sha", $releaseSha) + $scenarioWatchArgs) `
            -LogRoot $logRoot `
            -LogName "trade_watch-$MarketDate"
        if ($watch.exit_code -ne 0) {
            $exitCode = $watch.exit_code
            $errorCode = "trade_watcher_failed"
        }
    }
    $coreExitCode = $exitCode
    $coreErrorCode = $errorCode
    $status = if ($coreExitCode -eq 0) { "COMPLETE" } else { "FAILED" }
    $coreRecord = Write-MonitorStage `
        -Status $status `
        -ExitCode $coreExitCode `
        -ErrorCode $coreErrorCode
    if (
        $coreExitCode -eq 0 -and
        $env:DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED -match '^(?i:true|1|yes|y)$'
    ) {
        if ($scenarioCandidateCount -le 0) {
            $scenarioStatus = "SKIPPED_NOT_APPLICABLE"
            $scenarioErrorCode = ""
        }
        else {
            try {
                # The five-minute monitor owns time-sensitive paper lifecycle checks.
                # Scenario work therefore runs after trade-watch and is hard-bounded to
                # at most three 30-second model calls inside the four-minute task limit.
                $env:DAWNSTRIKE_SCENARIO_MAX_ARTICLES_PER_RUN = "3"
                $env:DAWNSTRIKE_SCENARIO_OPENAI_TIMEOUT_SECONDS = "30"
                $scenarioSince = Get-ScenarioMonitorWatermark
                # Record the start rather than the end. The next query overlaps this run,
                # so news published while processing is re-read and deduplicated.
                $nextScenarioWatermark = (Get-Date).ToUniversalTime().ToString("o")
                $scenario = Invoke-DawnstrikeNativeProcess `
                    -FilePath "py.exe" `
                    -ArgumentList @("-m", "intraday_scanner.cli", "scenario-monitor", "--db-path", $dbPath, "--symbols", $scenarioSymbols, "--since", $scenarioSince, "--notify", $Notify) `
                    -LogRoot $logRoot `
                    -LogName "scenario_monitor-$MarketDate"
                $scenarioExitCode = [int]$scenario.exit_code
                $scenarioStatus = if ($scenario.exit_code -eq 0) { "COMPLETE" } else { "FAILED" }
                $scenarioErrorCode = if ($scenario.exit_code -eq 0) { "" } else { "scenario_cycle_failed" }
                if ($scenario.exit_code -ne 0) {
                    # The optional Scenario stage records its own failure below.
                }
                else {
            Save-ScenarioMonitorWatermark -WatermarkUtc $nextScenarioWatermark
                }
            }
            catch {
                $scenarioExitCode = 2
                $scenarioStatus = "FAILED"
                $scenarioErrorCode = "scenario_orchestration_failed"
            }
        }
        try {
            $scenarioStage = Write-ScenarioStage `
                -Status $scenarioStatus `
                -ExitCode $(if ($null -eq $scenarioExitCode) { 0 } else { $scenarioExitCode }) `
                -ErrorCode $scenarioErrorCode
            if ($scenarioStage.exit_code -ne 0) {
                $scenarioStageRecordFailed = $true
            }
        }
        catch {
            $scenarioStageRecordFailed = $true
        }
    }
    $outcome = Resolve-DawnstrikeCoreOptionalOutcome `
        -CoreExitCode $coreExitCode `
        -OptionalExitCode $scenarioExitCode `
        -RecordStageFailed ($scenarioStageRecordFailed -or $coreRecord.exit_code -ne 0)
    exit $outcome.final_exit_code
}
finally {
    Pop-Location
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
}
