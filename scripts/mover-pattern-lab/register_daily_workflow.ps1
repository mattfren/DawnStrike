[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    [string]$TaskPrefix = "Dawnstrike Mover Paper",

    [string]$Python = "py",

    [ValidateRange(0, 4)]
    [int]$CutoffDelayMinutes = 1,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$configPath = (Resolve-Path -LiteralPath $Config).Path
$settings = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
if ($settings.schema_version -ne "dawnstrike.mover_daily_workflow.v1") {
    throw "Unsupported mover daily workflow schema: $($settings.schema_version)"
}
if (-not $settings.cutoffs_et -or $settings.cutoffs_et.Count -eq 0) {
    throw "Mover daily workflow config must declare cutoffs_et."
}
if ($settings.research_only -ne $true -or $settings.broker_execution_enabled -ne $false) {
    throw "Scheduler registration requires research_only=true and broker_execution_enabled=false."
}

$runner = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run_operator.ps1")).Path
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "../..")).Path
$eastern = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$local = [TimeZoneInfo]::Local
$referenceDate = [DateTime]::Today
$planned = [System.Collections.Generic.List[object]]::new()
$legacyTaskNames = @("$TaskPrefix Reconcile")

function Convert-EasternClockToLocal {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Clock,
        [int]$DelayMinutes = 0
    )

    $parts = $Clock.Split(':')
    if ($parts.Count -ne 2) {
        throw "Invalid ET clock: $Clock"
    }
    $easternWallClock = [DateTime]::SpecifyKind(
        $referenceDate.AddHours([int]$parts[0]).AddMinutes([int]$parts[1] + $DelayMinutes),
        [DateTimeKind]::Unspecified
    )
    $asUtc = [TimeZoneInfo]::ConvertTimeToUtc($easternWallClock, $eastern)
    return [TimeZoneInfo]::ConvertTimeFromUtc($asUtc, $local)
}

function New-OperatorActionArguments {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Stage,
        [string]$Cutoff
    )

    $values = @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $runner + '"'),
        "-Stage", $Stage,
        "-Config", ('"' + $configPath + '"'),
        "-Python", ('"' + $Python + '"')
    )
    if (-not [string]::IsNullOrWhiteSpace($Cutoff)) {
        $values += @("-Cutoff", $Cutoff)
    }
    return $values -join ' '
}

foreach ($cutoff in $settings.cutoffs_et) {
    $clock = [string]$cutoff
    $localStart = Convert-EasternClockToLocal -Clock $clock -DelayMinutes $CutoffDelayMinutes
    $planned.Add([ordered]@{
        task_name = "$TaskPrefix Scan $($clock.Replace(':', ''))"
        stage = "Scan"
        cutoff_et = $clock
        source_start_clock_et = $clock
        local_start = $localStart.ToString("yyyy-MM-ddTHH:mm:ss")
        local_start_clock = $localStart.ToString("HH:mm")
    })
}
$reconcileClock = [string]$settings.reconcile_not_before_et
$reconcileParts = $reconcileClock.Split(':')
if ($reconcileParts.Count -ne 2) {
    throw "Invalid reconcile_not_before_et clock: $reconcileClock"
}
$regularCloseEt = $referenceDate.Date.AddHours(16)
$configuredReconcileEt = $referenceDate.Date.AddHours([int]$reconcileParts[0]).AddMinutes(
    [int]$reconcileParts[1]
)
$postCloseLag = $configuredReconcileEt - $regularCloseEt
if ($postCloseLag.TotalMinutes -lt 0) {
    throw "reconcile_not_before_et cannot precede the regular 16:00 ET close."
}
$reconcileRetryMinutes = 30
$reconcileRetryStartEt = $referenceDate.Date.AddHours(13).Add($postCloseLag)
$reconcileRetryEndEt = $configuredReconcileEt.AddMinutes($reconcileRetryMinutes)
$reconcileIndex = 0
for (
    $probeEt = $reconcileRetryStartEt;
    $probeEt -le $reconcileRetryEndEt;
    $probeEt = $probeEt.AddMinutes($reconcileRetryMinutes)
) {
    $probeClock = $probeEt.ToString("HH:mm")
    $reconcileLocalStart = Convert-EasternClockToLocal -Clock $probeClock
    $planned.Add([ordered]@{
        task_name = "$TaskPrefix Reconcile $($probeClock.Replace(':', ''))"
        stage = "Reconcile"
        cutoff_et = $null
        source_start_clock_et = $probeClock
        reconcile_retry_index = $reconcileIndex
        local_start = $reconcileLocalStart.ToString("yyyy-MM-ddTHH:mm:ss")
        local_start_clock = $reconcileLocalStart.ToString("HH:mm")
    })
    $reconcileIndex += 1
}

if (-not $Apply) {
    [ordered]@{
        status = "preview_only"
        apply_command = "Re-run with -Apply to register the listed tasks."
        host_time_zone = $local.Id
        source_time_zone = $eastern.Id
        cutoff_delay_minutes = $CutoffDelayMinutes
        reconcile_retry_interval_minutes = $reconcileRetryMinutes
        reconcile_retry_start_et = $reconcileRetryStartEt.ToString("HH:mm")
        reconcile_retry_end_et = $reconcileRetryEndEt.ToString("HH:mm")
        legacy_task_names_to_remove = $legacyTaskNames
        tasks = $planned
        research_only = $true
        broker_execution_enabled = $false
    } | ConvertTo-Json -Depth 8
    exit 0
}

$scanTaskSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$reconcileTaskSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable
$removedLegacyTaskNames = [System.Collections.Generic.List[string]]::new()

foreach ($legacyTaskName in $legacyTaskNames) {
    $legacyTask = Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
    if ($null -ne $legacyTask) {
        Unregister-ScheduledTask -TaskName $legacyTaskName -Confirm:$false
        $removedLegacyTaskNames.Add($legacyTaskName)
    }
}

foreach ($item in $planned) {
    $arguments = New-OperatorActionArguments `
        -Stage ([string]$item.stage) `
        -Cutoff ([string]$item.cutoff_et)
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $arguments `
        -WorkingDirectory $repo
    $trigger = New-ScheduledTaskTrigger `
        -Weekly `
        -WeeksInterval 1 `
        -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday `
        -At ([DateTime]$item.local_start)
    $selectedSettings = if ($item.stage -eq "Reconcile") {
        $reconcileTaskSettings
    }
    else {
        $scanTaskSettings
    }
    Register-ScheduledTask `
        -TaskName ([string]$item.task_name) `
        -Action $action `
        -Trigger $trigger `
        -Settings $selectedSettings `
        -Description "Dawnstrike research-only mover paper workflow; never places orders." `
        -Force | Out-Null
}

[ordered]@{
    status = "registered"
    host_time_zone = $local.Id
    source_time_zone = $eastern.Id
    reconcile_retry_interval_minutes = $reconcileRetryMinutes
    reconcile_retry_start_et = $reconcileRetryStartEt.ToString("HH:mm")
    reconcile_retry_end_et = $reconcileRetryEndEt.ToString("HH:mm")
    removed_legacy_task_names = $removedLegacyTaskNames
    tasks = $planned
    research_only = $true
    broker_execution_enabled = $false
} | ConvertTo-Json -Depth 8
