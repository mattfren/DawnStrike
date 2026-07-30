[CmdletBinding()]
param(
    [string]$TaskName = "Dawnstrike X3 Vercel Daily Publish"
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "Cannot restore missing scheduled task: $TaskName"
}
Enable-ScheduledTask -TaskName $TaskName | Out-Null
Write-Output "Restored scheduled task: $TaskName"
