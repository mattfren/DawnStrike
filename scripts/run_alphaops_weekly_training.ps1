[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd")
)

$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path $RuntimeRoot).Path
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
. (Join-Path $PSScriptRoot "import_dawnstrike_environment.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_process_runner.ps1")
Import-DawnstrikeEnvironment -StateRoot $state

$dbPath = Join-Path $state "shadow_real.sqlite"
$outputRoot = Join-Path $state "outputs\alpha_v6_research"
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$releaseSha = Resolve-DawnstrikeReleaseSha -RuntimeRoot $runtime -LogRoot $logRoot

Push-Location $runtime
try {
    $calendar = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.services.market_calendar", "--date", $MarketDate) `
        -LogRoot $logRoot `
        -LogName "alpha_v6_weekly_calendar-$MarketDate"
    if ($calendar.exit_code -eq 10) {
        Write-Output "Skipped weekly V6 training: $MarketDate is not an exchange session."
        exit 0
    }
    if ($calendar.exit_code -ne 0) {
        throw "Market calendar failed with exit code $($calendar.exit_code)."
    }

    $training = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-v6-train-weekly", "--db-path", $dbPath, "--code-sha", $releaseSha, "--market-date", $MarketDate) `
        -LogRoot $logRoot `
        -LogName "alpha_v6_weekly_training-$MarketDate"
    if ($training.exit_code -ne 0) {
        exit $training.exit_code
    }

    $packet = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-v6-research-packet", "--db-path", $dbPath, "--code-sha", $releaseSha, "--out-dir", $outputRoot) `
        -LogRoot $logRoot `
        -LogName "alpha_v6_weekly_packet-$MarketDate"
    exit $packet.exit_code
} finally {
    Pop-Location
}
