[CmdletBinding()]
param(
    [string]$TaskName = "Dawnstrike Delayed SIP Capture",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
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
. (Join-Path $PSScriptRoot "capture_task_safety.ps1")
$hardeningRuntimeRoot = $RuntimeRoot
$hardeningStateRoot = $StateRoot
$hardeningCandidateSha = $CandidateSha
$hardeningCandidateTree = $CandidateTree
$hardeningCredential = $RunAsCredential
$RuntimeRoot = $hardeningRuntimeRoot
$StateRoot = $hardeningStateRoot
$CandidateSha = $hardeningCandidateSha
$CandidateTree = $hardeningCandidateTree
$RunAsCredential = $hardeningCredential
# The shared dawnstrike-runtime-activation.lock and Get-Credential boundary
# remain explicit governance markers; $broker_execution_enabled = $false is
# part of every sealed receipt. Unregister-ScheduledTask is forbidden.

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

. (Join-Path $PSScriptRoot "capture_task_hardening_recovery.ps1")

function Get-HardeningInterpreterDeclaration {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$CandidateRoot)
    $path = Join-Path $CandidateRoot "config\state_preparation_contract.json"
    Assert-HardeningNoReparseComponents $path "Capture interpreter declaration"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Capture interpreter declaration is missing." }
    try {
        $raw = [System.IO.File]::ReadAllText($path, [System.Text.UTF8Encoding]::new($false))
        # ConvertFrom-Json silently accepts duplicate members.  Walk JSON
        # string tokens lexically and inspect only tokens followed by a colon;
        # in valid JSON those are object keys, including nested objects.  This
        # avoids treating a colon embedded in a hostile string value as a key.
        $seenKeys = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
        $inString = $false
        $escaped = $false
        $keyStart = -1
        for ($jsonIndex = 0; $jsonIndex -lt $raw.Length; $jsonIndex++) {
            $character = $raw[$jsonIndex]
            if (-not $inString) {
                if ($character -eq '"') { $inString = $true; $keyStart = $jsonIndex + 1 }
                continue
            }
            if ($escaped) { $escaped = $false; continue }
            if ($character -eq '\') { $escaped = $true; continue }
            if ($character -ne '"') { continue }
            $encodedKey = $raw.Substring($keyStart, $jsonIndex - $keyStart)
            $lookahead = $jsonIndex + 1
            while ($lookahead -lt $raw.Length -and [char]::IsWhiteSpace($raw[$lookahead])) { $lookahead++ }
            if ($lookahead -lt $raw.Length -and $raw[$lookahead] -eq ':') {
                $decodedKey = [regex]::Replace($encodedKey, '\\u([0-9a-fA-F]{4})', { param($m) ([char][Convert]::ToInt32($m.Groups[1].Value, 16)) })
                $decodedKey = $decodedKey.Replace('\"', '"').Replace('\\', '\').Replace('\/', '/')
                if (-not $seenKeys.Add($decodedKey)) { throw "duplicate declaration field" }
            }
            $inString = $false
            $keyStart = -1
        }
        if ($inString -or $escaped) { throw "unterminated JSON string" }
        $value = $raw | ConvertFrom-Json
    }
    catch { throw "Capture interpreter declaration is invalid JSON." }
    $required = @("schema_version", "sidecar_contract", "sidecar_version", "legacy_schema_marker", "required_before_activation", "research_only", "broker_execution_enabled", "capture_interpreter_path", "capture_interpreter_version", "capture_interpreter_sha256", "capture_interpreter_signer_subject", "capture_interpreter_signer_thumbprint")
    $actual = @($value.psobject.Properties.Name)
    if ($actual.Count -ne $required.Count -or @($required | Where-Object { $_ -notin $actual }).Count -ne 0 -or @($actual | Where-Object { $_ -notin $required }).Count -ne 0) { throw "Capture interpreter declaration has an unexpected or missing field." }
    if ([string]$value.schema_version -ne "dawnstrike.state_preparation_contract.v1" -or [string]$value.sidecar_contract -ne "dawnstrike.account_capture_trial_sidecar.v1" -or [int]$value.sidecar_version -ne 1 -or [int]$value.legacy_schema_marker -ne 30 -or $value.required_before_activation -ne $true -or $value.research_only -ne $true -or $value.broker_execution_enabled -ne $false) { throw "Capture interpreter declaration violates the governed sidecar contract." }
    if (-not [System.IO.Path]::IsPathRooted([string]$value.capture_interpreter_path) -or [string]$value.capture_interpreter_path -notmatch '\\python\.exe$' -or [string]$value.capture_interpreter_version -notmatch '^3\.13\.\d+$' -or [string]$value.capture_interpreter_sha256 -notmatch '^[0-9a-f]{64}$' -or [string]$value.capture_interpreter_signer_thumbprint -notmatch '^[0-9A-F]{40}$' -or [string]$value.capture_interpreter_signer_subject -ne "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US") { throw "Capture interpreter declaration identity is invalid." }
    return $value
}

function Assert-HardeningInterpreterIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Declaration)
    $path = Assert-DawnstrikeCaptureRegularPath ([string]$Declaration.capture_interpreter_path) "Capture interpreter"
    $expectedPath = [System.IO.Path]::GetFullPath([string]$Declaration.capture_interpreter_path)
    if (-not [string]::Equals($path, $expectedPath, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Capture interpreter path could not be canonicalized." }
    $sha = Get-DawnstrikeCaptureFileSha256 $path
    if ($sha -ne [string]$Declaration.capture_interpreter_sha256) { throw "Capture interpreter hash does not match the declaration." }
    $signature = Get-AuthenticodeSignature -LiteralPath $path -ErrorAction Stop
    if ([string]$signature.Status -ne "Valid" -or $null -eq $signature.SignerCertificate -or [string]$signature.SignerCertificate.Subject -ne [string]$Declaration.capture_interpreter_signer_subject -or [string]$signature.SignerCertificate.Thumbprint -ne [string]$Declaration.capture_interpreter_signer_thumbprint) { throw "Capture interpreter Authenticode identity is not approved." }
    $version = @(& $path -I -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $version.Count -ne 1 -or [string]$version[0] -ne [string]$Declaration.capture_interpreter_version) { throw "Capture interpreter version does not match the declaration." }
    return [pscustomobject]@{ path = $path; sha256 = $sha; version = [string]$Declaration.capture_interpreter_version; signer_subject = [string]$Declaration.capture_interpreter_signer_subject; signer_thumbprint = [string]$Declaration.capture_interpreter_signer_thumbprint }
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
    if ((Read-Git @("rev-parse", "HEAD")) -cne $ExpectedSha) { throw "Candidate HEAD is not the requested SHA." }
    if ((Read-Git @("rev-parse", "HEAD^{tree}")) -cne $ExpectedTree) { throw "Candidate tree is not the requested tree." }
    if ((Read-Git @("rev-parse", "refs/remotes/origin/main")) -cne $ExpectedSha) { throw "Candidate is not the exact clean origin/main SHA." }
    $script:HardeningOriginUrl = (Read-Git @("remote", "get-url", "origin"))
    if (
        [string]::IsNullOrWhiteSpace($script:HardeningOriginUrl) -or
        $script:HardeningOriginUrl -match '(gh[pousr]_|oauth|password|access[_-]?token|private[_-]?key)' -or
        $script:HardeningOriginUrl.Contains('?') -or $script:HardeningOriginUrl.Contains('#') -or
        $script:HardeningOriginUrl -match '^https?://[^/]*@'
    ) { throw "Candidate origin contains forbidden credential-like material." }
    $script:HardeningOriginUrlSha256 = Get-HardeningSha256Text $script:HardeningOriginUrl
    # Ignored caches and governed build evidence are not source changes and
    # must not make a candidate unusable.  Tracked and untracked bytes remain
    # a hard boundary, while every executed runner is blob-bound below.
    $status = Read-Git @("status", "--porcelain", "--untracked-files=all")
    if (-not [string]::IsNullOrWhiteSpace($status)) { throw "Candidate checkout has tracked or untracked changes." }
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
        [Parameter(Mandatory = $true)][string]$BackupPath,
        [Parameter(Mandatory = $true)][string]$AfterSha256,
        [Parameter(Mandatory = $true)][string]$AfterActionSha256,
        [Parameter(Mandatory = $true)][object]$Lock,
        [Parameter(Mandatory = $true)][object]$RuntimeIdentity,
        [Parameter(Mandatory = $true)][object]$InterpreterIdentity,
        [Parameter(Mandatory = $true)][string]$RunnerBeforeSha256,
        [Parameter(Mandatory = $true)][string]$RunnerTargetSha256,
        [Parameter(Mandatory = $true)][string]$ContractScript,
        [Parameter(Mandatory = $true)][ValidateSet("LEGACY_MIGRATION", "CANONICAL_REPIN")][string]$InputStage
    )
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        $preparedOutput = & $InterpreterIdentity.path -I -B $ContractScript verify-prepared --prepared $Path 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Existing PREPARED record failed strict validation." }
        $existingPrepared = (($preparedOutput -join "") | ConvertFrom-Json)
        if ($existingPrepared.candidate_sha -cne $CandidateSha -or $existingPrepared.candidate_tree -cne $CandidateTree -or
            $existingPrepared.task_path -cne $Before.task_path -or $existingPrepared.xml_before_sha256 -cne $Before.xml_sha256 -or
            $existingPrepared.backup_path -cne $BackupPath -or $existingPrepared.backup_xml_sha256 -cne $BackupSha256 -or $existingPrepared.backup_xml_file_sha256 -cne $BackupFileSha256 -or
            $existingPrepared.xml_after_sha256 -cne $AfterSha256 -or $existingPrepared.action_before_sha256 -cne $Before.action_contract_sha256 -or
            $existingPrepared.action_after_sha256 -cne $AfterActionSha256 -or $existingPrepared.runtime_head -cne $RuntimeIdentity.head -or
            $existingPrepared.runtime_tree -cne $RuntimeIdentity.tree -or $existingPrepared.runtime_origin -cne $RuntimeIdentity.origin -or
            $existingPrepared.lock_token -cne $Lock.token -or $existingPrepared.lock_bytes_sha256 -cne $Lock.bytes_sha256 -or
            $existingPrepared.interpreter_sha256 -cne $InterpreterIdentity.sha256 -or
            $existingPrepared.interpreter_signer_thumbprint -cne $InterpreterIdentity.signer_thumbprint -or
            $existingPrepared.runner_before_sha256 -cne $RunnerBeforeSha256 -or $existingPrepared.runner_target_sha256 -cne $RunnerTargetSha256 -or
            $existingPrepared.intended_receipt_path -cne $ReceiptPath) {
            throw "Existing PREPARED record does not match the exact hardening invocation."
        }
        return Get-HardeningSha256File $Path
    }
    $core = [ordered]@{
        schema_version = "dawnstrike.capture_task_hardening_prepared.v2"
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
        action_before_sha256 = $Before.action_contract_sha256
        action_after_sha256 = $AfterActionSha256
        runtime_head = $RuntimeIdentity.head
        runtime_tree = $RuntimeIdentity.tree
        runtime_origin = $RuntimeIdentity.origin
        runtime_origin_sha256 = $RuntimeIdentity.origin_sha256
        backup_path = $BackupPath
        lock_token = $Lock.token
        lock_bytes_sha256 = $Lock.bytes_sha256
        interpreter_path = $InterpreterIdentity.path
        interpreter_sha256 = $InterpreterIdentity.sha256
        interpreter_version = $InterpreterIdentity.version
        interpreter_signer_subject = $InterpreterIdentity.signer_subject
        interpreter_signer_thumbprint = $InterpreterIdentity.signer_thumbprint
        runner_before_sha256 = $RunnerBeforeSha256
        runner_target_sha256 = $RunnerTargetSha256
        old_last_task_result = $BeforeInfo.last_task_result
        old_last_run_time = $BeforeInfo.last_run_time
        intended_receipt_path = $ReceiptPath
        rollback_contract = "RESTORE_EXACT_XML_AND_ENABLEMENT_HISTORY_NOT_RESTORED"
        research_only = $true
        broker_execution_enabled = $false
        prepared_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
        input_stage = $InputStage
    }
    $inputPath = Join-Path (Split-Path -Parent $Path) (".prepared-input-" + [Guid]::NewGuid().ToString("N") + ".json")
    try {
        Write-HardeningExactTextFile -Text (ConvertTo-Json $core -Depth 8) -Path $inputPath
        & $InterpreterIdentity.path -I -B $ContractScript seal-prepared --input $inputPath --output $Path 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Strict PREPARED contract rejected the record." }
        & $InterpreterIdentity.path -I -B $ContractScript verify-prepared --prepared $Path 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Strict PREPARED record read-back failed." }
    }
    finally {
        Remove-Item -LiteralPath $inputPath -Force -ErrorAction SilentlyContinue
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

    if ($Info.last_task_result -is [bool] -or $Info.last_task_result -isnot [int]) {
        throw "Replacement task history result is not a valid integer."
    }
    if ($null -ne $Info.last_run_time -and ([string]$Info.last_run_time -notmatch '^\d{4}-\d{2}-\d{2}T')) {
        throw "Replacement task history timestamp is invalid."
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
    # Rebuild the governed Settings section instead of mutating legacy idle
    # behavior in place.  This is the hardened contract and is intentionally
    # born Disabled; the rebind lane is the only enablement seam.
    while ($Settings.HasChildNodes) { $Settings.RemoveChild($Settings.FirstChild) | Out-Null }
    $settingsValues = [ordered]@{
        "Enabled" = "false"
        "StartWhenAvailable" = "true"
        "WakeToRun" = "true"
        # Legacy aliases AllowStartIfOnBatteries and DontStopIfGoingOnBatteries
        # are intentionally not emitted in the rebuilt hardened section.
        "DisallowStartIfOnBatteries" = "false"
        "StopIfGoingOnBatteries" = "false"
        "ExecutionTimeLimit" = "PT3H"
        "MultipleInstancesPolicy" = "IgnoreNew"
    }
    foreach ($entry in $settingsValues.GetEnumerator()) { Set-HardeningChildText $Settings $entry.Key $entry.Value $namespace }
    $restartNode = $Document.CreateElement("RestartOnFailure", $namespace)
    $null = $Settings.AppendChild($restartNode)
    Set-HardeningChildText $restartNode "Interval" "PT15M" $namespace
    Set-HardeningChildText $restartNode "Count" "3" $namespace
    Set-HardeningChildText $Settings "UseUnifiedSchedulingEngine" "true" $namespace
}

function Get-HardeningRuntimeIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)
    $git = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0].Source
    $read = { param([string[]]$Arguments) $value = & $git -C $Root @Arguments 2>$null; if ($LASTEXITCODE -ne 0) { throw "Live runtime identity command failed." }; return ([string]($value -join "")).Trim() }
    if (-not (Test-Path -LiteralPath (Join-Path $Root ".git") -PathType Container)) { throw "Live RuntimeRoot must be a self-contained Git checkout." }
    $head = & $read @("rev-parse", "HEAD")
    $tree = & $read @("rev-parse", "HEAD^{tree}")
    $origin = Get-DawnstrikeCanonicalOrigin ((& $read @("remote", "get-url", "origin")).Trim())
    $status = & $read @("status", "--porcelain", "--untracked-files=all")
    if ($head -notmatch '^[0-9a-fA-F]{40}$' -or $tree -notmatch '^[0-9a-fA-F]{40}$' -or $status) { throw "Live RuntimeRoot has tracked or untracked working-tree changes." }
    $runner = Join-Path $Root "scripts\run_daily_intraday_capture.py"
    $runnerWorktreeBlob = & $read @("hash-object", "--", "scripts/run_daily_intraday_capture.py")
    $runnerHeadBlob = & $read @("rev-parse", ("HEAD:scripts/run_daily_intraday_capture.py"))
    if ($runnerWorktreeBlob -notmatch '^[0-9a-fA-F]{40}$' -or $runnerHeadBlob -notmatch '^[0-9a-fA-F]{40}$' -or $runnerWorktreeBlob -ne $runnerHeadBlob) { throw "Live RuntimeRoot runner bytes do not match the exact HEAD blob." }
    return [pscustomobject]@{ head = $head; tree = $tree; origin = $origin; origin_sha256 = Get-HardeningSha256Text $origin; runner_blob = $runnerHeadBlob; runner_sha256 = Get-HardeningSha256File $runner }
}

function Set-HardeningDirectCaptureAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.Xml.XmlDocument]$Document,
        [Parameter(Mandatory = $true)][System.Xml.XmlNode]$Actions,
        [Parameter(Mandatory = $true)][string]$InterpreterPath,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$BytecodePrefix
    )
    $exec = @(Get-HardeningDirectNodes $Actions "Exec")
    if ($exec.Count -ne 1) { throw "Capture task must contain exactly one Exec action." }
    $command = @(Get-HardeningDirectNodes $exec[0] "Command")
    $arguments = @(Get-HardeningDirectNodes $exec[0] "Arguments")
    if ($command.Count -ne 1 -or $arguments.Count -ne 1) { throw "Capture action command contract is incomplete." }
    $tokens = @([regex]::Matches([string]$arguments[0].InnerText, '"(?<value>[^"\r\n]*)"') | ForEach-Object { [string]$_.Groups["value"].Value })
    if ($tokens.Count -eq 0 -or (($tokens | ForEach-Object { '"' + $_ + '"' }) -join ' ') -ne [string]$arguments[0].InnerText) { throw "Capture action arguments are not in canonical quoted form." }
    $initialRunnerIndex = if ($tokens[0] -eq "-I" -and $tokens.Count -ge 6 -and $tokens[1] -eq "-B") { 5 } else { 2 }
    if ($tokens.Count -le $initialRunnerIndex -or $tokens[$initialRunnerIndex] -notmatch 'run_daily_intraday_capture\.py$') { throw "Capture action runner binding is invalid." }
    if ($tokens[0] -eq "-3.13") {
        $tokens = @("-I", "-B", "-X", ("pycache_prefix=" + [System.IO.Path]::GetFullPath($BytecodePrefix)), "-u") + $tokens[2..($tokens.Count - 1)]
    }
    elseif ($tokens[0] -eq "-I" -and $tokens.Count -ge 5 -and $tokens[1] -eq "-B" -and $tokens[2] -eq "-X" -and $tokens[3] -like "pycache_prefix=*") {
        if ([System.IO.Path]::GetFullPath($tokens[3].Substring(15)) -ine [System.IO.Path]::GetFullPath($BytecodePrefix) -or $tokens[4] -ne "-u") { throw "Capture action bytecode prefix is not candidate-bound." }
    }
    elseif ($tokens[0] -eq "-I" -and $tokens[1] -eq "-u") {
        $tokens = @("-I", "-B", "-X", ("pycache_prefix=" + [System.IO.Path]::GetFullPath($BytecodePrefix)), "-u") + $tokens[2..($tokens.Count - 1)]
    }
    else { throw "Capture action has an unsafe interpreter prefix." }
    if ($tokens.Count -lt 8 -or $tokens[6] -ne "--candidate-sha" -or $tokens[7] -notmatch '^[0-9a-f]{40}$') { throw "Capture migration action candidate binding is invalid." }
    $tokens[7] = $CandidateSha
    $command[0].InnerText = $InterpreterPath
    $arguments[0].InnerText = (($tokens | ForEach-Object { '"' + $_ + '"' }) -join ' ')
    return [string]$Actions.OuterXml
}

function Assert-HardeningDefinition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Before,
        [Parameter(Mandatory = $true)][string]$AfterXml,
        [Parameter(Mandatory = $true)][string]$ExpectedState,
        [string]$ExpectedActionXml = ""
    )
    $afterDocument = [System.Xml.XmlDocument]::new()
    $afterDocument.PreserveWhitespace = $true
    try { $afterDocument.LoadXml($AfterXml) }
    catch { throw "Hardened delayed SIP task XML is invalid." }
    $afterPrincipals = Get-HardeningSingleSection $afterDocument "Principals"
    $afterTriggers = Get-HardeningSingleSection $afterDocument "Triggers"
    $afterSettings = Get-HardeningSingleSection $afterDocument "Settings"
    $afterActions = Get-HardeningSingleSection $afterDocument "Actions"
    $expectedAction = if ($ExpectedActionXml) { $ExpectedActionXml } else { [string]$Before.actions.OuterXml }
    if ([string]$afterActions.OuterXml -ne $expectedAction) {
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
        UseUnifiedSchedulingEngine = "true"
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
$hardeningCandidateRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")
$lockInterpreter = Get-DawnstrikeApprovedLockInterpreter
$declarationContract = Join-Path $hardeningCandidateRoot "scripts\runtime_activation_contract.py"
if (-not (Test-Path -LiteralPath $declarationContract -PathType Leaf)) { throw "State preparation declaration contract is missing." }
$declarationCheck = & $lockInterpreter.path -I -B $declarationContract validate-state-preparation-declaration `
    --input (Join-Path $hardeningCandidateRoot "config\state_preparation_contract.json") 2>$null
if ($LASTEXITCODE -ne 0) { throw "State preparation declaration failed strict duplicate-aware validation." }
$interpreterDeclaration = Get-HardeningInterpreterDeclaration -CandidateRoot $hardeningCandidateRoot
$interpreterIdentity = Assert-HardeningInterpreterIdentity -Declaration $interpreterDeclaration
$runtimeRootResolved = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
Assert-HardeningNoReparseComponents $runtimeRootResolved "Live RuntimeRoot"
$runtimeIdentity = Get-HardeningRuntimeIdentity -Root $runtimeRootResolved
if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
    throw "RunAsCredential is required; invoke this script with a locally prompted Get-Credential value."
}
. (Join-Path $PSScriptRoot "resolve_dawnstrike_task_principal.ps1")
$taskPrincipal = Resolve-DawnstrikeTaskPrincipal -Credential $RunAsCredential
$taskPassword = $RunAsCredential.GetNetworkCredential().Password
if ([string]::IsNullOrWhiteSpace($taskPassword)) { throw "RunAsCredential must contain a non-empty Windows password." }
$contractScript = Join-Path $PSScriptRoot "capture_task_hardening_contract.py"
if (-not (Test-Path -LiteralPath $contractScript -PathType Leaf)) { throw "Hardening receipt contract is missing." }
. (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")
$lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $script:HardeningOriginUrl
$lockInterpreter = Get-DawnstrikeApprovedLockInterpreter

$preparedPath = Join-Path $StateRoot ("receipts\capture-task\capture-task-hardening-" + $CandidateSha + ".prepared.json")
$lockPath = Join-Path $StateRoot "locks\dawnstrike-runtime-activation.lock"
if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
    if (-not (Test-Path -LiteralPath $preparedPath -PathType Leaf)) {
        throw "Existing hardening lock has no deterministic PREPARED recovery record."
    }
    $preparedOutput = & $interpreterIdentity.path -I -B $contractScript verify-prepared --prepared $preparedPath 2>$null
    if ($LASTEXITCODE -ne 0) { throw "Existing hardening lock PREPARED record failed strict validation." }
    $prepared = (($preparedOutput -join "") | ConvertFrom-Json)
    if ($prepared.candidate_sha -cne $CandidateSha -or $prepared.candidate_tree -cne $CandidateTree -or
        $prepared.runtime_head -cne $runtimeIdentity.head -or $prepared.runtime_tree -cne $runtimeIdentity.tree -or
        $prepared.runtime_origin -cne $runtimeIdentity.origin -or
        $prepared.runner_before_sha256 -cne $runtimeIdentity.runner_sha256 -or
        $prepared.interpreter_sha256 -cne $interpreterIdentity.sha256) {
        throw "Existing PREPARED record does not bind this exact hardening invocation."
    }
    $hardeningLock = Adopt-DawnstrikeGovernedRuntimeLock -StateRoot $StateRoot `
        -ExpectedToken $prepared.lock_token -ExpectedFileSha256 $prepared.lock_bytes_sha256 `
        -ExpectedOperation capture_task_hardening -CandidateSha $CandidateSha -CandidateTree $CandidateTree `
        -OriginIdentity $lockOrigin -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
    & $interpreterIdentity.path -I -B $contractScript reseal-prepared-lock --prepared $preparedPath `
        --lock-token $hardeningLock.token --lock-bytes-sha256 $hardeningLock.bytes_sha256 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Adopted hardening lock could not be resealed into PREPARED." }
}
else {
    $hardeningLock = Enter-DawnstrikeGovernedRuntimeLock -StateRoot $StateRoot -Operation capture_task_hardening `
         -CandidateSha $CandidateSha -CandidateTree $CandidateTree -OriginIdentity $lockOrigin `
         -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
}
try {
    # The task and its scheduler history are re-read only after the shared
    # activation lock is held; no stale pre-lock observation may be replaced.
    Assert-HardeningCandidateIdentity -ExpectedSha $CandidateSha -ExpectedTree $CandidateTree
    $runtimeIdentityLocked = Get-HardeningRuntimeIdentity -Root $runtimeRootResolved
    if ($runtimeIdentityLocked.head -cne $runtimeIdentity.head -or
        $runtimeIdentityLocked.tree -cne $runtimeIdentity.tree -or
        $runtimeIdentityLocked.origin -cne $runtimeIdentity.origin -or
        $runtimeIdentityLocked.runner_blob -cne $runtimeIdentity.runner_blob) {
        throw "Live RuntimeRoot identity changed before the locked task read."
    }
    $runtimeIdentity = $runtimeIdentityLocked
    $before = Get-HardeningTaskRecord
    $beforeInfo = Get-HardeningTaskInfo -TaskPath $before.task_path
    $receiptOldResult = $beforeInfo.last_task_result
    $receiptOldTime = $beforeInfo.last_run_time
    $receiptOriginalState = $before.state
    $recoveringPrepared = $false
    $candidateReceiptPath = Join-Path $StateRoot ("receipts\capture-task\capture-task-hardening-" + $CandidateSha + ".json")
    if (-not [string]::IsNullOrWhiteSpace($ReceiptPath) -and
        [System.IO.Path]::GetFullPath($ReceiptPath) -cne [System.IO.Path]::GetFullPath($candidateReceiptPath)) {
        throw "Hardening receipt path must be the exact candidate-bound path."
    }
    $ReceiptPath = $candidateReceiptPath
    # A retry may reuse only an already sealed, exact current-candidate
    # Disabled definition.  It must never re-register or clear task history.
    if ($before.state -eq "Disabled" -and (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
        $verification = & $interpreterIdentity.path -I -B $contractScript verify-hardening `
            --receipt $ReceiptPath --candidate-sha $CandidateSha --candidate-tree $CandidateTree 2>$null
        if ($LASTEXITCODE -ne 0) { throw "Existing hardening receipt failed strict validation." }
        $existingReceipt = (($verification -join "") | ConvertFrom-Json)
        if ($existingReceipt.status -ne "COMPLETE" -or $existingReceipt.final_state -ne "Disabled" -or
            $existingReceipt.xml_after_sha256 -ne $before.xml_sha256 -or
            $existingReceipt.previous_candidate_sha -cne $runtimeIdentity.head -or
            $existingReceipt.runner_before_sha256 -cne $runtimeIdentity.runner_sha256) {
            throw "Existing hardening receipt does not attest the exact current Disabled task."
        }
        $null = Assert-DawnstrikeCaptureTaskSafety -Xml $before.xml -RuntimeRoot $runtimeRootResolved `
            -StateRoot $StateRoot -ExpectedPrincipal $taskPrincipal -ExpectedCandidateSha $CandidateSha `
            -ExpectedInterpreterPath ([string]$interpreterIdentity.path) -ExpectedInterpreterSha256 ([string]$interpreterIdentity.sha256) `
            -ExpectedEnabled "false" -RequirePasswordPrincipal -RequireRunner
        Write-Output ([System.IO.File]::ReadAllText($ReceiptPath, [System.Text.UTF8Encoding]::new($false)).Trim())
        return
    }
    # Crash recovery boundary: Register-ScheduledTask may have committed the
    # Disabled replacement before receipt sealing.  Recognize only the exact
    # immutable PREPARED after-state, then continue to COMPLETE sealing.
    if ($null -ne $prepared -and $before.state -eq "Disabled" -and
        $before.xml_sha256 -ceq $prepared.xml_after_sha256) {
        $null = Assert-HardeningPreparedRecoveryState `
            -Prepared $prepared -CurrentTask $before -PreparedPath $preparedPath `
            -ExpectedTaskName $script:HardeningTaskName -ExpectedTaskPath $before.task_path `
            -ExpectedCandidateSha $CandidateSha -ExpectedCandidateTree $CandidateTree `
            -RuntimeIdentity $runtimeIdentity -InterpreterIdentity $interpreterIdentity `
            -ExpectedReceiptPath $ReceiptPath
        $backupXmlPath = [string]$prepared.backup_path
        $backupRoot = Split-Path -Parent $backupXmlPath
        $backupName = Split-Path -Leaf $backupXmlPath
        $bytecodePrefix = Join-Path $StateRoot ("capture-bytecode\" + $CandidateSha)
        $null = Assert-DawnstrikeCaptureTaskSafety -Xml $before.xml -RuntimeRoot $runtimeRootResolved -StateRoot $StateRoot `
            -ExpectedPrincipal $taskPrincipal -ExpectedCandidateSha $CandidateSha `
            -ExpectedInterpreterPath ([string]$interpreterIdentity.path) -ExpectedInterpreterSha256 ([string]$interpreterIdentity.sha256) `
            -ExpectedEnabled "false" -RequirePasswordPrincipal -RequireRunner
        $hardeningInputStage = [string]$prepared.input_stage
        $beforeActionHash = [string]$prepared.action_before_sha256
        $beforeActionXml = [string]$prepared.action_sha256
        $runnerBeforeSha256 = [string]$prepared.runner_before_sha256
        $runnerTargetPath = Join-Path $hardeningCandidateRoot "scripts\run_daily_intraday_capture.py"
        $runnerTargetSha256 = Get-HardeningSha256File $runnerTargetPath
        if ($runnerTargetSha256 -cne $prepared.runner_target_sha256) { throw "PREPARED target runner identity changed." }
        $currentTask = $before
        $verified = $before
        $replacementInfo = Get-HardeningTaskInfo -TaskPath $before.task_path
        $receiptOldResult = $prepared.old_last_task_result
        $receiptOldTime = $prepared.old_last_run_time
        $receiptOriginalState = [string]$prepared.original_state
        $runtimeIdentityAfter = $runtimeIdentity
        $receiptBeforeXmlSha = [string]$prepared.xml_before_sha256
        $receiptBeforeActionHash = [string]$prepared.action_before_sha256
        $receiptBeforeTriggerHash = [string]$prepared.trigger_sha256
        $receiptBeforePrincipalHash = [string]$prepared.principal_sha256
        $receiptBeforeSettingsHash = [string]$prepared.settings_sha256
        $preparedRecordSha = Get-HardeningSha256File $preparedPath
        $stateRootFull = ([System.IO.Path]::GetFullPath($StateRoot)).TrimEnd('\')
        $statePrefix = $stateRootFull + '\'
        $backupRelativePath = (([System.IO.Path]::GetFullPath($backupXmlPath).Substring($statePrefix.Length)) -replace '\\','/')
        $preparedRelativePath = (([System.IO.Path]::GetFullPath($preparedPath).Substring($statePrefix.Length)) -replace '\\','/')
        $receiptRelativePath = (([System.IO.Path]::GetFullPath($ReceiptPath).Substring($statePrefix.Length)) -replace '\\','/')
        $recoveringPrepared = $true
    }
    if (-not $recoveringPrepared) {
    # This is the migration boundary.  It is deliberately before the XML
    # backup and before any scheduler mutation; a malformed or credential-
    # mismatched legacy task cannot be preserved or replaced.
    $beforeExec = @((Get-HardeningSingleSection $before.document "Actions").ChildNodes | Where-Object { $_.LocalName -eq "Exec" })
    $beforeCommandNode = @($beforeExec[0].ChildNodes | Where-Object { $_.LocalName -eq "Command" })
    if ($beforeExec.Count -ne 1 -or $beforeCommandNode.Count -ne 1) { throw "Migration input action command is ambiguous." }
    $beforeCommand = [string]$beforeCommandNode[0].InnerText
    if ([string]::Equals($beforeCommand, "py.exe", [System.StringComparison]::OrdinalIgnoreCase)) {
        $hardeningInputStage = "LEGACY_MIGRATION"
    }
    elseif ([string]::Equals($beforeCommand, [string]$interpreterIdentity.path, [System.StringComparison]::OrdinalIgnoreCase)) {
        $hardeningInputStage = "CANONICAL_REPIN"
    }
    else {
        throw "Existing delayed SIP task command is neither the exact legacy launcher nor the declared signed interpreter."
    }
    $safetyArguments = @{
        Xml = $before.xml
        RuntimeRoot = $runtimeRootResolved
        StateRoot = $StateRoot
        ExpectedPrincipal = $taskPrincipal
        ExpectedCandidateSha = $runtimeIdentity.head
        ExpectedInterpreterPath = [string]$interpreterIdentity.path
        ExpectedInterpreterSha256 = [string]$interpreterIdentity.sha256
        RequireRunner = $true
    }
    if ($hardeningInputStage -eq "LEGACY_MIGRATION") {
        $safetyArguments.AllowLegacyLauncher = $true
        $safetyArguments.AllowLegacySettings = $true
    }
    else {
        $safetyArguments.RequirePasswordPrincipal = $true
        $safetyArguments.ExpectedEnabled = if ($before.state -eq "Ready") { "true" } else { "false" }
    }
    $null = Assert-DawnstrikeCaptureTaskSafety @safetyArguments
    # Freeze the legacy/current action identity before rebuilding the XML;
    # Set-HardeningTaskDefinition mutates the same document in place.
    $beforeActionExec = @((Get-HardeningSingleSection $before.document "Actions").ChildNodes | Where-Object { $_.LocalName -eq "Exec" })
    if ($beforeActionExec.Count -ne 1) { throw "Existing capture action is ambiguous." }
    $beforeActionArguments = @($beforeActionExec[0].ChildNodes | Where-Object { $_.LocalName -eq "Arguments" })
    $beforeActionTokens = @([regex]::Matches([string]$beforeActionArguments[0].InnerText, '"(?<value>[^"\r\n]*)"') | ForEach-Object { [string]$_.Groups["value"].Value })
    if ($beforeActionTokens.Count -lt 3) { throw "Existing capture action bindings are incomplete." }
    $beforeRunnerIndex = if ($beforeActionTokens[0] -eq "-I" -and $beforeActionTokens.Count -ge 6 -and $beforeActionTokens[1] -eq "-B") { 5 } else { 2 }
    $runnerBeforePath = Assert-DawnstrikeCaptureRegularPath ([string]$beforeActionTokens[$beforeRunnerIndex]) "Existing capture runner"
    $runnerBeforeSha256 = Get-HardeningSha256File $runnerBeforePath
    $beforeActionXml = [string]$beforeActionExec[0].ParentNode.OuterXml
    $beforeActionHash = Get-HardeningSha256Text $beforeActionXml
    $receiptBeforeXmlSha = [string]$before.xml_sha256
    $receiptBeforeActionHash = $beforeActionHash
    $receiptBeforeTriggerHash = [string]$before.trigger_contract_sha256
    $receiptBeforePrincipalHash = [string]$before.principal_contract_sha256
    $receiptBeforeSettingsHash = [string]$before.settings_contract_sha256
    $bytecodePrefix = Join-Path $StateRoot ("capture-bytecode\" + $CandidateSha)
    New-Item -ItemType Directory -Path $bytecodePrefix -Force -ErrorAction Stop | Out-Null
    Assert-HardeningNoReparseComponents $bytecodePrefix "Capture bytecode prefix"
    if (@(Get-ChildItem -LiteralPath $bytecodePrefix -Force -Recurse -ErrorAction Stop).Count -ne 0) { throw "Capture bytecode prefix is not empty." }
if ([string]::IsNullOrWhiteSpace($BackupRoot)) {
    # The candidate-bound backup is the only recoverable PREPARED target;
    # historical copies may be retained separately but are never scanned.
    $BackupRoot = Join-Path $StateRoot ("scheduler-backups\capture-hardening-" + $CandidateSha)
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
# Convert the legacy launcher to the exact signed Python executable declared by
# the candidate.  The action's runner and all governed path/hash bindings are
# retained only after the full migration-input safety check above.
$replacementActionXml = Set-HardeningDirectCaptureAction `
    -Document $before.document -Actions (Get-HardeningSingleSection $before.document "Actions") `
    -InterpreterPath ([string]$interpreterIdentity.path) -CandidateSha $CandidateSha `
    -BytecodePrefix (Join-Path $StateRoot ("capture-bytecode\" + $CandidateSha))
$previewDocument = [System.Xml.XmlDocument]::new()
$previewDocument.LoadXml('<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">' + $replacementActionXml + '</Task>')
$previewArguments = @($previewDocument.SelectNodes("//*[local-name()='Arguments']"))
if ($previewArguments.Count -ne 1) { throw "Prepared capture action arguments are missing." }
$previewTokens = @([regex]::Matches([string]$previewArguments[0].InnerText, '"(?<value>[^"\r\n]*)"') | ForEach-Object { [string]$_.Groups["value"].Value })
if ($previewTokens.Count -lt 3) { throw "Prepared capture action bindings are incomplete." }
$runnerTargetPath = Join-Path $hardeningCandidateRoot "scripts\run_daily_intraday_capture.py"
$null = Assert-DawnstrikeCaptureRegularPath $runnerTargetPath "Candidate capture runner"
$runnerTargetSha256 = Get-HardeningSha256File $runnerTargetPath
# A replacement must be born disabled.  The activation/rebind seam is the only
# governed path allowed to enable this auxiliary task for an exact candidate.
$settingsForReplacement = Get-HardeningSingleSection $before.document "Settings"
Set-HardeningChildText $settingsForReplacement "Enabled" "false" ([string]$before.document.DocumentElement.NamespaceURI)
$afterXml = [string]$before.document.OuterXml
$after = Assert-HardeningDefinition -Before $before -AfterXml $afterXml -ExpectedState "Disabled" -ExpectedActionXml $replacementActionXml
if ($after.xml_sha256 -eq $before.xml_sha256) { throw "Task hardening produced no definition change." }
# Exactly one current receipt is addressable by candidate SHA.  Historical
# receipts are not candidates and must not make activation ambiguous.
Assert-HardeningNoReparseComponents $ReceiptPath "Hardening receipt"
$receiptRoot = [System.IO.Path]::GetFullPath((Join-Path $StateRoot "receipts\capture-task")).TrimEnd('\') + '\'
if (-not [System.IO.Path]::GetFullPath($ReceiptPath).StartsWith($receiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Hardening receipt must be inside the governed capture-task receipt root."
}
$stateRootPrefix = ([System.IO.Path]::GetFullPath($StateRoot)).TrimEnd('\') + '\'
$receiptRelativePath = ([System.IO.Path]::GetFullPath($ReceiptPath).Substring($stateRootPrefix.Length) -replace '\','/')
    $preparedRecordSha = Write-HardeningPreparedRecord `
    -Path $preparedPath `
    -Before $before `
    -BeforeInfo $beforeInfo `
        -BackupSha256 $backupXmlSha `
        -BackupFileSha256 $backupXmlFileSha -BackupPath $backupXmlPath `
        -AfterSha256 $after.xml_sha256 `
        -AfterActionSha256 (Get-HardeningSha256Text ([string]$replacementActionXml)) `
        -Lock $hardeningLock -RuntimeIdentity $runtimeIdentity -InterpreterIdentity $interpreterIdentity `
        -RunnerBeforeSha256 $runnerBeforeSha256 -RunnerTargetSha256 $runnerTargetSha256 -ContractScript $contractScript `
        -InputStage $hardeningInputStage
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

    # Register-ScheduledTask -Force performs the TASK_CREATE_OR_UPDATE
    # replacement atomically.  There is intentionally no Unregister gap:
    # Unregister-ScheduledTask is forbidden because it clears history.
    # clearing the task would erase the Monday history evidence.
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
    $verified = Assert-HardeningDefinition -Before $before -AfterXml $currentTask.xml -ExpectedState "Disabled" -ExpectedActionXml $replacementActionXml
    if ($currentTask.xml_sha256 -ne $verified.xml_sha256) { throw "Hardened task XML identity changed during verification." }
    $replacementInfo = Get-HardeningTaskInfo -TaskPath $currentTask.task_path
    Assert-HardeningFreshReplacementInfo -Info $replacementInfo
    $null = Assert-DawnstrikeCaptureTaskSafety `
        -Xml $currentTask.xml -RuntimeRoot $runtimeRootResolved -StateRoot $StateRoot `
        -ExpectedPrincipal $taskPrincipal -ExpectedCandidateSha $CandidateSha `
        -ExpectedInterpreterPath ([string]$interpreterIdentity.path) `
        -ExpectedInterpreterSha256 ([string]$interpreterIdentity.sha256) -RequirePasswordPrincipal -RequireRunner `
        -ExpectedEnabled "false"
    $runtimeIdentityAfter = Get-HardeningRuntimeIdentity -Root $runtimeRootResolved
    if ($runtimeIdentityAfter.head -ne $runtimeIdentity.head -or $runtimeIdentityAfter.tree -ne $runtimeIdentity.tree -or $runtimeIdentityAfter.origin -ne $runtimeIdentity.origin) {
        throw "Live RuntimeRoot identity changed during task hardening."
    }

    }
    $replacementExec = @($verified.actions.ChildNodes | Where-Object { $_.LocalName -eq "Exec" })[0]
    $replacementTokens = @([regex]::Matches([string](@($replacementExec.ChildNodes | Where-Object { $_.LocalName -eq "Arguments" })[0].InnerText), '"(?<value>[^"\r\n]*)"') | ForEach-Object { [string]$_.Groups["value"].Value })
    if ($replacementTokens.Count -lt 3) { throw "Replacement capture action bindings are incomplete." }
    $runnerBeforePath = [string]$replacementTokens[5]
    $bindingValues = @{}
    for ($bindingIndex = 6; $bindingIndex -lt ($replacementTokens.Count - 1); $bindingIndex += 2) { $bindingValues[[string]$replacementTokens[$bindingIndex]] = [string]$replacementTokens[$bindingIndex + 1] }
    $changedFields = @()
    if ($receiptBeforePrincipalHash -ne $verified.principal_contract_sha256) { $changedFields += "principal" }
    if ($receiptBeforeSettingsHash -ne $verified.settings_contract_sha256) { $changedFields += "settings" }
    if ($receiptBeforeActionHash -ne $verified.action_contract_sha256) { $changedFields += "action" }
    $actionMigrated = ($receiptBeforeActionHash -ne $verified.action_contract_sha256)
    $payload = [ordered]@{
        schema_version = "dawnstrike.capture_task_hardening_receipt.v2"
        status = "COMPLETE"
        task_name = $script:HardeningTaskName
        task_path = $before.task_path
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        original_state = $receiptOriginalState
        final_state = "Disabled"
        backup_name = $backupName
        backup_relative_path = $backupRelativePath
        prepared_relative_path = $preparedRelativePath
        backup_xml_sha256 = $backupXmlSha
        backup_xml_file_sha256 = $backupXmlFileSha
        xml_before_sha256 = $receiptBeforeXmlSha
        xml_after_sha256 = $currentTask.xml_sha256
        action_sha256 = $receiptBeforeActionHash
        trigger_sha256 = $receiptBeforeTriggerHash
        principal_before_sha256 = $receiptBeforePrincipalHash
        principal_after_sha256 = $verified.principal_contract_sha256
        settings_before_sha256 = $receiptBeforeSettingsHash
        settings_after_sha256 = $verified.settings_contract_sha256
        prepared_record_sha256 = $preparedRecordSha
        origin_main_refreshed_at_utc = $script:HardeningOriginRefreshUtc
        origin_url = $script:HardeningOriginUrl
        origin_url_sha256 = $script:HardeningOriginUrlSha256
        old_last_task_result = $receiptOldResult
        old_last_run_time = $receiptOldTime
        new_last_task_result = $replacementInfo.last_task_result
        new_last_run_time = $replacementInfo.last_run_time
        history_reset_proven = ($null -eq $replacementInfo.last_run_time -and $replacementInfo.last_task_result -in @(0, 267011))
        history_evidence_preserved = $true
        history_disposition = if ($null -eq $replacementInfo.last_run_time -and $replacementInfo.last_task_result -in @(0, 267011)) { "RESET_AS_UPDATE_SIDE_EFFECT" } else { "PRESERVED" }
        changed_fields = $changedFields
        preserved_action = (-not $actionMigrated)
        action_migrated = $actionMigrated
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
        receipt_relative_path = $receiptRelativePath
        interpreter_path = [string]$interpreterIdentity.path
        interpreter_sha256 = [string]$interpreterIdentity.sha256
        interpreter_version = [string]$interpreterIdentity.version
        interpreter_signer_subject = [string]$interpreterIdentity.signer_subject
        interpreter_signer_thumbprint = [string]$interpreterIdentity.signer_thumbprint
        runner_path = [string]$replacementTokens[5]
        runner_before_sha256 = $runnerBeforeSha256
        runner_sha256 = $runnerTargetSha256
        action_bindings = [ordered]@{
            candidate_sha = [string]$bindingValues["--candidate-sha"]
            bytecode_prefix = $bytecodePrefix
            runner_path = [string]$replacementTokens[5]
            runner_sha256 = $runnerTargetSha256
            symbols_manifest_path = [string]$bindingValues["--symbols-manifest"]
            symbols_manifest_sha256 = [string]$bindingValues["--symbols-manifest-sha256"]
            entitlement_receipt_path = [string]$bindingValues["--entitlement-receipt"]
            entitlement_receipt_sha256 = [string]$bindingValues["--entitlement-receipt-sha256"]
            source_config_path = [string]$bindingValues["--source-config"]
            source_config_sha256 = [string]$bindingValues["--source-config-sha256"]
        }
        previous_candidate_sha = $runtimeIdentity.head
        action_before_sha256 = $receiptBeforeActionHash
        action_after_sha256 = Get-HardeningSha256Text ([string]$verified.actions.OuterXml)
        input_stage = $hardeningInputStage
    }
    $inputPath = Join-Path $BackupRoot (".hardening-receipt-" + [Guid]::NewGuid().ToString("N") + ".json")
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($inputPath, (ConvertTo-Json $payload -Depth 8), $encoding)
    try {
        if ((Get-HardeningSha256File ([string]$interpreterIdentity.path)) -cne [string]$interpreterIdentity.sha256) {
            throw "Pinned receipt-contract interpreter changed before sealing."
        }
        & $interpreterIdentity.path -I -B $contractScript seal-hardening --input $inputPath --output $ReceiptPath | Out-Null
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
    Exit-DawnstrikeGovernedRuntimeLock -Lock $hardeningLock
}
