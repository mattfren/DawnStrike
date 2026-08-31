[CmdletBinding()]
param(
    [string]$TaskName = "Dawnstrike Delayed SIP Capture",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
    [string]$BackupRoot = "",
    [string]$ReceiptPath = "",
    [string]$BackupXmlPath = "",
    [pscredential]$RunAsCredential,
    [switch]$Rollback
)

$ErrorActionPreference = "Stop"
$script:HardeningTaskName = "Dawnstrike Delayed SIP Capture"

function Get-HardeningSha256Text {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-HardeningSha256File {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Assert-HardeningNoReparseComponents {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    $cursor = [System.IO.DirectoryInfo]::new($full)
    if (-not $cursor.Exists) {
        $cursor = [System.IO.DirectoryInfo]::new((Split-Path -Parent $full))
    }
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point path component."
        }
        if ([string]::Equals(
                $cursor.FullName.TrimEnd('\'),
                $cursor.Root.FullName.TrimEnd('\'),
                [System.StringComparison]::OrdinalIgnoreCase
            )) { break }
        $cursor = $cursor.Parent
    }
}

function Enter-HardeningActivationLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)
    $lockRoot = Join-Path $StateRoot "locks"
    Assert-HardeningNoReparseComponents $lockRoot "Hardening lock root"
    New-Item -ItemType Directory -Path $lockRoot -Force -ErrorAction Stop | Out-Null
    $path = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    Assert-HardeningNoReparseComponents $path "Hardening activation lock"
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        throw "A runtime activation lock already exists; task hardening is not permitted."
    }
    $dailyLocks = @(Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue)
    if ($dailyLocks.Count -gt 0) { throw "A daily run lock exists; task hardening is not permitted." }
    $token = [Guid]::NewGuid().ToString("N")
    $payload = [ordered]@{
        schema_version = "dawnstrike.runtime_activation_lock.v1"
        operation = "capture-task-hardening"
        lock_token = $token
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        origin_main_refreshed_at_utc = $script:HardeningOriginRefreshUtc
        origin_url = $script:HardeningOriginUrl
        origin_url_sha256 = $script:HardeningOriginUrlSha256
        created_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
    }
    $encoding = [System.Text.UTF8Encoding]::new($false)
    try {
        $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $bytes = $encoding.GetBytes((ConvertTo-Json $payload -Compress)); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
    }
    catch { throw "Hardening could not acquire the runtime activation lock." }
    $readBack = Get-Content -LiteralPath $path -Raw -ErrorAction Stop | ConvertFrom-Json
    if ([string]$readBack.lock_token -ne $token) { throw "Hardening activation lock read-back failed." }
    $dailyLocks = @(Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue)
    if ($dailyLocks.Count -gt 0) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        throw "A daily run lock appeared during hardening lock acquisition."
    }
    return [pscustomobject]@{ path = $path; token = $token }
}

function Exit-HardeningActivationLock {
    [CmdletBinding()]
    param([AllowNull()][object]$Lock)
    if ($null -eq $Lock) { return }
    if (-not (Test-Path -LiteralPath $Lock.path -PathType Leaf)) { throw "Hardening activation lock disappeared before release." }
    try {
        $payload = Get-Content -LiteralPath $Lock.path -Raw -ErrorAction Stop | ConvertFrom-Json
        if (
            [string]$payload.schema_version -ne "dawnstrike.runtime_activation_lock.v1" -or
            [string]$payload.lock_token -ne [string]$Lock.token -or
            [string]$payload.candidate_sha -ne [string]$CandidateSha -or
            [string]$payload.candidate_tree -ne [string]$CandidateTree
        ) { throw "Hardening activation lock ownership changed." }
        Remove-Item -LiteralPath $Lock.path -Force -ErrorAction Stop
        if (Test-Path -LiteralPath $Lock.path -PathType Leaf) { throw "Hardening activation lock could not be removed." }
    } catch { throw "Hardening activation lock could not be released; operator recovery is required." }
}

function Assert-HardeningCandidateIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][string]$ExpectedTree,
        [switch]$RefreshOrigin
    )
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $gitDir = Join-Path $repoRoot ".git"
    if (-not (Test-Path -LiteralPath $gitDir -PathType Container)) { throw "Hardening requires a self-contained candidate checkout." }
    $git = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0].Source
    if ($RefreshOrigin) {
        & $git -C $repoRoot fetch --quiet --prune origin "+refs/heads/main:refs/remotes/origin/main" 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Candidate origin/main refresh failed." }
        $script:HardeningOriginRefreshUtc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
    }
    function Read-Git([string[]]$Arguments) {
        $value = & $git -C $repoRoot @Arguments 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Candidate identity command failed." }
        return ([string]($value -join "")).Trim()
    }
    if ((Read-Git @("rev-parse", "HEAD")).ToLowerInvariant() -ne $ExpectedSha) { throw "Candidate HEAD is not the requested SHA." }
    if ((Read-Git @("rev-parse", "HEAD^{tree}")).ToLowerInvariant() -ne $ExpectedTree) { throw "Candidate tree is not the requested tree." }
    if ((Read-Git @("rev-parse", "refs/remotes/origin/main")).ToLowerInvariant() -ne $ExpectedSha) { throw "Candidate is not the exact clean origin/main SHA." }
    $script:HardeningOriginUrl = (Read-Git @("remote", "get-url", "origin"))
    if (
        [string]::IsNullOrWhiteSpace($script:HardeningOriginUrl) -or
        $script:HardeningOriginUrl -match '(gh[pousr]_|oauth|password|access[_-]?token|private[_-]?key)' -or
        $script:HardeningOriginUrl.Contains('?') -or $script:HardeningOriginUrl.Contains('#') -or
        $script:HardeningOriginUrl -match '^https?://[^/]*@'
    ) { throw "Candidate origin contains forbidden credential-like material." }
    $script:HardeningOriginUrlSha256 = Get-HardeningSha256Text $script:HardeningOriginUrl
    $status = Read-Git @("status", "--porcelain", "--ignored")
    if (-not [string]::IsNullOrWhiteSpace($status)) { throw "Candidate checkout is not clean, including ignored files." }
}

function Write-HardeningExactTextFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Text,
        [Parameter(Mandatory = $true)][string]$Path
    )

    Assert-HardeningNoReparseComponents $Path "Hardening backup"
    $parent = Split-Path -Parent ([System.IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        New-Item -ItemType Directory -Path $parent -Force -ErrorAction Stop | Out-Null
    }
    Assert-HardeningNoReparseComponents $parent "Hardening backup directory"
    $encoding = [System.Text.UTF8Encoding]::new($false)
    $bytes = $encoding.GetBytes($Text)
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
    }
    catch [System.IO.IOException] {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw }
        $existing = [System.IO.File]::ReadAllText($Path, $encoding)
        if ($existing -ne $Text) { throw "Hardening XML backup already exists with different content." }
    }
    Assert-HardeningNoReparseComponents $Path "Hardening backup"
}

function Write-HardeningPreparedRecord {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][object]$Before,
        [Parameter(Mandatory = $true)][object]$BeforeInfo,
        [Parameter(Mandatory = $true)][string]$BackupSha256,
        [Parameter(Mandatory = $true)][string]$BackupFileSha256,
        [Parameter(Mandatory = $true)][string]$AfterSha256
    )
    $core = [ordered]@{
        schema_version = "dawnstrike.capture_task_hardening_prepared.v1"
        status = "PREPARED"
        task_name = $script:HardeningTaskName
        task_path = $Before.task_path
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        original_state = $Before.state
        backup_xml_sha256 = $BackupSha256
        backup_xml_file_sha256 = $BackupFileSha256
        xml_before_sha256 = $Before.xml_sha256
        xml_after_sha256 = $AfterSha256
        action_sha256 = $Before.action_contract_sha256
        trigger_sha256 = $Before.trigger_contract_sha256
        principal_sha256 = $Before.principal_contract_sha256
        settings_sha256 = $Before.settings_contract_sha256
        old_last_task_result = $BeforeInfo.last_task_result
        old_last_run_time = $BeforeInfo.last_run_time
        intended_receipt_path = $ReceiptPath
        rollback_contract = "RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED"
        research_only = $true
        broker_execution_enabled = $false
        prepared_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
    }
    $text = ConvertTo-Json $core -Depth 8
    $record = [ordered]@{}
    foreach ($entry in $core.GetEnumerator()) { $record[$entry.Key] = $entry.Value }
    $record.prepared_record_sha256 = Get-HardeningSha256Text $text
    $recordText = ConvertTo-Json $record -Depth 8
    Write-HardeningExactTextFile -Text $recordText -Path $Path
    $read = [System.IO.File]::ReadAllText($Path, [System.Text.UTF8Encoding]::new($false))
    $parsed = $read | ConvertFrom-Json
    if ([string]$parsed.prepared_record_sha256 -ne (Get-HardeningSha256Text $text)) {
        throw "Prepared hardening record could not be read back exactly."
    }
    return Get-HardeningSha256File $Path
}

function Get-HardeningDirectNodes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlNode]$Parent,
        [Parameter(Mandatory = $true)][string]$LocalName
    )
    return @($Parent.ChildNodes | Where-Object { $_.LocalName -eq $LocalName })
}

function Get-HardeningSingleSection {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlDocument]$Document,
        [Parameter(Mandatory = $true)][string]$LocalName
    )
    $nodes = @($Document.DocumentElement.ChildNodes | Where-Object { $_.LocalName -eq $LocalName })
    if ($nodes.Count -ne 1) { throw "Task XML must contain exactly one $LocalName section." }
    return $nodes[0]
}

function Set-HardeningChildText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlNode]$Parent,
        [Parameter(Mandatory = $true)][string]$LocalName,
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$NamespaceUri
    )
    $nodes = @(Get-HardeningDirectNodes $Parent $LocalName)
    if ($nodes.Count -gt 1) { throw "Task XML contains duplicate $LocalName settings." }
    if ($nodes.Count -eq 0) {
        $node = $Parent.OwnerDocument.CreateElement($LocalName, $NamespaceUri)
        $null = $Parent.AppendChild($node)
    }
    else { $node = $nodes[0] }
    $node.InnerText = $Value
}

function Get-HardeningTaskRecord {
    [CmdletBinding()]
    param([switch]$AllowDisabled)

    $matches = @(Get-ScheduledTask -TaskName $script:HardeningTaskName -ErrorAction SilentlyContinue)
    if ($matches.Count -ne 1) {
        throw "Existing delayed SIP capture task must be present exactly once."
    }
    $task = $matches[0]
    $state = [string]$task.State
    if ($state -notin @("Ready", "Disabled")) {
        throw "Delayed SIP capture task must be quiescent before hardening."
    }
    $taskPath = [string]$task.TaskPath
    if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
    if ($taskPath -ne "\") { throw "Delayed SIP capture task must be registered at the root task path." }
    $xml = [string](Export-ScheduledTask -TaskName $script:HardeningTaskName -TaskPath $taskPath -ErrorAction Stop)
    if ([string]::IsNullOrWhiteSpace($xml)) { throw "Delayed SIP capture task export is empty." }
    $document = [System.Xml.XmlDocument]::new()
    $document.PreserveWhitespace = $true
    try { $document.LoadXml($xml) }
    catch { throw "Delayed SIP capture task export is invalid XML." }
    if ($null -eq $document.DocumentElement -or $document.DocumentElement.LocalName -ne "Task") {
        throw "Delayed SIP capture task export has an invalid root."
    }
    $principals = Get-HardeningSingleSection $document "Principals"
    $triggers = Get-HardeningSingleSection $document "Triggers"
    $settings = Get-HardeningSingleSection $document "Settings"
    $actions = Get-HardeningSingleSection $document "Actions"
    $principalNodes = @(Get-HardeningDirectNodes $principals "Principal")
    if ($principalNodes.Count -ne 1) { throw "Delayed SIP capture XML must contain one Principal." }
    $execNodes = @($actions.ChildNodes | Where-Object { $_.LocalName -eq "Exec" })
    if ($execNodes.Count -ne 1) { throw "Delayed SIP capture XML must contain one Exec action." }
    if (@($triggers.ChildNodes).Count -lt 1) { throw "Delayed SIP capture XML must contain a trigger." }
    $enabledNodes = @(Get-HardeningDirectNodes $settings "Enabled")
    if ($enabledNodes.Count -gt 1) { throw "Delayed SIP capture XML contains duplicate Enabled settings." }
    if ($enabledNodes.Count -eq 1) {
        $enabledValue = ([string]$enabledNodes[0].InnerText).Trim().ToLowerInvariant()
        $xmlState = if ($enabledValue -eq "true") { "Ready" } elseif ($enabledValue -eq "false") { "Disabled" } else { "" }
        if ($xmlState -and $xmlState -ne $state) { throw "Task XML enablement disagrees with scheduler state." }
    }
    return [pscustomobject]@{
        task = $task
        state = $state
        task_path = $taskPath
        xml = $xml
        document = $document
        principals = $principals
        principal = $principalNodes[0]
        triggers = $triggers
        settings = $settings
        actions = $actions
        action_contract_sha256 = Get-HardeningSha256Text ([string]$actions.OuterXml)
        trigger_contract_sha256 = Get-HardeningSha256Text ([string]$triggers.OuterXml)
        principal_contract_sha256 = Get-HardeningSha256Text ([string]$principalNodes[0].OuterXml)
        settings_contract_sha256 = Get-HardeningSha256Text ([string]$settings.OuterXml)
        xml_sha256 = Get-HardeningSha256Text $xml
    }
}

function Get-HardeningTaskInfo {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$TaskPath)

    $info = Get-ScheduledTaskInfo -TaskName $script:HardeningTaskName -TaskPath $TaskPath -ErrorAction Stop
    $lastRun = $null
    if ($null -ne $info.LastRunTime) {
        $lastRunValue = [DateTime]$info.LastRunTime
        if ($lastRunValue -ne [DateTime]::MinValue) {
            $lastRun = $lastRunValue.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
        }
    }
    return [pscustomobject]@{
        last_task_result = [int]$info.LastTaskResult
        last_run_time = $lastRun
    }
}

function Assert-HardeningFreshReplacementInfo {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Info)

    if ($Info.last_task_result -notin @(0, 267011)) {
        throw "Replacement task history was not reset to an acceptable initial result."
    }
    if ($null -ne $Info.last_run_time) {
        throw "Replacement task still has a LastRunTime; history reset is unproven."
    }
}

function Set-HardeningTaskDefinition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlDocument]$Document,
        [Parameter(Mandatory = $true)][System.Xml.XmlNode]$Principal,
        [Parameter(Mandatory = $true)][System.Xml.XmlNode]$Settings,
        [Parameter(Mandatory = $true)][string]$TaskPrincipal
    )
    $namespace = [string]$Document.DocumentElement.NamespaceURI
    Set-HardeningChildText $Principal "UserId" $TaskPrincipal $namespace
    Set-HardeningChildText $Principal "LogonType" "Password" $namespace
    Set-HardeningChildText $Principal "RunLevel" "LeastPrivilege" $namespace
    Set-HardeningChildText $Settings "StartWhenAvailable" "true" $namespace
    Set-HardeningChildText $Settings "WakeToRun" "true" $namespace
    Set-HardeningChildText $Settings "DisallowStartIfOnBatteries" "false" $namespace
    Set-HardeningChildText $Settings "StopIfGoingOnBatteries" "false" $namespace
    Set-HardeningChildText $Settings "ExecutionTimeLimit" "PT3H" $namespace
    Set-HardeningChildText $Settings "MultipleInstancesPolicy" "IgnoreNew" $namespace
    $restart = @(Get-HardeningDirectNodes $Settings "RestartOnFailure")
    if ($restart.Count -gt 1) { throw "Task XML contains duplicate RestartOnFailure settings." }
    if ($restart.Count -eq 0) {
        $restartNode = $Document.CreateElement("RestartOnFailure", $namespace)
        $null = $Settings.AppendChild($restartNode)
    }
    else { $restartNode = $restart[0] }
    Set-HardeningChildText $restartNode "Interval" "PT15M" $namespace
    Set-HardeningChildText $restartNode "Count" "3" $namespace
}

function Assert-HardeningDefinition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Before,
        [Parameter(Mandatory = $true)][string]$AfterXml,
        [Parameter(Mandatory = $true)][string]$ExpectedState
    )
    $afterDocument = [System.Xml.XmlDocument]::new()
    $afterDocument.PreserveWhitespace = $true
    try { $afterDocument.LoadXml($AfterXml) }
    catch { throw "Hardened delayed SIP task XML is invalid." }
    $afterPrincipals = Get-HardeningSingleSection $afterDocument "Principals"
    $afterTriggers = Get-HardeningSingleSection $afterDocument "Triggers"
    $afterSettings = Get-HardeningSingleSection $afterDocument "Settings"
    $afterActions = Get-HardeningSingleSection $afterDocument "Actions"
    if ([string]$afterActions.OuterXml -ne [string]$Before.actions.OuterXml) {
        throw "Hardening changed the capture action or input bindings."
    }
    if ([string]$afterTriggers.OuterXml -ne [string]$Before.triggers.OuterXml) {
        throw "Hardening changed the capture schedule trigger."
    }
    $principalNodes = @(Get-HardeningDirectNodes $afterPrincipals "Principal")
    if ($principalNodes.Count -ne 1) { throw "Hardened XML must contain one Principal." }
    $logon = @(Get-HardeningDirectNodes $principalNodes[0] "LogonType")
    if ($logon.Count -ne 1 -or [string]$logon[0].InnerText -ne "Password") { throw "Hardened XML does not use Password logon." }
    $settingsValues = @{
        StartWhenAvailable = "true"
        WakeToRun = "true"
        DisallowStartIfOnBatteries = "false"
        StopIfGoingOnBatteries = "false"
        ExecutionTimeLimit = "PT3H"
        MultipleInstancesPolicy = "IgnoreNew"
    }
    foreach ($entry in $settingsValues.GetEnumerator()) {
        $nodes = @(Get-HardeningDirectNodes $afterSettings $entry.Key)
        if ($nodes.Count -ne 1 -or [string]$nodes[0].InnerText -ne $entry.Value) {
            throw "Hardened XML has an invalid $($entry.Key) setting."
        }
    }
    foreach ($alias in @("AllowStartIfOnBatteries", "DontStopIfGoingOnBatteries")) {
        if (@(Get-HardeningDirectNodes $afterSettings $alias).Count -ne 0) {
            throw "Hardened XML contains a non-canonical battery setting alias."
        }
    }
    $restart = @(Get-HardeningDirectNodes $afterSettings "RestartOnFailure")
    if ($restart.Count -ne 1) { throw "Hardened XML has no unique RestartOnFailure setting." }
    $interval = @(Get-HardeningDirectNodes $restart[0] "Interval")
    $count = @(Get-HardeningDirectNodes $restart[0] "Count")
    if ($interval.Count -ne 1 -or [string]$interval[0].InnerText -ne "PT15M" -or $count.Count -ne 1 -or [string]$count[0].InnerText -ne "3") {
        throw "Hardened XML has an invalid restart contract."
    }
    $enabled = @(Get-HardeningDirectNodes $afterSettings "Enabled")
    if ($enabled.Count -eq 1) {
        $enabledValue = ([string]$enabled[0].InnerText).Trim().ToLowerInvariant()
        $actualState = if ($enabledValue -eq "true") { "Ready" } elseif ($enabledValue -eq "false") { "Disabled" } else { "" }
        if ($actualState -and $actualState -ne $ExpectedState) { throw "Hardening changed task enablement." }
    }
    return [pscustomobject]@{
        document = $afterDocument
        principals = $afterPrincipals
        triggers = $afterTriggers
        settings = $afterSettings
        actions = $afterActions
        xml_sha256 = Get-HardeningSha256Text $AfterXml
        principal_contract_sha256 = Get-HardeningSha256Text ([string]$principalNodes[0].OuterXml)
        trigger_contract_sha256 = Get-HardeningSha256Text ([string]$afterTriggers.OuterXml)
        settings_contract_sha256 = Get-HardeningSha256Text ([string]$afterSettings.OuterXml)
    }
}

function Restore-HardeningExactTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][string]$TaskPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedState,
        [string]$User,
        [string]$Password
    )
    $registerArgs = @{
        TaskName = $script:HardeningTaskName
        TaskPath = $TaskPath
        Xml = $Xml
        Force = $true
        ErrorAction = "Stop"
    }
    if (-not [string]::IsNullOrWhiteSpace($User)) {
        if ([string]::IsNullOrWhiteSpace($Password)) { throw "Exact rollback credential is incomplete." }
        $registerArgs.User = $User
        $registerArgs.Password = $Password
    }
    Register-ScheduledTask @registerArgs | Out-Null
    $restored = Get-HardeningTaskRecord -AllowDisabled
    if ($restored.xml_sha256 -ne $ExpectedSha256) { throw "Exact task XML rollback could not be proven." }
    if ($ExpectedState -eq "Ready" -and $restored.state -eq "Disabled") {
        Enable-ScheduledTask -TaskName $script:HardeningTaskName -TaskPath $TaskPath -ErrorAction Stop | Out-Null
    }
    elseif ($ExpectedState -eq "Disabled" -and $restored.state -eq "Ready") {
        Disable-ScheduledTask -TaskName $script:HardeningTaskName -TaskPath $TaskPath -ErrorAction Stop | Out-Null
    }
    $final = Get-HardeningTaskRecord -AllowDisabled
    if ($final.xml_sha256 -ne $ExpectedSha256 -or $final.state -ne $ExpectedState) {
        throw "Exact task XML rollback or enablement could not be proven."
    }
    return $final
}

function Invoke-HardeningRollback {
    if ([string]::IsNullOrWhiteSpace($BackupXmlPath)) {
        throw "Rollback requires -BackupXmlPath pointing to the exact exported task XML."
    }
    Assert-HardeningNoReparseComponents $BackupXmlPath "Rollback XML"
    if (-not (Test-Path -LiteralPath $BackupXmlPath -PathType Leaf)) { throw "Rollback XML is missing." }
    $xml = [System.IO.File]::ReadAllText($BackupXmlPath, [System.Text.UTF8Encoding]::new($false))
    $document = [System.Xml.XmlDocument]::new()
    $document.PreserveWhitespace = $true
    try { $document.LoadXml($xml) }
    catch { throw "Rollback XML is invalid." }
    $settings = Get-HardeningSingleSection $document "Settings"
    $enabled = @(Get-HardeningDirectNodes $settings "Enabled")
    if ($enabled.Count -ne 1 -or ([string]$enabled[0].InnerText).Trim().ToLowerInvariant() -notin @("true", "false")) {
        throw "Rollback XML has no unambiguous enablement state."
    }
    $expectedState = if (([string]$enabled[0].InnerText).Trim().ToLowerInvariant() -eq "true") { "Ready" } else { "Disabled" }
    $task = Get-HardeningTaskRecord -AllowDisabled
    $expectedSha = Get-HardeningSha256Text $xml
    $rollbackUser = $null
    $rollbackPassword = $null
    $rollbackLogon = @($document.SelectNodes("//*[local-name()='Principal']/*[local-name()='LogonType']"))
    if ($rollbackLogon.Count -eq 1 -and [string]$rollbackLogon[0].InnerText -eq "Password") {
        if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
            throw "Exact rollback requires the locally prompted RunAsCredential for a Password task."
        }
        $rollbackUser = $RunAsCredential.UserName
        $rollbackPassword = $RunAsCredential.GetNetworkCredential().Password
        if ([string]::IsNullOrWhiteSpace($rollbackPassword)) { throw "Rollback credential is incomplete." }
    }
    $result = Restore-HardeningExactTask -Xml $xml -TaskPath $task.task_path -ExpectedSha256 $expectedSha -ExpectedState $expectedState -User $rollbackUser -Password $rollbackPassword
    Write-Output (ConvertTo-Json ([ordered]@{
        status = "ROLLED_BACK_EXACT"
        task_name = $script:HardeningTaskName
        task_path = $task.task_path
        state = $result.state
        xml_sha256 = $result.xml_sha256
        research_only = $true
        broker_execution_enabled = $false
    }) -Compress)
}

if ($Rollback) {
    throw "Standalone rollback is disabled; use the receipt-bound in-process compensation path."
}

if ($TaskName -ne $script:HardeningTaskName) {
    throw "Only the governed delayed SIP capture task may be hardened."
}
if ([string]::IsNullOrWhiteSpace($CandidateSha) -or [string]::IsNullOrWhiteSpace($CandidateTree)) {
    throw "CandidateSha and CandidateTree are required to bind the hardening receipt."
}
        Assert-HardeningCandidateIdentity -ExpectedSha $CandidateSha -ExpectedTree $CandidateTree -RefreshOrigin
if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
    throw "RunAsCredential is required; invoke this script with a locally prompted Get-Credential value."
}
. (Join-Path $PSScriptRoot "resolve_dawnstrike_task_principal.ps1")
$taskPrincipal = Resolve-DawnstrikeTaskPrincipal -Credential $RunAsCredential
$taskPassword = $RunAsCredential.GetNetworkCredential().Password
if ([string]::IsNullOrWhiteSpace($taskPassword)) { throw "RunAsCredential must contain a non-empty Windows password." }
$contractScript = Join-Path $PSScriptRoot "capture_task_hardening_contract.py"
if (-not (Test-Path -LiteralPath $contractScript -PathType Leaf)) { throw "Hardening receipt contract is missing." }

$hardeningLock = Enter-HardeningActivationLock -StateRoot $StateRoot
try {
    # The task and its scheduler history are re-read only after the shared
    # activation lock is held; no stale pre-lock observation may be replaced.
    Assert-HardeningCandidateIdentity -ExpectedSha $CandidateSha -ExpectedTree $CandidateTree
    $before = Get-HardeningTaskRecord
    $beforeInfo = Get-HardeningTaskInfo -TaskPath $before.task_path
if ([string]::IsNullOrWhiteSpace($BackupRoot)) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $BackupRoot = Join-Path $StateRoot ("scheduler-backups\capture-hardening-" + $stamp)
}
$stateSchedulerRoot = ([System.IO.Path]::GetFullPath((Join-Path $StateRoot "scheduler-backups"))).TrimEnd('\') + '\'
if (-not [System.IO.Path]::GetFullPath($BackupRoot).StartsWith($stateSchedulerRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Hardening BackupRoot must be inside StateRoot scheduler-backups."
}
Assert-HardeningNoReparseComponents $BackupRoot "Hardening backup root"
New-Item -ItemType Directory -Path $BackupRoot -Force -ErrorAction Stop | Out-Null
Assert-HardeningNoReparseComponents $BackupRoot "Hardening backup root"
$backupName = "Dawnstrike_Delayed_SIP_Capture.xml"
$backupXmlPath = Join-Path $BackupRoot $backupName
Write-HardeningExactTextFile -Text $before.xml -Path $backupXmlPath
$backupXmlSha = Get-HardeningSha256Text $before.xml
$backupXmlFileSha = Get-HardeningSha256File $backupXmlPath
if ($backupXmlSha -ne $before.xml_sha256) { throw "Exact task XML backup identity could not be proven." }

Set-HardeningTaskDefinition -Document $before.document -Principal $before.principal -Settings $before.settings -TaskPrincipal $taskPrincipal
# A replacement must be born disabled.  The activation/rebind seam is the only
# governed path allowed to enable this auxiliary task for an exact candidate.
$settingsForReplacement = Get-HardeningSingleSection $before.document "Settings"
Set-HardeningChildText $settingsForReplacement "Enabled" "false" ([string]$before.document.DocumentElement.NamespaceURI)
$afterXml = [string]$before.document.OuterXml
$after = Assert-HardeningDefinition -Before $before -AfterXml $afterXml -ExpectedState "Disabled"
if ($after.xml_sha256 -eq $before.xml_sha256) { throw "Task hardening produced no definition change." }
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $StateRoot ("receipts\capture-task\capture-task-hardening-" + $before.xml_sha256 + ".json")
}
Assert-HardeningNoReparseComponents $ReceiptPath "Hardening receipt"
$receiptRoot = [System.IO.Path]::GetFullPath((Join-Path $StateRoot "receipts\capture-task")).TrimEnd('\') + '\'
if (-not [System.IO.Path]::GetFullPath($ReceiptPath).StartsWith($receiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Hardening receipt must be inside the governed capture-task receipt root."
}
$preparedPath = Join-Path $BackupRoot "capture-task-hardening-prepared.json"
    $preparedRecordSha = Write-HardeningPreparedRecord `
    -Path $preparedPath `
    -Before $before `
    -BeforeInfo $beforeInfo `
    -BackupSha256 $backupXmlSha `
        -BackupFileSha256 $backupXmlFileSha `
        -AfterSha256 $after.xml_sha256
    $stateRootFull = ([System.IO.Path]::GetFullPath($StateRoot)).TrimEnd('\')
    $statePrefix = $stateRootFull + '\'
    $backupFull = [System.IO.Path]::GetFullPath($backupXmlPath)
    $preparedFull = [System.IO.Path]::GetFullPath($preparedPath)
    foreach ($durablePath in @($backupFull, $preparedFull)) {
        if (-not $durablePath.StartsWith($statePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Hardening durable record escaped StateRoot."
        }
        Assert-HardeningNoReparseComponents $durablePath "Hardening durable record"
    }
    $backupRelativePath = (($backupFull.Substring($statePrefix.Length)) -replace '\\','/')
    $preparedRelativePath = (($preparedFull.Substring($statePrefix.Length)) -replace '\\','/')

    Unregister-ScheduledTask `
        -TaskName $script:HardeningTaskName `
        -TaskPath $before.task_path `
        -Confirm:$false `
        -ErrorAction Stop
    Register-ScheduledTask `
        -TaskName $script:HardeningTaskName `
        -TaskPath $before.task_path `
        -Xml $afterXml `
        -User $taskPrincipal `
        -Password $taskPassword `
        -Force `
        -ErrorAction Stop | Out-Null
    $currentTask = Get-HardeningTaskRecord -AllowDisabled
    if ($currentTask.state -ne "Disabled") { throw "Replacement task was not created Disabled." }
    $verified = Assert-HardeningDefinition -Before $before -AfterXml $currentTask.xml -ExpectedState "Disabled"
    if ($currentTask.xml_sha256 -ne $verified.xml_sha256) { throw "Hardened task XML identity changed during verification." }
    $replacementInfo = Get-HardeningTaskInfo -TaskPath $currentTask.task_path
    Assert-HardeningFreshReplacementInfo -Info $replacementInfo

    $payload = [ordered]@{
        schema_version = "dawnstrike.capture_task_hardening_receipt.v1"
        status = "COMPLETE"
        task_name = $script:HardeningTaskName
        task_path = $before.task_path
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        original_state = $before.state
        final_state = "Disabled"
        backup_name = $backupName
        backup_relative_path = $backupRelativePath
        prepared_relative_path = $preparedRelativePath
        backup_xml_sha256 = $backupXmlSha
        backup_xml_file_sha256 = $backupXmlFileSha
        xml_before_sha256 = $before.xml_sha256
        xml_after_sha256 = $currentTask.xml_sha256
        action_sha256 = $before.action_contract_sha256
        trigger_sha256 = $before.trigger_contract_sha256
        principal_before_sha256 = $before.principal_contract_sha256
        principal_after_sha256 = $verified.principal_contract_sha256
        settings_before_sha256 = $before.settings_contract_sha256
        settings_after_sha256 = $verified.settings_contract_sha256
        prepared_record_sha256 = $preparedRecordSha
        origin_main_refreshed_at_utc = $script:HardeningOriginRefreshUtc
        origin_url = $script:HardeningOriginUrl
        origin_url_sha256 = $script:HardeningOriginUrlSha256
        old_last_task_result = $beforeInfo.last_task_result
        old_last_run_time = $beforeInfo.last_run_time
        new_last_task_result = $replacementInfo.last_task_result
        new_last_run_time = $replacementInfo.last_run_time
        history_reset_proven = $true
        changed_fields = @("principal", "settings")
        preserved_action = $true
        preserved_trigger = $true
        preserved_input_bindings = $true
        logon_type = "Password"
        network_capable = $true
        start_when_available = $true
        wake_to_run = $true
        battery_safe = $true
        restart_count = 3
        restart_interval = "PT15M"
        execution_time_limit = "PT3H"
        multiple_instances = "IgnoreNew"
        rollback_contract = "RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED"
        research_only = $true
        broker_execution_enabled = $false
        completed_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
    }
    $inputPath = Join-Path $BackupRoot (".hardening-receipt-" + [Guid]::NewGuid().ToString("N") + ".json")
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($inputPath, (ConvertTo-Json $payload -Depth 8), $encoding)
    try {
        $python = @(Get-Command py.exe -CommandType Application -ErrorAction Stop)[0].Source
        & $python -3.13 -u $contractScript seal-hardening --input $inputPath --output $ReceiptPath | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Hardening receipt contract rejected the payload." }
    }
    finally {
        Remove-Item -LiteralPath $inputPath -Force -ErrorAction SilentlyContinue
    }
    Assert-HardeningNoReparseComponents $ReceiptPath "Hardening receipt"
    if (-not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) { throw "Hardening receipt was not sealed." }
    Assert-HardeningCandidateIdentity -ExpectedSha $CandidateSha -ExpectedTree $CandidateTree
    Write-Output ([System.IO.File]::ReadAllText($ReceiptPath, $encoding).Trim())
}
catch {
    try {
        $rollbackUser = $null
        $rollbackPassword = $null
        $beforeLogon = @($before.document.SelectNodes("//*[local-name()='Principal']/*[local-name()='LogonType']"))
        if ($beforeLogon.Count -eq 1 -and [string]$beforeLogon[0].InnerText -eq "Password") {
            $rollbackUser = $taskPrincipal
            $rollbackPassword = $taskPassword
        }
        $null = Restore-HardeningExactTask `
            -Xml $before.xml `
            -TaskPath $before.task_path `
            -ExpectedSha256 $before.xml_sha256 `
            -ExpectedState $before.state `
            -User $rollbackUser `
            -Password $rollbackPassword
    }
    catch {
        throw "Delayed SIP task hardening failed and exact rollback could not be proven; operator recovery is required."
    }
    throw "Delayed SIP task hardening failed; exact original XML and enablement were restored."
}
finally {
    Exit-HardeningActivationLock -Lock $hardeningLock
}
