[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$TaskName = "Dawnstrike 10of10 Daily Finalize",
    [datetime]$StartTime = (Get-Date -Hour 17 -Minute 30 -Second 0)
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$runner = Join-Path $resolvedRoot "scripts\run_daily_finalize.ps1"

if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Daily finalize runner not found: $runner"
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "Scheduled task already exists; inspect it before changing it: $TaskName"
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$resolvedRoot`""
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Dawnstrike canonical performance reconcile, public snapshot, readiness, and stage manifest. Research-only; no broker execution."

Write-Output "Registered $TaskName for $($StartTime.ToString('HH:mm')) local time."
