[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SignalsPath,

    [Parameter(Mandatory = $true)]
    [string]$BarsCsv,

    [Parameter(Mandatory = $true)]
    [string]$ScanManifest,

    [ValidateRange(1, 30)]
    [int]$BarIntervalMinutes = 5,
    [ValidateRange(0.01, 1000000000)]
    [double]$NotionalPerTrade = 1000.0,
    [ValidateRange(0, 10000)]
    [double]$SlippageBps = 10.0,
    [ValidateRange(0, 10000)]
    [double]$FeeBps = 1.0,
    [string]$OutputRoot = "data/v2_mover_pattern_lab",
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"

& (Join-Path $PSScriptRoot "mover-pattern-lab/run_reconcile.ps1") `
    -SignalsPath $SignalsPath `
    -ScanManifest $ScanManifest `
    -BarsCsv $BarsCsv `
    -BarIntervalMinutes $BarIntervalMinutes `
    -NotionalPerTrade $NotionalPerTrade `
    -SlippageBps $SlippageBps `
    -FeeBps $FeeBps `
    -OutputRoot $OutputRoot `
    -Python $Python
