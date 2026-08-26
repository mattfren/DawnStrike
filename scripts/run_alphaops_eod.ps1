[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [int]$PaperOpsRetryLimit = 3,
    [int]$PaperOpsRetryDelaySeconds = 60
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
$outputRoot = Join-Path $state "outputs"
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $paperOpsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$releaseSha = Resolve-DawnstrikeReleaseSha -RuntimeRoot $runtime -LogRoot $logRoot
$overallExit = 0
function Set-OverallFailure {
    param([int]$ExitCode)
    if ($ExitCode -ne 0 -and $script:overallExit -eq 0) {
        $script:overallExit = $ExitCode
    }
}
$dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "alphaops_eod"
if (-not $dailyLock.acquired) {
    $receiptWritten = Write-DawnstrikeLockDenialReceipt -StateRoot $state -MarketDate $MarketDate -Owner "alphaops_eod" -Lock $dailyLock
    [Console]::Error.WriteLine(
        "Required AlphaOps EOD run blocked by daily lock: $($dailyLock.reason)"
    )
    exit $(if ($receiptWritten) { 3 } else { 2 })
}
$heartbeat = Invoke-DawnstrikeNativeProcess `
    -FilePath "py.exe" `
    -ArgumentList @("-m", "intraday_scanner.cli", "daily-heartbeat", "--state-root", $state, "--runtime-root", $runtime, "--market-date", $MarketDate, "--stage", "eod_outcome_capture", "--status", "RUNNING") `
    -LogRoot $logRoot `
    -LogName "alpha_eod_heartbeat-$MarketDate"
if ($heartbeat.exit_code -ne 0) {
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
    throw "Could not persist EOD heartbeat."
}

function Write-Stage {
    param(
        [string]$Name,
        [string]$Status,
        [int]$ExitCode,
        [string]$StartedAt,
        [string]$ResultFile = "",
        [string]$OutputFile = "",
        [string]$ErrorCode = "",
        [switch]$NotRequired
    )
    $arguments = @(
        "scripts\record_daily_stage.py",
        "--db-path", $dbPath,
        "--market-date", $MarketDate,
        "--stage", $Name,
        "--status", $Status,
        "--runtime-root", $runtime,
        "--state-root", $state,
        "--exit-code", "$ExitCode",
        "--started-at", $StartedAt
    )
    if ($ResultFile) { $arguments += @("--result-file", $ResultFile) }
    if ($OutputFile) { $arguments += @("--output-file", $OutputFile) }
    if ($ErrorCode) { $arguments += @("--error-code", $ErrorCode) }
    if ($NotRequired) { $arguments += "--not-required" }
    $receipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList $arguments `
        -LogRoot $logRoot `
        -LogName "record_stage-$Name-$MarketDate"
    if ($receipt.exit_code -ne 0) {
        $script:overallExit = 2
    }
}

Push-Location $runtime
try {
    $calendar = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.services.market_calendar", "--date", $MarketDate) `
        -LogRoot $logRoot `
        -LogName "alpha_eod_calendar-$MarketDate"
    $calendarExit = $calendar.exit_code
    if ($calendarExit -eq 10) {
        foreach ($stage in @(
            "eod_outcome_capture",
            "paper_reconciliation",
            "alpha_learning",
            "paperops_forward",
            "scenario_finalization"
        )) {
            Write-Stage `
                -Name $stage `
                -Status SKIPPED_NOT_APPLICABLE `
                -ExitCode 0 `
                -StartedAt ((Get-Date).ToUniversalTime().ToString("o")) `
                -NotRequired:($stage -eq "scenario_finalization")
        }
        exit $overallExit
    }
    if ($calendarExit -ne 0) {
        throw "Market calendar failed with exit code $calendarExit"
    }

    $captureRoot = Join-Path $outputRoot "alpha_outcomes\$MarketDate"
    New-Item -ItemType Directory -Path $captureRoot -Force | Out-Null
    $captureResult = Join-Path $captureRoot "alpha_outcome_capture.json"
    $captureStarted = (Get-Date).ToUniversalTime().ToString("o")
    $capture = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-capture-outcomes", "--db-path", $dbPath, "--market-date", $MarketDate, "--out-dir", $captureRoot, "--persist") `
        -LogRoot $logRoot `
        -LogName "alpha_outcomes-$MarketDate"
    $captureExit = $capture.exit_code
    $outcomeGapResult = Join-Path $captureRoot "outcome-gap.json"
    $outcomeGap = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "outcome-gap", "--db-path", $dbPath, "--market-date", $MarketDate, "--out", $outcomeGapResult) `
        -LogRoot $logRoot `
        -LogName "alpha_outcome_gap-$MarketDate"
    $gateResult = Join-Path $captureRoot "alpha_eod_gate.json"
    $gate = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-eod-gate", "--db-path", $dbPath, "--market-date", $MarketDate, "--capture-exit-code", "$captureExit", "--capture-result", $captureResult, "--outcome-gap", $outcomeGapResult, "--out", $gateResult) `
        -LogRoot $logRoot `
        -LogName "alpha_eod_gate-$MarketDate"
    $gateExit = $gate.exit_code
    $officialOutcomesRequired = $true
    if ($gateExit -eq 0 -and (Test-Path -LiteralPath $gateResult)) {
        try {
            $gatePayload = Get-Content -LiteralPath $gateResult -Raw | ConvertFrom-Json
            $officialOutcomesRequired = [bool]$gatePayload.official_outcomes_required
        }
        catch {
            $gateExit = 2
        }
    }
    if ($gateExit -ne 0) {
        Set-OverallFailure -ExitCode $gateExit
        Write-Stage `
            -Name eod_outcome_capture `
            -Status TERMINAL_MISSING `
            -ExitCode $gateExit `
            -StartedAt $captureStarted `
            -ResultFile $gateResult `
            -OutputFile $captureResult `
            -ErrorCode outcome_capture_incomplete
    }
    elseif (-not $officialOutcomesRequired) {
        Write-Stage `
            -Name eod_outcome_capture `
            -Status SKIPPED_NOT_APPLICABLE `
            -ExitCode 0 `
            -StartedAt $captureStarted `
            -ResultFile $gateResult `
            -OutputFile $captureResult
    }
    else {
        Write-Stage `
            -Name eod_outcome_capture `
            -Status COMPLETE `
            -ExitCode 0 `
            -StartedAt $captureStarted `
            -ResultFile $gateResult `
            -OutputFile $captureResult
    }

    $reconcileRoot = Join-Path $outputRoot "strategy_reconciliation\$MarketDate"
    New-Item -ItemType Directory -Path $reconcileRoot -Force | Out-Null
    $reconcileResult = Join-Path $reconcileRoot "reconciliation.json"
    $reconcileStarted = (Get-Date).ToUniversalTime().ToString("o")
    if ($gateExit -ne 0) {
        $reconcileExit = $gateExit
    }
    elseif (-not $officialOutcomesRequired) {
        $reconcileExit = 0
        Write-Stage `
            -Name paper_reconciliation `
            -Status SKIPPED_NOT_APPLICABLE `
            -ExitCode 0 `
            -StartedAt $reconcileStarted `
            -ResultFile $gateResult
    }
    else {
        $reconcile = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "alpha-paper-reconcile", "--db-path", $dbPath, "--market-date", $MarketDate, "--out-dir", $reconcileRoot, "--persist") `
            -LogRoot $logRoot `
            -LogName "alpha_reconcile-$MarketDate"
        $reconcileExit = $reconcile.exit_code
    }
    if ($reconcileExit -eq 0 -and $officialOutcomesRequired) {
        Write-Stage `
            -Name paper_reconciliation `
            -Status COMPLETE `
            -ExitCode 0 `
            -StartedAt $reconcileStarted `
            -ResultFile $reconcileResult `
            -OutputFile $reconcileResult
    } elseif ($reconcileExit -ne 0) {
        Set-OverallFailure -ExitCode $reconcileExit
        Write-Stage `
            -Name paper_reconciliation `
            -Status FAILED `
            -ExitCode $reconcileExit `
            -StartedAt $reconcileStarted `
            -ResultFile $reconcileResult `
            -ErrorCode paper_reconciliation_failed
    }

    $learnStarted = (Get-Date).ToUniversalTime().ToString("o")
    $alphaLearningRequired = $officialOutcomesRequired
    if ($gateExit -ne 0) {
        $learnExit = $gateExit
    }
    elseif (-not $alphaLearningRequired) {
        $learnExit = 0
    }
    elseif ($reconcileExit -eq 0) {
        $learning = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "alpha-learn", "--db-path", $dbPath) `
            -LogRoot $logRoot `
            -LogName "alpha_learning-$MarketDate"
        $learnExit = $learning.exit_code
    } else {
        $learnExit = 2
    }
    if ($learnExit -ne 0) {
        Set-OverallFailure -ExitCode $learnExit
    }

    $v6DailyMonitor = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-v6-daily-monitor", "--db-path", $dbPath, "--market-date", $MarketDate) `
        -LogRoot $logRoot `
        -LogName "alpha_v6_daily_monitor-$MarketDate"
    if ($v6DailyMonitor.exit_code -ne 0) {
        Set-OverallFailure -ExitCode $v6DailyMonitor.exit_code
    }
    $v6Attribution = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-v6-attribution", "--db-path", $dbPath) `
        -LogRoot $logRoot `
        -LogName "alpha_v6_attribution-$MarketDate"
    if ($v6Attribution.exit_code -ne 0) {
        Set-OverallFailure -ExitCode $v6Attribution.exit_code
    }
    $v6Research = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-v6-research-packet", "--db-path", $dbPath, "--code-sha", $releaseSha, "--out-dir", (Join-Path $outputRoot "alpha_v6_research")) `
        -LogRoot $logRoot `
        -LogName "alpha_v6_research-$MarketDate"
    if ($v6Research.exit_code -ne 0) {
        Set-OverallFailure -ExitCode $v6Research.exit_code
    }
    $strategyLearningCutoff = "${MarketDate}T23:59:59+00:00"
    $strategyLearningSource = "sqlite-readonly:$dbPath;portfolio_performance.date<=${MarketDate};mode=ro;query_only=on"
    $strategyLearning = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @(
            "-m", "intraday_scanner.cli", "strategy-learning-daily",
            "--market-date", $MarketDate,
            "--cutoff", $strategyLearningCutoff,
            "--source-identity", $strategyLearningSource,
            "--code-sha", $releaseSha,
            "--out-dir", (Join-Path $outputRoot "strategy_learning"),
            "--db-path", $dbPath,
            "--paper-ops-root", $paperOpsRoot
        ) `
        -LogRoot $logRoot `
        -LogName "strategy_learning_daily-$MarketDate"
    if ($strategyLearning.exit_code -ne 0) {
        Set-OverallFailure -ExitCode $strategyLearning.exit_code
    }
    $learningStageExit = $learnExit
    $learningErrorCode = if ($learnExit -ne 0) { "alpha_learning_failed" } else { "" }
    foreach ($v6Result in @(
        @{ Result = $v6DailyMonitor; Code = "alpha_v6_daily_monitor_failed" },
        @{ Result = $v6Attribution; Code = "alpha_v6_attribution_failed" },
        @{ Result = $v6Research; Code = "alpha_v6_research_packet_failed" },
        @{ Result = $strategyLearning; Code = "strategy_learning_daily_failed" }
    )) {
        if ($learningStageExit -eq 0 -and $v6Result.Result.exit_code -ne 0) {
            $learningStageExit = $v6Result.Result.exit_code
            $learningErrorCode = $v6Result.Code
        }
    }
    if ($learningStageExit -eq 0 -and -not $alphaLearningRequired) {
        Write-Stage `
            -Name alpha_learning `
            -Status SKIPPED_NOT_APPLICABLE `
            -ExitCode 0 `
            -StartedAt $learnStarted `
            -ResultFile $gateResult
    } elseif ($learningStageExit -eq 0) {
        Write-Stage -Name alpha_learning -Status COMPLETE -ExitCode 0 -StartedAt $learnStarted
    } else {
        Write-Stage `
            -Name alpha_learning `
            -Status FAILED `
            -ExitCode $learningStageExit `
            -StartedAt $learnStarted `
            -ErrorCode $learningErrorCode
    }

    $attribution = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.cli", "alpha-attribution", "--db-path", $dbPath, "--out-dir", (Join-Path $outputRoot "alpha_attribution"), "--end", $MarketDate, "--paper-ops-root", $paperOpsRoot) `
        -LogRoot $logRoot `
        -LogName "alpha_attribution-$MarketDate"
    if ($attribution.exit_code -ne 0) {
        Set-OverallFailure -ExitCode $attribution.exit_code
    }
    # The aggregate outcome-gap artifact remains diagnostic.  The exact EOD
    # gate above binds the frozen official cohort and each official signal_id;
    # unrelated shadow candidates must not fail the official outcome stages.

    # Scenario Intelligence is an independent paper challenger.  Its terminal
    # lifecycle and return reconciliation must finish before the canonical
    # PaperOps chain and daily publication; a failure is recorded explicitly
    # rather than silently presenting stale Scenario states tomorrow.
    if ($env:DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED -match '^(?i:true|1|yes|y)$') {
        $scenarioStarted = (Get-Date).ToUniversalTime().ToString("o")
        $scenarioClose = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.cli", "scenario-close", "--db-path", $dbPath, "--market-date", $MarketDate, "--source", "auto", "--notify", "telegram") `
            -LogRoot $logRoot `
            -LogName "scenario_close-$MarketDate"
        $scenarioEodExit = $scenarioClose.exit_code
        if ($scenarioEodExit -eq 0) {
            $scenarioFinalize = Invoke-DawnstrikeNativeProcess `
                -FilePath "py.exe" `
                -ArgumentList @("-m", "intraday_scanner.cli", "scenario-finalize", "--db-path", $dbPath, "--market-date", $MarketDate) `
                -LogRoot $logRoot `
                -LogName "scenario_finalize-$MarketDate"
            $scenarioEodExit = $scenarioFinalize.exit_code
        }
        if ($scenarioEodExit -eq 0) {
            Write-Stage -Name scenario_finalization -Status COMPLETE -ExitCode 0 -StartedAt $scenarioStarted -NotRequired
        }
        else {
            Set-OverallFailure -ExitCode $scenarioEodExit
            Write-Stage `
                -Name scenario_finalization `
                -Status FAILED `
                -ExitCode $scenarioEodExit `
                -StartedAt $scenarioStarted `
                -ErrorCode scenario_finalization_failed `
                -NotRequired
        }
    }
    else {
        Write-Stage `
            -Name scenario_finalization `
            -Status SKIPPED_NOT_APPLICABLE `
            -ExitCode 0 `
            -StartedAt ((Get-Date).ToUniversalTime().ToString("o")) `
            -NotRequired
    }

    $paperStarted = (Get-Date).ToUniversalTime().ToString("o")
    $paperInit = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.v2.paper_ops", "init", "--output-root", $paperOpsRoot) `
        -LogRoot $logRoot `
        -LogName "paperops_init-$MarketDate"
    $paperExit = $paperInit.exit_code
    $reuseExistingDailyReport = $false
    $existingDailyReport = Join-Path $paperOpsRoot "reports\daily\forward_$MarketDate.json"
    if ($paperExit -eq 0 -and (Test-Path -LiteralPath $existingDailyReport -PathType Leaf)) {
        # A scheduler retry must not regenerate immutable events from a newer
        # provider snapshot.  Reuse an existing completed day only when the
        # entire stored ledger reconciles first; all truth checks below still run.
        $paperResume = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("-m", "intraday_scanner.v2.paper_ops", "reconcile", "--output-root", $paperOpsRoot) `
            -LogRoot $logRoot `
            -LogName "paperops-resume-reconcile-$MarketDate"
        $paperExit = $paperResume.exit_code
        $reuseExistingDailyReport = ($paperExit -eq 0)
    }
    if ($paperExit -eq 0 -and -not $reuseExistingDailyReport) {
        for ($attempt = 1; $attempt -le $PaperOpsRetryLimit; $attempt++) {
            $paperDay = Invoke-DawnstrikeNativeProcess `
                -FilePath "py.exe" `
                -ArgumentList @("-m", "intraday_scanner.v2.paper_ops", "run-day", "--date", $MarketDate, "--mode", "forward", "--output-root", $paperOpsRoot) `
                -LogRoot $logRoot `
                -LogName "paperops-$MarketDate-attempt-$attempt"
            $paperExit = $paperDay.exit_code
            if ($paperExit -eq 0) { break }
            if ($attempt -lt $PaperOpsRetryLimit) {
                Start-Sleep -Seconds $PaperOpsRetryDelaySeconds
            }
        }
    }
    if ($paperExit -eq 0) {
        foreach ($command in @(
            "reconcile",
            "verify-calendar",
            "calendar-view",
            "rebuild-ledger",
            "verify-source-bars",
            "blotter",
            "verify-blotter",
            "evidence",
            "readiness"
        )) {
            $extra = @()
            if ($command -in @("verify-source-bars", "blotter", "verify-blotter")) {
                $extra += @("--mode", "forward")
            }
            if ($command -eq "blotter") {
                $extra += @("--date", $MarketDate)
            }
            $paperCheck = Invoke-DawnstrikeNativeProcess `
                -FilePath "py.exe" `
                -ArgumentList (@("-m", "intraday_scanner.v2.paper_ops", $command, "--output-root", $paperOpsRoot) + $extra) `
                -LogRoot $logRoot `
                -LogName "paperops-$command-$MarketDate"
            if ($paperCheck.exit_code -ne 0) {
                $paperExit = $paperCheck.exit_code
                break
            }
        }
    }
    if ($paperExit -eq 0) {
        Write-Stage `
            -Name paperops_forward `
            -Status COMPLETE `
            -ExitCode 0 `
            -StartedAt $paperStarted
    } else {
        Set-OverallFailure -ExitCode $paperExit
        Write-Stage `
            -Name paperops_forward `
            -Status FAILED `
            -ExitCode $paperExit `
            -StartedAt $paperStarted `
            -ErrorCode paperops_forward_truth_failed
    }
    if ($overallExit -ne 0) {
        $failureNotification = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @("scripts\send_stage_failure_notification.py", "--db-path", $dbPath, "--market-date", $MarketDate) `
            -LogRoot $logRoot `
            -LogName "stage_failure_notification-$MarketDate"
        if ($failureNotification.exit_code -ne 0) {
            Write-Warning "Required-stage failure alert could not be recorded or sent."
        }
    }
    exit $overallExit
}
finally {
    Pop-Location
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
}
