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
$dbPath = Join-Path $state "shadow_real.sqlite"
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$exitCode = 0
$errorCode = ""

Push-Location $runtime
try {
    & py.exe -m intraday_scanner.services.market_calendar --date $MarketDate
    $calendarExit = $LASTEXITCODE
    if ($calendarExit -eq 10) {
        & py.exe scripts\record_daily_stage.py `
            --db-path $dbPath `
            --market-date $MarketDate `
            --stage intraday_monitor `
            --status SKIPPED_NOT_APPLICABLE `
            --runtime-root $runtime `
            --state-root $state `
            --exit-code 0 `
            --started-at $startedAt
        exit $LASTEXITCODE
    }
    if ($calendarExit -ne 0) {
        $exitCode = $calendarExit
        $errorCode = "market_calendar_failed"
    }

    if ($exitCode -eq 0) {
        & py.exe -m intraday_scanner.cli alpha-monitor `
            --db-path $dbPath `
            --notify $Notify 2>&1 |
            Tee-Object -FilePath (Join-Path $logRoot "alpha_monitor-$MarketDate.log") -Append
        if ($LASTEXITCODE -ne 0) {
            $exitCode = $LASTEXITCODE
            $errorCode = "alpha_monitor_failed"
        }
    }
    if ($exitCode -eq 0) {
        & py.exe -m intraday_scanner.cli trade-watch `
            --db-path $dbPath `
            --market-date $MarketDate `
            --mode paper_execute `
            --source auto `
            --notify $Notify `
            --simulated-equity 100000 `
            --max-open-positions 3 `
            --max-daily-entries 10 `
            --min-reward-risk 1.5 2>&1 |
            Tee-Object -FilePath (Join-Path $logRoot "trade_watch-$MarketDate.log") -Append
        if ($LASTEXITCODE -ne 0) {
            $exitCode = $LASTEXITCODE
            $errorCode = "trade_watcher_failed"
        }
    }
    $status = if ($exitCode -eq 0) { "COMPLETE" } else { "FAILED" }
    & py.exe scripts\record_daily_stage.py `
        --db-path $dbPath `
        --market-date $MarketDate `
        --stage intraday_monitor `
        --status $status `
        --runtime-root $runtime `
        --state-root $state `
        --exit-code $exitCode `
        --started-at $startedAt `
        --error-code $errorCode
    if ($LASTEXITCODE -ne 0) {
        $exitCode = 2
    }
    exit $exitCode
}
finally {
    Pop-Location
}
