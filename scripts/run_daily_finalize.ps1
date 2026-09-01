[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$MarketDate = "",
    [int]$RetryLimit = 2,
    [int]$RetryDelaySeconds = 900,
    [ValidateSet("LocalOnly", "Preview", "Production")]
    [string]$PublicationMode = "Production",
    [string]$VercelProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [string]$ProductionUrl = "https://dawnstrike-command-center-x3.vercel.app",
    [string]$TestNowUtc = ""
)

$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path $RuntimeRoot).Path
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
. (Join-Path $PSScriptRoot "import_dawnstrike_environment.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_process_runner.ps1")
. (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")
Import-DawnstrikeEnvironment -StateRoot $state

function Get-DawnstrikeFinalizeNowUtc {
    param([string]$Override)
    if (-not [string]::IsNullOrWhiteSpace($Override)) {
        if ($env:DAWNSTRIKE_TEST_CLOCK -ne "1") {
            throw "Finalize clock override is test-only."
        }
        try {
            $parsed = [DateTimeOffset]::Parse(
                $Override,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            )
        } catch { throw "Finalize clock override is invalid." }
        if ($parsed.Offset -ne [TimeSpan]::Zero) {
            throw "Finalize clock override must be UTC."
        }
        return $parsed.ToUniversalTime()
    }
    return [DateTimeOffset]::UtcNow
}

function Resolve-DawnstrikeFinalizeMarketBoundary {
    param(
        [Parameter(Mandatory = $true)][string]$Mode,
        [Parameter(Mandatory = $true)][string]$RequestedDate,
        [Parameter(Mandatory = $true)][DateTimeOffset]$NowUtc,
        [Parameter(Mandatory = $true)][string]$LogRoot,
        [switch]$UseClockOverride
    )
    if ($Mode -eq "LocalOnly") {
        return [pscustomobject]@{
            expected_market_date = $RequestedDate
            current_market_date = $null
            authorization_required = $false
            offline_replay = $true
        }
    }
    $boundaryArguments = @(
        "scripts\publication_boundary.py", "validate",
        "--market-date", $RequestedDate,
        "--publication-mode", $Mode
    )
    if ($UseClockOverride) {
        $boundaryArguments += @("--now-utc", $NowUtc.ToUniversalTime().ToString("o"))
    }
    $boundary = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList $boundaryArguments `
        -LogRoot $LogRoot `
        -LogName "daily_finalize_market_boundary-$RequestedDate"
    if ($boundary.exit_code -ne 0) {
        throw "Daily finalize market boundary blocked the requested date $RequestedDate."
    }
    try {
        $payload = (Get-Content -LiteralPath $boundary.stdout_path -Raw) | ConvertFrom-Json
    } catch { throw "Daily finalize market boundary returned invalid JSON." }
    if ($payload.ready -ne $true -or [string]$payload.expected_market_date -ne $RequestedDate) {
        throw "Daily finalize market boundary did not authorize the requested date."
    }
    return $payload
}

$finalizeNowUtc = Get-DawnstrikeFinalizeNowUtc -Override $TestNowUtc
$requestedMarketDate = if ([string]::IsNullOrWhiteSpace($MarketDate)) {
    if ($PublicationMode -eq "LocalOnly") {
        (Get-Date).ToString("yyyy-MM-dd")
    } else {
        [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId(
            $finalizeNowUtc, "Eastern Standard Time"
        ).ToString("yyyy-MM-dd")
    }
} else { $MarketDate.Trim() }
$logRoot = Join-Path $state "logs"
$releaseSha = Resolve-DawnstrikeReleaseSha -RuntimeRoot $runtime -LogRoot $logRoot
if ($PublicationMode -eq "Production") {
    # Converge any uniquely sealed interrupted provider operation before the
    # calendar, build, database, or current authorization can short-circuit
    # this scheduled run. RecoveryOnly cannot stage or promote fresh bytes.
    & (Join-Path $runtime "scripts\publish_vercel_public.ps1") `
        -ProjectRoot $runtime `
        -ProjectId $VercelProjectId `
        -StateRoot $state `
        -ExpectedMarketDate $requestedMarketDate `
        -RecoveryOnly
}
$boundary = Resolve-DawnstrikeFinalizeMarketBoundary `
    -Mode $PublicationMode `
    -RequestedDate $requestedMarketDate `
    -NowUtc $finalizeNowUtc `
    -LogRoot $logRoot `
    -UseClockOverride:(-not [string]::IsNullOrWhiteSpace($TestNowUtc))
$MarketDate = [string]$boundary.expected_market_date
$dbPath = Join-Path $state "shadow_real.sqlite"
$paperOpsRoot = Join-Path $state "v2_paper_ops_live"
$outputPath = Join-Path $runtime "build\public"
$resultPath = Join-Path $state "outputs\daily_finalize\$MarketDate\daily-finalize-result.json"
$deploymentResult = Join-Path $runtime "build\daily-deployment-result.json"

if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) {
    throw "Dawnstrike durable state database not found: $dbPath"
}
New-Item -ItemType Directory -Path $paperOpsRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $resultPath) -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "daily_finalize"
if (-not $dailyLock.acquired) {
    $receiptWritten = Write-DawnstrikeLockDenialReceipt -StateRoot $state -MarketDate $MarketDate -Owner "daily_finalize" -Lock $dailyLock
    [Console]::Error.WriteLine(
        "Required daily finalize run blocked by daily lock: $($dailyLock.reason)"
    )
    exit $(if ($receiptWritten) { 3 } else { 2 })
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
    $calendar = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.services.market_calendar", "--date", $MarketDate) `
        -LogRoot $logRoot `
        -LogName "daily_finalize_calendar-$MarketDate"
    $calendarExit = $calendar.exit_code
    if ($calendarExit -eq 10) {
        # A closed/holiday date is a successful terminal observation, not a
        # missing run. Persist every DAG stage and a terminal heartbeat so
        # status tooling can distinguish it from a stale or failed weekday.
        $terminalStartedAt = (Get-Date).ToUniversalTime().ToString("o")
        $closedFunnelPath = Join-Path $state "outputs\daily_finalize\$MarketDate\non-session-terminal.json"
        $closedFunnel = [ordered]@{
            schema_version = "dawnstrike.daily_no_trade_funnel.v1"
            terminal_state = "SKIPPED_NOT_APPLICABLE"
            status = "NO_TRADE"
            reason = "market_closed"
            candidate_count = 0
            selected_count = 0
            delivered_count = 0
            paper_fill_count = 0
            return_pct = $null
            gross_return_pct = $null
            net_pnl = $null
            picks = $null
            missing_truth_is_zero = $false
            research_only = $true
            live_trading_enabled = $false
        }
        $closedFunnelJson = $closedFunnel | ConvertTo-Json -Depth 8
        [System.IO.File]::WriteAllText(
            $closedFunnelPath,
            $closedFunnelJson,
            (New-Object System.Text.UTF8Encoding($false))
        )
        $closedStages = @(
            "morning_collection", "ranking_delivery", "indeterminate_research",
            "intraday_monitor", "scenario_intelligence", "scenario_finalization",
            "eod_outcome_capture", "paper_reconciliation", "alpha_learning",
            "paperops_forward", "canonical_performance", "calendar_build",
            "publication", "readiness"
        )
        foreach ($stageName in $closedStages) {
            $optional = @(
                "indeterminate_research", "scenario_intelligence",
                "scenario_finalization", "alpha_learning", "paperops_forward"
            ) -contains $stageName
            $stageArguments = @(
                "scripts\record_daily_stage.py", "--db-path", $dbPath,
                "--market-date", $MarketDate, "--stage", $stageName,
                "--status", "SKIPPED_NOT_APPLICABLE", "--runtime-root", $runtime,
                "--state-root", $state, "--release-sha", $releaseSha,
                "--exit-code", "0", "--started-at", $terminalStartedAt,
                "--result-file", $closedFunnelPath,
                "--error-code", "market_closed",
                "--error-detail", "No scheduled equity session; no-trade funnel is explicit and returns remain null."
            )
            if ($optional) { $stageArguments += "--not-required" }
            $stageReceipt = Invoke-DawnstrikeNativeProcess `
                -FilePath "py.exe" `
                -ArgumentList $stageArguments `
                -LogRoot $logRoot `
                -LogName "daily_finalize_closed_stage-$stageName-$MarketDate"
            if ($stageReceipt.exit_code -ne 0) {
                throw "Could not persist closed-market stage $stageName."
            }
        }
        $terminalHeartbeat = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "daily-heartbeat", "--state-root", $state, "--runtime-root", $runtime, "--market-date", $MarketDate, "--stage", "readiness", "--status", "SKIPPED_NOT_APPLICABLE", "--release-sha", $releaseSha) `
            -LogRoot $logRoot `
            -LogName "daily_finalize_terminal_heartbeat-$MarketDate"
        if ($terminalHeartbeat.exit_code -ne 0) {
            throw "Could not persist closed-market terminal heartbeat."
        }
        Write-Output (
            "Closed/non-session date $MarketDate recorded as SKIPPED_NOT_APPLICABLE; no publication was attempted."
        )
        exit 0
    }
    if ($calendarExit -ne 0) {
        throw "Market calendar failed with exit code $calendarExit"
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

    $buildAttemptId = [guid]::NewGuid().ToString("N").ToLowerInvariant()
    $build = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("scripts\build_public.py", "--db-path", $dbPath, "--state-root", $state, "--paper-ops-root", $paperOpsRoot, "--out-dir", $outputPath, "--result-out", $resultPath, "--build-attempt-id", $buildAttemptId, "--date", $MarketDate, "--retry-limit", "$RetryLimit", "--retry-delay-seconds", "$RetryDelaySeconds") `
        -LogRoot $logRoot `
        -LogName "daily_finalize_build-$MarketDate"
    $buildExit = $build.exit_code

    if ($buildExit -ne 0) {
        Write-Output (
            "Daily finalize remained degraded or failed; production was not " +
            "updated. Exit code: $buildExit"
        )
        exit $buildExit
    }
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
        throw "Daily finalize build succeeded without its attempt-bound result receipt."
    }
    $notification = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("scripts\send_daily_finalize_notification.py", "--result-file", $resultPath, "--db-path", $dbPath, "--expected-build-attempt-id", $buildAttemptId) `
        -LogRoot $logRoot `
        -LogName "daily_finalize_notification-$MarketDate"
    if ($notification.exit_code -ne 0) {
        throw "Daily finalize result receipt did not match this build attempt."
    }
    if ($PublicationMode -eq "LocalOnly") {
        exit 0
    }

    # Authorize upload from the local, exact-SHA artifact only after all
    # non-publication stages and the strict readiness/artifact checks pass.
    # The publication stage is deliberately excluded because Vercel upload is
    # the side effect this gate authorizes; post-publication identity is
    # verified by verify_daily_finalize_receipt.py.
    $prepublication = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @(
            "scripts\verify_daily_prepublication.py", "--db-path", $dbPath,
            "--artifact-root", $outputPath, "--market-date", $MarketDate,
            "--release-sha", $releaseSha, "--runtime-root", $runtime,
            "--expected-market-date", $MarketDate
        ) `
        -LogRoot $logRoot `
        -LogName "daily_finalize_prepublication-$MarketDate"
    if ($prepublication.exit_code -ne 0) {
        throw "Daily prepublication gate blocked Vercel publication."
    }
    try {
        $prepublicationPayload = (Get-Content -LiteralPath $prepublication.stdout_path -Raw) | ConvertFrom-Json
    } catch { throw "Daily prepublication gate returned invalid JSON." }
    $authorizationId = [string]$prepublicationPayload.authorization_id
    if ([string]::IsNullOrWhiteSpace($authorizationId)) {
        throw "Daily prepublication gate did not return an immutable authorization identity."
    }

    try {
        & (Join-Path $runtime "scripts\publish_vercel_public.ps1") `
            -ProjectRoot $runtime `
            -ProjectId $VercelProjectId `
            -StateRoot $state `
            -ExpectedMarketDate $MarketDate `
            -PrepublicationAuthorizationId $authorizationId `
            -DailyLedgerAuthorizationId ([string]$prepublicationPayload.daily_ledger_authorization_id) `
            -TestNowUtc $TestNowUtc `
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
        -ArgumentList @("scripts\send_daily_finalize_notification.py", "--result-file", $resultPath, "--db-path", $dbPath, "--expected-build-attempt-id", $buildAttemptId, "--deployment-url", $ProductionUrl) `
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
