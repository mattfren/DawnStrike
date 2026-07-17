param([switch]$Yes)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $Yes) {
    throw 'Pass -Yes to remove only Dawnstrike-owned OMEGA tasks.'
}

$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $RepoRoot
$TaskPath = '\Dawnstrike\'
foreach ($TaskName in @('OMEGA After Close', 'OMEGA Morning Check', 'OMEGA Verify', 'OMEGA Watchdog')) {
    $Task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $Task) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
    }
}

py -m intraday_scanner.v2.autonomous_runner status
$ExitCode = $LASTEXITCODE
exit $ExitCode
