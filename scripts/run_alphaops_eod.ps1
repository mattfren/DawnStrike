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
Import-DawnstrikeEnvironment -StateRoot $state
$dbPath = Join-Path $state "shadow_real.sqlite"
$paperOpsRoot = Join-Path $state "v2_paper_ops_live"
$outputRoot = Join-Path $state "outputs"
$logRoot = Join-Path $state "logs"
New-Item -ItemType Directory -Path $paperOpsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$overallExit = 0

function Write-Stage {
    param(
        [string]$Name,
        [string]$Status,
        [int]$ExitCode,
        [string]$StartedAt,
        [string]$ResultFile = "",
        [string]$OutputFile = "",
        [string]$ErrorCode = ""
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
    & py.exe @arguments
    if ($LASTEXITCODE -ne 0) {
        $script:overallExit = 2
    }
}

Push-Location $runtime
try {
    & py.exe -m intraday_scanner.services.market_calendar --date $MarketDate
    $calendarExit = $LASTEXITCODE
    if ($calendarExit -eq 10) {
        foreach ($stage in @(
            "eod_outcome_capture",
            "paper_reconciliation",
            "alpha_learning",
            "paperops_forward"
        )) {
            Write-Stage `
                -Name $stage `
                -Status SKIPPED_NOT_APPLICABLE `
                -ExitCode 0 `
                -StartedAt ((Get-Date).ToUniversalTime().ToString("o"))
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
    & py.exe -m intraday_scanner.cli alpha-capture-outcomes `
        --db-path $dbPath `
        --market-date $MarketDate `
        --out-dir $captureRoot `
        --persist 2>&1 |
        Tee-Object -FilePath (Join-Path $logRoot "alpha_outcomes-$MarketDate.log")
    $captureExit = $LASTEXITCODE
    if ($captureExit -eq 0) {
        Write-Stage `
            -Name eod_outcome_capture `
            -Status COMPLETE `
            -ExitCode 0 `
            -StartedAt $captureStarted `
            -ResultFile $captureResult `
            -OutputFile $captureResult
    } else {
        $overallExit = $captureExit
        Write-Stage `
            -Name eod_outcome_capture `
            -Status TERMINAL_MISSING `
            -ExitCode $captureExit `
            -StartedAt $captureStarted `
            -ResultFile $captureResult `
            -OutputFile $captureResult `
            -ErrorCode outcome_capture_incomplete
    }

    $reconcileRoot = Join-Path $outputRoot "strategy_reconciliation\$MarketDate"
    New-Item -ItemType Directory -Path $reconcileRoot -Force | Out-Null
    $reconcileResult = Join-Path $reconcileRoot "reconciliation.json"
    $reconcileStarted = (Get-Date).ToUniversalTime().ToString("o")
    if ($captureExit -eq 0) {
        & py.exe -m intraday_scanner.cli alpha-paper-reconcile `
            --db-path $dbPath `
            --market-date $MarketDate `
            --out-dir $reconcileRoot `
            --persist 2>&1 |
            Tee-Object -FilePath (Join-Path $logRoot "alpha_reconcile-$MarketDate.log")
        $reconcileExit = $LASTEXITCODE
    } else {
        $reconcileExit = 2
    }
    if ($reconcileExit -eq 0) {
        Write-Stage `
            -Name paper_reconciliation `
            -Status COMPLETE `
            -ExitCode 0 `
            -StartedAt $reconcileStarted `
            -ResultFile $reconcileResult `
            -OutputFile $reconcileResult
    } else {
        $overallExit = $reconcileExit
        Write-Stage `
            -Name paper_reconciliation `
            -Status FAILED `
            -ExitCode $reconcileExit `
            -StartedAt $reconcileStarted `
            -ResultFile $reconcileResult `
            -ErrorCode paper_reconciliation_failed
    }

    $learnStarted = (Get-Date).ToUniversalTime().ToString("o")
    if ($reconcileExit -eq 0) {
        & py.exe -m intraday_scanner.cli alpha-learn --db-path $dbPath 2>&1 |
            Tee-Object -FilePath (Join-Path $logRoot "alpha_learning-$MarketDate.log")
        $learnExit = $LASTEXITCODE
    } else {
        $learnExit = 2
    }
    if ($learnExit -eq 0) {
        Write-Stage -Name alpha_learning -Status COMPLETE -ExitCode 0 -StartedAt $learnStarted
    } else {
        $overallExit = $learnExit
        Write-Stage `
            -Name alpha_learning `
            -Status FAILED `
            -ExitCode $learnExit `
            -StartedAt $learnStarted `
            -ErrorCode alpha_learning_failed
    }

    & py.exe -m intraday_scanner.cli alpha-attribution `
        --db-path $dbPath `
        --out-dir (Join-Path $outputRoot "alpha_attribution") `
        --end $MarketDate
    if ($LASTEXITCODE -ne 0) {
        $overallExit = $LASTEXITCODE
    }
    & py.exe -m intraday_scanner.cli outcome-gap `
        --db-path $dbPath `
        --market-date $MarketDate `
        --out (Join-Path $captureRoot "outcome-gap.json")
    if ($LASTEXITCODE -ne 0) {
        $overallExit = $LASTEXITCODE
    }

    $paperStarted = (Get-Date).ToUniversalTime().ToString("o")
    & py.exe -m intraday_scanner.v2.paper_ops init --output-root $paperOpsRoot
    $paperExit = $LASTEXITCODE
    if ($paperExit -eq 0) {
        for ($attempt = 1; $attempt -le $PaperOpsRetryLimit; $attempt++) {
            & py.exe -m intraday_scanner.v2.paper_ops run-day `
                --date $MarketDate `
                --mode forward `
                --output-root $paperOpsRoot 2>&1 |
                Tee-Object -FilePath (Join-Path $logRoot "paperops-$MarketDate.log") -Append
            $paperExit = $LASTEXITCODE
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
            & py.exe -m intraday_scanner.v2.paper_ops $command `
                --output-root $paperOpsRoot @extra
            if ($LASTEXITCODE -ne 0) {
                $paperExit = $LASTEXITCODE
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
        $overallExit = $paperExit
        Write-Stage `
            -Name paperops_forward `
            -Status FAILED `
            -ExitCode $paperExit `
            -StartedAt $paperStarted `
            -ErrorCode paperops_forward_truth_failed
    }
    if ($overallExit -ne 0) {
        & py.exe scripts\send_stage_failure_notification.py `
            --db-path $dbPath `
            --market-date $MarketDate
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Required-stage failure alert could not be recorded or sent."
        }
    }
    exit $overallExit
}
finally {
    Pop-Location
}
