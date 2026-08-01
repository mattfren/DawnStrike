[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "",
    [pscredential]$RunAsCredential,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path $RuntimeRoot).Path
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
    throw (
        "RunAsCredential is required. Register AlphaOps with a password-logon " +
        "Windows identity that can reach the network, encrypted secrets, the " +
        "Dawnstrike state root, and Telegram. Do not use S4U: Windows prevents " +
        "S4U tasks from accessing network or encrypted files."
    )
}
$taskPassword = $RunAsCredential.GetNetworkCredential().Password
if ([string]::IsNullOrWhiteSpace($taskPassword)) {
    throw "RunAsCredential must contain a non-empty Windows password."
}
if (-not $BackupRoot) {
    $BackupRoot = Join-Path $state (
        "scheduler-backups\" +
        (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    )
}
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null

$taskDefinitions = @(
    [ordered]@{
        Name = "Dawnstrike AlphaOps Morning"
        Description = "Dawnstrike V6 morning research collection and ranked delivery. Research-only; no broker execution."
        Script = "run_alphaops_morning.ps1"
        Start = "08:10"
        Repeat = $false
        DurationHours = 0
    },
    [ordered]@{
        Name = "Dawnstrike AlphaOps Monitor 5m"
        Description = "Dawnstrike V6 intraday paper monitor on a five-minute cadence. Research-only; no broker execution."
        Script = "run_alphaops_monitor.ps1"
        Start = "08:35"
        Repeat = $true
        DurationHours = 7
    },
    [ordered]@{
        Name = "Dawnstrike AlphaOps EOD Full Report"
        Description = "Dawnstrike V6 sourced outcomes, reconciliation, learning, attribution, and PaperOps forward evidence. Research-only; no broker execution."
        Script = "run_alphaops_eod.ps1"
        Start = "15:15"
        Repeat = $false
        DurationHours = 0
    }
)

function Export-ExistingTask {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        return
    }
    $safeName = $TaskName -replace "[^A-Za-z0-9._-]", "_"
    Export-ScheduledTask -TaskName $TaskName |
        Set-Content -LiteralPath (Join-Path $BackupRoot "$safeName.xml") -Encoding Unicode
}

foreach ($definition in $taskDefinitions) {
    $taskName = [string]$definition.Name
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existing -and -not $ReplaceExisting) {
        throw "Scheduled task already exists; rerun with -ReplaceExisting after inspection: $taskName"
    }
    Export-ExistingTask -TaskName $taskName

    $runner = Join-Path $runtime ("scripts\" + [string]$definition.Script)
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
        throw "Dawnstrike task runner not found: $runner"
    }
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" " +
        "-RuntimeRoot `"$runtime`" -StateRoot `"$state`""
    )
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $arguments `
        -WorkingDirectory $runtime
    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -WeeksInterval 1 `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
        -At ([datetime]::ParseExact(
            [string]$definition.Start,
            "HH:mm",
            [System.Globalization.CultureInfo]::InvariantCulture
        ))
    if ([bool]$definition.Repeat) {
        $repetition = New-CimInstance `
            -Namespace "Root/Microsoft/Windows/TaskScheduler" `
            -ClassName "MSFT_TaskRepetitionPattern" `
            -ClientOnly `
            -Property @{
                Interval = "PT5M"
                Duration = "PT$([int]$definition.DurationHours)H"
                StopAtDurationEnd = $true
            }
        $trigger.Repetition = $repetition
    }
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -DisallowStartIfOnBatteries:$false `
        -StopIfGoingOnBatteries:$false `
        -WakeToRun `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $RunAsCredential.UserName `
        -Password $taskPassword `
        -RunLevel Limited `
        -Description ([string]$definition.Description) `
        -Force | Out-Null
    Write-Output "Registered $taskName from $runtime against $state."
}

Write-Output "Rollback task XML saved under $BackupRoot."
