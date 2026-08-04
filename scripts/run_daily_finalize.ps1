[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [int]$RetryLimit = 2,
    [int]$RetryDelaySeconds = 900,
    [ValidateSet("LocalOnly", "Preview", "Production")]
    [string]$PublicationMode = "Production",
    [string]$VercelProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [string]$ProductionUrl = "https://dawnstrike-command-center-x3.vercel.app"
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
$paperOpsRoot = Join-Path $state "v2_paper_ops_live"
$outputPath = Join-Path $runtime "build\public"
$resultPath = Join-Path $state "outputs\daily_finalize\$MarketDate\daily-finalize-result.json"
$deploymentResult = Join-Path $runtime "build\daily-deployment-result.json"
$logRoot = Join-Path $state "logs"
$marketDateWasExplicit = $PSBoundParameters.ContainsKey("MarketDate")

if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) {
    throw "Dawnstrike durable state database not found: $dbPath"
}
New-Item -ItemType Directory -Path $paperOpsRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $resultPath) -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "daily_finalize"
if (-not $dailyLock.acquired) {
    Write-Output "Skipped duplicate daily finalize run: $($dailyLock.reason)"
    exit 0
}
$heartbeat = Invoke-DawnstrikeNativeProcess `
    -FilePath "py.exe" `
    -ArgumentList @("-m", "intraday_scanner.cli", "daily-heartbeat", "--state-root", $state, "--runtime-root", $runtime, "--market-date", $MarketDate, "--stage", "canonical_performance", "--status", "RUNNING") `
    -LogRoot $logRoot `
    -LogName "daily_finalize_heartbeat-$MarketDate"
if ($heartbeat.exit_code -ne 0) {
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
    throw "Could not persist daily-finalize heartbeat."
}

Push-Location $runtime
try {
    if (-not $marketDateWasExplicit) {
        $calendar = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.services.market_calendar", "--date", $MarketDate) `
            -LogRoot $logRoot `
            -LogName "daily_finalize_calendar-$MarketDate"
        $calendarExit = $calendar.exit_code
        if ($calendarExit -eq 10) {
            Write-Output (
                "Skipping non-market date $MarketDate; no publication was attempted."
            )
            exit 0
        }
        if ($calendarExit -ne 0) {
            throw "Market calendar failed with exit code $calendarExit"
        }
    }

    if ($env:DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED -match '^(?i:true|1|yes|y)$') {
        $scenarioClose = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "scenario-close", "--db-path", $dbPath, "--market-date", $MarketDate, "--at", "16:00", "--source", "alpaca", "--notify", "telegram") `
            -LogRoot $logRoot `
            -LogName "scenario_close-$MarketDate"
        if ($scenarioClose.exit_code -ne 0) {
            throw "Scenario end-of-day paper lifecycle failed with exit code $($scenarioClose.exit_code)"
        }
        $scenarioFinalize = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "scenario-finalize", "--db-path", $dbPath, "--market-date", $MarketDate) `
            -LogRoot $logRoot `
            -LogName "scenario_finalize-$MarketDate"
        if ($scenarioFinalize.exit_code -ne 0) {
            throw "Scenario performance finalization failed with exit code $($scenarioFinalize.exit_code)"
        }
    }

    $build = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("scripts\build_public.py", "--db-path", $dbPath, "--state-root", $state, "--paper-ops-root", $paperOpsRoot, "--out-dir", $outputPath, "--result-out", $resultPath, "--date", $MarketDate, "--retry-limit", "$RetryLimit", "--retry-delay-seconds", "$RetryDelaySeconds") `
        -LogRoot $logRoot `
        -LogName "daily_finalize_build-$MarketDate"
    $buildExit = $build.exit_code

    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        $notification = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("scripts\send_daily_finalize_notification.py", "--result-file", $resultPath, "--db-path", $dbPath) `
            -LogRoot $logRoot `
            -LogName "daily_finalize_notification-$MarketDate"
        if ($notification.exit_code -ne 0) {
            Write-Warning "Daily finalize notification could not be recorded or sent."
        }
    }
    if ($buildExit -ne 0) {
        Write-Output (
            "Daily finalize remained degraded or failed; production was not " +
            "updated. Exit code: $buildExit"
        )
        exit $buildExit
    }
    if ($PublicationMode -eq "LocalOnly") {
        exit 0
    }

    try {
        & (Join-Path $runtime "scripts\publish_vercel_public.ps1") `
            -ProjectRoot $runtime `
            -ProjectId $VercelProjectId `
            -Promote:($PublicationMode -eq "Production")
    }
    catch {
        $failure = $_.Exception.Message
        [void](Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("scripts\record_daily_stage.py", "--db-path", $dbPath, "--market-date", $MarketDate, "--stage", "publication", "--status", "FAILED", "--runtime-root", $runtime, "--state-root", $state, "--exit-code", "2", "--error-code", "vercel_publication_failed", "--error-detail", $failure) `
            -LogRoot $logRoot `
            -LogName "daily_finalize_publication_failure-$MarketDate")
        throw
    }

    if (-not (Test-Path -LiteralPath $deploymentResult -PathType Leaf)) {
        throw "Verified deployment result was not written: $deploymentResult"
    }
    $publicationStage = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("scripts\record_daily_stage.py", "--db-path", $dbPath, "--market-date", $MarketDate, "--stage", "publication", "--status", "COMPLETE", "--runtime-root", $runtime, "--state-root", $state, "--exit-code", "0", "--result-file", $deploymentResult, "--output-file", $deploymentResult) `
        -LogRoot $logRoot `
        -LogName "daily_finalize_publication-$MarketDate"
    if ($publicationStage.exit_code -ne 0) {
        throw "Verified deployment could not be recorded in the shared run ledger."
    }
    $postDeploymentNotification = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("scripts\send_daily_finalize_notification.py", "--result-file", $resultPath, "--db-path", $dbPath, "--deployment-url", $ProductionUrl) `
        -LogRoot $logRoot `
        -LogName "daily_finalize_post_deployment_notification-$MarketDate"
    if ($postDeploymentNotification.exit_code -ne 0) {
        Write-Warning "Post-deployment notification could not be recorded or sent."
    }
}
finally {
    Pop-Location
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
}
