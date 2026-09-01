[CmdletBinding()]
param(
    [string]$TaskName = "Dawnstrike Delayed SIP Capture",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
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
    [ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256 = "",
    [datetime]$StartAt = (Get-Date).Date.AddDays(1).AddHours(15).AddMinutes(20),
    [switch]$Create,
    [switch]$ReplaceExisting,
    [switch]$InteractiveCurrentUser,
    [pscredential]$RunAsCredential,
    [string]$HardeningBackupRoot = "",
    [string]$HardeningReceiptPath = ""
)

$ErrorActionPreference = "Stop"
if ($ReplaceExisting) {
    if ($Create -or $InteractiveCurrentUser) {
        throw "ReplaceExisting is the governed hardening path; do not combine it with Create or InteractiveCurrentUser."
    }
    $hardener = Join-Path $PSScriptRoot "harden_intraday_capture_task.ps1"
    if (-not (Test-Path -LiteralPath $hardener -PathType Leaf)) {
        throw "Delayed SIP hardening script is missing: $hardener"
    }
    $hardeningArgs = @{
        TaskName = $TaskName
        StateRoot = $StateRoot
        CandidateSha = $CandidateSha
        CandidateTree = $CandidateTree
        RunAsCredential = $RunAsCredential
    }
    if (-not [string]::IsNullOrWhiteSpace($HardeningBackupRoot)) {
        $hardeningArgs.BackupRoot = $HardeningBackupRoot
    }
    if (-not [string]::IsNullOrWhiteSpace($HardeningReceiptPath)) {
        $hardeningArgs.ReceiptPath = $HardeningReceiptPath
    }
    & $hardener @hardeningArgs
    if ($LASTEXITCODE -ne 0) { throw "Delayed SIP hardening did not complete." }
    return
}
if ([string]::IsNullOrWhiteSpace($CandidateSha)) { throw "CandidateSha is required." }
if ($Mode -ne "forward_observed") { throw "Scheduled capture registration is only for forward_observed." }
foreach ($name in @("SymbolsManifest", "SymbolsManifestSha256", "EntitlementReceipt", "EntitlementReceiptSha256", "SourceConfig", "SourceConfigSha256")) {
    if ([string]::IsNullOrWhiteSpace((Get-Variable -Name $name -ValueOnly))) { throw "$name is required." }
}

function Assert-DawnstrikeCaptureInputBindingFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $full = [System.IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if (
        $item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) { throw "$Label must be a regular non-reparse file." }
    $before = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    $actual = (Get-FileHash -LiteralPath $full -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    $after = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if (
        $after.PSIsContainer -or
        ($after.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $before.Length -ne $after.Length -or
        $before.LastWriteTimeUtc -ne $after.LastWriteTimeUtc
    ) { throw "$Label changed during identity capture." }
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) { throw "$Label hash does not match the supplied identity." }
    return $full
}

$SymbolsManifest = Assert-DawnstrikeCaptureInputBindingFile `
    -Path $SymbolsManifest -ExpectedSha256 $SymbolsManifestSha256 -Label "Symbols manifest"
$EntitlementReceipt = Assert-DawnstrikeCaptureInputBindingFile `
    -Path $EntitlementReceipt -ExpectedSha256 $EntitlementReceiptSha256 -Label "Entitlement receipt"
$SourceConfig = Assert-DawnstrikeCaptureInputBindingFile `
    -Path $SourceConfig -ExpectedSha256 $SourceConfigSha256 -Label "Source config"
$approvedPython = "C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe"
$approvedPythonSha256 = "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"
if ([string]::IsNullOrWhiteSpace($Python)) { $Python = $approvedPython }
$Python = [System.IO.Path]::GetFullPath($Python)
if (-not [string]::Equals($Python, $approvedPython, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Capture interpreter path is not the exact approved identity."
}
if ($PythonSha256 -and $PythonSha256.ToLowerInvariant() -ne $approvedPythonSha256) {
    throw "Supplied capture interpreter SHA-256 is not the approved identity."
}
$pythonItem = Get-Item -LiteralPath $Python -Force -ErrorAction Stop
if ($pythonItem.PSIsContainer -or ($pythonItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Capture interpreter must be a regular non-reparse file."
}
$pythonFileSha256 = (Get-FileHash -LiteralPath $Python -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
if ($pythonFileSha256 -ne $approvedPythonSha256) {
    throw "Capture interpreter hash is not the exact approved identity."
}
$pythonSignature = Get-AuthenticodeSignature -LiteralPath $Python -ErrorAction Stop
if (
    [string]$pythonSignature.Status -ne "Valid" -or
    $null -eq $pythonSignature.SignerCertificate -or
    [string]$pythonSignature.SignerCertificate.Subject -ne "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US" -or
    [string]$pythonSignature.SignerCertificate.Thumbprint -ne "9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48"
) { throw "Capture interpreter Authenticode identity is not approved." }
$pythonVersion = @(& $Python -I -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null)
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Count -ne 1 -or [string]$pythonVersion[0] -notmatch '^3\.13\.') {
    throw "Capture interpreter must be exact Python 3.13."
}
$bytecodePrefix = Join-Path $StateRoot ("capture-bytecode\" + $CandidateSha)
New-Item -ItemType Directory -Path $bytecodePrefix -Force -ErrorAction Stop | Out-Null
$bytecodePrefix = [System.IO.Path]::GetFullPath($bytecodePrefix)
$bytecodePrefixItem = Get-Item -LiteralPath $bytecodePrefix -Force -ErrorAction Stop
if (-not $bytecodePrefixItem.PSIsContainer -or ($bytecodePrefixItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw "Capture bytecode prefix must be a regular non-reparse directory."
}
$pythonPrefix = @("-I", "-B", "-X", ("pycache_prefix=" + $bytecodePrefix), "-u")
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
    action = @{
        execute = $Python
        execute_version = [string]$pythonVersion[0]
        execute_sha256 = $pythonFileSha256
        execute_signer_thumbprint = [string]$pythonSignature.SignerCertificate.Thumbprint
        arguments = $actionArguments
        working_directory = $RuntimeRoot
    }
    mode = $Mode
    feed = "sip"
    candidate_sha = $CandidateSha
    input_bindings = @(
        @{ name = "symbols_manifest"; path = $SymbolsManifest; sha256 = $SymbolsManifestSha256.ToLowerInvariant() },
        @{ name = "entitlement_receipt"; path = $EntitlementReceipt; sha256 = $EntitlementReceiptSha256.ToLowerInvariant() },
        @{ name = "source_config"; path = $SourceConfig; sha256 = $SourceConfigSha256.ToLowerInvariant() }
    )
    expected_session_policy = "dynamic_checked_in_market_calendar"
    session_root = $SessionRoot
    research_only = $true
    broker_execution_enabled = $false
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
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 15)
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
