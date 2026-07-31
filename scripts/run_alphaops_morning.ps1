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
Import-DawnstrikeEnvironment -StateRoot $state
$dbPath = Join-Path $state "shadow_real.sqlite"
$outputRoot = Join-Path $state "outputs\alpha_cycle\$MarketDate"
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$startedAt = (Get-Date).ToUniversalTime().ToString("o")

Push-Location $runtime
try {
    & py.exe -m intraday_scanner.services.market_calendar --date $MarketDate
    $calendarExit = $LASTEXITCODE
    if ($calendarExit -eq 10) {
        foreach ($stage in @("morning_collection", "ranking_delivery")) {
            & py.exe scripts\record_daily_stage.py `
                --db-path $dbPath `
                --market-date $MarketDate `
                --stage $stage `
                --status SKIPPED_NOT_APPLICABLE `
                --runtime-root $runtime `
                --state-root $state `
                --exit-code 0 `
                --started-at $startedAt
        }
        exit 0
    }
    if ($calendarExit -ne 0) {
        throw "Market calendar failed with exit code $calendarExit"
    }

    $configPath = Join-Path $runtime "config\web_sources.yaml"
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        $configPath = Join-Path $runtime "config\web_sources.example.yaml"
    }
    & py.exe -m intraday_scanner.cli alpha-cycle `
        --config $configPath `
        --db-path $dbPath `
        --out-dir $outputRoot `
        --notify $Notify 2>&1 |
        Tee-Object -FilePath (Join-Path $logRoot "alpha_morning-$MarketDate.log")
    $stageExit = $LASTEXITCODE
    $status = if ($stageExit -eq 0) { "COMPLETE" } else { "FAILED" }
    $errorCode = if ($stageExit -eq 0) { "" } else { "alpha_cycle_failed" }
    foreach ($stage in @("morning_collection", "ranking_delivery")) {
        & py.exe scripts\record_daily_stage.py `
            --db-path $dbPath `
            --market-date $MarketDate `
            --stage $stage `
            --status $status `
            --runtime-root $runtime `
            --state-root $state `
            --exit-code $stageExit `
            --started-at $startedAt `
            --error-code $errorCode
        if ($LASTEXITCODE -ne 0) {
            $stageExit = 2
        }
    }
    exit $stageExit
}
finally {
    Pop-Location
}
