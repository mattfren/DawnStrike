[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SnapshotsPath,

    [Parameter(Mandatory = $true)]
    [string]$BarsCsv,

    [Parameter(Mandatory = $true)]
    [string]$UniverseDenominators,

    [Parameter(Mandatory = $true)]
    [string]$SplitAssignments,

    [string]$DescriptiveEodMovers,

    [ValidateRange(1, 30)]
    [int]$BarIntervalMinutes = 5,

    [ValidateRange(0, 10000)]
    [double]$SlippageBps = 10.0,

    [ValidateRange(0, 10000)]
    [double]$FeeBps = 1.0,

    [string]$OutputRoot = "data/v2_mover_pattern_lab",
    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "_common.ps1")

Assert-MoverLabInputFile -Path $SnapshotsPath -Label "Retained snapshots ledger"
Assert-MoverLabInputFile -Path $BarsCsv -Label "Candidate outcome bars CSV"
Assert-MoverLabInputFile -Path $UniverseDenominators -Label "Universe denominators"
Assert-MoverLabInputFile -Path $SplitAssignments -Label "Frozen split assignments"
if (-not [string]::IsNullOrWhiteSpace($DescriptiveEodMovers)) {
    Assert-MoverLabInputFile `
        -Path $DescriptiveEodMovers `
        -Label "Descriptive EOD movers"
}

$arguments = @(
    "-m", "intraday_scanner.v2.mover_pattern_lab", "study-candidates",
    "--snapshots", $SnapshotsPath,
    "--bars-csv", $BarsCsv,
    "--universe-denominators", $UniverseDenominators,
    "--split-assignments", $SplitAssignments,
    "--bar-interval-minutes", $BarIntervalMinutes.ToString(),
    "--slippage-bps", $SlippageBps.ToString(
        [Globalization.CultureInfo]::InvariantCulture
    ),
    "--fee-bps", $FeeBps.ToString(
        [Globalization.CultureInfo]::InvariantCulture
    ),
    "--bar-timestamp-semantics", "bar_close",
    "--output-root", $OutputRoot
)
if (-not [string]::IsNullOrWhiteSpace($DescriptiveEodMovers)) {
    $arguments += @("--descriptive-eod-movers", $DescriptiveEodMovers)
}

$study = Invoke-MoverLabJson `
    -Python $Python `
    -Arguments $arguments `
    -Operation "All-candidate mover study"

if (-not $study.run_manifest_path) {
    throw "Candidate study did not return an immutable run manifest."
}

[ordered]@{
    status = $study.status
    study_id = $study.study_id
    evidence_mode = $study.evidence_mode
    snapshot_count = $study.snapshot_count
    complete_outcome_count = $study.complete_outcome_count
    pending_outcome_count = $study.pending_outcome_count
    all_candidate_coverage_complete = $study.all_candidate_coverage_complete
    general_mover_research_data_complete = $study.general_mover_research_data_complete
    forward_learning_eligible = $study.forward_learning_eligible
    study_path = $study.study_path
    outcomes_path = $study.outcomes_path
    coverage_path = $study.coverage_path
    candidate_study_manifest_path = $study.run_manifest_path
    automatic_strategy_creation = $false
    automatic_promotion_enabled = $false
    research_only = $true
    broker_execution_enabled = $false
} | ConvertTo-Json -Depth 10
