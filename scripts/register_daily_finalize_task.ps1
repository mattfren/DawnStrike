[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$SourceRoot = "C:\Users\MattFields\Dawnstrike",
    [string]$TaskName = "Dawnstrike 10of10 Daily Finalize",
    [datetime]$StartTime = (Get-Date -Hour 17 -Minute 30 -Second 0),
    [ValidateSet("LocalOnly", "Preview", "Production")]
    [string]$PublicationMode = "LocalOnly",
    [string]$VercelProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [switch]$AllowDegraded,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$resolvedSourceRoot = (Resolve-Path $SourceRoot).Path
$runner = Join-Path $resolvedRoot "scripts\run_daily_finalize.ps1"

if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Daily finalize runner not found: $runner"
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $ReplaceExisting) {
    throw "Scheduled task already exists; inspect it before changing it: $TaskName"
}
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$arguments = (
    "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" " +
    "-ProjectRoot `"$resolvedRoot`" " +
    "-SourceRoot `"$resolvedSourceRoot`" " +
    "-PublicationMode $PublicationMode " +
    "-VercelProjectId `"$VercelProjectId`""
)
if ($AllowDegraded) {
    $arguments += " -AllowDegraded"
}
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments
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

Write-Output (
    "Registered $TaskName for $($StartTime.ToString('HH:mm')) local time " +
    "with publication mode $PublicationMode."
)
