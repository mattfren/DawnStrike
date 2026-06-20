param(
    [string]$Python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$Repo = (Split-Path -Parent $PSScriptRoot),
    [string]$Snapshot = "sample_data\premarket_snapshot_sample.csv",
    [string]$DbPath = "data\scanner.sqlite",
    [string]$ScanOut = "outputs\latest_scan",
    [string]$MonitorOut = "outputs\latest_monitor",
    [string]$TaskPrefix = "Dawnstrike",
    [string]$DailyScanTime = "08:20"
)

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}

$scanArgs = @(
    "-m", "intraday_scanner.cli", "scan",
    "--snapshot", $Snapshot,
    "--out-dir", $ScanOut,
    "--db-path", $DbPath,
    "--persist",
    "--print"
)

$monitorArgs = @(
    "-m", "intraday_scanner.cli", "monitor-setups",
    "--snapshot", $Snapshot,
    "--db-path", $DbPath,
    "--out-dir", $MonitorOut,
    "--persist"
)

$scanAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ($scanArgs -join " ") `
    -WorkingDirectory $Repo
$scanTrigger = New-ScheduledTaskTrigger -Daily -At $DailyScanTime
Register-ScheduledTask `
    -TaskName "$TaskPrefix Daily Scan" `
    -Action $scanAction `
    -Trigger $scanTrigger `
    -Description "Run Dawnstrike persisted scan from the configured snapshot." `
    -Force | Out-Null

$monitorAction = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument ($monitorArgs -join " ") `
    -WorkingDirectory $Repo
$monitorTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(8).AddMinutes(30) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Hours 8)
Register-ScheduledTask `
    -TaskName "$TaskPrefix Setup Monitor 5m" `
    -Action $monitorAction `
    -Trigger $monitorTrigger `
    -Description "Re-check Dawnstrike ranked setups every 5 minutes during the trading session." `
    -Force | Out-Null

Write-Host "Registered scheduled tasks:"
Write-Host " - $TaskPrefix Daily Scan"
Write-Host " - $TaskPrefix Setup Monitor 5m"
