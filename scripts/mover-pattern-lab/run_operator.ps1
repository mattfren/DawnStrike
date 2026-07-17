[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Scan", "Reconcile")]
    [string]$Stage,

    [Parameter(Mandatory = $true)]
    [string]$Config,

    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$MarketDate,

    [ValidatePattern('^\d{2}:\d{2}$')]
    [string]$Cutoff,

    [ValidateSet("telegram", "console")]
    [string]$Notify,

    [string]$Python = "py"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
    throw "Mover daily workflow config does not exist: $Config"
}
if ($Stage -eq "Scan" -and [string]::IsNullOrWhiteSpace($Cutoff)) {
    throw "Stage Scan requires Cutoff."
}
if ($Stage -eq "Reconcile" -and -not [string]::IsNullOrWhiteSpace($Cutoff)) {
    throw "Stage Reconcile does not accept Cutoff."
}

$arguments = @(
    "-m", "intraday_scanner.mover_pattern_operator_cli",
    "--config", (Resolve-Path -LiteralPath $Config).Path,
    "--stage", $Stage.ToLowerInvariant()
)
if (-not [string]::IsNullOrWhiteSpace($MarketDate)) {
    $arguments += @("--date", $MarketDate)
}
if (-not [string]::IsNullOrWhiteSpace($Cutoff)) {
    $arguments += @("--cutoff", $Cutoff)
}
if (-not [string]::IsNullOrWhiteSpace($Notify)) {
    $arguments += @("--notify", $Notify)
}

& $Python @arguments
exit $LASTEXITCODE
