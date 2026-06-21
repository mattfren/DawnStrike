param(
    [string]$TaskName = "Dawnstrike Notification Automation",
    [switch]$AtLogon,
    [switch]$MarketDays
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$bat = Join-Path $root "scripts\run_automation_daemon.bat"
$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root

if ($AtLogon) {
    $trigger = New-ScheduledTaskTrigger -AtLogOn
}
elseif ($MarketDays) {
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At 8:00am
}
else {
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2)
}

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force
Write-Host "Registered $TaskName -> $bat"
