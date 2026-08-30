[CmdletBinding()]
param(
    [string]$TaskName = "Dawnstrike Delayed SIP Capture",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$CandidateSha,
    [ValidateSet("forward_observed", "retrospective_research")]
    [string]$Mode = "forward_observed",
    [string]$DbPath = "C:\r\dawnstrike-forward-evidence\staging.sqlite",
    [string]$EvidenceRoot = "C:\r\dawnstrike-forward-evidence",
    [string]$RunRoot = "C:\r\dawnstrike-forward-evidence\runs",
    [string]$OutputRoot = "C:\r\dawnstrike-forward-evidence\evidence",
    [string]$SymbolsManifest,
    [string]$SymbolsManifestSha256,
    [string]$ExpectedSession,
    [string]$EntitlementReceipt,
    [string]$EntitlementReceiptSha256,
    [string]$SourceConfig,
    [string]$SourceConfigSha256,
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$EnvFile = "",
    [string]$Python = "",
    [datetime]$StartAt = (Get-Date).Date.AddDays(1).AddHours(15).AddMinutes(20),
    [switch]$Create,
    [pscredential]$RunAsCredential
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($CandidateSha)) { throw "CandidateSha is required." }
if ($Mode -ne "forward_observed") { throw "Scheduled capture registration is only for forward_observed." }
foreach ($name in @("SymbolsManifest", "SymbolsManifestSha256", "ExpectedSession", "EntitlementReceipt", "EntitlementReceiptSha256", "SourceConfig", "SourceConfigSha256")) {
    if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $name -ValueOnly))) { throw "$name is required." }
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = Join-Path $RuntimeRoot ".venv\Scripts\python.exe"
}
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $StateRoot "secrets\runtime.env"
}
$runner = Join-Path $RuntimeRoot "scripts\capture_intraday_operations.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Capture runner not found: $runner" }

$captureArgs = @(
    $runner, "--mode", $Mode, "--provider", "alpaca", "--feed", "sip",
    "--candidate-sha", $CandidateSha, "--repo-root", $RuntimeRoot,
    "--db-path", $DbPath, "--evidence-root", $EvidenceRoot, "--run-root", $RunRoot,
    "--output-root", $OutputRoot, "--symbols-manifest", $SymbolsManifest,
    "--symbols-manifest-sha256", $SymbolsManifestSha256, "--expected-session", $ExpectedSession,
    "--entitlement-receipt", $EntitlementReceipt, "--entitlement-receipt-sha256", $EntitlementReceiptSha256,
    "--source-config", $SourceConfig, "--source-config-sha256", $SourceConfigSha256,
    "--env-file", $EnvFile, "--max-pages", "100", "--retries", "3", "--execute"
)
$actionArguments = ('-u "{0}" {1}' -f $Python, (($captureArgs | ForEach-Object { '"' + $_ + '"' }) -join ' '))
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $StartAt.TimeOfDay

$preview = [ordered]@{
    status = if ($Create) { "CREATE_REQUESTED" } else { "PREVIEW_ONLY" }
    task_name = $TaskName
    schedule_local = $StartAt.ToString("HH:mm")
    days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    action = @{ execute = $Python; arguments = $actionArguments; working_directory = $RuntimeRoot }
    mode = $Mode
    feed = "sip"
    candidate_sha = $CandidateSha
    research_only = $true
    broker_execution = "disabled"
}
Write-Output ($preview | ConvertTo-Json -Depth 6 -Compress)
if (-not $Create) { return }
if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
    throw "RunAsCredential is required only when -Create is explicitly supplied."
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) { throw "Scheduled task already exists; no existing Dawnstrike task was changed: $TaskName" }
. (Join-Path $RuntimeRoot "scripts\resolve_dawnstrike_task_principal.ps1")
$principal = Resolve-DawnstrikeTaskPrincipal -Credential $RunAsCredential
$password = $RunAsCredential.GetNetworkCredential().Password
$action = New-ScheduledTaskAction -Execute $Python -Argument $actionArguments -WorkingDirectory $RuntimeRoot
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -User $principal -Password $password -RunLevel Limited -Description "Dawnstrike delayed SIP research capture; no broker execution." | Out-Null
Write-Output "Registered $TaskName without modifying existing Dawnstrike tasks."
