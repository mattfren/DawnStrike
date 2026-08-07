[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "",
    [pscredential]$RunAsCredential,
    [switch]$ReuseExistingPrincipal,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path $RuntimeRoot).Path
. (Join-Path $runtime "scripts\resolve_dawnstrike_task_principal.ps1")
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
$sourceConfigPath = Join-Path $state "config\web_sources.yaml"
if (-not (Test-Path -LiteralPath $sourceConfigPath -PathType Leaf)) {
    throw "Durable AlphaOps source configuration is missing: $sourceConfigPath"
}
$sourceConfigText = Get-Content -LiteralPath $sourceConfigPath -Raw
if ($sourceConfigText -match "REQUIRED_ACCOUNTABLE_EMAIL|REQUIRED_CONFIGURED_PROVIDER") {
    throw "Durable AlphaOps source configuration still contains REQUIRED placeholders."
}
$sourceConfigValidator = Join-Path $runtime "scripts\validate_web_source_config.py"
if (-not (Test-Path -LiteralPath $sourceConfigValidator -PathType Leaf)) {
    throw "Durable source configuration validator is missing: $sourceConfigValidator"
}
$sourceConfigReceipt = Join-Path $state "receipts\source-config-validation.json"
& py.exe $sourceConfigValidator --config $sourceConfigPath --runtime-root $runtime --receipt $sourceConfigReceipt | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Durable AlphaOps source configuration failed semantic validation: $sourceConfigPath"
}
if ($ReuseExistingPrincipal -and $null -ne $RunAsCredential) {
    throw "Choose either ReuseExistingPrincipal or RunAsCredential, not both."
}
if (
    -not $ReuseExistingPrincipal -and
    ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName))
) {
    throw (
        "RunAsCredential is required. Register AlphaOps with a password-logon " +
        "Windows identity that can reach the network, encrypted secrets, the " +
        "Dawnstrike state root, and Telegram. Do not use S4U: Windows prevents " +
        "S4U tasks from accessing network or encrypted files."
    )
}
$taskPassword = ""
$taskPrincipal = ""
if (-not $ReuseExistingPrincipal) {
    $taskPrincipal = Resolve-DawnstrikeTaskPrincipal -Credential $RunAsCredential
    $taskPassword = $RunAsCredential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($taskPassword)) {
        throw "RunAsCredential must contain a non-empty Windows password."
    }
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
        # Scheduled Task timestamps use the host's local zone (Central here);
        # 08:00 Central captures materially deeper premarket liquidity while
        # preserving a 30-minute buffer before the 09:30 Eastern open.
        Start = "08:00"
        Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        Repeat = $false
        RepetitionDuration = ""
        ExecutionLimitMinutes = 60
        RestartCount = 3
        RestartIntervalMinutes = 5
    },
    [ordered]@{
        Name = "Dawnstrike AlphaOps Monitor 5m"
        Description = "Dawnstrike V6 intraday paper monitor on a five-minute cadence. Research-only; no broker execution."
        Script = "run_alphaops_monitor.ps1"
        Start = "08:35"
        Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        Repeat = $true
        # Ends at 15:10 local, before the required 15:15 EOD task.
        RepetitionDuration = "PT6H35M"
        ExecutionLimitMinutes = 4
        RestartCount = 3
        RestartIntervalMinutes = 5
    },
    [ordered]@{
        Name = "Dawnstrike AlphaOps EOD Full Report"
        Description = "Dawnstrike V6 sourced outcomes, reconciliation, learning, attribution, and PaperOps forward evidence. Research-only; no broker execution."
        Script = "run_alphaops_eod.ps1"
        Start = "15:15"
        Days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
        Repeat = $false
        RepetitionDuration = ""
        ExecutionLimitMinutes = 120
        RestartCount = 3
        RestartIntervalMinutes = 5
    },
    [ordered]@{
        Name = "Dawnstrike AlphaOps V6 Weekly Training"
        Description = "Dawnstrike V6 weekly-only model refit and all-family purged OOF research evaluation. Research-only; no broker execution."
        Script = "run_alphaops_weekly_training.ps1"
        # Finalize can legally run until 20:30; preserve a 30-minute buffer.
        Start = "21:00"
        Days = @("Monday")
        Repeat = $false
        RepetitionDuration = ""
        ExecutionLimitMinutes = 180
        # A missing completion receipt is retryable, but never unbounded.
        RestartCount = 4
        RestartIntervalMinutes = 15
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

# Validate the entire DAG and export every recoverable definition before the
# first mutation. A missing/invalid later task must never leave an earlier task
# partially updated.
$taskPreflight = @{}
$storedPrincipal = ""
foreach ($definition in $taskDefinitions) {
    $taskName = [string]$definition.Name
    $existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($ReuseExistingPrincipal -and -not $existing) {
        throw "Cannot reuse the stored principal because the task does not exist: $taskName"
    }
    if (
        $ReuseExistingPrincipal -and
        $existing.Principal.LogonType.ToString() -eq "Password"
    ) {
        throw (
            "Existing Password tasks require RunAsCredential because Windows " +
            "will not modify them without revalidating the account password: $taskName"
        )
    }
    if (
        $ReuseExistingPrincipal -and
        $existing.Principal.LogonType.ToString() -ne "ServiceAccount"
    ) {
        throw "Existing task principal is not a reusable service account: $taskName"
    }
    if ($ReuseExistingPrincipal) {
        $principal = [string]$existing.Principal.UserId
        if ([string]::IsNullOrWhiteSpace($principal)) {
            throw "Existing task principal identity is blank: $taskName"
        }
        if ($storedPrincipal -and $storedPrincipal -ne $principal) {
            throw "Existing AlphaOps tasks do not share one approved principal identity."
        }
        $storedPrincipal = $principal
    }
    if ($existing -and -not $ReplaceExisting -and -not $ReuseExistingPrincipal) {
        throw "Scheduled task already exists; rerun with -ReplaceExisting after inspection: $taskName"
    }
    $runner = Join-Path $runtime ("scripts\" + [string]$definition.Script)
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
        throw "Dawnstrike task runner not found: $runner"
    }
    $taskPreflight[$taskName] = [pscustomobject]@{
        existing = $existing
        runner = $runner
    }
}
foreach ($definition in $taskDefinitions) {
    Export-ExistingTask -TaskName ([string]$definition.Name)
}

foreach ($definition in $taskDefinitions) {
    $taskName = [string]$definition.Name
    $existing = $taskPreflight[$taskName].existing
    $runner = [string]$taskPreflight[$taskName].runner
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
        -DaysOfWeek $definition.Days `
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
                Duration = [string]$definition.RepetitionDuration
                StopAtDurationEnd = $true
            }
        $trigger.Repetition = $repetition
    }
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -WakeToRun `
        -MultipleInstances IgnoreNew `
        -RestartCount ([int]$definition.RestartCount) `
        -RestartInterval (New-TimeSpan -Minutes ([int]$definition.RestartIntervalMinutes)) `
        -ExecutionTimeLimit (New-TimeSpan -Minutes ([int]$definition.ExecutionLimitMinutes))
    if ($ReuseExistingPrincipal) {
        Set-ScheduledTask `
            -TaskName $taskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -ErrorAction Stop | Out-Null
        Write-Output "Updated $taskName while preserving its approved stored principal."
    }
    else {
        Register-ScheduledTask `
            -TaskName $taskName `
            -Action $action `
            -Trigger $trigger `
            -Settings $settings `
            -User $taskPrincipal `
            -Password $taskPassword `
            -RunLevel Limited `
            -Description ([string]$definition.Description) `
            -Force | Out-Null
        Write-Output "Registered $taskName from $runtime against $state."
    }
}

Write-Output "Rollback task XML saved under $BackupRoot."
