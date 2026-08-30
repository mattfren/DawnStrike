[CmdletBinding()]
param(
    [string]$RuntimeRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [string]$Notify = "telegram",
    [string]$CoreUniverseManifest = "",
    [string]$PaperOpsRoot = "",
    [string]$BackupRoot = "C:\r\dawnstrike-state-backups",
    [ValidateRange(1, 365)]
    [int]$BackupRetention = 7
)

$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path $RuntimeRoot).Path
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
$paperOpsRoot = if ($PaperOpsRoot) { [System.IO.Path]::GetFullPath($PaperOpsRoot) } else { Join-Path $state "v2_paper_ops_live" }
. (Join-Path $PSScriptRoot "import_dawnstrike_environment.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_process_runner.ps1")
. (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")
. (Join-Path $PSScriptRoot "alpha_cycle_artifact.ps1")
Import-DawnstrikeEnvironment -StateRoot $state
$dbPath = Join-Path $state "shadow_real.sqlite"
$sourceConfigPath = Join-Path $state "config\web_sources.yaml"
$outputRoot = Join-Path $state "outputs\alpha_cycle\$MarketDate"
$defaultCoreUniverseManifest = Join-Path $state "config\luna_core_universe.json"
if (-not $CoreUniverseManifest -and (Test-Path -LiteralPath $defaultCoreUniverseManifest -PathType Leaf)) {
    $CoreUniverseManifest = $defaultCoreUniverseManifest
}
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$releaseSha = Resolve-DawnstrikeReleaseSha -RuntimeRoot $runtime -LogRoot $logRoot
$startedAt = (Get-Date).ToUniversalTime().ToString("o")
$recordStageFailed = $false
function Write-MorningStage {
    param(
        [string]$Name,
        [string]$Status,
        [int]$ExitCode,
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
        "--release-sha", $releaseSha,
        "--exit-code", "$ExitCode",
        "--started-at", $startedAt
    )
    if ($ErrorCode) { $arguments += @("--error-code", $ErrorCode) }
    if (Test-Path -LiteralPath $sourceConfigPath -PathType Leaf) {
        $arguments += @("--input-file", $sourceConfigPath)
    }
    if ($NotRequired) { $arguments += "--not-required" }
    $receipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList $arguments `
        -LogRoot $logRoot `
        -LogName "record_stage-$Name-$MarketDate"
    if ($receipt.exit_code -ne 0) { $script:recordStageFailed = $true }
}
$dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "alphaops_morning"
if (-not $dailyLock.acquired) {
    $lockReceiptWritten = Write-DawnstrikeLockDenialReceipt -StateRoot $state -MarketDate $MarketDate -Owner "alphaops_morning" -Lock $dailyLock
    foreach ($stage in @("morning_collection", "ranking_delivery")) {
        Write-MorningStage `
            -Name $stage `
            -Status FAILED `
            -ExitCode 3 `
            -ErrorCode "daily_lock_unavailable"
    }
    Write-Error "Required AlphaOps morning run blocked by daily lock: $($dailyLock.reason)"
    exit $(if ($recordStageFailed -or -not $lockReceiptWritten) { 2 } else { 3 })
}
$backup = Invoke-DawnstrikeNativeProcess `
    -FilePath "py.exe" `
    -ArgumentList @(
        (Join-Path $runtime "scripts\state_disaster_recovery.py"),
        "backup",
        "--source-db", $dbPath,
        "--backup-root", $BackupRoot,
        "--state-root", $state,
        "--retention", "$BackupRetention",
        "--source-sha", $releaseSha
    ) `
    -LogRoot $logRoot `
    -LogName "state_backup-$MarketDate"
if ($backup.exit_code -ne 0) {
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
    throw "Durable state backup failed; no Morning mutation was permitted."
}
$heartbeat = Invoke-DawnstrikeNativeProcess `
    -FilePath "py.exe" `
    -ArgumentList @("-m", "intraday_scanner.cli", "daily-heartbeat", "--state-root", $state, "--runtime-root", $runtime, "--market-date", $MarketDate, "--stage", "morning_collection", "--status", "RUNNING", "--release-sha", $releaseSha) `
    -LogRoot $logRoot `
    -LogName "alpha_morning_heartbeat-$MarketDate"
if ($heartbeat.exit_code -ne 0) {
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
    throw "Could not persist morning heartbeat."
}

Push-Location $runtime
try {
    $calendar = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @("-m", "intraday_scanner.services.market_calendar", "--date", $MarketDate) `
        -LogRoot $logRoot `
        -LogName "alpha_morning_calendar-$MarketDate"
    $calendarExit = $calendar.exit_code
    if ($calendarExit -eq 10) {
        foreach ($stage in @("morning_collection", "ranking_delivery")) {
            Write-MorningStage `
                -Name $stage `
                -Status SKIPPED_NOT_APPLICABLE `
                -ExitCode 0
        }
        exit $(if ($recordStageFailed) { 2 } else { 0 })
    }
    if ($calendarExit -ne 0) {
        throw "Market calendar failed with exit code $calendarExit"
    }

    # Refresh the governed current-session core generation before Morning.
    # Failure is lane-local: the refresh script leaves the active generation
    # untouched, while this run omits the core manifest so AlphaOps records a
    # DATA_UNAVAILABLE core lane/shortfall and can still evaluate movers.
    try {
        $coreRefresh = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @(
                "scripts\refresh_luna_core_universe.py",
                "--state-root", $state,
                "--market-date", $MarketDate
            ) `
            -LogRoot $logRoot `
            -LogName "luna_core_refresh-$MarketDate"
    }
    catch {
        $coreRefresh = [pscustomobject]@{ exit_code = 2 }
    }
    if ($coreRefresh.exit_code -eq 0) {
        $CoreUniverseManifest = $defaultCoreUniverseManifest
    }
    else {
        $CoreUniverseManifest = ""
    }

    $configPath = $sourceConfigPath
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        $stageExit = 2
        $errorCode = "source_config_missing"
    } else {
        $alphaCyclePath = Join-Path $outputRoot "alpha_cycle.json"
        $priorAlphaCyclePath = Move-DawnstrikePriorAlphaCycleArtifact `
            -ArtifactPath $alphaCyclePath `
            -ArchiveRoot (Join-Path $outputRoot "attempt_archive")
        $cycleObservedAt = (Get-Date).ToUniversalTime().ToString("o")
        $alphaArguments = @(
            "-m", "intraday_scanner.cli", "alpha-cycle",
            "--config", $configPath,
            "--db-path", $dbPath,
            "--out-dir", $outputRoot,
            "--notify", $Notify,
            "--market-date", $MarketDate,
            "--as-of", $cycleObservedAt,
            "--code-sha", $releaseSha,
            "--paper-ops-root", $paperOpsRoot
        )
        if ($CoreUniverseManifest) { $alphaArguments += @("--core-universe-manifest", $CoreUniverseManifest) }
        $alphaCycle = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList $alphaArguments `
            -LogRoot $logRoot `
            -LogName "alpha_morning-$MarketDate"
        $stageExit = $alphaCycle.exit_code
        $errorCode = if ($stageExit -eq 0) { "" } else { "alpha_cycle_failed" }
        if ($stageExit -ne 0) {
            # A failed child is allowed to leave a partial/invalid file. Keep
            # that attempt in the archive, then restore the prior canonical
            # artifact so downstream readers never see failed-attempt bytes.
            Restore-DawnstrikePriorAlphaCycleArtifact `
                -ArtifactPath $alphaCyclePath `
                -ArchivePath $priorAlphaCyclePath `
                -QuarantineReplacement | Out-Null
        }
    }
    $coreStageExit = $stageExit
    $coreErrorCode = $errorCode
    $scenarioCandidateCount = 0
    $scenarioSymbols = ""
    $selectionOutcome = ""
    $indeterminateResearchExitCode = $null
    $scenarioExitCode = $null
    if ($coreStageExit -eq 0) {
        try {
            if ($CoreUniverseManifest) {
                $alphaArtifact = Test-DawnstrikeAlphaCycleArtifact `
                    -ArtifactPath $alphaCyclePath `
                    -ProcessReceipt $alphaCycle `
                    -MarketDate $MarketDate `
                    -ReleaseSha $releaseSha `
                    -RequireCoreCoverage
            }
            else {
                # A governed core refresh outage is lane-local.  The run
                # contract must retain DATA_UNAVAILABLE/shortfall truth while
                # the independent mover lane remains publishable.
                $alphaArtifact = Test-DawnstrikeAlphaCycleArtifact `
                    -ArtifactPath $alphaCyclePath `
                    -ProcessReceipt $alphaCycle `
                    -MarketDate $MarketDate `
                    -ReleaseSha $releaseSha `
                    -AllowCoreShortfall
            }
            $scenarioCandidateCount = [int64]$alphaArtifact.research_candidate_count
            $scenarioSymbols = [string]::Join(",", @($alphaArtifact.research_symbols))
            $selectionOutcome = [string]$alphaArtifact.selection_outcome
        }
        catch {
            $coreStageExit = 2
            $coreErrorCode = "alpha_cycle_artifact_invalid"
            # Validation is the promotion gate. If the child wrote malformed
            # or stale bytes, quarantine them and restore the prior artifact.
            Restore-DawnstrikePriorAlphaCycleArtifact `
                -ArtifactPath $alphaCyclePath `
                -ArchivePath $priorAlphaCyclePath `
                -QuarantineReplacement | Out-Null
        }
    }
    if (
        $coreStageExit -eq 0 -and
        $env:DAWNSTRIKE_INDETERMINATE_RESEARCH_ENABLED -match '^(?i:true|1|yes|y)$'
    ) {
        if ($selectionOutcome -ne "data_ineligible" -or $scenarioCandidateCount -le 0) {
            Write-MorningStage `
                -Name "indeterminate_research" `
                -Status "SKIPPED_NOT_APPLICABLE" `
                -ExitCode 0 `
                -NotRequired
        }
        else {
            $indeterminateResearchPath = Join-Path $outputRoot "indeterminate_research.json"
            $indeterminateResearch = Invoke-DawnstrikeNativeProcess `
                -FilePath "py.exe" `
                -ArgumentList @(
                    "-m", "intraday_scanner.cli", "indeterminate-research",
                    "--db-path", $dbPath,
                    "--symbols", $scenarioSymbols,
                    "--selection-outcome", $selectionOutcome,
                    "--market-date", $MarketDate,
                    "--out", $indeterminateResearchPath,
                    "--notify", $Notify
                ) `
                -LogRoot $logRoot `
                -LogName "indeterminate_research-$MarketDate"
            $indeterminateResearchExitCode = [int]$indeterminateResearch.exit_code
            Write-MorningStage `
                -Name "indeterminate_research" `
                -Status $(if ($indeterminateResearch.exit_code -eq 0) { "COMPLETE" } else { "FAILED" }) `
                -ExitCode $indeterminateResearch.exit_code `
                -ErrorCode $(if ($indeterminateResearch.exit_code -eq 0) { "" } else { "indeterminate_research_failed" }) `
                -NotRequired
        }
    }
    elseif ($env:DAWNSTRIKE_INDETERMINATE_RESEARCH_ENABLED -notmatch '^(?i:true|1|yes|y)$') {
        Write-MorningStage `
            -Name "indeterminate_research" `
            -Status "SKIPPED_NOT_APPLICABLE" `
            -ExitCode 0 `
            -NotRequired
    }
    if ($coreStageExit -eq 0) {
        # Persist the exact current-day PIT union used by scheduled PaperOps.
        # The builder verifies the immutable Morning artifacts and fails closed
        # on missing, stale, cross-date, or hash-invalid source evidence.
        $universeHandoffPath = Join-Path $outputRoot "paperops_universe_handoff.json"
        $universeHandoff = Invoke-DawnstrikeNativeProcess `
            -FilePath "py.exe" `
            -ArgumentList @(
                "scripts\build_paperops_universe_handoff.py",
                "--morning-root", $outputRoot,
                "--market-date", $MarketDate,
                "--out", $universeHandoffPath
            ) `
            -LogRoot $logRoot `
            -LogName "paperops_universe_handoff-$MarketDate"
        if ($universeHandoff.exit_code -ne 0) {
            $coreStageExit = 2
            $coreErrorCode = "paperops_universe_handoff_invalid"
        }
    }
    if (
        $coreStageExit -eq 0 -and
        $env:DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED -match '^(?i:true|1|yes|y)$'
    ) {
        if ($scenarioCandidateCount -le 0) {
            Write-MorningStage `
                -Name "scenario_intelligence" `
                -Status "SKIPPED_NOT_APPLICABLE" `
                -ExitCode 0 `
                -NotRequired
        }
        else {
            $scenarioSince = (Get-Date).ToUniversalTime().AddHours(-12).ToString("o")
            $scenarioCycle = Invoke-DawnstrikeNativeProcess `
                -FilePath "py.exe" `
                -ArgumentList @("-m", "intraday_scanner.cli", "scenario-monitor", "--db-path", $dbPath, "--symbols", $scenarioSymbols, "--since", $scenarioSince, "--notify", $Notify) `
                -LogRoot $logRoot `
                -LogName "scenario_morning-$MarketDate"
            if ($scenarioCycle.exit_code -ne 0) {
                $scenarioExitCode = [int]$scenarioCycle.exit_code
            }
            else {
                $scenarioExitCode = 0
            }
            Write-MorningStage `
                -Name "scenario_intelligence" `
                -Status $(if ($scenarioCycle.exit_code -eq 0) { "COMPLETE" } else { "FAILED" }) `
                -ExitCode $scenarioCycle.exit_code `
                -ErrorCode $(if ($scenarioCycle.exit_code -eq 0) { "" } else { "scenario_cycle_failed" }) `
                -NotRequired
        }
    }
    elseif ($env:DAWNSTRIKE_SCENARIO_INTELLIGENCE_ENABLED -notmatch '^(?i:true|1|yes|y)$') {
        Write-MorningStage `
            -Name "scenario_intelligence" `
            -Status "SKIPPED_NOT_APPLICABLE" `
            -ExitCode 0 `
            -NotRequired
    }
    $status = if ($coreStageExit -eq 0) { "COMPLETE" } else { "FAILED" }
    foreach ($stage in @("morning_collection", "ranking_delivery")) {
        Write-MorningStage `
            -Name $stage `
            -Status $status `
            -ExitCode $coreStageExit `
            -ErrorCode $coreErrorCode
    }
    $optionalExitCode = $scenarioExitCode
    if (
        $null -ne $indeterminateResearchExitCode -and
        [int]$indeterminateResearchExitCode -ne 0
    ) {
        $optionalExitCode = [int]$indeterminateResearchExitCode
    }
    $outcome = Resolve-DawnstrikeMorningOutcome `
        -CoreExitCode $coreStageExit `
        -ScenarioExitCode $optionalExitCode `
        -RecordStageFailed $recordStageFailed
    exit $outcome.final_exit_code
}
finally {
    Pop-Location
    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
}
