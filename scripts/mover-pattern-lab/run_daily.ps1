[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Scan", "Reconcile", "StudyCandidates")]
    [string]$Stage,

    [Parameter(Mandatory = $true)]
    [string]$BarsCsv,

    [string]$ContextCsv,

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$MarketDate,

    [ValidateSet("forward_observation", "historical_replay")]
    [string]$EvidenceMode,

    [string]$SourceCapturedAt,
    [string]$Cutoffs = "09:45,10:00,12:00,15:00",
    [string]$SignalsPath,
    [string]$ScanManifest,
    [string]$SnapshotsPath,
    [string]$UniverseDenominators,
    [string]$SplitAssignments,
    [string]$DescriptiveEodMovers,

    [ValidateRange(1, 30)]
    [int]$BarIntervalMinutes = 5,

    [ValidateRange(1, 100)]
    [int]$MinBaselineSessions = 10,

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
Set-StrictMode -Version Latest

if ($Stage -eq "Scan") {
    if ([string]::IsNullOrWhiteSpace($ContextCsv)) {
        throw "Stage Scan requires ContextCsv."
    }
    if ([string]::IsNullOrWhiteSpace($MarketDate)) {
        throw "Stage Scan requires MarketDate."
    }
    if ([string]::IsNullOrWhiteSpace($EvidenceMode)) {
        throw "Stage Scan requires an explicit EvidenceMode."
    }

    $scanParameters = @{
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
        $scanParameters.SourceCapturedAt = $SourceCapturedAt
    }

    & (Join-Path $PSScriptRoot "run_scan.ps1") @scanParameters
    return
}

if ($Stage -eq "StudyCandidates") {
    if ([string]::IsNullOrWhiteSpace($SnapshotsPath)) {
        throw "Stage StudyCandidates requires SnapshotsPath returned by Stage Scan."
    }
    if ([string]::IsNullOrWhiteSpace($UniverseDenominators)) {
        throw "Stage StudyCandidates requires UniverseDenominators."
    }
    if ([string]::IsNullOrWhiteSpace($SplitAssignments)) {
        throw "Stage StudyCandidates requires immutable SplitAssignments."
    }

    $studyParameters = @{
        SnapshotsPath = $SnapshotsPath
        BarsCsv = $BarsCsv
        UniverseDenominators = $UniverseDenominators
        SplitAssignments = $SplitAssignments
        BarIntervalMinutes = $BarIntervalMinutes
        SlippageBps = $SlippageBps
        FeeBps = $FeeBps
        OutputRoot = $OutputRoot
        Python = $Python
    }
    if (-not [string]::IsNullOrWhiteSpace($DescriptiveEodMovers)) {
        $studyParameters.DescriptiveEodMovers = $DescriptiveEodMovers
    }

    & (Join-Path $PSScriptRoot "run_candidate_study.ps1") @studyParameters
    return
}

if ([string]::IsNullOrWhiteSpace($SignalsPath)) {
    throw "Stage Reconcile requires SignalsPath returned by Stage Scan."
}
if ([string]::IsNullOrWhiteSpace($ScanManifest)) {
    throw "Stage Reconcile requires the immutable ScanManifest returned by Stage Scan."
}

& (Join-Path $PSScriptRoot "run_reconcile.ps1") `
    -SignalsPath $SignalsPath `
    -ScanManifest $ScanManifest `
    -BarsCsv $BarsCsv `
    -BarIntervalMinutes $BarIntervalMinutes `
    -NotionalPerTrade $NotionalPerTrade `
    -SlippageBps $SlippageBps `
    -FeeBps $FeeBps `
    -OutputRoot $OutputRoot `
    -Python $Python
