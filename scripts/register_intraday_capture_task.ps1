[CmdletBinding()]
param(
    [string]$TaskName = "Dawnstrike Delayed SIP Capture",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$CandidateSha,
    [ValidateSet("forward_observed", "retrospective_research")]
    [string]$Mode = "forward_observed",
    [string]$DbPath = "C:\r\dawnstrike-forward-db\staging.sqlite",
    [string]$EvidenceRoot = "C:\r\dawnstrike-forward-evidence",
    [string]$RunRoot = "C:\r\dawnstrike-forward-runs",
    [string]$OutputRoot = "C:\r\dawnstrike-forward-output",
    [string]$SessionRoot = "C:\r\dawnstrike-forward-sessions",
    [string]$SymbolsManifest,
    [string]$SymbolsManifestSha256,
    [string]$EntitlementReceipt,
    [string]$EntitlementReceiptSha256,
    [string]$SourceConfig,
    [string]$SourceConfigSha256,
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$EnvFile = "",
    [string]$Python = "",
    [datetime]$StartAt = (Get-Date).Date.AddDays(1).AddHours(15).AddMinutes(20),
    [switch]$Create,
    [switch]$InteractiveCurrentUser,
    [pscredential]$RunAsCredential
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($CandidateSha)) { throw "CandidateSha is required." }
if ($Mode -ne "forward_observed") { throw "Scheduled capture registration is only for forward_observed." }
foreach ($name in @("SymbolsManifest", "SymbolsManifestSha256", "EntitlementReceipt", "EntitlementReceiptSha256", "SourceConfig", "SourceConfigSha256")) {
    if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $name -ValueOnly))) { throw "$name is required." }
}
if ([string]::IsNullOrWhiteSpace($Python)) {
    # The active runtime may intentionally have no project-local venv.  Use
    # the governed launcher and pin the interpreter family explicitly.
    $Python = "py.exe"
    $pythonPrefix = @("-3.13", "-u")
} else {
    $pythonPrefix = @("-u")
}
if ([string]::IsNullOrWhiteSpace($EnvFile)) {
    $EnvFile = Join-Path $StateRoot "secrets\runtime.env"
}
$runner = Join-Path $RuntimeRoot "scripts\run_daily_intraday_capture.py"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Capture runner not found: $runner" }

$captureArgs = @(
    $runner,
    "--candidate-sha", $CandidateSha, "--repo-root", $RuntimeRoot,
    "--db-path", $DbPath, "--evidence-root", $EvidenceRoot, "--run-root", $RunRoot,
    "--output-root", $OutputRoot, "--session-root", $SessionRoot,
    "--symbols-manifest", $SymbolsManifest,
    "--symbols-manifest-sha256", $SymbolsManifestSha256,
    "--entitlement-receipt", $EntitlementReceipt, "--entitlement-receipt-sha256", $EntitlementReceiptSha256,
    "--source-config", $SourceConfig, "--source-config-sha256", $SourceConfigSha256,
    "--env-file", $EnvFile, "--max-pages", "100", "--retries", "3", "--execute"
)
$argumentTokens = @($pythonPrefix + $captureArgs)
$actionArguments = (($argumentTokens | ForEach-Object { '"' + $_ + '"' }) -join ' ')
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At $StartAt

$preview = [ordered]@{
    status = if ($Create) { "CREATE_REQUESTED" } else { "PREVIEW_ONLY" }
    task_name = $TaskName
    schedule_local = $StartAt.ToString("HH:mm")
    days = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    action = @{ execute = $Python; arguments = $actionArguments; working_directory = $RuntimeRoot }
    mode = $Mode
    feed = "sip"
    candidate_sha = $CandidateSha
    expected_session_policy = "dynamic_checked_in_market_calendar"
    session_root = $SessionRoot
    research_only = $true
    broker_execution = "disabled"
}
Write-Output ($preview | ConvertTo-Json -Depth 6 -Compress)
# Registration remains preview-only unless the operator explicitly supplies -Create.
if (-not $Create) { return }
if ($InteractiveCurrentUser -and $null -ne $RunAsCredential) {
    throw "Choose either InteractiveCurrentUser or RunAsCredential, not both."
}
if (
    -not $InteractiveCurrentUser -and
    ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName))
) {
    throw "Create requires InteractiveCurrentUser or RunAsCredential."
}
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) { throw "Scheduled task already exists; no existing Dawnstrike task was changed: $TaskName" }
$action = New-ScheduledTaskAction -Execute $Python -Argument $actionArguments -WorkingDirectory $RuntimeRoot
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 3)
if ($InteractiveCurrentUser) {
    $currentPrincipal = [string][System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    if ([string]::IsNullOrWhiteSpace($currentPrincipal)) { throw "Current Windows principal is unavailable." }
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $currentPrincipal -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $taskPrincipal -Description "Dawnstrike delayed SIP research capture; no broker execution." | Out-Null
} else {
    . (Join-Path $RuntimeRoot "scripts\resolve_dawnstrike_task_principal.ps1")
    $principal = Resolve-DawnstrikeTaskPrincipal -Credential $RunAsCredential
    $password = $RunAsCredential.GetNetworkCredential().Password
    if ([string]::IsNullOrWhiteSpace($password)) { throw "RunAsCredential must contain a non-empty Windows password." }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -User $principal -Password $password -RunLevel Limited -Description "Dawnstrike delayed SIP research capture; no broker execution." | Out-Null
}
Write-Output "Registered $TaskName without modifying existing Dawnstrike tasks."
