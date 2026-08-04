[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$TaskName = "Dawnstrike 10of10 Daily Finalize",
    [datetime]$StartTime = (Get-Date -Hour 17 -Minute 30 -Second 0),
    [ValidateSet("LocalOnly", "Preview", "Production")]
    [string]$PublicationMode = "Production",
    [string]$VercelProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
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
        "RunAsCredential is required. Register finalization with a password-logon " +
        "Windows identity that can reach the network, encrypted Vercel credentials, " +
        "the Dawnstrike state root, and Telegram. Do not use S4U."
    )
}
$taskPassword = $RunAsCredential.GetNetworkCredential().Password
if ([string]::IsNullOrWhiteSpace($taskPassword)) {
    throw "RunAsCredential must contain a non-empty Windows password."
}
$runner = Join-Path $runtime "scripts\run_daily_finalize.ps1"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Daily finalize runner not found: $runner"
}
if (-not $BackupRoot) {
    $BackupRoot = Join-Path $state (
        "scheduler-backups\" +
        (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    )
}
New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing -and -not $ReplaceExisting) {
    throw "Scheduled task already exists; inspect it before replacement: $TaskName"
}
if ($existing) {
    $safeName = $TaskName -replace "[^A-Za-z0-9._-]", "_"
    Export-ScheduledTask -TaskName $TaskName |
        Set-Content -LiteralPath (Join-Path $BackupRoot "$safeName.xml") -Encoding Unicode
}

$arguments = (
    "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" " +
    "-RuntimeRoot `"$runtime`" " +
    "-StateRoot `"$state`" " +
    "-PublicationMode $PublicationMode " +
    "-VercelProjectId `"$VercelProjectId`""
)
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $arguments `
    -WorkingDirectory $runtime
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 15) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -User $RunAsCredential.UserName `
    -Password $taskPassword `
    -RunLevel Limited `
    -Description "Dawnstrike V6 canonical performance, Calendar, readiness, and publication. Research-only; no broker execution." `
    -Force | Out-Null

Write-Output (
    "Registered $TaskName for $($StartTime.ToString('HH:mm')) local time " +
    "from $runtime against $state with publication mode $PublicationMode."
)
Write-Output "Rollback task XML saved under $BackupRoot."
