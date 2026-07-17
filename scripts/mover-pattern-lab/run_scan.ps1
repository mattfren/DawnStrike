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
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "_common.ps1")

Assert-MoverLabInputFile -Path $BarsCsv -Label "Bars CSV"
Assert-MoverLabInputFile -Path $ContextCsv -Label "Context CSV"

$cutoffValues = @(
    $Cutoffs.Split(',') |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }
)
if ($cutoffValues.Count -eq 0) {
    throw "At least one cutoff is required."
}

$capture = $null
if ($EvidenceMode -eq "forward_observation") {
    if ($cutoffValues.Count -ne 1) {
        throw "A forward scan requires exactly one cutoff per source capture."
    }
    if (-not [string]::IsNullOrWhiteSpace($SourceCapturedAt)) {
        $capture = ConvertTo-MoverLabAwareTimestamp -Value $SourceCapturedAt
    }
}
elseif (-not [string]::IsNullOrWhiteSpace($SourceCapturedAt)) {
    throw "SourceCapturedAt must be omitted for historical_replay to keep modes unambiguous."
}

$buildArguments = @(
    "-m", "intraday_scanner.v2.mover_pattern_lab", "build-snapshots",
    "--bars-csv", $BarsCsv,
    "--context-csv", $ContextCsv,
    "--date", $MarketDate,
    "--cutoffs", ($cutoffValues -join ','),
    "--bar-interval-minutes", $BarIntervalMinutes.ToString(),
    "--min-baseline-sessions", $MinBaselineSessions.ToString(),
    "--bar-timestamp-semantics", "bar_close",
    "--evidence-mode", $EvidenceMode,
    "--output-root", $OutputRoot
)
if ($capture) {
    $buildArguments += @("--source-captured-at", $capture)
}

$build = Invoke-MoverLabJson `
    -Python $Python `
    -Arguments $buildArguments `
    -Operation "Mover snapshot build" `
    -AllowedExitCodes @(0, 2)

if (-not $build.snapshot_path -or -not (Test-Path -LiteralPath $build.snapshot_path)) {
    throw "Mover snapshot build did not return a retained snapshot ledger."
}

$scan = Invoke-MoverLabJson `
    -Python $Python `
    -Arguments @(
        "-m", "intraday_scanner.v2.mover_pattern_lab", "paper-scan",
        "--snapshots", $build.snapshot_path,
        "--expected-market-dates", $MarketDate,
        "--output-root", $OutputRoot
    ) `
    -Operation "Mover paper scan"

if (-not $scan.run_manifest_path) {
    throw "Mover paper scan did not return an immutable run manifest."
}

[ordered]@{
    status = if ($build.snapshot_count -gt 0) { "passed" } else { "blocked" }
    snapshot_build_status = $build.status
    market_date = $MarketDate
    evidence_mode = $EvidenceMode
    declared_source_captured_at = $capture
    authoritative_source_captured_at = $build.source_captured_at
    system_received_at = $build.system_received_at
    forward_receipt_ref = $build.forward_receipt_ref
    forward_receipt_path = $build.forward_receipt_path
    snapshot_count = $build.snapshot_count
    rejected_snapshot_count = $build.rejected_count
    decision_count = $scan.decision_count
    signal_count = $scan.signal_count
    not_evaluated_market_dates = $scan.not_evaluated_market_dates
    snapshots_path = $build.snapshot_path
    rejected_path = $build.rejected_path
    decisions_path = $scan.decisions_path
    signals_path = $scan.signals_path
    scan_manifest_path = $scan.run_manifest_path
    snapshot_build_latest_pointer = (
        Join-Path $OutputRoot "manifests/snapshot_build_latest.json"
    )
    research_only = $true
    broker_execution_enabled = $false
} | ConvertTo-Json -Depth 10
