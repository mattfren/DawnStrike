[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ExpectedSha,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,
    [Parameter(Mandatory = $true)]
    [string]$StateRoot,
    [switch]$ReplaceCurrent
)

$ErrorActionPreference = "Stop"
$backup = (Resolve-Path $BackupRoot).Path
$runtime = (Resolve-Path $RuntimeRoot).Path.TrimEnd('\')
$state = (Resolve-Path $StateRoot).Path.TrimEnd('\')
$activationScript = Join-Path $runtime "scripts\activate_dawnstrike_runtime.ps1"
if (-not (Test-Path -LiteralPath $activationScript -PathType Leaf)) {
    throw "Runtime activation contract is missing; rollback cannot bind task actions to the exact runtime SHA."
}
$activationScriptContent = Get-Content -Raw -LiteralPath $activationScript
if ($activationScriptContent -notmatch 'function Set-DawnstrikeCanonicalTaskExpectedSha') {
    throw "Runtime activation contract cannot rebind rollback task actions to an externally activated SHA."
}
$activationScriptPath = (Resolve-Path $activationScript).Path
. (Join-Path $runtime "scripts\dawnstrike_process_runner.ps1")
$null = Assert-DawnstrikeProcessSourceBoundToHead -ReleaseRoot $runtime -ExpectedSha $ExpectedSha -EntryScript $activationScriptPath
$backupItem = Get-Item -LiteralPath $backup -Force
if (-not $backupItem.PSIsContainer -or ($backupItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
    throw "Rollback backup root must be a non-reparse directory."
}
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
        throw "Rollback backup is incomplete; missing canonical task XML: $taskName"
    }
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and -not $ReplaceCurrent) {
        throw "Current task exists; rerun with -ReplaceCurrent to restore: $taskName"
    }
    $xml = Get-Content -Raw -LiteralPath $xmlPath
    Register-ScheduledTask -TaskName $taskName -Xml $xml -Force | Out-Null
    $restored += $taskName
}

$null = . $activationScriptPath
foreach ($taskName in $expectedNames) {
    Disable-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop | Out-Null
}
Set-DawnstrikeCanonicalTaskExpectedSha -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
Enable-DawnstrikeCanonicalTasks
$null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
$restored | ForEach-Object { Write-Output "Restored scheduled task: $_" }
