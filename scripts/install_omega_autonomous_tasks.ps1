param([switch]$Yes)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if (-not $Yes) {
    throw 'Pass -Yes to install the Dawnstrike OMEGA tasks for the current Windows user.'
}

$RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $RepoRoot
$Live_Trading_Enabled = $false
$TaskPath = '\Dawnstrike\'
$Definitions = @(
    [pscustomobject]@{ name = 'OMEGA After Close'; script = 'scripts/run_omega_scheduler_after_close.ps1'; at = '16:35'; do_not_start_new_instance = $true },
    [pscustomobject]@{ name = 'OMEGA Morning Check'; script = 'scripts/run_omega_scheduler_morning_check.ps1'; at = '09:10'; do_not_start_new_instance = $true },
    [pscustomobject]@{ name = 'OMEGA Verify'; script = 'scripts/run_omega_scheduler_verify.ps1'; at = '17:10'; do_not_start_new_instance = $true },
    [pscustomobject]@{ name = 'OMEGA Watchdog'; script = 'scripts/run_omega_scheduler_watchdog.ps1'; at = '18:00'; do_not_start_new_instance = $true }
)

foreach ($Definition in $Definitions) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $Definition.script))) {
        throw "Required scheduler wrapper is missing: $($Definition.script)"
    }
}

$Scheduler = New-Object -ComObject Schedule.Service
$Scheduler.Connect()
$SchedulerRoot = $Scheduler.GetFolder('\')
try {
    $SchedulerRoot.GetFolder('Dawnstrike') | Out-Null
}
catch {
    $SchedulerRoot.CreateFolder('Dawnstrike') | Out-Null
}

$Principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

foreach ($Definition in $Definitions) {
    $Action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File $($Definition.script)" `
        -WorkingDirectory $RepoRoot
    $Trigger = New-ScheduledTaskTrigger -Daily -At $Definition.at
    $Task = New-ScheduledTask `
        -Action $Action `
        -Trigger $Trigger `
        -Principal $Principal `
        -Settings $Settings `
        -Description 'Dawnstrike research and paper-audit workflow; no broker execution.'
    Register-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $Definition.name `
        -InputObject $Task `
        -Force | Out-Null
}

py -m intraday_scanner.v2.autonomous_runner status
$ExitCode = $LASTEXITCODE
exit $ExitCode
