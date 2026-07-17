[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SignalsPath,

    [Parameter(Mandatory = $true)]
    [string]$ScanManifest,

    [Parameter(Mandatory = $true)]
    [string]$BarsCsv,

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
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot "_common.ps1")

Assert-MoverLabInputFile -Path $SignalsPath -Label "Signals ledger"
Assert-MoverLabInputFile -Path $ScanManifest -Label "Paper-scan run manifest"
Assert-MoverLabInputFile -Path $BarsCsv -Label "Reconciliation bars CSV"

$reconcile = Invoke-MoverLabJson `
    -Python $Python `
    -Arguments @(
        "-m", "intraday_scanner.v2.mover_pattern_lab", "reconcile",
        "--signals", $SignalsPath,
        "--bars-csv", $BarsCsv,
        "--bar-interval-minutes", $BarIntervalMinutes.ToString(),
        "--bar-timestamp-semantics", "bar_close",
        "--notional-per-trade", $NotionalPerTrade.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        ),
        "--slippage-bps", $SlippageBps.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        ),
        "--fee-bps", $FeeBps.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        ),
        "--output-root", $OutputRoot
    ) `
    -Operation "Mover paper reconciliation"

if (-not $reconcile.run_manifest_path) {
    throw "Mover reconciliation did not return an immutable run manifest."
}

$analysis = Invoke-MoverLabJson `
    -Python $Python `
    -Arguments @(
        "-m", "intraday_scanner.v2.mover_pattern_lab", "analyze",
        "--scan-manifest", $ScanManifest,
        "--reconcile-manifest", $reconcile.run_manifest_path,
        "--output-root", $OutputRoot
    ) `
    -Operation "Mover strategy analysis"

$verification = Invoke-MoverLabJson `
    -Python $Python `
    -Arguments @(
        "-m", "intraday_scanner.v2.mover_pattern_lab", "verify",
        "--output-root", $OutputRoot
    ) `
    -Operation "Mover evidence verification"

[ordered]@{
    status = $reconcile.status
    signal_count = $reconcile.signal_count
    closed_trade_count = $reconcile.closed_trade_count
    pending_trade_count = $reconcile.pending_trade_count
    not_entered_count = $reconcile.not_entered_count
    trades_path = $reconcile.trades_path
    scan_manifest_path = $ScanManifest
    reconcile_manifest_path = $reconcile.run_manifest_path
    analysis_path = $analysis.report_path
    analysis_markdown_path = $analysis.markdown_path
    calendar_path = $analysis.strategy_daily_calendar_path
    calendar_csv_path = $analysis.strategy_daily_calendar_csv_path
    calendar_html_path = $analysis.strategy_daily_calendar_html_path
    evidence_status = $verification.evidence_status
    verification_status = $verification.status
    pending_return_semantics = "null_not_zero"
    research_only = $true
    broker_execution_enabled = $false
} | ConvertTo-Json -Depth 10
