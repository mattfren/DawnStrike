param(
    [string]$Python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$Repo = (Split-Path -Parent $PSScriptRoot),
    [string]$Snapshot = "sample_data\premarket_snapshot_sample.csv",
    [string]$MinuteBars = "sample_data\minute_bars\2026-06-18.csv",
    [string]$DbPath = "data\scanner.sqlite",
    [string]$ScanOut = "outputs\latest_scan",
    [string]$AuditOut = "outputs\latest_audit",
    [string]$MonitorOut = "outputs\latest_monitor"
)

function Invoke-Dawnstrike {
    param([string[]]$CommandArgs)
    & $Python @CommandArgs
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}

Set-Location -LiteralPath $Repo

Invoke-Dawnstrike @("-m", "intraday_scanner.cli", "init-db", "--db-path", $DbPath)
Invoke-Dawnstrike @(
    "-m", "intraday_scanner.cli", "scan",
    "--snapshot", $Snapshot,
    "--out-dir", $ScanOut,
    "--db-path", $DbPath,
    "--persist",
    "--print"
)
Invoke-Dawnstrike @(
    "-m", "intraday_scanner.cli", "audit-latest",
    "--db-path", $DbPath,
    "--minute-bars", $MinuteBars,
    "--out-dir", $AuditOut,
    "--persist"
)
Invoke-Dawnstrike @(
    "-m", "intraday_scanner.cli", "monitor-setups",
    "--snapshot", $Snapshot,
    "--db-path", $DbPath,
    "--out-dir", $MonitorOut,
    "--persist"
)
