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
Import-DawnstrikeEnvironment -StateRoot $state
$dbPath = Join-Path $state "shadow_real.sqlite"
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$exitCode = 0
$errorCode = ""
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
$dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "alphaops_monitor"
if (-not $dailyLock.acquired) {
    Write-Output "Skipped duplicate AlphaOps monitor run: $($dailyLock.reason)"
    exit 0
}
$heartbeat = Invoke-DawnstrikeNativeProcess `
    -FilePath "py.exe" `
    -ArgumentList @("-m", "intraday_scanner.cli", "daily-heartbeat", "--state-root", $state, "--runtime-root", $runtime, "--market-date", $MarketDate, "--stage", "intraday_monitor", "--status", "RUNNING") `
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
        exit $record.exit_code
    }
    if ($calendarExit -ne 0) {
        $exitCode = $calendarExit
        $errorCode = "market_calendar_failed"
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
        $watch = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "trade-watch", "--db-path", $dbPath, "--market-date", $MarketDate, "--mode", "paper_execute", "--source", "auto", "--notify", $Notify, "--simulated-equity", "100000", "--max-open-positions", "3", "--max-daily-entries", "10", "--min-reward-risk", "1.5") `
            -LogRoot $logRoot `
            -LogName "trade_watch-$MarketDate"
        if ($watch.exit_code -ne 0) {
            $exitCode = $watch.exit_code
            $errorCode = "trade_watcher_failed"
        }
    }
    $status = if ($exitCode -eq 0) { "COMPLETE" } else { "FAILED" }
    $record = Write-MonitorStage `
        -Status $status `
        -ExitCode $exitCode `
        -ErrorCode $errorCode
    if ($record.exit_code -ne 0) {
        $exitCode = 2
    }
    exit $exitCode
}
finally {
    Pop-Location
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
}
