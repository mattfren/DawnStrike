param(
    [string]$TaskName = "Dawnstrike Web Telegram AutoPilot",
    [string]$StartTime = "08:00"
)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$bat = Join-Path $root "scripts\run_web_telegram_daemon.bat"

if (-not (Test-Path -LiteralPath $bat)) {
    throw "Missing daemon script: $bat"
}

$action = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Dawnstrike notification-only web auto-pilot. Research/watchlist only." `
    -Force

Write-Host "Registered scheduled task '$TaskName' to run $bat at $StartTime."
