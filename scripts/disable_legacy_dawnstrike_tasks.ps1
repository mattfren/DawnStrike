[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = ""
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
if (-not $BackupRoot) {
    $BackupRoot = Join-Path $state (
        "scheduler-backups\legacy-disable-" +
        (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    )
}
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null

$legacyTaskNames = @(
    "Dawnstrike AlphaOps EOD Report",
    "Dawnstrike Daily Scan",
    "Dawnstrike Setup Monitor 5m",
    "Dawnstrike Web Telegram AutoPilot",
    "Dawnstrike X3 Vercel Daily Publish"
)

foreach ($taskName in $legacyTaskNames) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Output "Legacy task not present: $taskName"
        continue
    }
    $safeName = $taskName -replace "[^A-Za-z0-9._-]", "_"
    Export-ScheduledTask -TaskName $taskName |
        Set-Content -LiteralPath (Join-Path $BackupRoot "$safeName.xml") -Encoding Unicode
    if ($PSCmdlet.ShouldProcess($taskName, "Disable legacy Dawnstrike scheduled task")) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        Write-Output "Disabled legacy task: $taskName"
    }
}

Write-Output "Rollback XML saved under $BackupRoot. No task was deleted."
