[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,
    [switch]$ReplaceCurrent
)

$ErrorActionPreference = "Stop"
$backup = (Resolve-Path $BackupRoot).Path
$expectedNames = @(
    "Dawnstrike AlphaOps Morning",
    "Dawnstrike AlphaOps Monitor 5m",
    "Dawnstrike AlphaOps EOD Full Report",
    "Dawnstrike AlphaOps V6 Weekly Training",
    "Dawnstrike 10of10 Daily Finalize"
)
$restored = @()

foreach ($taskName in $expectedNames) {
    $safeName = $taskName -replace "[^A-Za-z0-9._-]", "_"
    $xmlPath = Join-Path $backup "$safeName.xml"
    if (-not (Test-Path -LiteralPath $xmlPath -PathType Leaf)) {
        continue
    }
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and -not $ReplaceCurrent) {
        throw "Current task exists; rerun with -ReplaceCurrent to restore: $taskName"
    }
    $xml = Get-Content -Raw -LiteralPath $xmlPath
    Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null
    $restored += $taskName
}

if (-not $restored.Count) {
    throw "No Dawnstrike task XML files were found under $backup."
}
$restored | ForEach-Object { Write-Output "Restored scheduled task: $_" }
