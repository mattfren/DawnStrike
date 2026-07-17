Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-OmegaSchedulerSafeText {
    param([AllowNull()][object]$Value)

    $Text = [string]$Value
    foreach ($Pattern in @(
        '(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+',
        '(?i)(authorization:\s*bearer\s+)[^\s,;]+',
        '(?i)(password\s*[:=]\s*)[^\s,;]+',
        '(?i)(secret\s*[:=]\s*)[^\s,;]+',
        '(?i)(token\s*[:=]\s*)[^\s,;]+'
    )) {
        $Text = [regex]::Replace($Text, $Pattern, '${1}<redacted>')
    }
    return $Text
}

function Write-OmegaSchedulerStatus {
    param(
        [string]$CommandName,
        [string]$RunDate,
        [string]$StartedAt,
        [string]$CompletedAt,
        [int]$ExitCode,
        [string]$RelativeLogPath,
        [object[]]$StepResults,
        [string]$RepoRoot,
        [string]$StatusOverride = ''
    )

    $StatusDirectory = Join-Path $RepoRoot 'data/v2_scheduler/status'
    New-Item -ItemType Directory -Force -Path $StatusDirectory | Out-Null
    $Status = if ($StatusOverride) { $StatusOverride } elseif ($ExitCode -eq 0) { 'passed' } else { 'failed' }
    $MarketMastersEnabled = [bool]($StepResults | Where-Object { [string]$_.command -like '*--market-masters*' })
    $LatestMarketMastersStatus = 'missing'
    $LatestMarketMastersBuildId = 'n/a'
    $LatestMarketMastersReport = Join-Path $RepoRoot 'data/v2_market_masters/reports/report_latest.json'
    if (Test-Path -LiteralPath $LatestMarketMastersReport) {
        try {
            $MarketMastersPayload = Get-Content -Raw -LiteralPath $LatestMarketMastersReport | ConvertFrom-Json
            if ($null -ne $MarketMastersPayload.final_status) {
                $LatestMarketMastersStatus = [string]$MarketMastersPayload.final_status
            }
            if ($null -ne $MarketMastersPayload.build_id) {
                $LatestMarketMastersBuildId = [string]$MarketMastersPayload.build_id
            }
        }
        catch {
            $LatestMarketMastersStatus = 'unreadable'
        }
    }
    $Payload = [ordered]@{
        schema_version = 'v2.scheduler_status.v1'
        status = $Status
        command_name = $CommandName
        run_date = $RunDate
        started_at = $StartedAt
        completed_at = $CompletedAt
        exit_code = $ExitCode
        log_path = $RelativeLogPath
        repo_root = '.'
        browser_opened = $false
        scheduled_task_installed = $false
        live_trading_enabled = $false
        market_masters_enabled = $MarketMastersEnabled
        latest_market_masters_status = $LatestMarketMastersStatus
        latest_market_masters_build_id = $LatestMarketMastersBuildId
        steps = $StepResults
    }
    $Utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText(
        (Join-Path $StatusDirectory 'latest_status.json'),
        ($Payload | ConvertTo-Json -Depth 8),
        $Utf8NoBom
    )
    $Lines = @(
        '# OMEGA Scheduler Status',
        '',
        "- Status: ``$Status``",
        "- Command: ``$CommandName``",
        "- Run date: ``$RunDate``",
        "- Exit code: ``$ExitCode``",
        "- Log: ``$RelativeLogPath``",
        '- Scheduled task installed: `false`',
        '- Browser opened: `false`',
        '- Live trading enabled: `false`',
        "- Market Masters enabled: ``$MarketMastersEnabled``",
        "- Latest Market Masters status: ``$LatestMarketMastersStatus``",
        "- Latest Market Masters build ID: ``$LatestMarketMastersBuildId``",
        '',
        '## Steps',
        ''
    )
    foreach ($Step in $StepResults) {
        $Lines += "- ``$($Step.name)`` exit ``$($Step.exit_code)`` status ``$($Step.status)``"
    }
    [System.IO.File]::WriteAllLines(
        (Join-Path $StatusDirectory 'latest_status.md'),
        [string[]]$Lines,
        $Utf8NoBom
    )
}

function Invoke-OmegaSchedulerRun {
    param(
        [string]$CommandName,
        [string]$RunDate,
        [string]$ScriptRoot,
        [object[]]$Steps
    )

    $RepoRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptRoot '..')).Path
    Set-Location -LiteralPath $RepoRoot
    $LogDirectory = Join-Path $RepoRoot 'data/v2_scheduler/logs'
    New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
    $Timestamp = Get-Date -Format 'yyyyMMddTHHmmss'
    $LogFileName = "$CommandName`_$Timestamp.log"
    $LogPath = Join-Path $LogDirectory $LogFileName
    $RelativeLogPath = "data/v2_scheduler/logs/$LogFileName"
    $StartedAt = (Get-Date).ToUniversalTime().ToString('o')
    $ExitCode = 0
    $StatusOverride = ''
    $StepResults = @()
    "[$StartedAt] starting $CommandName for $RunDate" | Set-Content -LiteralPath $LogPath -Encoding UTF8

    foreach ($Step in $Steps) {
        $StepName = [string]$Step.Name
        $Arguments = [string[]]$Step.Arguments
        $CommandLine = 'py ' + [string]::Join(' ', $Arguments)
        $PreviousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $Output = & py @Arguments 2>&1
            $StepExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($null -eq $StepExitCode) { $StepExitCode = 0 }
        foreach ($Line in $Output) {
            $SafeLine = ConvertTo-OmegaSchedulerSafeText $Line
            $SafeLine | Add-Content -LiteralPath $LogPath -Encoding UTF8
            Write-Host $SafeLine
        }
        $KnownMarketClosure = $StepName -eq 'market-session' -and $StepExitCode -eq 10
        $StepStatus = if ($KnownMarketClosure) { 'skipped_market_closed' } elseif ($StepExitCode -eq 0) { 'passed' } else { 'failed' }
        $StepResults += [ordered]@{
            name = $StepName
            command = ConvertTo-OmegaSchedulerSafeText $CommandLine
            exit_code = $StepExitCode
            status = $StepStatus
        }
        if ($KnownMarketClosure) {
            $StatusOverride = 'skipped_market_closed'
            break
        }
        if ($StepExitCode -ne 0) {
            $ExitCode = $StepExitCode
            break
        }
    }

    $CompletedAt = (Get-Date).ToUniversalTime().ToString('o')
    Write-OmegaSchedulerStatus `
        -CommandName $CommandName `
        -RunDate $RunDate `
        -StartedAt $StartedAt `
        -CompletedAt $CompletedAt `
        -ExitCode $ExitCode `
        -RelativeLogPath $RelativeLogPath `
        -StepResults $StepResults `
        -RepoRoot $RepoRoot `
        -StatusOverride $StatusOverride
    return $ExitCode
}
