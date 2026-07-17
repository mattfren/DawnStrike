[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BarsCsv,

    [Parameter(Mandatory = $true)]
    [string]$ContextCsv,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$MarketDate,

    [Parameter(Mandatory = $true)]
    [ValidateSet("forward_observation", "historical_replay")]
    [string]$EvidenceMode,

    [string]$SourceCapturedAt,
    [string]$Cutoffs = "09:45,10:00,12:00,15:00",
    [ValidateRange(1, 30)]
    [int]$BarIntervalMinutes = 5,
    [ValidateRange(1, 100)]
    [int]$MinBaselineSessions = 10,
    [string]$OutputRoot = "data/v2_mover_pattern_lab",
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"
$forward = @{
    BarsCsv = $BarsCsv
    ContextCsv = $ContextCsv
    MarketDate = $MarketDate
    EvidenceMode = $EvidenceMode
    Cutoffs = $Cutoffs
    BarIntervalMinutes = $BarIntervalMinutes
    MinBaselineSessions = $MinBaselineSessions
    OutputRoot = $OutputRoot
    Python = $Python
}
if (-not [string]::IsNullOrWhiteSpace($SourceCapturedAt)) {
    $forward.SourceCapturedAt = $SourceCapturedAt
}

& (Join-Path $PSScriptRoot "mover-pattern-lab/run_scan.ps1") @forward
