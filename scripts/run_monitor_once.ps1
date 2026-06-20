param(
    [string]$Python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    [string]$Repo = (Split-Path -Parent $PSScriptRoot),
    [string]$Snapshot = "sample_data\premarket_snapshot_sample.csv",
    [string]$DbPath = "data\scanner.sqlite",
    [string]$MonitorOut = "outputs\latest_monitor",
    [int]$TopN = 10,
    [string]$Symbols = ""
)

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python runtime not found: $Python"
}

Set-Location -LiteralPath $Repo

$argsList = @(
    "-m", "intraday_scanner.cli", "monitor-setups",
    "--snapshot", $Snapshot,
    "--db-path", $DbPath,
    "--out-dir", $MonitorOut,
    "--persist",
    "--top-n", "$TopN"
)

if ($Symbols.Trim().Length -gt 0) {
    $argsList += @("--symbols", $Symbols)
}

& $Python @argsList
exit $LASTEXITCODE
