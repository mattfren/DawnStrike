$global:PSModuleAutoLoadingPreference = 'None'
$env:PSModulePath = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
if ([string]::IsNullOrWhiteSpace([string]$PSScriptRoot)) {
    if (
        [string]$global:DawnstrikePowerShellModuleBoundary.schema_version -cne
        'dawnstrike.powershell_module_boundary.v1'
    ) { throw 'In-memory StateRoot helper requires an established PowerShell module boundary.' }
    & ${function:Initialize-DawnstrikePowerShellModuleBoundary}
    if (
        [string]$global:DawnstrikePowerShellSupportBoundary.schema_version -cne
        'dawnstrike.powershell_native_support.v1'
    ) { throw 'In-memory StateRoot helper requires exact precompiled native support.' }
    $null = & ${function:Import-DawnstrikePowerShellSupport}
}
else {
    . ([IO.Path]::Combine($PSScriptRoot, 'powershell_module_boundary.ps1'))
    . ([IO.Path]::Combine($PSScriptRoot, 'powershell_native_support.ps1'))
}

Set-StrictMode -Version Latest

$script:DawnstrikeStateBoundaryFixedRoot = 'C:\r\dawnstrike-state'
$script:DawnstrikeStateBoundaryEvidenceRoot = 'C:\ProgramData\Dawnstrike'
$launcherLoadedBoundary = Get-Variable `
    -Name DawnstrikeReleaseStateBoundaryPath -Scope Script -ErrorAction SilentlyContinue
if (
    $null -ne $launcherLoadedBoundary -and
    [string]$launcherLoadedBoundary.Value -match `
        '^C:\\Program Files\\Dawnstrike\\releases\\[0-9a-f]{40}\\scripts\\state_root_boundary\.ps1$'
) {
    # The protected launcher retains the exact source handle and evaluates its
    # captured bytes as a ScriptBlock, for which PSScriptRoot is intentionally
    # unavailable. Bind to the already validated protected source path.
    $script:DawnstrikeStateBoundaryInstalledHelper = [IO.Path]::GetFullPath(
        [string]$launcherLoadedBoundary.Value
    )
}
else {
    $script:DawnstrikeStateBoundaryInstalledHelper = [IO.Path]::GetFullPath(
        (Join-Path $PSScriptRoot 'state_root_boundary.ps1')
    )
}
$script:DawnstrikeStateBoundaryCanonicalTasks = @(
    'Dawnstrike AlphaOps Morning',
    'Dawnstrike AlphaOps Monitor 5m',
    'Dawnstrike AlphaOps EOD Full Report',
    'Dawnstrike AlphaOps V6 Weekly Training',
    'Dawnstrike 10of10 Daily Finalize'
)
$script:DawnstrikeStateBoundaryAuxiliaryTask = 'Dawnstrike Delayed SIP Capture'

$null = & ${function:Import-DawnstrikePowerShellSupport}
if (-not ('Dawnstrike.StateBoundary.BoundPath' -as [type])) {
    throw 'Precompiled Dawnstrike StateRoot support did not load.'
}

function Get-DawnstrikeStateBoundarySha256Bytes {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-DawnstrikeStateBoundarySha256File {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        }
        finally { $sha.Dispose() }
    }
    finally { $stream.Dispose() }
}

function Get-DawnstrikeStateBoundarySha256Text {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    return Get-DawnstrikeStateBoundarySha256Bytes ([Text.UTF8Encoding]::new($false).GetBytes($Text))
}

function ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString {
    [CmdletBinding()]
    param([AllowNull()]$Value)

    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) { if ($Value) { return 'true' } else { return 'false' } }
    if ($Value -is [string]) {
        $builder = [Text.StringBuilder]::new()
        $null = $builder.Append('"')
        foreach ($character in $Value.ToCharArray()) {
            $code = [int]$character
            switch ($code) {
                8 { $null = $builder.Append('\b'); continue }
                9 { $null = $builder.Append('\t'); continue }
                10 { $null = $builder.Append('\n'); continue }
                12 { $null = $builder.Append('\f'); continue }
                13 { $null = $builder.Append('\r'); continue }
                34 { $null = $builder.Append('\"'); continue }
                92 { $null = $builder.Append('\\'); continue }
            }
            if ($code -lt 32 -or $code -gt 126) {
                $null = $builder.Append('\u' + $code.ToString('x4'))
            }
            else { $null = $builder.Append($character) }
        }
        $null = $builder.Append('"')
        return $builder.ToString()
    }
    if ($Value -is [Collections.IDictionary] -or $Value -is [Management.Automation.PSCustomObject]) {
        $properties = @{}
        if ($Value -is [Collections.IDictionary]) {
            foreach ($key in $Value.Keys) { $properties[[string]$key] = $Value[$key] }
        }
        else {
            foreach ($property in $Value.PSObject.Properties) {
                $properties[[string]$property.Name] = $property.Value
            }
        }
        $names = [string[]]@($properties.Keys)
        [Array]::Sort($names, [StringComparer]::Ordinal)
        $members = foreach ($name in $names) {
            (ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString $name) + ':' +
                (ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString $properties[$name])
        }
        return '{' + ($members -join ',') + '}'
    }
    if ($Value -is [Collections.IEnumerable]) {
        $members = foreach ($item in $Value) {
            ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString $item
        }
        return '[' + ($members -join ',') + ']'
    }
    if ($Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or
        $Value -is [uint16] -or $Value -is [int32] -or $Value -is [uint32] -or
        $Value -is [int64] -or $Value -is [uint64]) {
        return [Convert]::ToString($Value, [Globalization.CultureInfo]::InvariantCulture)
    }
    throw 'Terminal evidence contains a JSON value outside the canonical receipt domain.'
}

function Get-DawnstrikeStateBoundaryJsonSelfHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Payload,
        [Parameter(Mandatory = $true)][string]$Field,
        [switch]$TrailingNewline
    )
    $unsigned = [ordered]@{}
    if ($Payload -is [Collections.IDictionary]) {
        foreach ($key in $Payload.Keys) {
            if ([string]$key -cne $Field) { $unsigned[[string]$key] = $Payload[$key] }
        }
    }
    else {
        foreach ($property in $Payload.PSObject.Properties) {
            if ([string]$property.Name -cne $Field) {
                $unsigned[[string]$property.Name] = $property.Value
            }
        }
    }
    $canonical = ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString $unsigned
    if ($TrailingNewline) { $canonical += "`n" }
    return Get-DawnstrikeStateBoundarySha256Text $canonical
}

function Assert-DawnstrikeStateBoundaryFixedPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot
    )

    $state = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $evidence = [IO.Path]::GetFullPath($EvidenceRoot).TrimEnd('\')
    if (-not [string]::Equals($state, $script:DawnstrikeStateBoundaryFixedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Production StateRoot is not the fixed Dawnstrike state trust anchor.'
    }
    if (-not [string]::Equals($evidence, $script:DawnstrikeStateBoundaryEvidenceRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'StateRoot evidence is not under the fixed protected ProgramData trust anchor.'
    }
    return [pscustomobject]@{ state_root = $state; evidence_root = $evidence }
}

function Assert-DawnstrikeStateBoundaryNoReparse {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Label = 'StateRoot boundary'
    )

    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root)) { throw "$Label has no filesystem root." }
    $cursor = $root.TrimEnd('\') + '\'
    $tail = $full.Substring($root.Length)
    foreach ($segment in @($tail -split '[\\/]' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })) {
        $cursor = Join-Path $cursor ([string]$segment)
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse point."
        }
    }
    return $full
}

function Open-DawnstrikeStateBoundaryPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$Label = 'StateRoot boundary'
    )

    $full = Assert-DawnstrikeStateBoundaryNoReparse -Path $Path -Label $Label
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    # The lease pins the complete namespace, not merely the leaf.  In
    # particular, C:\r is user-writable on the production host and must not be
    # renameable while later path-based I/O relies on dawnstrike-state.
    $bound = [Dawnstrike.StateBoundary.BoundPathChain]::Open($full)
    return [pscustomobject]@{
        path = $full
        is_directory = [bool]$item.PSIsContainer
        identity = [string]$bound.Identity
        attributes = [uint32]$bound.Attributes
        handle = $bound
    }
}

function Resolve-DawnstrikeStateBoundaryPrincipalSid {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Principal)

    $value = $Principal.Trim()
    if ([string]::IsNullOrWhiteSpace($value)) { throw 'Scheduled task principal is blank.' }
    try {
        if ($value -match '^S-1-') {
            return ([Security.Principal.SecurityIdentifier]::new($value)).Value
        }
        return ([Security.Principal.NTAccount]::new($value)).Translate(
            [Security.Principal.SecurityIdentifier]
        ).Value
    }
    catch {
        throw "Scheduled task principal cannot be mapped to an exact SID: $value"
    }
}

function Get-DawnstrikeStateBoundaryTaskDefinitionText {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Xml)

    try {
        $document = [Xml.XmlDocument]::new()
        # Task Scheduler materializes Settings/Enabled only for Disabled tasks.
        # This is intentionally byte-for-byte equivalent to the activation
        # contract's enablement-independent definition algorithm.
        $document.PreserveWhitespace = $false
        $document.LoadXml($Xml)
        $namespace = [string]$document.DocumentElement.NamespaceURI
        if ([string]::IsNullOrWhiteSpace($namespace)) {
            $enabledNodes = @($document.SelectNodes('/Task/Settings/Enabled'))
        }
        else {
            $manager = [Xml.XmlNamespaceManager]::new($document.NameTable)
            $manager.AddNamespace('task', $namespace)
            $enabledNodes = @($document.SelectNodes(
                '/task:Task/task:Settings/task:Enabled', $manager
            ))
        }
        if ($enabledNodes.Count -gt 1) {
            throw 'Task XML contains more than one Settings/Enabled element.'
        }
        if ($enabledNodes.Count -eq 1) {
            $null = $enabledNodes[0].ParentNode.RemoveChild($enabledNodes[0])
        }
        return [string]$document.OuterXml
    }
    catch {
        throw 'StateRoot task XML cannot produce an enablement-independent definition contract.'
    }
}

function Get-DawnstrikeStateBoundaryXmlSectionHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][ValidateSet(
            'Principal', 'Triggers', 'Settings', 'Actions'
        )][string]$Name
    )

    try {
        $document = [Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $nodes = @($document.SelectNodes("//*[local-name()='$Name']"))
        if ($nodes.Count -ne 1) { throw "expected one $Name section" }
        return Get-DawnstrikeStateBoundarySha256Text ([string]$nodes[0].OuterXml)
    }
    catch { throw "StateRoot task XML has an invalid $Name section." }
}

function Get-DawnstrikeStateBoundaryTaskInventory {
    [CmdletBinding()]
    param([switch]$IncludeXml)

    $records = @()
    $canonicalXmlRecords = @()
    $canonicalDefinitionRecords = @()
    $canonicalActionRecords = @()
    foreach ($taskName in @($script:DawnstrikeStateBoundaryCanonicalTasks + $script:DawnstrikeStateBoundaryAuxiliaryTask)) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
        $required = $taskName -in $script:DawnstrikeStateBoundaryCanonicalTasks
        if (($required -and $matches.Count -ne 1) -or (-not $required -and $matches.Count -gt 1)) {
            throw "StateRoot boundary requires a unique governed task definition: $taskName"
        }
        if ($matches.Count -eq 0) { continue }
        $task = $matches[0]
        $taskPath = [string]$task.TaskPath
        if ($taskPath -cne '\') { throw "StateRoot writer task is outside the canonical task path: $taskName" }
        $principal = $task.Principal
        $userId = [string]$principal.UserId
        $sid = Resolve-DawnstrikeStateBoundaryPrincipalSid -Principal $userId
        $logonType = [string]$principal.LogonType
        $runLevel = [string]$principal.RunLevel
        if ($runLevel -cne 'Limited') {
            throw "StateRoot writer task does not use Limited run level: $taskName"
        }
        if ($required -and $logonType -notin @('Password', 'ServiceAccount')) {
            throw "Canonical StateRoot writer has an invalid logon type: $taskName"
        }
        if (-not $required -and $logonType -notin @('Password', 'ServiceAccount', 'Interactive')) {
            throw "Auxiliary StateRoot writer has an invalid logon type: $taskName"
        }
        $xml = [string](Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop)
        if ([string]::IsNullOrWhiteSpace($xml)) {
            throw "StateRoot writer task export is empty: $taskName"
        }
        $actions = @($task.Actions)
        if ($actions.Count -lt 1) {
            throw "StateRoot writer task has no action: $taskName"
        }
        $actionText = ($actions | ForEach-Object {
            "{0}|{1}|{2}" -f $_.Execute, $_.Arguments, $_.WorkingDirectory
        }) -join "`n"
        $definitionContract = Get-DawnstrikeStateBoundaryTaskDefinitionText $xml
        $xmlHash = Get-DawnstrikeStateBoundarySha256Text $xml
        $definitionContractHash = Get-DawnstrikeStateBoundarySha256Text $definitionContract
        $actionContractHash = Get-DawnstrikeStateBoundarySha256Text $actionText
        $record = [ordered]@{
            task_name = $taskName
            task_path = $taskPath
            state = [string]$task.State
            principal_user_id = $userId
            principal_sid = $sid
            logon_type = $logonType
            run_level = $runLevel
            definition_sha256 = $xmlHash
            definition_contract_sha256 = $definitionContractHash
            action_contract_sha256 = $actionContractHash
            action_section_sha256 = Get-DawnstrikeStateBoundaryXmlSectionHash `
                -Xml $xml -Name Actions
            canonical = [bool]$required
        }
        if ($required) {
            $canonicalXmlRecords += "$taskName`0$xmlHash`n"
            $canonicalDefinitionRecords += "$taskName`0$definitionContractHash`n"
            $canonicalActionRecords += "$taskName`0$taskPath`0$actionText`n"
        }
        if ($IncludeXml) {
            $record['definition_xml_base64'] = [Convert]::ToBase64String(
                [Text.UTF8Encoding]::new($false).GetBytes($xml)
            )
        }
        $records += [pscustomobject]$record
    }
    if (@($records | Where-Object { [bool]$_.canonical }).Count -ne
        $script:DawnstrikeStateBoundaryCanonicalTasks.Count) {
        throw 'StateRoot task inventory is missing a canonical task contract.'
    }
    $canonicalContract = Get-DawnstrikeStateBoundarySha256Text ($canonicalXmlRecords -join '')
    $canonicalDefinitionContract = Get-DawnstrikeStateBoundarySha256Text (
        $canonicalDefinitionRecords -join ''
    )
    $canonicalActionContract = Get-DawnstrikeStateBoundarySha256Text (
        $canonicalActionRecords -join ''
    )
    foreach ($record in @($records | Where-Object { [bool]$_.canonical })) {
        $record | Add-Member -NotePropertyName canonical_task_count `
            -NotePropertyValue $script:DawnstrikeStateBoundaryCanonicalTasks.Count
        $record | Add-Member -NotePropertyName canonical_task_contract_sha256 `
            -NotePropertyValue $canonicalContract
        $record | Add-Member -NotePropertyName canonical_task_definition_contract_sha256 `
            -NotePropertyValue $canonicalDefinitionContract
        $record | Add-Member -NotePropertyName canonical_task_action_contract_sha256 `
            -NotePropertyValue $canonicalActionContract
    }
    return @($records)
}

function Assert-DawnstrikeStateBoundaryQuiescent {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $running = @(
        Get-ScheduledTask -TaskName 'Dawnstrike*' -ErrorAction SilentlyContinue |
            Where-Object { [string]$_.State -eq 'Running' }
    )
    if ($running.Count -ne 0) {
        throw 'StateRoot ACL migration requires every Dawnstrike task to be quiescent.'
    }
    $lockFiles = @(
        Get-ChildItem -LiteralPath $StateRoot -Recurse -Force -File -ErrorAction Stop |
            Where-Object { $_.Name -match '(?i)\.lock(?:\.|$)' }
    )
    if ($lockFiles.Count -ne 0) {
        throw 'StateRoot ACL migration is blocked by an active or unresolved lock file.'
    }
}

function Assert-DawnstrikeStateBoundaryNoPendingRecovery {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $pending = @(
        Get-ChildItem -LiteralPath $EvidenceRoot `
            -Filter 'state-boundary-pending-*.json' -File -Force -ErrorAction Stop
    )
    if ($pending.Count -ne 0) {
        throw 'StateRoot ACL migration has an unresolved recovery intent; dispatch is denied.'
    }
    return $true
}

function Assert-DawnstrikeStateBoundaryTaskInventoryMatches {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$ExpectedTasks,
        [Parameter(Mandatory = $true)]$LiveTasks,
        [Parameter(Mandatory = $true)][string[]]$WriterSids
    )
    $expected = @{}
    foreach ($task in @($ExpectedTasks)) {
        $name = [string]$task.task_name
        if ([string]::IsNullOrWhiteSpace($name) -or $expected.ContainsKey($name)) {
            throw 'Protected StateRoot task inventory contains an invalid or duplicate task.'
        }
        $expected[$name] = $task
    }
    if (@($LiveTasks).Count -ne $expected.Count) {
        throw 'Live StateRoot writer task inventory differs from the protected installation receipt.'
    }
    foreach ($task in @($LiveTasks)) {
        $name = [string]$task.task_name
        $expectedTask = if ($expected.ContainsKey($name)) { $expected[$name] } else { $null }
        if (-not $expected.ContainsKey($name) -or
            [string]$expectedTask.task_path -cne [string]$task.task_path -or
            [string]$expectedTask.principal_sid -cne [string]$task.principal_sid -or
            [string]$expectedTask.logon_type -cne [string]$task.logon_type -or
            [string]$expectedTask.run_level -cne [string]$task.run_level -or
            [string]$expectedTask.definition_sha256 -cne [string]$task.definition_sha256 -or
            [string]$expectedTask.definition_contract_sha256 -cne [string]$task.definition_contract_sha256 -or
            [string]$expectedTask.action_contract_sha256 -cne [string]$task.action_contract_sha256 -or
            [string]$expectedTask.action_section_sha256 -cne [string]$task.action_section_sha256 -or
            [bool]$expectedTask.canonical -ne [bool]$task.canonical -or
            [string]$task.principal_sid -notin @($WriterSids)) {
            throw 'Live scheduled task definition or principal differs from the exact StateRoot writer binding.'
        }
    }
    return $true
}

function Assert-DawnstrikeStateBoundaryFailClosedTaskInventory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$ExpectedTasks,
        [Parameter(Mandatory = $true)]$LiveTasks,
        [Parameter(Mandatory = $true)][string[]]$WriterSids
    )
    $expected = @{}
    foreach ($task in @($ExpectedTasks)) {
        $name = [string]$task.task_name
        if ([string]::IsNullOrWhiteSpace($name) -or $expected.ContainsKey($name)) {
            throw 'Fail-closed StateRoot task inventory contains an invalid or duplicate task.'
        }
        $expected[$name] = $task
    }
    if (@($LiveTasks).Count -ne $expected.Count) {
        throw 'Fail-closed live task set differs from the protected predecessor.'
    }
    foreach ($task in @($LiveTasks)) {
        $name = [string]$task.task_name
        $prior = if ($expected.ContainsKey($name)) { $expected[$name] } else { $null }
        if ($null -eq $prior -or [string]$task.state -cne 'Disabled' -or
            [string]$prior.task_path -cne [string]$task.task_path -or
            [string]$prior.principal_sid -cne [string]$task.principal_sid -or
            [string]$prior.logon_type -cne [string]$task.logon_type -or
            [string]$prior.run_level -cne [string]$task.run_level -or
            [string]$prior.definition_contract_sha256 -cne
                [string]$task.definition_contract_sha256 -or
            [string]$prior.action_contract_sha256 -cne
                [string]$task.action_contract_sha256 -or
            [string]$prior.action_section_sha256 -cne
                [string]$task.action_section_sha256 -or
            [bool]$prior.canonical -ne [bool]$task.canonical -or
            [string]$task.principal_sid -notin @($WriterSids)) {
            throw 'Fail-closed live task differs from the exact predecessor except for enablement.'
        }
        if ([bool]$task.canonical -and (
            [string]$prior.canonical_task_definition_contract_sha256 -cne
                [string]$task.canonical_task_definition_contract_sha256 -or
            [string]$prior.canonical_task_action_contract_sha256 -cne
                [string]$task.canonical_task_action_contract_sha256
        )) { throw 'Fail-closed canonical aggregate differs from the protected predecessor.' }
    }
    return $true
}

function Get-DawnstrikeStateBoundaryTaskBindingHash {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Tasks)
    $text = (@($Tasks | Sort-Object task_name | ForEach-Object {
        @(
            [string]$_.task_name,
            [string]$_.task_path,
            [string]$_.principal_sid,
            [string]$_.logon_type,
            [string]$_.run_level,
            [string]$_.definition_sha256,
            [string]$_.definition_contract_sha256,
            [string]$_.action_contract_sha256,
            [string]$_.action_section_sha256,
            [string]$_.canonical_task_contract_sha256,
            [string]$_.canonical_task_definition_contract_sha256,
            [string]$_.canonical_task_action_contract_sha256,
            ([bool]$_.canonical).ToString().ToLowerInvariant()
        ) -join "`0"
    }) -join "`n") + "`n"
    return Get-DawnstrikeStateBoundarySha256Text $text
}

function Assert-DawnstrikeStateBoundaryTaskPrincipalsMatch {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$ExpectedTasks,
        [Parameter(Mandatory = $true)]$LiveTasks,
        [Parameter(Mandatory = $true)][string[]]$WriterSids
    )
    $expected = @{}
    foreach ($task in @($ExpectedTasks)) {
        $name = [string]$task.task_name
        if ([string]::IsNullOrWhiteSpace($name) -or $expected.ContainsKey($name)) {
            throw 'Protected StateRoot task inventory contains an invalid or duplicate task.'
        }
        $expected[$name] = $task
    }
    if (@($LiveTasks).Count -ne $expected.Count) {
        throw 'Live StateRoot writer task inventory differs from the protected installation receipt.'
    }
    foreach ($task in @($LiveTasks)) {
        $name = [string]$task.task_name
        $expectedTask = if ($expected.ContainsKey($name)) { $expected[$name] } else { $null }
        if (-not $expected.ContainsKey($name) -or
            [string]$expectedTask.task_path -cne [string]$task.task_path -or
            [string]$expectedTask.principal_sid -cne [string]$task.principal_sid -or
            [string]$expectedTask.logon_type -cne [string]$task.logon_type -or
            [string]$expectedTask.run_level -cne [string]$task.run_level -or
            [bool]$expectedTask.canonical -ne [bool]$task.canonical -or
            [string]$task.principal_sid -notin @($WriterSids)) {
            throw 'Live scheduled task principal is not an exact admitted StateRoot writer SID.'
        }
    }
    return $true
}

function Get-DawnstrikeStateBoundaryTaskMutationAffectedNames {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][ValidateSet(
        'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
    )][string]$Mode)
    if ($Mode -in @('HardenCapture', 'RebindCapture')) {
        return @($script:DawnstrikeStateBoundaryAuxiliaryTask)
    }
    if ($Mode -eq 'Rollback') {
        return @(
            $script:DawnstrikeStateBoundaryCanonicalTasks +
                $script:DawnstrikeStateBoundaryAuxiliaryTask
        )
    }
    return @($script:DawnstrikeStateBoundaryCanonicalTasks)
}

function Get-DawnstrikeStateBoundaryTaskMutationIntentPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    return Join-Path $EvidenceRoot 'state-boundary-task-mutation-pending.json'
}

function Get-DawnstrikeStateBoundaryCandidateMigrationIntentPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    # Candidate migration and task resealing share one atomic no-replace
    # sentinel so the two protected mutation classes can never race after
    # independently observing an idle boundary.
    return Get-DawnstrikeStateBoundaryTaskMutationIntentPath -EvidenceRoot $EvidenceRoot
}

function Get-DawnstrikeStateBoundaryTaskMutationIntent {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $path = Get-DawnstrikeStateBoundaryTaskMutationIntentPath -EvidenceRoot $EvidenceRoot
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $read = Read-DawnstrikeStateBoundaryProtectedJson -Path $path
    try {
        $schema = [string]$read.payload.schema_version
        if ($schema -ceq 'dawnstrike.state_boundary_candidate_migration_intent.v1') {
            return $null
        }
        if ($schema -cne 'dawnstrike.state_boundary_task_mutation.v1') {
            throw 'Protected StateRoot mutation sentinel has an unknown schema.'
        }
        return [pscustomobject]@{ path = $path; payload = $read.payload; sha256 = $read.sha256 }
    }
    finally { $read.stream.Dispose() }
}

function Assert-DawnstrikeStateBoundaryNoTaskMutation {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $EvidenceRoot
    if ($null -ne $intent) {
        throw 'StateRoot task binding has an unresolved protected mutation intent; dispatch is denied.'
    }
    return $true
}

function Get-DawnstrikeStateBoundaryCandidateMigrationIntent {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $path = Get-DawnstrikeStateBoundaryCandidateMigrationIntentPath `
        -EvidenceRoot $EvidenceRoot
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $read = Read-DawnstrikeStateBoundaryProtectedJson -Path $path
    try {
        $schema = [string]$read.payload.schema_version
        if ($schema -ceq 'dawnstrike.state_boundary_task_mutation.v1') {
            return $null
        }
        if ($schema -cne 'dawnstrike.state_boundary_candidate_migration_intent.v1') {
            throw 'Protected StateRoot mutation sentinel has an unknown schema.'
        }
        return [pscustomobject]@{
            path = $path
            payload = $read.payload
            sha256 = $read.sha256
        }
    }
    finally { $read.stream.Dispose() }
}

function Assert-DawnstrikeStateBoundaryNoCandidateMigration {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $intent = Get-DawnstrikeStateBoundaryCandidateMigrationIntent `
        -EvidenceRoot $EvidenceRoot
    if ($null -ne $intent) {
        throw 'StateRoot candidate binding has an unresolved protected migration intent; dispatch is denied.'
    }
    return $true
}

function Assert-DawnstrikeStateBoundaryTaskMutationIntent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Intent,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$RequestContractSha256 = ''
    )
    $payload = $Intent.payload
    $activationLineage = Get-DawnstrikeStateBoundaryActivationLineage -Receipt $payload
    $currentRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
        -Receipt $payload -Kind current -StateRoot $StateRoot
    $rollbackRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
        -Receipt $payload -Kind rollback -StateRoot $StateRoot
    if ($Mode -eq 'Activate' -and
        [string]$currentRuntimeAuthorization.status -cne 'AUTHORIZED') {
        throw 'Protected activation mutation intent has no authorized current runtime.'
    }
    if ($Mode -eq 'BootstrapBaseline' -and (
        [string]$currentRuntimeAuthorization.status -notin @('NONE', 'LEGACY_NONE') -or
        [string]$rollbackRuntimeAuthorization.status -notin @('NONE', 'LEGACY_NONE') -or
        [string]$activationLineage.status -notin @('NONE', 'LEGACY_NONE')
    )) { throw 'Protected baseline bootstrap is restricted to the first unsealed runtime.' }
    if ($Mode -eq 'Rollback' -and (
        [string]$activationLineage.status -cne 'ACTIVE' -or
        [string]$currentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
        [string]$rollbackRuntimeAuthorization.status -cne 'AUTHORIZED'
    )) {
        throw 'Protected rollback mutation intent has no activation lineage.'
    }
    $expectedAffected = @(Get-DawnstrikeStateBoundaryTaskMutationAffectedNames -Mode $Mode)
    $actualAffected = @($payload.affected_tasks | ForEach-Object { [string]$_ })
    $expectedCompletionPath = Join-Path $script:DawnstrikeStateBoundaryEvidenceRoot (
        'state-boundary-task-mutation-completion-' + [string]$payload.operation_id + '.json'
    )
    $predecessorPairs = @($payload.predecessor_terminal_evidence_pairs | ForEach-Object { [string]$_ })
    $normalizedPredecessorPairs = @($predecessorPairs | Sort-Object -Unique)
    $predecessorPairsHash = Get-DawnstrikeStateBoundarySha256Text (
        ($predecessorPairs -join "`n") + "`n"
    )
    if (
        [string]$payload.schema_version -cne 'dawnstrike.state_boundary_task_mutation.v1' -or
        [string]$payload.operation_id -notmatch '^[0-9a-f]{32}$' -or
        [string]$payload.mode -cne $Mode -or
        [string]$payload.expected_sha -cne $ExpectedSha.ToLowerInvariant() -or
        [string]$payload.expected_tree -cne $ExpectedTree.ToLowerInvariant() -or
        [string]$payload.state_root -cne [IO.Path]::GetFullPath($StateRoot).TrimEnd('\') -or
        [string]$payload.old_current_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.old_task_binding_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.request_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
        ($RequestContractSha256 -and
            [string]$payload.request_contract_sha256 -cne $RequestContractSha256) -or
        $null -eq $payload.predecessor_terminal_evidence_pairs -or
        [string]$payload.predecessor_terminal_evidence_sha256 -cne $predecessorPairsHash -or
        @($predecessorPairs | Where-Object {
            $_ -notmatch '^[0-9a-f]{64}:[0-9a-f]{64}$'
        }).Count -ne 0 -or
        ($predecessorPairs -join "`n") -cne ($normalizedPredecessorPairs -join "`n") -or
        [string]$payload.completion_path -cne $expectedCompletionPath -or
        (@($actualAffected | Sort-Object) -join "`n") -cne (@($expectedAffected | Sort-Object) -join "`n") -or
        $payload.research_only -ne $true -or
        $payload.broker_execution_enabled -ne $false
    ) { throw 'Protected StateRoot task-mutation intent is invalid or belongs to another operation.' }
    return $true
}

function Assert-DawnstrikeStateBoundaryTaskMutationScope {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$ExpectedTasks,
        [Parameter(Mandatory = $true)]$LiveTasks,
        [Parameter(Mandatory = $true)][string[]]$AffectedTasks
    )
    $expected = @{}
    foreach ($task in @($ExpectedTasks)) { $expected[[string]$task.task_name] = $task }
    foreach ($task in @($LiveTasks)) {
        $name = [string]$task.task_name
        if (-not $expected.ContainsKey($name)) {
            throw 'Task-mutation scope contains an unbound task.'
        }
        if ($name -notin @($AffectedTasks) -and
            [string]$expected[$name].definition_sha256 -cne [string]$task.definition_sha256) {
            throw 'A task outside the protected mutation scope changed definition.'
        }
    }
    return $true
}

function Assert-DawnstrikeStateBoundaryTerminalTaskContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)]$TerminalRecord,
        [Parameter(Mandatory = $true)]$LiveTasks
    )

    if ($Mode -in @('BootstrapBaseline', 'Activate', 'Rollback')) {
        $canonical = @()
        foreach ($name in $script:DawnstrikeStateBoundaryCanonicalTasks) {
            $matches = @($LiveTasks | Where-Object {
                [string]$_.task_name -ceq $name -and [bool]$_.canonical
            })
            if ($matches.Count -ne 1) {
                throw 'Terminal task contract does not contain the exact canonical task set.'
            }
            $canonical += $matches[0]
        }
        $first = $canonical[0]
        foreach ($task in $canonical) {
            if (
                [int]$task.canonical_task_count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
                [string]$task.canonical_task_contract_sha256 -cne [string]$first.canonical_task_contract_sha256 -or
                [string]$task.canonical_task_definition_contract_sha256 -cne [string]$first.canonical_task_definition_contract_sha256 -or
                [string]$task.canonical_task_action_contract_sha256 -cne [string]$first.canonical_task_action_contract_sha256 -or
                [string]$task.state -cne $(if ($Mode -eq 'BootstrapBaseline') { 'Disabled' } else { 'Ready' })
            ) { throw 'Terminal canonical task inventory has an inconsistent state or contract.' }
        }
        if (
            [int]$TerminalRecord.task_count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
            [string]$TerminalRecord.task_contract_sha256 -cne [string]$first.canonical_task_contract_sha256 -or
            [string]$TerminalRecord.task_definition_contract_sha256 -cne [string]$first.canonical_task_definition_contract_sha256 -or
            [string]$TerminalRecord.task_action_contract_sha256 -cne [string]$first.canonical_task_action_contract_sha256
        ) { throw 'Terminal activation/rollback receipt does not bind the exact live canonical task contract.' }
        $contract = [ordered]@{
            mode = $Mode
            task_count = $script:DawnstrikeStateBoundaryCanonicalTasks.Count
            task_contract_sha256 = [string]$first.canonical_task_contract_sha256
            task_definition_contract_sha256 = [string]$first.canonical_task_definition_contract_sha256
            task_action_contract_sha256 = [string]$first.canonical_task_action_contract_sha256
            required_state = if ($Mode -eq 'BootstrapBaseline') { 'Disabled' } else { 'Ready' }
        }
        if ($Mode -in @('BootstrapBaseline', 'Activate')) { return [pscustomobject]$contract }

        if ($TerminalRecord.auxiliary_capture_present -isnot [bool] -or
            [string]::IsNullOrWhiteSpace([string]$TerminalRecord.auxiliary_capture_disposition)) {
            throw 'Terminal rollback receipt does not explicitly bind auxiliary capture presence.'
        }
        $capture = @($LiveTasks | Where-Object {
            [string]$_.task_name -ceq $script:DawnstrikeStateBoundaryAuxiliaryTask -and
            -not [bool]$_.canonical
        })
        $auxiliaryPresent = [bool]$TerminalRecord.auxiliary_capture_present
        if (-not $auxiliaryPresent) {
            if ($capture.Count -ne 0) {
                throw 'Terminal rollback receipt says auxiliary capture is absent but a live task exists.'
            }
            $contract['auxiliary_capture_present'] = $false
            $contract['auxiliary_capture_disposition'] =
                [string]$TerminalRecord.auxiliary_capture_disposition
            return [pscustomobject]$contract
        }
        if ($capture.Count -ne 1) {
            throw 'Terminal rollback receipt has no unique live auxiliary capture task.'
        }
        $captureTask = $capture[0]
        if (
            [string]$TerminalRecord.auxiliary_capture_action -cne 'RESTORED_EXACT' -or
            [string]$TerminalRecord.auxiliary_capture_state_after -notin @('Ready', 'Disabled') -or
            [string]$captureTask.state -cne [string]$TerminalRecord.auxiliary_capture_state_after -or
            [string]$captureTask.definition_sha256 -cne [string]$TerminalRecord.auxiliary_capture_xml_sha256 -or
            [string]$captureTask.definition_contract_sha256 -cne
                [string]$TerminalRecord.auxiliary_capture_definition_contract_sha256 -or
            [string]$captureTask.action_contract_sha256 -cne
                [string]$TerminalRecord.auxiliary_capture_action_contract_sha256
        ) { throw 'Terminal rollback receipt does not bind the exact live auxiliary capture task.' }
        $contract['auxiliary_capture_present'] = $true
        $contract['auxiliary_capture_disposition'] =
            [string]$TerminalRecord.auxiliary_capture_disposition
        $contract['auxiliary_capture_state'] = [string]$captureTask.state
        $contract['auxiliary_capture_xml_sha256'] = [string]$captureTask.definition_sha256
        $contract['auxiliary_capture_definition_contract_sha256'] = [string]$captureTask.definition_contract_sha256
        $contract['auxiliary_capture_action_contract_sha256'] = [string]$captureTask.action_contract_sha256
        return [pscustomobject]$contract
    }

    $capture = @($LiveTasks | Where-Object {
        [string]$_.task_name -ceq $script:DawnstrikeStateBoundaryAuxiliaryTask -and
        -not [bool]$_.canonical
    })
    if ($capture.Count -ne 1) {
        throw 'Terminal capture receipt has no unique live auxiliary task contract.'
    }
    $task = $capture[0]
    if ([string]$TerminalRecord.task_name -cne $script:DawnstrikeStateBoundaryAuxiliaryTask) {
        throw 'Terminal capture receipt names a different task.'
    }
    if ($Mode -eq 'HardenCapture') {
        # The v2 hardening contract predates a separate normalized-definition
        # field. Its exact raw XML hash cryptographically commits to that
        # definition, while action_after_sha256 commits to the XML Actions node.
        if (
            [string]$task.state -cne 'Disabled' -or
            [string]$TerminalRecord.final_state -cne 'Disabled' -or
            [string]$TerminalRecord.xml_after_sha256 -cne [string]$task.definition_sha256 -or
            [string]$TerminalRecord.action_after_sha256 -cne [string]$task.action_section_sha256
        ) { throw 'Terminal hardening receipt does not bind the exact live Disabled capture task.' }
    }
    else {
        if (
            [string]$task.state -cne 'Ready' -or
            [string]$TerminalRecord.enablement_after -cne 'Ready' -or
            [string]$TerminalRecord.xml_after_sha256 -cne [string]$task.definition_sha256 -or
            [string]$TerminalRecord.action_after_sha256 -cne [string]$task.action_contract_sha256 -or
            [string]$TerminalRecord.definition_after_sha256 -cne [string]$task.definition_contract_sha256
        ) { throw 'Terminal rebind receipt does not bind the exact live Ready capture task.' }
    }
    return [pscustomobject][ordered]@{
        mode = $Mode
        task_name = $script:DawnstrikeStateBoundaryAuxiliaryTask
        state = [string]$task.state
        xml_sha256 = [string]$task.definition_sha256
        action_contract_sha256 = [string]$task.action_contract_sha256
        action_section_sha256 = [string]$task.action_section_sha256
        definition_contract_sha256 = [string]$task.definition_contract_sha256
    }
}

function Disable-DawnstrikeStateBoundaryAffectedTasks {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string[]]$TaskNames)
    $presentNames = @()
    foreach ($name in @($TaskNames)) {
        $matches = @(Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
        if ($matches.Count -eq 0 -and $name -ceq $script:DawnstrikeStateBoundaryAuxiliaryTask) {
            continue
        }
        if ($matches.Count -ne 1 -or [string]$matches[0].TaskPath -cne '\') {
            throw 'Protected task-mutation recovery found a missing, duplicate, or noncanonical affected task.'
        }
        Disable-ScheduledTask -TaskName $name -TaskPath '\' -ErrorAction Stop | Out-Null
        $presentNames += $name
    }
    foreach ($name in @($presentNames)) {
        $task = @(Get-ScheduledTask -TaskName $name -ErrorAction Stop)
        if ($task.Count -ne 1 -or [string]$task[0].State -ne 'Disabled') {
            throw 'Protected task-mutation recovery could not hold an affected task Disabled.'
        }
    }
}

function Copy-DawnstrikeStateBoundaryReceipt {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Receipt)
    $copy = [ordered]@{}
    foreach ($property in @($Receipt.PSObject.Properties)) {
        $copy[[string]$property.Name] = $property.Value
    }
    return $copy
}

function Get-DawnstrikeStateBoundaryActivationLineage {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Receipt)

    $fields = @(
        'last_activation_id',
        'last_activation_terminal_receipt_relative_path',
        'last_activation_terminal_receipt_sha256',
        'last_activation_terminal_journal_relative_path',
        'last_activation_terminal_journal_sha256'
    )
    $present = @($fields | Where-Object {
        $Receipt.PSObject.Properties.Name -contains $_
    })
    if ($present.Count -eq 0) {
        # Existing protected installations predate activation-lineage sealing.
        # They may be upgraded by a governed Activate completion, but they can
        # never authorize Rollback because no activation identity is present.
        return [pscustomobject]@{
            status = 'LEGACY_NONE'
            activation_id = 'NONE'
            receipt_relative_path = 'NONE'
            receipt_sha256 = 'NONE'
            journal_relative_path = 'NONE'
            journal_sha256 = 'NONE'
        }
    }
    if ($present.Count -ne $fields.Count) {
        throw 'Protected StateRoot activation lineage is partial.'
    }
    $activationId = [string]$Receipt.last_activation_id
    $receiptRelative = [string]$Receipt.last_activation_terminal_receipt_relative_path
    $receiptSha256 = [string]$Receipt.last_activation_terminal_receipt_sha256
    $journalRelative = [string]$Receipt.last_activation_terminal_journal_relative_path
    $journalSha256 = [string]$Receipt.last_activation_terminal_journal_sha256
    if ($activationId -ceq 'NONE') {
        if ($receiptRelative -cne 'NONE' -or $receiptSha256 -cne 'NONE' -or
            $journalRelative -cne 'NONE' -or $journalSha256 -cne 'NONE') {
            throw 'Protected StateRoot empty activation lineage is not exact.'
        }
        return [pscustomobject]@{
            status = 'NONE'
            activation_id = 'NONE'
            receipt_relative_path = 'NONE'
            receipt_sha256 = 'NONE'
            journal_relative_path = 'NONE'
            journal_sha256 = 'NONE'
        }
    }
    if ($activationId -notmatch '^[0-9a-f]{24}$' -or
        $receiptRelative -cne "receipts/runtime-activation/runtime-activation-$activationId.json" -or
        $journalRelative -cne "receipts/runtime-operation/runtime-activation-$activationId.json" -or
        $receiptSha256 -notmatch '^[0-9a-f]{64}$' -or
        $journalSha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Protected StateRoot activation lineage identity is invalid.'
    }
    return [pscustomobject]@{
        status = 'ACTIVE'
        activation_id = $activationId
        receipt_relative_path = $receiptRelative
        receipt_sha256 = $receiptSha256
        journal_relative_path = $journalRelative
        journal_sha256 = $journalSha256
    }
}

function Get-DawnstrikeStateBoundaryRuntimeAuthorizationMaterial {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Material,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [ValidateSet('', 'ACTIVATE', 'BOOTSTRAP')][string]$ExpectedOperationType = '',
        [string]$ExpectedSha256 = ''
    )

    $required = @(
        'schema_version', 'operation_type', 'operation_id',
        'request_contract_sha256', 'runtime_sha', 'runtime_tree',
        'runtime_origin_identity', 'runtime_origin_sha256',
        'protected_release_root', 'release_admission_path',
        'release_admission_sha256', 'python_path', 'python_sha256',
        'python_boundary_manifest_path', 'python_boundary_manifest_sha256',
        'requirements_lock_sha256', 'requirements_lock_blob',
        'dependency_root', 'dependency_manifest_path',
        'dependency_manifest_sha256', 'launch_manifests',
        'canonical_task_definition_contract_sha256',
        'canonical_task_action_contract_sha256', 'research_only',
        'broker_execution_enabled'
    )
    $names = @($Material.PSObject.Properties.Name | ForEach-Object { [string]$_ })
    if (($names.Count -ne $required.Count) -or
        (($names | Sort-Object) -join "`n") -cne (($required | Sort-Object) -join "`n")) {
        throw 'Protected runtime authorization material has a non-exact field set.'
    }
    $operationType = [string]$Material.operation_type
    $runtimeSha = [string]$Material.runtime_sha
    $state = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $release = [IO.Path]::GetFullPath(
        "C:\Program Files\Dawnstrike\releases\$runtimeSha"
    ).TrimEnd('\')
    $admission = Join-Path (Join-Path $release '.git') 'dawnstrike-host-admission-v1.json'
    $python = 'C:\Program Files\Dawnstrike\Python313\python.exe'
    $pythonBoundary = 'C:\Program Files\Dawnstrike\Python313\.dawnstrike-python-boundary-v1.json'
    $dependency = [IO.Path]::GetFullPath(
        (Join-Path 'C:\Program Files\Dawnstrike\Dependencies' `
            ([string]$Material.requirements_lock_sha256))
    ).TrimEnd('\')
    $dependencyManifest = Join-Path $dependency '.dawnstrike-dependency-boundary-v1.json'
    if (
        [string]$Material.schema_version -cne 'dawnstrike.protected_runtime_authorization_material.v1' -or
        $operationType -notin @('ACTIVATE', 'BOOTSTRAP') -or
        ($ExpectedOperationType -and $operationType -cne $ExpectedOperationType) -or
        [string]$Material.operation_id -notmatch '^[0-9a-f]{32}$' -or
        [string]$Material.request_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $runtimeSha -notmatch '^[0-9a-f]{40}$' -or
        [string]$Material.runtime_tree -notmatch '^[0-9a-f]{40}$' -or
        [string]::IsNullOrWhiteSpace([string]$Material.runtime_origin_identity) -or
        [string]$Material.runtime_origin_sha256 -notmatch '^[0-9a-f]{64}$' -or
        -not [string]::Equals(
            [string]$Material.protected_release_root, $release,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            [string]$Material.release_admission_path, $admission,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$Material.release_admission_sha256 -notmatch '^[0-9a-f]{64}$' -or
        -not [string]::Equals([string]$Material.python_path, $python, [StringComparison]::OrdinalIgnoreCase) -or
        [string]$Material.python_sha256 -notmatch '^[0-9a-f]{64}$' -or
        -not [string]::Equals(
            [string]$Material.python_boundary_manifest_path, $pythonBoundary,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$Material.python_boundary_manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Material.requirements_lock_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Material.requirements_lock_blob -notmatch '^[0-9a-f]{40}$' -or
        -not [string]::Equals(
            [string]$Material.dependency_root, $dependency,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            [string]$Material.dependency_manifest_path, $dependencyManifest,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$Material.dependency_manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Material.canonical_task_definition_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Material.canonical_task_action_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $Material.research_only -ne $true -or
        $Material.broker_execution_enabled -ne $false
    ) { throw 'Protected runtime authorization material identity is invalid.' }
    $runnerByTask = [ordered]@{
        'Dawnstrike AlphaOps Morning' = 'run_alphaops_morning.ps1'
        'Dawnstrike AlphaOps Monitor 5m' = 'run_alphaops_monitor.ps1'
        'Dawnstrike AlphaOps EOD Full Report' = 'run_alphaops_eod.ps1'
        'Dawnstrike AlphaOps V6 Weekly Training' = 'run_alphaops_weekly_training.ps1'
        'Dawnstrike 10of10 Daily Finalize' = 'run_daily_finalize.ps1'
    }
    $manifests = @($Material.launch_manifests)
    if ($manifests.Count -ne $runnerByTask.Count) {
        throw 'Protected runtime authorization does not bind all five launch manifests.'
    }
    for ($index = 0; $index -lt $runnerByTask.Count; $index += 1) {
        $taskName = @($runnerByTask.Keys)[$index]
        $runner = [string]$runnerByTask[$taskName]
        $entry = $manifests[$index]
        $entryNames = @($entry.PSObject.Properties.Name | ForEach-Object { [string]$_ })
        if ((($entryNames | Sort-Object) -join "`n") -cne
            ((@('task_name', 'task_script', 'path', 'sha256') | Sort-Object) -join "`n")) {
            throw 'Protected runtime launch-manifest entry has a non-exact field set.'
        }
        $expectedPath = Join-Path $state (
            "receipts\scheduler-launch\$runtimeSha-$runner.json"
        )
        if (
            [string]$entry.task_name -cne $taskName -or
            [string]$entry.task_script -cne $runner -or
            -not [string]::Equals(
                [IO.Path]::GetFullPath([string]$entry.path),
                [IO.Path]::GetFullPath($expectedPath),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$entry.sha256 -notmatch '^[0-9a-f]{64}$'
        ) { throw 'Protected runtime launch-manifest identity is invalid.' }
    }
    $materialSha256 = Get-DawnstrikeStateBoundarySha256Text (
        ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString $Material
    )
    if ($ExpectedSha256 -and $materialSha256 -cne $ExpectedSha256) {
        throw 'Protected runtime authorization material hash is invalid.'
    }
    return [pscustomobject]@{
        material = $Material
        sha256 = $materialSha256
        operation_type = $operationType
        runtime_sha = $runtimeSha
        runtime_tree = [string]$Material.runtime_tree
    }
}

function Get-DawnstrikeStateBoundaryRuntimeAuthorization {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Receipt,
        [Parameter(Mandatory = $true)][ValidateSet('current', 'rollback')][string]$Kind,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )

    $contractName = $Kind + '_runtime_authorization_contract'
    $hashName = $Kind + '_runtime_authorization_sha256'
    $hasContract = $Receipt.PSObject.Properties.Name -contains $contractName
    $hasHash = $Receipt.PSObject.Properties.Name -contains $hashName
    if (-not $hasContract -and -not $hasHash) {
        return [pscustomobject]@{ status = 'LEGACY_NONE'; contract = $null; sha256 = 'NONE' }
    }
    if ($hasContract -ne $hasHash) {
        throw "Protected $Kind runtime authorization is partial."
    }
    $contract = $Receipt.$contractName
    $expectedHash = [string]$Receipt.$hashName
    if ($contract -is [string] -and [string]$contract -ceq 'NONE') {
        if ($expectedHash -cne 'NONE') {
            throw "Protected empty $Kind runtime authorization is not exact."
        }
        return [pscustomobject]@{ status = 'NONE'; contract = $null; sha256 = 'NONE' }
    }
    if ($null -eq $contract -or $expectedHash -notmatch '^[0-9a-f]{64}$') {
        throw "Protected $Kind runtime authorization identity is invalid."
    }
    $required = @(
        'schema_version', 'material', 'material_sha256', 'terminal_id',
        'terminal_receipt_relative_path', 'terminal_receipt_sha256',
        'terminal_journal_relative_path', 'terminal_journal_sha256'
    )
    $names = @($contract.PSObject.Properties.Name | ForEach-Object { [string]$_ })
    if (($names.Count -ne $required.Count) -or
        (($names | Sort-Object) -join "`n") -cne (($required | Sort-Object) -join "`n")) {
        throw "Protected $Kind runtime authorization has a non-exact field set."
    }
    $terminalId = [string]$contract.terminal_id
    if (
        [string]$contract.schema_version -cne 'dawnstrike.protected_runtime_authorization.v1' -or
        $terminalId -notmatch '^[0-9a-f]{24}$' -or
        [string]$contract.terminal_receipt_relative_path -cne
            "receipts/runtime-activation/runtime-activation-$terminalId.json" -or
        [string]$contract.terminal_journal_relative_path -cne
            "receipts/runtime-operation/runtime-activation-$terminalId.json" -or
        [string]$contract.terminal_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$contract.terminal_journal_sha256 -notmatch '^[0-9a-f]{64}$'
    ) { throw "Protected $Kind runtime authorization terminal identity is invalid." }
    $material = Get-DawnstrikeStateBoundaryRuntimeAuthorizationMaterial `
        -Material $contract.material -StateRoot $StateRoot `
        -ExpectedSha256 ([string]$contract.material_sha256)
    $actualHash = Get-DawnstrikeStateBoundarySha256Text (
        ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString $contract
    )
    if ($actualHash -cne $expectedHash) {
        throw "Protected $Kind runtime authorization contract hash is invalid."
    }
    return [pscustomobject]@{
        status = 'AUTHORIZED'
        contract = $contract
        sha256 = $actualHash
        material = $material.material
        operation_type = $material.operation_type
        runtime_sha = $material.runtime_sha
        runtime_tree = $material.runtime_tree
        terminal_id = $terminalId
    }
}

function New-DawnstrikeStateBoundaryRuntimeAuthorization {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Material,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$MaterialSha256,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{24}$')]
        [string]$TerminalId,
        [Parameter(Mandatory = $true)][string]$TerminalReceiptRelativePath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$TerminalReceiptSha256,
        [Parameter(Mandatory = $true)][string]$TerminalJournalRelativePath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$TerminalJournalSha256,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )

    $null = Get-DawnstrikeStateBoundaryRuntimeAuthorizationMaterial `
        -Material $Material -StateRoot $StateRoot -ExpectedSha256 $MaterialSha256
    $contract = [pscustomobject][ordered]@{
        schema_version = 'dawnstrike.protected_runtime_authorization.v1'
        material = $Material
        material_sha256 = $MaterialSha256.ToLowerInvariant()
        terminal_id = $TerminalId.ToLowerInvariant()
        terminal_receipt_relative_path = $TerminalReceiptRelativePath
        terminal_receipt_sha256 = $TerminalReceiptSha256.ToLowerInvariant()
        terminal_journal_relative_path = $TerminalJournalRelativePath
        terminal_journal_sha256 = $TerminalJournalSha256.ToLowerInvariant()
    }
    $hash = Get-DawnstrikeStateBoundarySha256Text (
        ConvertTo-DawnstrikeStateBoundaryCanonicalJsonString $contract
    )
    $probe = [pscustomobject]@{
        current_runtime_authorization_contract = $contract
        current_runtime_authorization_sha256 = $hash
    }
    $verified = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
        -Receipt $probe -Kind current -StateRoot $StateRoot
    return [pscustomobject]@{
        status = 'AUTHORIZED'
        contract = $verified.contract
        sha256 = $verified.sha256
        runtime_sha = $verified.runtime_sha
        runtime_tree = $verified.runtime_tree
        operation_type = $verified.operation_type
    }
}

function Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$JournalPath,
        [string]$ExpectedReceiptSha256 = '',
        [string]$ExpectedJournalSha256 = ''
    )

    $state = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $statePrefix = $state + '\'
    $receipt = [IO.Path]::GetFullPath($ReceiptPath)
    $journal = [IO.Path]::GetFullPath($JournalPath)
    if (-not $receipt.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not $journal.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Task-mutation terminal evidence escaped StateRoot.'
    }
    $receiptRelative = $receipt.Substring($statePrefix.Length).Replace('\', '/')
    $journalRelative = $journal.Substring($statePrefix.Length).Replace('\', '/')
    $expectedReceiptRelative = ''
    $expectedJournalRelative = ''
    $expectedSchema = ''
    $expectedStatus = ''
    $expectedOperation = ''
    switch ($Mode) {
        'HardenCapture' {
            $expectedReceiptRelative = "receipts/capture-task/capture-task-hardening-$ExpectedSha.json"
            $expectedJournalRelative = "receipts/runtime-operation/capture-task-hardening-$ExpectedSha.json"
            $expectedSchema = 'dawnstrike.capture_task_hardening_receipt.v2'
            $expectedStatus = 'COMPLETE'
            $expectedOperation = 'capture_task_hardening'
        }
        'RebindCapture' {
            $expectedReceiptRelative = "receipts/capture-task/capture-task-rebind-$ExpectedSha.json"
            $expectedJournalRelative = "receipts/runtime-operation/capture-task-rebind-$ExpectedSha.json"
            $expectedSchema = 'dawnstrike.capture_task_rebind_receipt.v2'
            $expectedStatus = 'COMPLETE'
            $expectedOperation = 'capture_task_rebind'
        }
        'Activate' {
            if ($receiptRelative -notmatch '^receipts/runtime-activation/runtime-activation-([0-9a-f]{24})\.json$') {
                throw 'Activation task-mutation receipt path is not canonical.'
            }
            $activationId = [string]$Matches[1]
            $expectedReceiptRelative = "receipts/runtime-activation/runtime-activation-$activationId.json"
            $expectedJournalRelative = "receipts/runtime-operation/runtime-activation-$activationId.json"
            $expectedSchema = 'dawnstrike.runtime_activation_receipt.v2'
            $expectedStatus = 'COMPLETE'
            $expectedOperation = 'runtime_activation'
        }
        'BootstrapBaseline' {
            if ($receiptRelative -notmatch '^receipts/runtime-activation/runtime-activation-([0-9a-f]{24})\.json$') {
                throw 'Baseline bootstrap task-mutation receipt path is not canonical.'
            }
            $activationId = [string]$Matches[1]
            $expectedReceiptRelative = "receipts/runtime-activation/runtime-activation-$activationId.json"
            $expectedJournalRelative = "receipts/runtime-operation/runtime-activation-$activationId.json"
            $expectedSchema = 'dawnstrike.runtime_activation_receipt.v2'
            $expectedStatus = 'COMPLETE'
            $expectedOperation = 'runtime_activation'
        }
        'Rollback' {
            if ($receiptRelative -notmatch '^receipts/runtime-rollback/runtime-rollback-([0-9a-f]{24})\.json$') {
                throw 'Rollback task-mutation receipt path is not canonical.'
            }
            $activationId = [string]$Matches[1]
            $expectedReceiptRelative = "receipts/runtime-rollback/runtime-rollback-$activationId.json"
            $expectedJournalRelative = "receipts/runtime-operation/runtime-rollback-$activationId.json"
            $expectedSchema = 'dawnstrike.runtime_rollback_receipt.v1'
            $expectedStatus = 'ROLLED_BACK'
            $expectedOperation = 'runtime_rollback'
        }
    }
    if ($receiptRelative -cne $expectedReceiptRelative -or $journalRelative -cne $expectedJournalRelative) {
        throw 'Task-mutation terminal receipt/journal paths do not match the exact mode identity.'
    }
    $locks = @()
    try {
        $receiptLease = Open-DawnstrikeStateBoundaryPath `
            -Path $receipt -Label 'Task-mutation terminal receipt'
        $locks += $receiptLease.handle
        $receiptStream = [IO.File]::Open($receipt, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $locks += $receiptStream
        $journalLease = Open-DawnstrikeStateBoundaryPath `
            -Path $journal -Label 'Task-mutation terminal journal'
        $locks += $journalLease.handle
        $journalStream = [IO.File]::Open($journal, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $locks += $journalStream
        $receiptMemory = [IO.MemoryStream]::new()
        try { $receiptStream.CopyTo($receiptMemory); $receiptBytes = $receiptMemory.ToArray() }
        finally { $receiptMemory.Dispose() }
        $journalMemory = [IO.MemoryStream]::new()
        try { $journalStream.CopyTo($journalMemory); $journalBytes = $journalMemory.ToArray() }
        finally { $journalMemory.Dispose() }
        $receiptHash = Get-DawnstrikeStateBoundarySha256Bytes $receiptBytes
        $journalHash = Get-DawnstrikeStateBoundarySha256Bytes $journalBytes
        if (($ExpectedReceiptSha256 -and $receiptHash -cne $ExpectedReceiptSha256) -or
            ($ExpectedJournalSha256 -and $journalHash -cne $ExpectedJournalSha256)) {
            throw 'Task-mutation terminal evidence differs from the protected completion.'
        }
        try {
            $receiptPayload = [Text.Encoding]::UTF8.GetString($receiptBytes) | ConvertFrom-Json
            $journalPayload = [Text.Encoding]::UTF8.GetString($journalBytes) | ConvertFrom-Json
        }
        catch { throw 'Task-mutation terminal evidence is not valid JSON.' }
        if (
            [string]$receiptPayload.receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$receiptPayload.receipt_sha256 -cne
                (Get-DawnstrikeStateBoundaryJsonSelfHash `
                    -Payload $receiptPayload -Field receipt_sha256 -TrailingNewline)
        ) { throw 'Task-mutation terminal receipt self hash is invalid.' }
        if (
            [string]$journalPayload.journal_self_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$journalPayload.journal_self_sha256 -cne
                (Get-DawnstrikeStateBoundaryJsonSelfHash `
                    -Payload $journalPayload -Field journal_self_sha256)
        ) { throw 'Task-mutation terminal journal self hash is invalid.' }
        if (
            [string]$receiptPayload.schema_version -cne $expectedSchema -or
            [string]$receiptPayload.status -cne $expectedStatus -or
            [string]$receiptPayload.candidate_sha -cne $ExpectedSha.ToLowerInvariant() -or
            [string]$receiptPayload.candidate_tree -cne $ExpectedTree.ToLowerInvariant() -or
            $receiptPayload.research_only -ne $true -or
            $receiptPayload.broker_execution_enabled -ne $false
        ) { throw 'Task-mutation terminal receipt safety identity is invalid.' }
        if ($Mode -in @('BootstrapBaseline', 'Activate')) {
            if (
                [string]$receiptPayload.activation_id -cne $activationId -or
                [string]$receiptPayload.previous_sha -notmatch '^[0-9a-f]{40}$' -or
                [string]$receiptPayload.previous_tree -notmatch '^[0-9a-f]{40}$' -or
                [string]$receiptPayload.market_date -notmatch '^\d{4}-\d{2}-\d{2}$' -or
                [string]$receiptPayload.scheduler_backup_manifest_sha256 -notmatch '^[0-9a-f]{64}$'
            ) { throw 'Terminal activation receipt rollback identity is incomplete.' }
            try {
                $parsedMarketDate = [DateTime]::ParseExact(
                    [string]$receiptPayload.market_date,
                    'yyyy-MM-dd',
                    [Globalization.CultureInfo]::InvariantCulture,
                    [Globalization.DateTimeStyles]::None
                )
                if ($parsedMarketDate.ToString('yyyy-MM-dd') -cne [string]$receiptPayload.market_date) {
                    throw 'noncanonical'
                }
            }
            catch { throw 'Terminal activation receipt market date is invalid.' }
            $expectedBaseline = $Mode -eq 'BootstrapBaseline'
            if ($receiptPayload.bootstrap_baseline -isnot [bool] -or
                [bool]$receiptPayload.bootstrap_baseline -ne $expectedBaseline) {
                throw 'Terminal activation receipt baseline disposition is invalid.'
            }
            $material = Get-DawnstrikeStateBoundaryRuntimeAuthorizationMaterial `
                -Material $receiptPayload.runtime_authorization_material `
                -StateRoot $StateRoot `
                -ExpectedOperationType $(if ($expectedBaseline) { 'BOOTSTRAP' } else { 'ACTIVATE' }) `
                -ExpectedSha256 ([string]$receiptPayload.runtime_authorization_material_sha256)
            if (
                [string]$material.runtime_sha -cne $ExpectedSha.ToLowerInvariant() -or
                [string]$material.runtime_tree -cne $ExpectedTree.ToLowerInvariant()
            ) { throw 'Terminal runtime authorization material belongs to another release.' }
        }
        $terminalTaskContract = [ordered]@{ mode = $Mode }
        if ($Mode -in @('BootstrapBaseline', 'Activate', 'Rollback')) {
            if (
                $receiptPayload.task_count -isnot [int] -or
                [int]$receiptPayload.task_count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
                [string]$receiptPayload.task_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
                [string]$receiptPayload.task_definition_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
                [string]$receiptPayload.task_action_contract_sha256 -notmatch '^[0-9a-f]{64}$'
            ) { throw 'Terminal activation/rollback receipt task contract is incomplete.' }
            $terminalTaskContract['task_count'] = [int]$receiptPayload.task_count
            $terminalTaskContract['task_contract_sha256'] = [string]$receiptPayload.task_contract_sha256
            $terminalTaskContract['task_definition_contract_sha256'] = [string]$receiptPayload.task_definition_contract_sha256
            $terminalTaskContract['task_action_contract_sha256'] = [string]$receiptPayload.task_action_contract_sha256
            if ($Mode -eq 'Rollback') {
                $hasAuxiliaryPresence = $receiptPayload.PSObject.Properties.Name -contains
                    'auxiliary_capture_present'
                if ($hasAuxiliaryPresence -and
                    $receiptPayload.auxiliary_capture_present -isnot [bool]) {
                    throw 'Terminal rollback receipt has an invalid auxiliary capture presence value.'
                }
                $auxiliaryPresent = if ($hasAuxiliaryPresence) {
                    [bool]$receiptPayload.auxiliary_capture_present
                }
                else { $false }
                $terminalTaskContract['auxiliary_capture_present'] = $auxiliaryPresent
                $terminalTaskContract['auxiliary_capture_disposition'] = if (-not $hasAuxiliaryPresence) {
                    'SCHEMA_V1_ABSENT_REQUIRES_NO_LIVE_AUXILIARY'
                }
                elseif ($auxiliaryPresent) { 'RESTORED_EXACT_PRESENT' }
                else { 'RESTORED_EXACT_ABSENT' }
                if ($hasAuxiliaryPresence) {
                    $terminalTaskContract['auxiliary_capture_action'] =
                        [string]$receiptPayload.auxiliary_capture_action
                }
                if ($auxiliaryPresent) {
                    if (
                        [string]$receiptPayload.auxiliary_capture_action -cne 'RESTORED_EXACT' -or
                        [string]$receiptPayload.auxiliary_capture_state_after -notin @('Ready', 'Disabled') -or
                        [string]$receiptPayload.auxiliary_capture_xml_sha256 -notmatch '^[0-9a-f]{64}$' -or
                        [string]$receiptPayload.auxiliary_capture_definition_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
                        [string]$receiptPayload.auxiliary_capture_action_contract_sha256 -notmatch '^[0-9a-f]{64}$'
                    ) { throw 'Terminal rollback receipt auxiliary task contract is incomplete.' }
                    $terminalTaskContract['auxiliary_capture_state_after'] =
                        [string]$receiptPayload.auxiliary_capture_state_after
                    $terminalTaskContract['auxiliary_capture_xml_sha256'] =
                        [string]$receiptPayload.auxiliary_capture_xml_sha256
                    $terminalTaskContract['auxiliary_capture_definition_contract_sha256'] =
                        [string]$receiptPayload.auxiliary_capture_definition_contract_sha256
                    $terminalTaskContract['auxiliary_capture_action_contract_sha256'] =
                        [string]$receiptPayload.auxiliary_capture_action_contract_sha256
                }
                elseif ($hasAuxiliaryPresence -and
                    [string]$receiptPayload.auxiliary_capture_action -cne 'RESTORED_EXACT') {
                    throw 'Terminal rollback receipt has an invalid absent auxiliary disposition.'
                }
            }
        }
        else {
            if (
                [string]$receiptPayload.task_name -cne $script:DawnstrikeStateBoundaryAuxiliaryTask -or
                [string]$receiptPayload.xml_after_sha256 -notmatch '^[0-9a-f]{64}$' -or
                [string]$receiptPayload.action_after_sha256 -notmatch '^[0-9a-f]{64}$'
            ) { throw 'Terminal capture receipt task contract is incomplete.' }
            $terminalTaskContract['task_name'] = [string]$receiptPayload.task_name
            $terminalTaskContract['xml_after_sha256'] = [string]$receiptPayload.xml_after_sha256
            $terminalTaskContract['action_after_sha256'] = [string]$receiptPayload.action_after_sha256
            if ($Mode -eq 'HardenCapture') {
                if ([string]$receiptPayload.final_state -cne 'Disabled') {
                    throw 'Terminal hardening receipt did not keep capture Disabled.'
                }
                $terminalTaskContract['final_state'] = [string]$receiptPayload.final_state
            }
            else {
                if (
                    [string]$receiptPayload.definition_after_sha256 -notmatch '^[0-9a-f]{64}$' -or
                    [string]$receiptPayload.enablement_after -cne 'Ready'
                ) { throw 'Terminal rebind receipt definition or enablement contract is incomplete.' }
                $terminalTaskContract['definition_after_sha256'] = [string]$receiptPayload.definition_after_sha256
                $terminalTaskContract['enablement_after'] = [string]$receiptPayload.enablement_after
            }
        }
        if (
            [string]$journalPayload.schema_version -notin @(
                'dawnstrike.runtime_operation_journal.v1',
                'dawnstrike.runtime_operation_journal.v2',
                'dawnstrike.runtime_operation_journal.v3'
            ) -or
            [string]$journalPayload.operation -cne $expectedOperation -or
            [string]$journalPayload.phase -cne 'COMPLETE' -or
            [string]$journalPayload.candidate_sha -cne $ExpectedSha.ToLowerInvariant() -or
            [string]$journalPayload.candidate_tree -cne $ExpectedTree.ToLowerInvariant() -or
            [string]$journalPayload.complete_receipt_relative_path -cne $receiptRelative -or
            [string]$journalPayload.complete_receipt_sha256 -cne $receiptHash -or
            $journalPayload.research_only -ne $true -or
            $journalPayload.broker_execution_enabled -ne $false
        ) { throw 'Task-mutation terminal journal is not COMPLETE and bound to the exact receipt.' }
        if ($Mode -in @('BootstrapBaseline', 'Activate') -and (
            [string]$journalPayload.schema_version -cne 'dawnstrike.runtime_operation_journal.v2' -or
            [string]$journalPayload.current_sha -cne [string]$receiptPayload.candidate_sha -or
            [string]$journalPayload.current_tree -cne [string]$receiptPayload.candidate_tree -or
            [string]$journalPayload.previous_sha -cne [string]$receiptPayload.previous_sha -or
            [string]$journalPayload.previous_tree -cne [string]$receiptPayload.previous_tree -or
            [string]$journalPayload.prepared_receipt_relative_path -cne
                "receipts/runtime-activation/runtime-activation-$activationId.prepared.json" -or
            [string]$journalPayload.prepared_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$journalPayload.backup_contract_sha256 -cne
                [string]$receiptPayload.scheduler_backup_manifest_sha256 -or
            [string]$journalPayload.task_contract_sha256 -cne
                [string]$receiptPayload.task_contract_sha256
        )) { throw 'Terminal activation receipt and journal rollback identities do not agree.' }
        return [pscustomobject]@{
            record = [pscustomobject][ordered]@{
                mode = $Mode
                receipt_path = $receipt
                receipt_sha256 = $receiptHash
                journal_path = $journal
                journal_sha256 = $journalHash
                journal_operation = $expectedOperation
                journal_phase = 'COMPLETE'
                task_contract = [pscustomobject]$terminalTaskContract
                activation_id = if ($Mode -in @('BootstrapBaseline', 'Activate')) { $activationId } else { 'NONE' }
                receipt_relative_path = $receiptRelative
                journal_relative_path = $journalRelative
                previous_sha = if ($Mode -in @('BootstrapBaseline', 'Activate')) {
                    [string]$receiptPayload.previous_sha
                }
                else { 'NONE' }
                previous_tree = if ($Mode -in @('BootstrapBaseline', 'Activate')) {
                    [string]$receiptPayload.previous_tree
                }
                else { 'NONE' }
                runtime_authorization_material = if ($Mode -in @('BootstrapBaseline', 'Activate')) {
                    $material.material
                }
                else { $null }
                runtime_authorization_material_sha256 = if ($Mode -in @('BootstrapBaseline', 'Activate')) {
                    $material.sha256
                }
                else { 'NONE' }
            }
            locks = @($locks)
        }
    }
    catch {
        foreach ($lock in $locks) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Assert-DawnstrikeStateBoundaryActivationLineage {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][string]$ActivationReceiptPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$ActivationReceiptSha256,
        [ValidatePattern('^$|^[0-9a-f]{24}$')][string]$ExpectedActivationId = ''
    )

    $fixed = Assert-DawnstrikeStateBoundaryFixedPath `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    $state = [string]$fixed.state_root
    $currentPath = Join-Path ([string]$fixed.evidence_root) 'state-boundary-current.json'
    $currentRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $currentPath
    $terminal = $null
    try {
        $current = $currentRead.payload
        if ([string]$current.schema_version -cne 'dawnstrike.state_boundary_installation.v2' -or
            [string]$current.status -cne 'PASS' -or
            [string]$current.state_root -cne $state -or
            [string]$current.candidate_sha -notmatch '^[0-9a-f]{40}$' -or
            [string]$current.candidate_tree -notmatch '^[0-9a-f]{40}$' -or
            $current.research_only -ne $true -or
            $current.broker_execution_enabled -ne $false) {
            throw 'Protected StateRoot activation lineage owner receipt is invalid.'
        }
        $lineage = Get-DawnstrikeStateBoundaryActivationLineage -Receipt $current
        if ([string]$lineage.status -cne 'ACTIVE') {
            throw 'Protected StateRoot has no rollback-authorizing activation lineage.'
        }
        if ($ExpectedActivationId -and
            [string]$lineage.activation_id -cne $ExpectedActivationId.ToLowerInvariant()) {
            throw 'Protected StateRoot activation lineage belongs to another activation.'
        }
        $expectedReceiptPath = [IO.Path]::GetFullPath(
            (Join-Path $state ([string]$lineage.receipt_relative_path).Replace('/', '\'))
        )
        $expectedJournalPath = [IO.Path]::GetFullPath(
            (Join-Path $state ([string]$lineage.journal_relative_path).Replace('/', '\'))
        )
        if (-not [string]::Equals(
            [IO.Path]::GetFullPath($ActivationReceiptPath),
            $expectedReceiptPath,
            [StringComparison]::OrdinalIgnoreCase
        ) -or [string]$lineage.receipt_sha256 -cne $ActivationReceiptSha256.ToLowerInvariant()) {
            throw 'Supplied activation receipt does not match protected StateRoot lineage.'
        }
        $terminal = Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `
            -StateRoot $state -Mode Activate `
            -ExpectedSha ([string]$current.candidate_sha) `
            -ExpectedTree ([string]$current.candidate_tree) `
            -ReceiptPath $expectedReceiptPath -JournalPath $expectedJournalPath `
            -ExpectedReceiptSha256 ([string]$lineage.receipt_sha256) `
            -ExpectedJournalSha256 ([string]$lineage.journal_sha256)
        if ([string]$terminal.record.activation_id -cne [string]$lineage.activation_id -or
            [string]$terminal.record.receipt_relative_path -cne [string]$lineage.receipt_relative_path -or
            [string]$terminal.record.journal_relative_path -cne [string]$lineage.journal_relative_path) {
            throw 'Protected StateRoot activation lineage terminal identity is inconsistent.'
        }
        return [pscustomobject]@{
            status = 'PASS'
            activation_id = [string]$lineage.activation_id
            receipt_path = $expectedReceiptPath
            receipt_sha256 = [string]$lineage.receipt_sha256
            journal_path = $expectedJournalPath
            journal_sha256 = [string]$lineage.journal_sha256
            current_receipt_path = $currentPath
            current_receipt_sha256 = [string]$currentRead.sha256
            candidate_sha = [string]$current.candidate_sha
            candidate_tree = [string]$current.candidate_tree
            research_only = $true
            broker_execution_enabled = $false
        }
    }
    finally {
        if ($null -ne $terminal) {
            foreach ($lock in @($terminal.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
        $currentRead.stream.Dispose()
    }
}

function Get-DawnstrikeStateBoundaryTerminalCandidatePaths {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha
    )
    $state = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $candidates = @()
    if ($Mode -eq 'HardenCapture') { return @() }
    if ($Mode -eq 'RebindCapture') {
        return @([pscustomobject]@{
            receipt = Join-Path $state "receipts\capture-task\capture-task-rebind-$ExpectedSha.json"
            journal = Join-Path $state "receipts\runtime-operation\capture-task-rebind-$ExpectedSha.json"
        })
    }
    $activationMode = $Mode -in @('BootstrapBaseline', 'Activate')
    $receiptFolderName = if ($activationMode) { 'runtime-activation' } else { 'runtime-rollback' }
    $receiptStem = if ($activationMode) { 'runtime-activation-' } else { 'runtime-rollback-' }
    $receiptFolder = Join-Path $state ("receipts\" + $receiptFolderName)
    if (-not (Test-Path -LiteralPath $receiptFolder)) { return @() }
    $folderLease = Open-DawnstrikeStateBoundaryPath `
        -Path $receiptFolder -Label 'Task-mutation terminal receipt directory'
    try {
        if (-not $folderLease.is_directory) {
            throw 'Task-mutation terminal receipt directory is not a directory.'
        }
        foreach ($item in @(Get-ChildItem -LiteralPath $receiptFolder -File -Force -ErrorAction Stop)) {
            if ([string]$item.Name -cmatch ('^' + [regex]::Escape($receiptStem) + '([0-9a-f]{24})\.json$')) {
                $activationId = [string]$Matches[1]
                $candidates += [pscustomobject]@{
                    receipt = [string]$item.FullName
                    journal = Join-Path $state (
                        "receipts\runtime-operation\$receiptStem$activationId.json"
                    )
                }
            }
        }
    }
    finally { $folderLease.handle.Dispose() }
    return @($candidates)
}

function Get-DawnstrikeStateBoundaryTerminalEvidencePairs {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha
    )
    $pairs = @()
    foreach ($candidate in @(Get-DawnstrikeStateBoundaryTerminalCandidatePaths `
        -StateRoot $StateRoot -Mode $Mode -ExpectedSha $ExpectedSha)) {
        if (-not (Test-Path -LiteralPath ([string]$candidate.receipt) -PathType Leaf) -or
            -not (Test-Path -LiteralPath ([string]$candidate.journal) -PathType Leaf)) {
            continue
        }
        $receiptLease = Open-DawnstrikeStateBoundaryPath `
            -Path ([string]$candidate.receipt) -Label 'Predecessor terminal receipt'
        $journalLease = $null
        try {
            if ($receiptLease.is_directory) { throw 'Predecessor terminal receipt is not a regular file.' }
            $journalLease = Open-DawnstrikeStateBoundaryPath `
                -Path ([string]$candidate.journal) -Label 'Predecessor terminal journal'
            if ($journalLease.is_directory) { throw 'Predecessor terminal journal is not a regular file.' }
            $receiptHash = Get-DawnstrikeStateBoundarySha256File ([string]$candidate.receipt)
            $journalHash = Get-DawnstrikeStateBoundarySha256File ([string]$candidate.journal)
            $pairs += ($receiptHash + ':' + $journalHash)
        }
        finally {
            if ($null -ne $journalLease) { $journalLease.handle.Dispose() }
            $receiptLease.handle.Dispose()
        }
    }
    return @($pairs | Sort-Object -Unique)
}

function Complete-DawnstrikeStateBoundaryTaskMutationAdoption {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Intent,
        [Parameter(Mandatory = $true)]$Completion,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [switch]$ValidationOnly
    )
    $intentPayload = $Intent.payload
    $completionPayload = $Completion.payload
    if (
        [string]$completionPayload.schema_version -cne 'dawnstrike.state_boundary_task_mutation_completion.v1' -or
        [string]$completionPayload.operation_id -cne [string]$intentPayload.operation_id -or
        [string]$completionPayload.mode -cne [string]$intentPayload.mode -or
        [string]$completionPayload.expected_sha -cne [string]$intentPayload.expected_sha -or
        [string]$completionPayload.expected_tree -cne [string]$intentPayload.expected_tree -or
        [string]$completionPayload.request_contract_sha256 -cne
            [string]$intentPayload.request_contract_sha256 -or
        [string]$completionPayload.predecessor_terminal_evidence_sha256 -cne
            [string]$intentPayload.predecessor_terminal_evidence_sha256 -or
        [string]$completionPayload.old_current_receipt_sha256 -cne [string]$intentPayload.old_current_receipt_sha256 -or
        [string]$completionPayload.new_current_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $completionPayload.research_only -ne $true -or
        $completionPayload.broker_execution_enabled -ne $false
    ) { throw 'Protected StateRoot task-mutation completion is invalid.' }
    $newReceipt = $completionPayload.new_current_receipt
    if (
        $null -eq $newReceipt -or
        [string]$newReceipt.schema_version -cne 'dawnstrike.state_boundary_installation.v2' -or
        [string]$newReceipt.status -cne 'PASS' -or
        [string]$newReceipt.candidate_sha -cne [string]$intentPayload.expected_sha -or
        [string]$newReceipt.candidate_tree -cne [string]$intentPayload.expected_tree -or
        [string]$newReceipt.state_root -cne [IO.Path]::GetFullPath($StateRoot).TrimEnd('\') -or
        [string]$newReceipt.task_binding_operation_id -cne [string]$intentPayload.operation_id -or
        [string]$newReceipt.task_binding_mode -cne [string]$intentPayload.mode -or
        [string]$newReceipt.task_binding_release_sha -cne [string]$intentPayload.expected_sha -or
        [string]$newReceipt.task_binding_release_tree -cne [string]$intentPayload.expected_tree -or
        [string]$newReceipt.task_binding_request_contract_sha256 -cne
            [string]$intentPayload.request_contract_sha256 -or
        [string]$newReceipt.task_binding_predecessor_terminal_evidence_sha256 -cne
            [string]$intentPayload.predecessor_terminal_evidence_sha256 -or
        $newReceipt.research_only -ne $true -or
        $newReceipt.broker_execution_enabled -ne $false
    ) { throw 'Protected task-mutation completion receipt safety identity is invalid.' }
    if ((Get-DawnstrikeStateBoundaryTaskBindingHash -Tasks $newReceipt.task_definitions_and_principals) -cne
        [string]$newReceipt.task_binding_sha256) {
        throw 'Protected task-mutation completion task binding hash is invalid.'
    }
    $newJson = $newReceipt | ConvertTo-Json -Depth 20
    $newHash = Get-DawnstrikeStateBoundarySha256Text ($newJson + "`r`n")
    if ($newHash -cne [string]$completionPayload.new_current_receipt_sha256) {
        throw 'Protected task-mutation completion receipt hash is invalid.'
    }
    $terminalRecord = $completionPayload.terminal_evidence
    if ($null -eq $terminalRecord -or
        [string]$newReceipt.task_binding_terminal_receipt_sha256 -cne [string]$terminalRecord.receipt_sha256 -or
        [string]$newReceipt.task_binding_terminal_journal_sha256 -cne [string]$terminalRecord.journal_sha256) {
        throw 'Protected task-mutation completion does not bind its terminal mode evidence.'
    }
    $intentActivationLineage = Get-DawnstrikeStateBoundaryActivationLineage `
        -Receipt $intentPayload
    $newActivationLineage = Get-DawnstrikeStateBoundaryActivationLineage `
        -Receipt $newReceipt
    $mode = [string]$intentPayload.mode
    if ($mode -eq 'Activate') {
        if ([string]$newActivationLineage.status -cne 'ACTIVE' -or
            [string]$newActivationLineage.activation_id -cne [string]$terminalRecord.activation_id -or
            [string]$newActivationLineage.receipt_relative_path -cne [string]$terminalRecord.receipt_relative_path -or
            [string]$newActivationLineage.receipt_sha256 -cne [string]$terminalRecord.receipt_sha256 -or
            [string]$newActivationLineage.journal_relative_path -cne [string]$terminalRecord.journal_relative_path -or
            [string]$newActivationLineage.journal_sha256 -cne [string]$terminalRecord.journal_sha256) {
            throw 'Protected activation completion did not advance exact activation lineage.'
        }
    }
    elseif ($mode -eq 'Rollback') {
        if ([string]$intentActivationLineage.status -cne 'ACTIVE' -or
            [string]$newActivationLineage.status -cne 'NONE') {
            throw 'Protected rollback completion did not consume exact activation lineage.'
        }
    }
    elseif (
        [string]$newActivationLineage.activation_id -cne [string]$intentActivationLineage.activation_id -or
        [string]$newActivationLineage.receipt_relative_path -cne [string]$intentActivationLineage.receipt_relative_path -or
        [string]$newActivationLineage.receipt_sha256 -cne [string]$intentActivationLineage.receipt_sha256 -or
        [string]$newActivationLineage.journal_relative_path -cne [string]$intentActivationLineage.journal_relative_path -or
        [string]$newActivationLineage.journal_sha256 -cne [string]$intentActivationLineage.journal_sha256
    ) { throw 'Protected capture mutation changed activation lineage.' }
    $intentCurrentRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
        -Receipt $intentPayload -Kind current -StateRoot $StateRoot
    $intentRollbackRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
        -Receipt $intentPayload -Kind rollback -StateRoot $StateRoot
    $newCurrentRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
        -Receipt $newReceipt -Kind current -StateRoot $StateRoot
    $newRollbackRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
        -Receipt $newReceipt -Kind rollback -StateRoot $StateRoot
    if ($mode -eq 'Activate') {
        if (
            [string]$intentCurrentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
            [string]$newCurrentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
            [string]$newCurrentRuntimeAuthorization.operation_type -cne 'ACTIVATE' -or
            [string]$newRollbackRuntimeAuthorization.sha256 -cne
                [string]$intentCurrentRuntimeAuthorization.sha256
        ) { throw 'Protected activation completion has an invalid runtime-authorization transition.' }
    }
    elseif ($mode -eq 'BootstrapBaseline') {
        if (
            [string]$intentCurrentRuntimeAuthorization.status -notin @('NONE', 'LEGACY_NONE') -or
            [string]$newCurrentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
            [string]$newCurrentRuntimeAuthorization.operation_type -cne 'BOOTSTRAP' -or
            [string]$newRollbackRuntimeAuthorization.status -cne 'NONE'
        ) { throw 'Protected baseline completion has an invalid runtime-authorization transition.' }
    }
    elseif ($mode -eq 'Rollback') {
        if (
            [string]$intentRollbackRuntimeAuthorization.status -cne 'AUTHORIZED' -or
            [string]$newCurrentRuntimeAuthorization.sha256 -cne
                [string]$intentRollbackRuntimeAuthorization.sha256 -or
            [string]$newRollbackRuntimeAuthorization.status -cne 'NONE'
        ) { throw 'Protected rollback completion has an invalid runtime-authorization transition.' }
    }
    elseif (
        [string]$newCurrentRuntimeAuthorization.sha256 -cne
            [string]$intentCurrentRuntimeAuthorization.sha256 -or
        [string]$newRollbackRuntimeAuthorization.sha256 -cne
            [string]$intentRollbackRuntimeAuthorization.sha256
    ) { throw 'Protected capture mutation changed runtime authorization.' }
    $terminalEvidence = Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `
        -StateRoot $StateRoot -Mode ([string]$intentPayload.mode) `
        -ExpectedSha ([string]$intentPayload.expected_sha) `
        -ExpectedTree ([string]$intentPayload.expected_tree) `
        -ReceiptPath ([string]$terminalRecord.receipt_path) `
        -JournalPath ([string]$terminalRecord.journal_path) `
        -ExpectedReceiptSha256 ([string]$terminalRecord.receipt_sha256) `
        -ExpectedJournalSha256 ([string]$terminalRecord.journal_sha256)
    $terminalActivationIdentityChanged = $mode -in @('BootstrapBaseline', 'Activate') -and (
        [string]$terminalRecord.activation_id -cne [string]$terminalEvidence.record.activation_id -or
        [string]$terminalRecord.receipt_relative_path -cne
            [string]$terminalEvidence.record.receipt_relative_path -or
        [string]$terminalRecord.journal_relative_path -cne
            [string]$terminalEvidence.record.journal_relative_path
    )
    if (
        (([pscustomobject]$terminalRecord.task_contract | ConvertTo-Json -Compress) -cne
            ($terminalEvidence.record.task_contract | ConvertTo-Json -Compress)) -or
        $terminalActivationIdentityChanged
    ) { throw 'Protected completion terminal task contract differs from the exact terminal receipt.' }
    if ($mode -in @('BootstrapBaseline', 'Activate') -and (
        [string]$newCurrentRuntimeAuthorization.terminal_id -cne
            [string]$terminalEvidence.record.activation_id -or
        [string]$newCurrentRuntimeAuthorization.contract.terminal_receipt_sha256 -cne
            [string]$terminalEvidence.record.receipt_sha256 -or
        [string]$newCurrentRuntimeAuthorization.contract.terminal_journal_sha256 -cne
            [string]$terminalEvidence.record.journal_sha256 -or
        [string]$newCurrentRuntimeAuthorization.contract.material_sha256 -cne
            [string]$terminalEvidence.record.runtime_authorization_material_sha256
    )) { throw 'Protected completion runtime authorization differs from the terminal evidence.' }
    $operationId = [string]$intentPayload.operation_id
    try {
        $boundary = Assert-DawnstrikeStateRootBoundary `
            -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot `
            -AllowedTaskMutationOperationId $operationId -AllowTaskDefinitionDrift
    }
    catch {
        foreach ($lock in @($terminalEvidence.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
    try {
        if ([string]$boundary.receipt_sha256 -notin @(
            [string]$intentPayload.old_current_receipt_sha256,
            [string]$completionPayload.new_current_receipt_sha256
        )) { throw 'Current StateRoot receipt is outside the task-mutation completion lineage.' }
        $immutableProperties = @(
            'schema_version', 'status', 'operation_id', 'installed_at_utc',
            'candidate_sha', 'candidate_tree', 'state_root',
            'state_root_identity', 'state_root_sddl', 'state_root_sddl_sha256',
            'locks_root', 'locks_root_identity', 'locks_root_sddl', 'locks_root_sddl_sha256',
            'state_entry_count', 'state_identity_contract_sha256',
            'rollback_manifest_path', 'rollback_manifest_sha256',
            'installed_helper_path', 'installed_helper_sha256'
        )
        foreach ($name in $immutableProperties) {
            if ([string]$newReceipt.$name -cne [string]$boundary.receipt.$name) {
                throw 'Protected task-mutation completion changed immutable StateRoot receipt identity.'
            }
        }
        $newWriterSet = @($newReceipt.writer_sids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        $oldWriterSet = @($boundary.receipt.writer_sids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        if (($newWriterSet -join "`n") -cne ($oldWriterSet -join "`n")) {
            throw 'Protected task-mutation completion changed the StateRoot writer SID set.'
        }
        $liveTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
            -ExpectedTasks $newReceipt.task_definitions_and_principals `
            -LiveTasks $liveTasks -WriterSids @($newReceipt.writer_sids)
        $terminalLiveContract = Assert-DawnstrikeStateBoundaryTerminalTaskContract `
            -Mode ([string]$intentPayload.mode) `
            -TerminalRecord $terminalEvidence.record.task_contract -LiveTasks $liveTasks
        $terminalLiveContractHash = Get-DawnstrikeStateBoundarySha256Text (
            $terminalLiveContract | ConvertTo-Json -Compress
        )
        if ([string]$newReceipt.task_binding_terminal_task_contract_sha256 -cne
            $terminalLiveContractHash) {
            throw 'Protected task-mutation completion does not bind the exact live terminal task contract.'
        }
        if ($ValidationOnly -and
            [string]$boundary.receipt_sha256 -cne [string]$completionPayload.new_current_receipt_sha256) {
            throw 'Read admission completion lineage is not the exact new current receipt.'
        }
        if (-not $ValidationOnly -and
            [string]$boundary.receipt_sha256 -cne [string]$completionPayload.new_current_receipt_sha256) {
            $currentPath = Join-Path $EvidenceRoot 'state-boundary-current.json'
            $boundary.locks[0].Dispose()
            $boundary.locks = @($boundary.locks | Select-Object -Skip 1)
            $write = Write-DawnstrikeStateBoundaryProtectedJson -Payload $newReceipt -Path $currentPath
            if ($write.sha256 -cne [string]$completionPayload.new_current_receipt_sha256) {
                throw 'Adopted StateRoot task binding receipt hash mismatch.'
            }
        }
        $sealed = Assert-DawnstrikeStateRootBoundary `
            -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot `
            -AllowedTaskMutationOperationId $operationId
        try {
            if ([string]$sealed.receipt_sha256 -cne [string]$completionPayload.new_current_receipt_sha256) {
                throw 'Adopted StateRoot task binding did not read back exactly.'
            }
        }
        finally {
            foreach ($lock in @($sealed.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
        if ($ValidationOnly) {
            return [pscustomobject]@{
                status = 'VALIDATED_COMPLETION_LINEAGE'
                operation_id = $operationId
                mode = [string]$intentPayload.mode
                already_completed = $true
                receipt_sha256 = [string]$completionPayload.new_current_receipt_sha256
                writer_sids = @($newReceipt.writer_sids)
                locks = @($boundary.locks)
                research_only = $true
                broker_execution_enabled = $false
            }
        }
        Remove-Item -LiteralPath ([string]$Intent.path) -Force -ErrorAction Stop
        return [pscustomobject]@{
            status = 'ADOPTED_COMPLETE'
            operation_id = $operationId
            mode = [string]$intentPayload.mode
            already_completed = $true
            receipt_sha256 = [string]$completionPayload.new_current_receipt_sha256
            writer_sids = @($newReceipt.writer_sids)
            locks = @($boundary.locks)
            research_only = $true
            broker_execution_enabled = $false
        }
    }
    catch {
        foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
    finally {
        foreach ($lock in @($terminalEvidence.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
    }
}

function Complete-DawnstrikeStateBoundaryTaskMutationFailClosedAdoption {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Intent,
        [Parameter(Mandatory = $true)]$Completion,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [switch]$ValidationOnly,
        [ValidateSet('', 'after_completion', 'after_current')]
        [string]$TestCrashPoint = ''
    )
    $intentPayload = $Intent.payload
    $payload = $Completion.payload
    $newReceipt = $payload.new_current_receipt
    if ([string]$intentPayload.mode -cne 'Activate' -or
        [string]$payload.schema_version -cne
            'dawnstrike.state_boundary_task_mutation_fail_closed.v1' -or
        [string]$payload.status -cne 'COMPENSATED_DISABLED' -or
        [string]$payload.operation_id -cne [string]$intentPayload.operation_id -or
        [string]$payload.expected_sha -cne [string]$intentPayload.expected_sha -or
        [string]$payload.expected_tree -cne [string]$intentPayload.expected_tree -or
        [string]$payload.request_contract_sha256 -cne
            [string]$intentPayload.request_contract_sha256 -or
        [string]$payload.old_current_receipt_sha256 -cne
            [string]$intentPayload.old_current_receipt_sha256 -or
        [string]$payload.source_activation_id -notmatch '^[0-9a-f]{24}$' -or
        [string]$payload.source_terminal_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.source_terminal_journal_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.compensation_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.compensation_journal_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.new_current_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $payload.research_only -ne $true -or
        $payload.broker_execution_enabled -ne $false) {
        throw 'Protected fail-closed task-mutation completion is invalid.'
    }
    $operationId = [string]$intentPayload.operation_id
    $state = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $sourceActivationId = [string]$payload.source_activation_id
    $sourceReceiptPath = Join-Path $state (
        "receipts\runtime-activation\runtime-activation-$sourceActivationId.json"
    )
    $sourceJournalPath = Join-Path $state (
        "receipts\runtime-operation\runtime-activation-$sourceActivationId.json"
    )
    $expectedJournalPath = Join-Path $state "receipts\runtime-operation\terminal-recovery-$operationId.json"
    $receiptPath = [IO.Path]::GetFullPath([string]$payload.compensation_receipt_path)
    $journalPath = [IO.Path]::GetFullPath([string]$payload.compensation_journal_path)
    if (-not [string]::Equals(
            $journalPath, $expectedJournalPath, [StringComparison]::OrdinalIgnoreCase
        ) -or -not $receiptPath.StartsWith(
            ($state + '\receipts\runtime-activation\'),
            [StringComparison]::OrdinalIgnoreCase
        )) { throw 'Protected fail-closed evidence paths are outside their exact namespace.' }
    $receiptEvidence = Open-DawnstrikeStateBoundaryExactFile `
        -Path $receiptPath `
        -ExpectedSha256 ([string]$payload.compensation_receipt_sha256) `
        -Label 'Activation fail-closed compensation receipt'
    $journalEvidence = $null
    $sourceTerminalEvidence = $null
    $authorizationEvidence = $null
    $boundary = $null
    try {
        $journalEvidence = Open-DawnstrikeStateBoundaryExactFile `
            -Path $journalPath `
            -ExpectedSha256 ([string]$payload.compensation_journal_sha256) `
            -Label 'Activation fail-closed compensation journal'
        try {
            $compensation = [Text.Encoding]::UTF8.GetString(
                $receiptEvidence.bytes
            ) | ConvertFrom-Json
            $journal = [Text.Encoding]::UTF8.GetString(
                $journalEvidence.bytes
            ) | ConvertFrom-Json
        }
        catch { throw 'Activation fail-closed evidence is invalid JSON.' }
        $relativeReceipt = [string]$journal.compensation_receipt_relative_path
        $boundReceiptPath = Join-Path $state ($relativeReceipt.Replace('/', '\'))
        $currentAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $intentPayload -Kind current -StateRoot $state
        if ([string]$currentAuthorization.status -cne 'AUTHORIZED') {
            throw 'Activation fail-closed predecessor has no protected runtime authorization.'
        }
        $sourceTerminalEvidence = Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `
            -StateRoot $state -Mode Activate `
            -ExpectedSha ([string]$intentPayload.expected_sha) `
            -ExpectedTree ([string]$intentPayload.expected_tree) `
            -ReceiptPath $sourceReceiptPath -JournalPath $sourceJournalPath `
            -ExpectedReceiptSha256 ([string]$payload.source_terminal_receipt_sha256) `
            -ExpectedJournalSha256 ([string]$payload.source_terminal_journal_sha256)
        $sourceTerminal = $sourceTerminalEvidence.record
        if ([string]$sourceTerminal.activation_id -cne $sourceActivationId -or
            [string]$sourceTerminal.previous_sha -cne [string]$currentAuthorization.runtime_sha -or
            [string]$sourceTerminal.previous_tree -cne [string]$currentAuthorization.runtime_tree) {
            throw 'Activation fail-closed source terminal does not name the protected predecessor.'
        }
        $authorizationEvidence = Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence `
            -Authorization $currentAuthorization -StateRoot $state `
            -ExpectedOperationType ([string]$currentAuthorization.operation_type) `
            -ExpectedSha ([string]$currentAuthorization.runtime_sha) `
            -ExpectedTree ([string]$currentAuthorization.runtime_tree)
        if ([string]$currentAuthorization.status -cne 'AUTHORIZED' -or
            [string]$compensation.schema_version -cne
                'dawnstrike.runtime_compensation_receipt.v2' -or
            [string]$compensation.status -cne 'COMPENSATED' -or
            [string]$compensation.operation -cne 'runtime_activation' -or
            [string]$compensation.candidate_sha -cne [string]$intentPayload.expected_sha -or
            [string]$compensation.candidate_tree -cne [string]$intentPayload.expected_tree -or
            [string]$compensation.task_state -cne 'Disabled' -or
            [string]$journal.schema_version -cne 'dawnstrike.runtime_operation_journal.v2' -or
            [string]$journal.operation -cne 'runtime_activation' -or
            [string]$journal.phase -cne 'COMPENSATED' -or
            [string]$journal.candidate_sha -cne [string]$intentPayload.expected_sha -or
            [string]$journal.candidate_tree -cne [string]$intentPayload.expected_tree -or
            [string]$journal.current_sha -cne [string]$currentAuthorization.runtime_sha -or
            [string]$journal.current_tree -cne [string]$currentAuthorization.runtime_tree -or
            [string]$journal.previous_sha -cne [string]$currentAuthorization.runtime_sha -or
            [string]$journal.previous_tree -cne [string]$currentAuthorization.runtime_tree -or
            [string]$journal.task_contract_sha256 -cne
                [string]$compensation.task_contract_sha256 -or
            [string]$journal.prior_journal_file_sha256 -cne
                [string]$compensation.prior_journal_file_sha256 -or
            [string]$journal.compensation_receipt_sha256 -cne
                [string]$payload.compensation_receipt_sha256 -or
            -not [string]::Equals(
                [IO.Path]::GetFullPath($boundReceiptPath), $receiptPath,
                [StringComparison]::OrdinalIgnoreCase
            )) { throw 'Activation fail-closed evidence does not restore the protected predecessor.' }
        if ($null -eq $newReceipt -or
            [string]$newReceipt.schema_version -cne 'dawnstrike.state_boundary_installation.v2' -or
            [string]$newReceipt.status -cne 'PASS' -or
            [string]$newReceipt.candidate_sha -cne [string]$intentPayload.candidate_sha -or
            [string]$newReceipt.candidate_tree -cne [string]$intentPayload.candidate_tree -or
            [string]$newReceipt.task_binding_operation_id -cne $operationId -or
            [string]$newReceipt.task_binding_mode -cne 'ActivateFailClosed' -or
            [string]$newReceipt.task_binding_release_sha -cne
                [string]$currentAuthorization.runtime_sha -or
            [string]$newReceipt.task_binding_release_tree -cne
                [string]$currentAuthorization.runtime_tree -or
            [string]$newReceipt.task_binding_request_contract_sha256 -cne
                [string]$intentPayload.request_contract_sha256 -or
            [string]$newReceipt.canonical_task_disposition -cne
                'DISABLED_BY_GOVERNED_ACTIVATION_CANCELLATION' -or
            [string]$newReceipt.fail_closed_compensation_receipt_sha256 -cne
                [string]$payload.compensation_receipt_sha256 -or
            [string]$newReceipt.fail_closed_compensation_journal_sha256 -cne
                [string]$payload.compensation_journal_sha256 -or
            [string]$newReceipt.fail_closed_source_activation_id -cne $sourceActivationId -or
            [string]$newReceipt.fail_closed_source_terminal_receipt_sha256 -cne
                [string]$payload.source_terminal_receipt_sha256 -or
            [string]$newReceipt.fail_closed_source_terminal_journal_sha256 -cne
                [string]$payload.source_terminal_journal_sha256 -or
            [string]$newReceipt.task_binding_predecessor_terminal_evidence_sha256 -cne
                [string]$intentPayload.predecessor_terminal_evidence_sha256 -or
            $newReceipt.research_only -ne $true -or
            $newReceipt.broker_execution_enabled -ne $false) {
            throw 'Protected fail-closed current receipt identity is invalid.'
        }
        $newJson = $newReceipt | ConvertTo-Json -Depth 20
        $newHash = Get-DawnstrikeStateBoundarySha256Text ($newJson + "`r`n")
        if ($newHash -cne [string]$payload.new_current_receipt_sha256 -or
            (Get-DawnstrikeStateBoundaryTaskBindingHash `
                -Tasks $newReceipt.task_definitions_and_principals) -cne
                [string]$newReceipt.task_binding_sha256) {
            throw 'Protected fail-closed current receipt hash is invalid.'
        }
        $priorLineage = Get-DawnstrikeStateBoundaryActivationLineage -Receipt $intentPayload
        $nextLineage = Get-DawnstrikeStateBoundaryActivationLineage -Receipt $newReceipt
        $priorRollback = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $intentPayload -Kind rollback -StateRoot $state
        $nextCurrent = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $newReceipt -Kind current -StateRoot $state
        $nextRollback = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $newReceipt -Kind rollback -StateRoot $state
        if ([string]$nextCurrent.sha256 -cne [string]$currentAuthorization.sha256 -or
            [string]$nextRollback.sha256 -cne [string]$priorRollback.sha256 -or
            [string]$nextLineage.status -cne [string]$priorLineage.status -or
            [string]$nextLineage.activation_id -cne [string]$priorLineage.activation_id -or
            [string]$nextLineage.receipt_relative_path -cne
                [string]$priorLineage.receipt_relative_path -or
            [string]$nextLineage.receipt_sha256 -cne [string]$priorLineage.receipt_sha256 -or
            [string]$nextLineage.journal_relative_path -cne
                [string]$priorLineage.journal_relative_path -or
            [string]$nextLineage.journal_sha256 -cne [string]$priorLineage.journal_sha256) {
            throw 'Fail-closed activation cancellation changed protected authorization or lineage.'
        }
        $boundary = Assert-DawnstrikeStateRootBoundary `
            -StateRoot $state -EvidenceRoot $EvidenceRoot `
            -AllowedTaskMutationOperationId $operationId -AllowTaskDefinitionDrift
        if ([string]$boundary.receipt_sha256 -notin @(
            [string]$intentPayload.old_current_receipt_sha256, $newHash
        )) { throw 'Fail-closed current receipt is outside its protected lineage.' }
        $immutableProperties = @(
            'schema_version', 'status', 'operation_id', 'installed_at_utc',
            'candidate_sha', 'candidate_tree', 'state_root',
            'state_root_identity', 'state_root_sddl', 'state_root_sddl_sha256',
            'locks_root', 'locks_root_identity', 'locks_root_sddl', 'locks_root_sddl_sha256',
            'state_entry_count', 'state_identity_contract_sha256',
            'rollback_manifest_path', 'rollback_manifest_sha256',
            'installed_helper_path', 'installed_helper_sha256'
        )
        foreach ($name in $immutableProperties) {
            if ([string]$newReceipt.$name -cne [string]$boundary.receipt.$name) {
                throw 'Fail-closed completion changed immutable StateRoot receipt identity.'
            }
        }
        $newWriterSet = @($newReceipt.writer_sids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        $oldWriterSet = @($boundary.receipt.writer_sids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        if (($newWriterSet -join "`n") -cne ($oldWriterSet -join "`n")) {
            throw 'Fail-closed completion changed the StateRoot writer SID set.'
        }
        $live = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $null = Assert-DawnstrikeStateBoundaryFailClosedTaskInventory `
            -ExpectedTasks $intentPayload.task_definitions_and_principals `
            -LiveTasks $live -WriterSids @($intentPayload.writer_sids)
        $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
            -ExpectedTasks $newReceipt.task_definitions_and_principals `
            -LiveTasks $live -WriterSids @($newReceipt.writer_sids)
        $canonical = @($live | Where-Object { [bool]$_.canonical })
        if ($canonical.Count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
            [string]$canonical[0].canonical_task_definition_contract_sha256 -cne
                [string]$compensation.task_definition_contract_sha256 -or
            [string]$canonical[0].canonical_task_action_contract_sha256 -cne
                [string]$compensation.task_action_contract_sha256 -or
            [string]$canonical[0].canonical_task_definition_contract_sha256 -cne
                [string]$currentAuthorization.material.canonical_task_definition_contract_sha256 -or
            [string]$canonical[0].canonical_task_action_contract_sha256 -cne
                [string]$currentAuthorization.material.canonical_task_action_contract_sha256) {
            throw 'Fail-closed compensation task contract differs from the live predecessor.'
        }
        $completionPath = [IO.Path]::GetFullPath([string]$Intent.payload.completion_path)
        if (-not [string]::Equals(
                [IO.Path]::GetFullPath([string]$Completion.path), $completionPath,
                [StringComparison]::OrdinalIgnoreCase
            )) { throw 'Fail-closed completion path is not the protected intent destination.' }
        $completionJson = $payload | ConvertTo-Json -Depth 20
        $completionHash = Get-DawnstrikeStateBoundarySha256Text ($completionJson + "`r`n")
        if ([string]$Completion.sha256 -cne $completionHash) {
            throw 'Fail-closed completion file hash is invalid.'
        }
        if ($ValidationOnly -and [string]$boundary.receipt_sha256 -cne $newHash) {
            throw 'Fail-closed read admission is not the exact new current receipt.'
        }
        if (-not $ValidationOnly) {
            $completionWrite = Write-DawnstrikeStateBoundaryProtectedJson `
                -Payload $payload -Path $completionPath -NoReplace
            if ([string]$completionWrite.sha256 -cne $completionHash) {
                throw 'Fail-closed protected completion write did not read back exactly.'
            }
            if ($TestCrashPoint -ceq 'after_completion') {
                throw 'TEST_CRASH_AFTER_FAIL_CLOSED_COMPLETION'
            }
        }
        if (-not $ValidationOnly -and [string]$boundary.receipt_sha256 -cne $newHash) {
            $currentPath = Join-Path $EvidenceRoot 'state-boundary-current.json'
            $boundary.locks[0].Dispose()
            $boundary.locks = @($boundary.locks | Select-Object -Skip 1)
            $write = Write-DawnstrikeStateBoundaryProtectedJson `
                -Payload $newReceipt -Path $currentPath
            if ([string]$write.sha256 -cne $newHash) {
                throw 'Fail-closed StateRoot current receipt write did not read back exactly.'
            }
        }
        $sealed = Assert-DawnstrikeStateRootBoundary `
            -StateRoot $state -EvidenceRoot $EvidenceRoot `
            -AllowedTaskMutationOperationId $operationId
        try {
            if ([string]$sealed.receipt_sha256 -cne $newHash) {
                throw 'Fail-closed StateRoot reseal did not converge.'
            }
        }
        finally {
            foreach ($lock in @($sealed.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
        if (-not $ValidationOnly -and $TestCrashPoint -ceq 'after_current') {
            throw 'TEST_CRASH_AFTER_FAIL_CLOSED_CURRENT'
        }
        if (-not $ValidationOnly) {
            Remove-Item -LiteralPath ([string]$Intent.path) -Force -ErrorAction Stop
        }
        return [pscustomobject]@{
            status = $(if ($ValidationOnly) {
                'VALIDATED_FAIL_CLOSED'
            } else { 'COMPENSATED_DISABLED' })
            operation_id = $operationId
            mode = 'Activate'
            already_completed = $true
            receipt_sha256 = $newHash
            writer_sids = @($newReceipt.writer_sids)
            locks = @($boundary.locks)
            research_only = $true
            broker_execution_enabled = $false
        }
    }
    catch {
        if ($null -ne $boundary) {
            foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
        throw
    }
    finally {
        if ($null -ne $authorizationEvidence) {
            foreach ($lock in @($authorizationEvidence.locks)) {
                if ($null -ne $lock) { $lock.Dispose() }
            }
        }
        if ($null -ne $sourceTerminalEvidence) {
            foreach ($lock in @($sourceTerminalEvidence.locks)) {
                if ($null -ne $lock) { $lock.Dispose() }
            }
        }
        if ($null -ne $journalEvidence) {
            $journalEvidence.stream.Dispose()
            $journalEvidence.lease.Dispose()
        }
        $receiptEvidence.stream.Dispose()
        $receiptEvidence.lease.Dispose()
    }
}

function Complete-DawnstrikeStateBoundaryTaskMutationFailClosed {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$RequestContractSha256,
        [Parameter(Mandatory = $true)][string]$CompensationReceiptPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$CompensationReceiptSha256,
        [Parameter(Mandatory = $true)][string]$CompensationJournalPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$CompensationJournalSha256,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{24}$')]
        [string]$SourceActivationId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$SourceTerminalReceiptSha256,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$SourceTerminalJournalSha256,
        [ValidateSet('', 'after_completion', 'after_current')]
        [string]$TestCrashPoint = ''
    )
    $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $EvidenceRoot
    if ($null -eq $intent -or
        [string]$intent.payload.operation_id -cne $OperationId) {
        throw 'Protected fail-closed mutation intent is missing or changed.'
    }
    $null = Assert-DawnstrikeStateBoundaryTaskMutationIntent `
        -Intent $intent -StateRoot $StateRoot -Mode Activate `
        -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree `
        -RequestContractSha256 $RequestContractSha256
    $boundary = Assert-DawnstrikeStateRootBoundary `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot `
        -AllowedTaskMutationOperationId $OperationId -AllowTaskDefinitionDrift
    try {
        if ([string]$boundary.receipt_sha256 -cne
                [string]$intent.payload.old_current_receipt_sha256 -or
            [string]$boundary.receipt.task_binding_sha256 -cne
                [string]$intent.payload.old_task_binding_sha256) {
            throw 'Protected fail-closed completion predecessor changed.'
        }
        $live = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $null = Assert-DawnstrikeStateBoundaryFailClosedTaskInventory `
            -ExpectedTasks $boundary.receipt.task_definitions_and_principals `
            -LiveTasks $live -WriterSids @($boundary.writer_sids)
        $currentAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $intent.payload -Kind current -StateRoot $StateRoot
        $newReceipt = Copy-DawnstrikeStateBoundaryReceipt -Receipt $boundary.receipt
        $newReceipt['task_definitions_and_principals'] = @($live)
        $newReceipt['task_binding_sha256'] =
            Get-DawnstrikeStateBoundaryTaskBindingHash -Tasks $live
        $newReceipt['task_binding_operation_id'] = $OperationId
        $newReceipt['task_binding_mode'] = 'ActivateFailClosed'
        $newReceipt['task_binding_release_sha'] = [string]$currentAuthorization.runtime_sha
        $newReceipt['task_binding_release_tree'] = [string]$currentAuthorization.runtime_tree
        $newReceipt['task_binding_request_contract_sha256'] =
            $RequestContractSha256.ToLowerInvariant()
        $newReceipt['canonical_task_disposition'] =
            'DISABLED_BY_GOVERNED_ACTIVATION_CANCELLATION'
        $capture = @($live | Where-Object { -not [bool]$_.canonical })
        $newReceipt['auxiliary_capture_disposition'] = if ($capture.Count -eq 0) {
            'ABSENT_BY_GOVERNED_ACTIVATION_CANCELLATION'
        } else { 'DISABLED_BY_GOVERNED_ACTIVATION_CANCELLATION' }
        $newReceipt['fail_closed_compensation_receipt_sha256'] =
            $CompensationReceiptSha256.ToLowerInvariant()
        $newReceipt['fail_closed_compensation_journal_sha256'] =
            $CompensationJournalSha256.ToLowerInvariant()
        $newReceipt['fail_closed_source_activation_id'] =
            $SourceActivationId.ToLowerInvariant()
        $newReceipt['fail_closed_source_terminal_receipt_sha256'] =
            $SourceTerminalReceiptSha256.ToLowerInvariant()
        $newReceipt['fail_closed_source_terminal_journal_sha256'] =
            $SourceTerminalJournalSha256.ToLowerInvariant()
        $newReceipt['task_binding_predecessor_terminal_evidence_sha256'] =
            [string]$intent.payload.predecessor_terminal_evidence_sha256
        $newJson = $newReceipt | ConvertTo-Json -Depth 20
        $newHash = Get-DawnstrikeStateBoundarySha256Text ($newJson + "`r`n")
        $completionPayload = [ordered]@{
            schema_version = 'dawnstrike.state_boundary_task_mutation_fail_closed.v1'
            status = 'COMPENSATED_DISABLED'
            operation_id = $OperationId
            expected_sha = $ExpectedSha.ToLowerInvariant()
            expected_tree = $ExpectedTree.ToLowerInvariant()
            request_contract_sha256 = $RequestContractSha256.ToLowerInvariant()
            old_current_receipt_sha256 = [string]$intent.payload.old_current_receipt_sha256
            source_activation_id = $SourceActivationId.ToLowerInvariant()
            source_terminal_receipt_sha256 = $SourceTerminalReceiptSha256.ToLowerInvariant()
            source_terminal_journal_sha256 = $SourceTerminalJournalSha256.ToLowerInvariant()
            compensation_receipt_path = [IO.Path]::GetFullPath($CompensationReceiptPath)
            compensation_receipt_sha256 = $CompensationReceiptSha256.ToLowerInvariant()
            compensation_journal_path = [IO.Path]::GetFullPath($CompensationJournalPath)
            compensation_journal_sha256 = $CompensationJournalSha256.ToLowerInvariant()
            new_current_receipt_sha256 = $newHash
            new_current_receipt = $newReceipt
            research_only = $true
            broker_execution_enabled = $false
        }
    }
    finally {
        foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
    }
    $completionJson = $completionPayload | ConvertTo-Json -Depth 20
    $completion = [pscustomobject]@{
        path = [string]$intent.payload.completion_path
        payload = [pscustomobject]$completionPayload
        sha256 = Get-DawnstrikeStateBoundarySha256Text ($completionJson + "`r`n")
    }
    return Complete-DawnstrikeStateBoundaryTaskMutationFailClosedAdoption `
        -Intent $intent -Completion $completion `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot `
        -TestCrashPoint $TestCrashPoint
}

function Get-DawnstrikeStateBoundaryTaskMutationReadAdmission {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree
    )
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'StateRoot task-mutation request admission requires an elevated administrator process.'
    }
    $fixed = Assert-DawnstrikeStateBoundaryFixedPath -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    $state = [string]$fixed.state_root
    $evidence = [string]$fixed.evidence_root
    $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $evidence
    if ($null -eq $intent) {
        return Assert-DawnstrikeStateRootBoundary -StateRoot $state -EvidenceRoot $evidence
    }
    $null = Assert-DawnstrikeStateBoundaryTaskMutationIntent `
        -Intent $intent -StateRoot $state -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree
    $operationId = [string]$intent.payload.operation_id
    $boundary = Assert-DawnstrikeStateRootBoundary `
        -StateRoot $state -EvidenceRoot $evidence `
        -AllowedTaskMutationOperationId $operationId -AllowTaskDefinitionDrift
    try {
        $isExactPredecessor = (
            [string]$boundary.receipt_sha256 -ceq [string]$intent.payload.old_current_receipt_sha256 -and
            [string]$boundary.receipt.task_binding_sha256 -ceq [string]$intent.payload.old_task_binding_sha256
        )
        if (-not $isExactPredecessor) {
            # A hard kill may occur after Complete durably writes its protected
            # completion and exact new current receipt but before removing the
            # intent. Permit request bytes to be hashed only after the entire
            # completion/terminal/live-task lineage has been revalidated. Enter
            # will then verify this launcher's request hash and remove the intent.
            $completionPath = [string]$intent.payload.completion_path
            if (-not (Test-Path -LiteralPath $completionPath -PathType Leaf)) {
                throw 'StateRoot task-mutation request admission has a changed predecessor without completion.'
            }
            $completionRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $completionPath
            try {
                $completion = [pscustomobject]@{
                    path = $completionPath
                    payload = $completionRead.payload
                    sha256 = $completionRead.sha256
                }
            }
            finally { $completionRead.stream.Dispose() }
            $validatedCompletion = if (
                [string]$completion.payload.schema_version -ceq
                    'dawnstrike.state_boundary_task_mutation_fail_closed.v1'
            ) {
                Complete-DawnstrikeStateBoundaryTaskMutationFailClosedAdoption `
                    -Intent $intent -Completion $completion `
                    -StateRoot $state -EvidenceRoot $evidence -ValidationOnly
            }
            else {
                Complete-DawnstrikeStateBoundaryTaskMutationAdoption `
                    -Intent $intent -Completion $completion `
                    -StateRoot $state -EvidenceRoot $evidence -ValidationOnly
            }
            try {
                if ([string]$validatedCompletion.receipt_sha256 -cne [string]$boundary.receipt_sha256) {
                    throw 'StateRoot task-mutation request admission completion receipt changed.'
                }
            }
            finally {
                foreach ($lock in @($validatedCompletion.locks)) {
                    if ($null -ne $lock) { $lock.Dispose() }
                }
            }
        }
        return $boundary
    }
    catch {
        foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Enter-DawnstrikeStateBoundaryTaskMutation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$RequestContractSha256
    )
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'StateRoot task mutation requires an elevated administrator process.'
    }
    $fixed = Assert-DawnstrikeStateBoundaryFixedPath -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    $state = [string]$fixed.state_root
    $evidence = [string]$fixed.evidence_root
    $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $evidence
    if ($null -eq $intent) {
        $boundary = Assert-DawnstrikeStateRootBoundary -StateRoot $state -EvidenceRoot $evidence
        try {
            if (
                [string]$boundary.candidate_sha -cne $ExpectedSha.ToLowerInvariant() -or
                [string]$boundary.candidate_tree -cne $ExpectedTree.ToLowerInvariant()
            ) { throw 'StateRoot host boundary belongs to another candidate SHA/tree.' }
            $affected = @(Get-DawnstrikeStateBoundaryTaskMutationAffectedNames -Mode $Mode)
            $liveTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
            if (@($liveTasks | Where-Object {
                [string]$_.task_name -in $affected -and [string]$_.state -eq 'Running'
            }).Count -ne 0) { throw 'An affected task is Running; task mutation is denied.' }
            $operationId = [Guid]::NewGuid().ToString('N')
            $predecessorTerminalEvidencePairs = @(
                Get-DawnstrikeStateBoundaryTerminalEvidencePairs `
                    -StateRoot $state -Mode $Mode -ExpectedSha $ExpectedSha
            )
            $predecessorActivationLineage = Get-DawnstrikeStateBoundaryActivationLineage `
                -Receipt $boundary.receipt
            $predecessorCurrentRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
                -Receipt $boundary.receipt -Kind current -StateRoot $state
            $predecessorRollbackRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
                -Receipt $boundary.receipt -Kind rollback -StateRoot $state
            if ($Mode -eq 'Activate' -and
                [string]$predecessorCurrentRuntimeAuthorization.status -cne 'AUTHORIZED') {
                throw 'Activation requires an exact protected current-runtime authorization.'
            }
            if ($Mode -eq 'BootstrapBaseline' -and (
                [string]$predecessorCurrentRuntimeAuthorization.status -notin @('NONE', 'LEGACY_NONE') -or
                [string]$predecessorRollbackRuntimeAuthorization.status -notin @('NONE', 'LEGACY_NONE') -or
                [string]$predecessorActivationLineage.status -notin @('NONE', 'LEGACY_NONE')
            )) { throw 'Baseline bootstrap is restricted to the first protected runtime authorization.' }
            if ($Mode -eq 'Rollback' -and (
                [string]$predecessorCurrentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
                [string]$predecessorRollbackRuntimeAuthorization.status -cne 'AUTHORIZED'
            )) { throw 'Rollback requires exact protected current and predecessor runtime authorizations.' }
            $completionPath = Join-Path $evidence (
                'state-boundary-task-mutation-completion-' + $operationId + '.json'
            )
            $payload = [ordered]@{
                schema_version = 'dawnstrike.state_boundary_task_mutation.v1'
                operation_id = $operationId
                created_at_utc = [DateTime]::UtcNow.ToString('o')
                mode = $Mode
                expected_sha = $ExpectedSha.ToLowerInvariant()
                expected_tree = $ExpectedTree.ToLowerInvariant()
                state_root = $state
                old_current_receipt_sha256 = [string]$boundary.receipt_sha256
                old_task_binding_sha256 = [string]$boundary.receipt.task_binding_sha256
                last_activation_id = [string]$predecessorActivationLineage.activation_id
                last_activation_terminal_receipt_relative_path =
                    [string]$predecessorActivationLineage.receipt_relative_path
                last_activation_terminal_receipt_sha256 =
                    [string]$predecessorActivationLineage.receipt_sha256
                last_activation_terminal_journal_relative_path =
                    [string]$predecessorActivationLineage.journal_relative_path
                last_activation_terminal_journal_sha256 =
                    [string]$predecessorActivationLineage.journal_sha256
                current_runtime_authorization_contract = if (
                    [string]$predecessorCurrentRuntimeAuthorization.status -eq 'AUTHORIZED'
                ) { $predecessorCurrentRuntimeAuthorization.contract } else { 'NONE' }
                current_runtime_authorization_sha256 = if (
                    [string]$predecessorCurrentRuntimeAuthorization.status -eq 'AUTHORIZED'
                ) { [string]$predecessorCurrentRuntimeAuthorization.sha256 } else { 'NONE' }
                rollback_runtime_authorization_contract = if (
                    [string]$predecessorRollbackRuntimeAuthorization.status -eq 'AUTHORIZED'
                ) { $predecessorRollbackRuntimeAuthorization.contract } else { 'NONE' }
                rollback_runtime_authorization_sha256 = if (
                    [string]$predecessorRollbackRuntimeAuthorization.status -eq 'AUTHORIZED'
                ) { [string]$predecessorRollbackRuntimeAuthorization.sha256 } else { 'NONE' }
                request_contract_sha256 = $RequestContractSha256.ToLowerInvariant()
                predecessor_terminal_evidence_pairs = @($predecessorTerminalEvidencePairs)
                predecessor_terminal_evidence_sha256 = Get-DawnstrikeStateBoundarySha256Text (
                    ($predecessorTerminalEvidencePairs -join "`n") + "`n"
                )
                affected_tasks = @($affected)
                completion_path = $completionPath
                research_only = $true
                broker_execution_enabled = $false
            }
            $intentPath = Get-DawnstrikeStateBoundaryTaskMutationIntentPath -EvidenceRoot $evidence
            # Unique, no-replace creation is the linearization point. Two
            # elevated launchers must never overwrite each other's operation.
            $write = Write-DawnstrikeStateBoundaryProtectedJson `
                -Payload $payload -Path $intentPath -NoReplace
            return [pscustomobject]@{
                status = 'PREPARED'
                operation_id = $operationId
                mode = $Mode
                intent_path = $intentPath
                intent_sha256 = [string]$write.sha256
                already_completed = $false
                resumed = $false
                writer_sids = @($boundary.writer_sids)
                locks = @($boundary.locks)
            }
        }
        catch {
            foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
            throw
        }
    }

    $null = Assert-DawnstrikeStateBoundaryTaskMutationIntent `
        -Intent $intent -StateRoot $state -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree `
        -RequestContractSha256 $RequestContractSha256
    $completionPath = [string]$intent.payload.completion_path
    if (Test-Path -LiteralPath $completionPath -PathType Leaf) {
        $completionRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $completionPath
        try { $completionObject = [pscustomobject]@{ path = $completionPath; payload = $completionRead.payload; sha256 = $completionRead.sha256 } }
        finally { $completionRead.stream.Dispose() }
        if ([string]$completionObject.payload.schema_version -ceq
            'dawnstrike.state_boundary_task_mutation_fail_closed.v1') {
            return Complete-DawnstrikeStateBoundaryTaskMutationFailClosedAdoption `
                -Intent $intent -Completion $completionObject `
                -StateRoot $state -EvidenceRoot $evidence
        }
        return Complete-DawnstrikeStateBoundaryTaskMutationAdoption `
            -Intent $intent -Completion $completionObject `
            -StateRoot $state -EvidenceRoot $evidence
    }
    $operationId = [string]$intent.payload.operation_id
    $boundary = Assert-DawnstrikeStateRootBoundary `
        -StateRoot $state -EvidenceRoot $evidence `
        -AllowedTaskMutationOperationId $operationId -AllowTaskDefinitionDrift
    try {
        if ([string]$boundary.receipt_sha256 -cne [string]$intent.payload.old_current_receipt_sha256 -or
            [string]$boundary.receipt.task_binding_sha256 -cne [string]$intent.payload.old_task_binding_sha256) {
            throw 'StateRoot task-mutation predecessor receipt changed without a completion record.'
        }
        $liveTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $affected = @($intent.payload.affected_tasks | ForEach-Object { [string]$_ })
        $null = Assert-DawnstrikeStateBoundaryTaskMutationScope `
            -ExpectedTasks $boundary.receipt.task_definitions_and_principals `
            -LiveTasks $liveTasks -AffectedTasks $affected
        $drift = @($liveTasks | Where-Object {
            $live = $_
            $expected = @($boundary.receipt.task_definitions_and_principals | Where-Object {
                [string]$_.task_name -ceq [string]$live.task_name
            })[0]
            [string]$expected.definition_sha256 -cne [string]$live.definition_sha256
        })
        if (@($liveTasks | Where-Object {
            [string]$_.task_name -in $affected -and [string]$_.state -eq 'Running'
        }).Count -ne 0) { throw 'An affected task is Running; recovery dispatch is denied.' }
        if ($drift.Count -ne 0) {
            Disable-DawnstrikeStateBoundaryAffectedTasks -TaskNames $affected
        }
        # StateRoot writers can create, self-hash, or make terminal-named files
        # unreadable. They are never authentication and are inspected only
        # after every changed affected task has been isolated. Thus a hostile
        # file-open failure cannot bypass fail-closed task disablement.
        $currentTerminalPairs = @(
            Get-DawnstrikeStateBoundaryTerminalEvidencePairs `
                -StateRoot $state -Mode $Mode -ExpectedSha $ExpectedSha
        )
        $postIntentTerminalPairs = @($currentTerminalPairs | Where-Object {
            $_ -notin @($intent.payload.predecessor_terminal_evidence_pairs)
        })
        $terminalReconciliationRequired = $postIntentTerminalPairs.Count -gt 0
        return [pscustomobject]@{
            status = 'RESUME_REQUIRED'
            operation_id = $operationId
            mode = $Mode
            intent_path = [string]$intent.path
            intent_sha256 = [string]$intent.sha256
            already_completed = $false
            resumed = $true
            terminal_reconciliation_required = $terminalReconciliationRequired
            writer_sids = @($boundary.writer_sids)
            locks = @($boundary.locks)
        }
    }
    catch {
        foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Complete-DawnstrikeStateBoundaryTaskMutation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$RequestContractSha256,
        [Parameter(Mandatory = $true)][string]$TerminalReceiptPath,
        [Parameter(Mandatory = $true)][string]$TerminalJournalPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$TerminalReceiptSha256,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$TerminalJournalSha256
    )
    $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $EvidenceRoot
    if ($null -eq $intent -or [string]$intent.payload.operation_id -cne $OperationId) {
        throw 'Protected StateRoot task-mutation intent is missing or changed.'
    }
    $null = Assert-DawnstrikeStateBoundaryTaskMutationIntent `
        -Intent $intent -StateRoot $StateRoot -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree `
        -RequestContractSha256 $RequestContractSha256
    $terminalEvidence = Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `
        -StateRoot $StateRoot -Mode $Mode -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree `
        -ReceiptPath $TerminalReceiptPath -JournalPath $TerminalJournalPath `
        -ExpectedReceiptSha256 $TerminalReceiptSha256.ToLowerInvariant() `
        -ExpectedJournalSha256 $TerminalJournalSha256.ToLowerInvariant()
    try {
        $boundary = Assert-DawnstrikeStateRootBoundary `
            -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot `
            -AllowedTaskMutationOperationId $OperationId -AllowTaskDefinitionDrift
    }
    catch {
        foreach ($lock in @($terminalEvidence.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
    try {
        if ([string]$boundary.receipt_sha256 -cne [string]$intent.payload.old_current_receipt_sha256) {
            throw 'StateRoot task-mutation predecessor receipt changed before completion.'
        }
        if ([string]$boundary.receipt.task_binding_sha256 -cne [string]$intent.payload.old_task_binding_sha256) {
            throw 'StateRoot task-mutation predecessor binding changed before completion.'
        }
        $liveTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $affected = @($intent.payload.affected_tasks | ForEach-Object { [string]$_ })
        $null = Assert-DawnstrikeStateBoundaryTaskMutationScope `
            -ExpectedTasks $boundary.receipt.task_definitions_and_principals `
            -LiveTasks $liveTasks -AffectedTasks $affected
        $terminalLiveContract = Assert-DawnstrikeStateBoundaryTerminalTaskContract `
            -Mode $Mode -TerminalRecord $terminalEvidence.record.task_contract `
            -LiveTasks $liveTasks
        $terminalLiveContractHash = Get-DawnstrikeStateBoundarySha256Text (
            $terminalLiveContract | ConvertTo-Json -Compress
        )
        if (@($liveTasks | Where-Object {
            [string]$_.task_name -in $affected -and [string]$_.state -eq 'Running'
        }).Count -ne 0) { throw 'A task is still Running at task-binding completion.' }
        $newReceipt = Copy-DawnstrikeStateBoundaryReceipt -Receipt $boundary.receipt
        $newReceipt['task_definitions_and_principals'] = @($liveTasks)
        $newReceipt['task_binding_sha256'] = Get-DawnstrikeStateBoundaryTaskBindingHash -Tasks $liveTasks
        $newReceipt['task_binding_operation_id'] = $OperationId
        $newReceipt['task_binding_mode'] = $Mode
        $newReceipt['task_binding_release_sha'] = $ExpectedSha.ToLowerInvariant()
        $newReceipt['task_binding_release_tree'] = $ExpectedTree.ToLowerInvariant()
        $newReceipt['task_binding_request_contract_sha256'] =
            [string]$intent.payload.request_contract_sha256
        $newReceipt['task_binding_predecessor_terminal_evidence_sha256'] =
            [string]$intent.payload.predecessor_terminal_evidence_sha256
        $newReceipt['task_binding_updated_at_utc'] = [DateTime]::UtcNow.ToString('o')
        $newReceipt['task_binding_terminal_receipt_sha256'] = [string]$terminalEvidence.record.receipt_sha256
        $newReceipt['task_binding_terminal_journal_sha256'] = [string]$terminalEvidence.record.journal_sha256
        $newReceipt['task_binding_terminal_task_contract_sha256'] = $terminalLiveContractHash
        $priorCurrentRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $intent.payload -Kind current -StateRoot $StateRoot
        $priorRollbackRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $intent.payload -Kind rollback -StateRoot $StateRoot
        $nextCurrentRuntimeAuthorization = $null
        $nextRollbackRuntimeAuthorization = $null
        if ($Mode -in @('BootstrapBaseline', 'Activate')) {
            $material = $terminalEvidence.record.runtime_authorization_material
            if (
                [string]$material.operation_id -cne $OperationId -or
                [string]$material.request_contract_sha256 -cne
                    [string]$intent.payload.request_contract_sha256 -or
                [string]$material.canonical_task_definition_contract_sha256 -cne
                    [string]$terminalEvidence.record.task_contract.task_definition_contract_sha256 -or
                [string]$material.canonical_task_action_contract_sha256 -cne
                    [string]$terminalEvidence.record.task_contract.task_action_contract_sha256
            ) { throw 'Terminal runtime authorization is not bound to this exact protected mutation.' }
            if ($Mode -eq 'Activate' -and (
                [string]$priorCurrentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
                [string]$priorCurrentRuntimeAuthorization.runtime_sha -cne
                    [string]$terminalEvidence.record.previous_sha -or
                [string]$priorCurrentRuntimeAuthorization.runtime_tree -cne
                    [string]$terminalEvidence.record.previous_tree
            )) { throw 'Activation terminal does not consume the exact authorized predecessor runtime.' }
            if ($Mode -eq 'BootstrapBaseline' -and
                [string]$priorCurrentRuntimeAuthorization.status -notin @('NONE', 'LEGACY_NONE')) {
                throw 'Baseline bootstrap cannot replace an already authorized runtime.'
            }
            $nextCurrentRuntimeAuthorization = New-DawnstrikeStateBoundaryRuntimeAuthorization `
                -Material $material `
                -MaterialSha256 ([string]$terminalEvidence.record.runtime_authorization_material_sha256) `
                -TerminalId ([string]$terminalEvidence.record.activation_id) `
                -TerminalReceiptRelativePath ([string]$terminalEvidence.record.receipt_relative_path) `
                -TerminalReceiptSha256 ([string]$terminalEvidence.record.receipt_sha256) `
                -TerminalJournalRelativePath ([string]$terminalEvidence.record.journal_relative_path) `
                -TerminalJournalSha256 ([string]$terminalEvidence.record.journal_sha256) `
                -StateRoot $StateRoot
            $nextRollbackRuntimeAuthorization = if ($Mode -eq 'Activate') {
                $priorCurrentRuntimeAuthorization
            }
            else { [pscustomobject]@{ status = 'NONE' } }
        }
        elseif ($Mode -eq 'Rollback') {
            if (
                [string]$priorCurrentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
                [string]$priorRollbackRuntimeAuthorization.status -cne 'AUTHORIZED' -or
                [string]$priorRollbackRuntimeAuthorization.material.canonical_task_definition_contract_sha256 -cne
                    [string]$terminalEvidence.record.task_contract.task_definition_contract_sha256 -or
                [string]$priorRollbackRuntimeAuthorization.material.canonical_task_action_contract_sha256 -cne
                    [string]$terminalEvidence.record.task_contract.task_action_contract_sha256
            ) { throw 'Rollback terminal does not restore the exact protected predecessor runtime contract.' }
            $nextCurrentRuntimeAuthorization = $priorRollbackRuntimeAuthorization
            $nextRollbackRuntimeAuthorization = [pscustomobject]@{ status = 'NONE' }
        }
        else {
            $nextCurrentRuntimeAuthorization = $priorCurrentRuntimeAuthorization
            $nextRollbackRuntimeAuthorization = $priorRollbackRuntimeAuthorization
        }
        $newReceipt['current_runtime_authorization_contract'] = if (
            [string]$nextCurrentRuntimeAuthorization.status -eq 'AUTHORIZED'
        ) { $nextCurrentRuntimeAuthorization.contract } else { 'NONE' }
        $newReceipt['current_runtime_authorization_sha256'] = if (
            [string]$nextCurrentRuntimeAuthorization.status -eq 'AUTHORIZED'
        ) { [string]$nextCurrentRuntimeAuthorization.sha256 } else { 'NONE' }
        $newReceipt['rollback_runtime_authorization_contract'] = if (
            [string]$nextRollbackRuntimeAuthorization.status -eq 'AUTHORIZED'
        ) { $nextRollbackRuntimeAuthorization.contract } else { 'NONE' }
        $newReceipt['rollback_runtime_authorization_sha256'] = if (
            [string]$nextRollbackRuntimeAuthorization.status -eq 'AUTHORIZED'
        ) { [string]$nextRollbackRuntimeAuthorization.sha256 } else { 'NONE' }
        $priorActivationLineage = Get-DawnstrikeStateBoundaryActivationLineage `
            -Receipt $intent.payload
        $nextActivationLineage = if ($Mode -eq 'Activate') {
            [pscustomobject]@{
                activation_id = [string]$terminalEvidence.record.activation_id
                receipt_relative_path = [string]$terminalEvidence.record.receipt_relative_path
                receipt_sha256 = [string]$terminalEvidence.record.receipt_sha256
                journal_relative_path = [string]$terminalEvidence.record.journal_relative_path
                journal_sha256 = [string]$terminalEvidence.record.journal_sha256
            }
        }
        elseif ($Mode -eq 'Rollback') {
            [pscustomobject]@{
                activation_id = 'NONE'
                receipt_relative_path = 'NONE'
                receipt_sha256 = 'NONE'
                journal_relative_path = 'NONE'
                journal_sha256 = 'NONE'
            }
        }
        else { $priorActivationLineage }
        $newReceipt['last_activation_id'] = [string]$nextActivationLineage.activation_id
        $newReceipt['last_activation_terminal_receipt_relative_path'] =
            [string]$nextActivationLineage.receipt_relative_path
        $newReceipt['last_activation_terminal_receipt_sha256'] =
            [string]$nextActivationLineage.receipt_sha256
        $newReceipt['last_activation_terminal_journal_relative_path'] =
            [string]$nextActivationLineage.journal_relative_path
        $newReceipt['last_activation_terminal_journal_sha256'] =
            [string]$nextActivationLineage.journal_sha256
        if ($Mode -in @('Activate', 'Rollback')) {
            $canonical = @($liveTasks | Where-Object { [bool]$_.canonical })
            if ($canonical.Count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
                @($canonical | Where-Object { [string]$_.state -ne 'Ready' }).Count -ne 0) {
                throw 'Governed activation/rollback did not leave every canonical task Ready.'
            }
            $newReceipt['canonical_task_disposition'] = 'ENABLED_BY_GOVERNED_' + $Mode.ToUpperInvariant() + '_RESEAL'
        }
        elseif ($Mode -eq 'BootstrapBaseline') {
            $canonical = @($liveTasks | Where-Object { [bool]$_.canonical })
            if ($canonical.Count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
                @($canonical | Where-Object { [string]$_.state -ne 'Disabled' }).Count -ne 0) {
                throw 'Governed baseline bootstrap did not leave every canonical task Disabled.'
            }
            $newReceipt['canonical_task_disposition'] =
                'DISABLED_BY_GOVERNED_BOOTSTRAP_BASELINE_RESEAL'
        }
        $capture = @($liveTasks | Where-Object {
            [string]$_.task_name -ceq $script:DawnstrikeStateBoundaryAuxiliaryTask
        })
        $newReceipt['auxiliary_capture_disposition'] = if ($Mode -eq 'Rollback') {
            if ($capture.Count -eq 0) { 'ABSENT_RESTORED_BY_GOVERNED_ROLLBACK_RESEAL' }
            elseif ([string]$capture[0].state -eq 'Ready') {
                'ENABLED_BY_GOVERNED_ROLLBACK_RESEAL'
            }
            else { 'DISABLED_BY_GOVERNED_ROLLBACK_RESEAL' }
        }
        elseif ($capture.Count -eq 1 -and [string]$capture[0].state -eq 'Ready') {
            'ENABLED_BY_GOVERNED_HARDEN_CAPTURE_REBIND'
        }
        else { 'DISABLED_PENDING_GOVERNED_HARDEN_CAPTURE_REBIND' }
        $newJson = $newReceipt | ConvertTo-Json -Depth 20
        $newHash = Get-DawnstrikeStateBoundarySha256Text ($newJson + "`r`n")
        $completionPayload = [ordered]@{
            schema_version = 'dawnstrike.state_boundary_task_mutation_completion.v1'
            operation_id = $OperationId
            completed_at_utc = [DateTime]::UtcNow.ToString('o')
            mode = $Mode
            expected_sha = $ExpectedSha.ToLowerInvariant()
            expected_tree = $ExpectedTree.ToLowerInvariant()
            request_contract_sha256 = [string]$intent.payload.request_contract_sha256
            predecessor_terminal_evidence_sha256 =
                [string]$intent.payload.predecessor_terminal_evidence_sha256
            old_current_receipt_sha256 = [string]$intent.payload.old_current_receipt_sha256
            new_current_receipt_sha256 = $newHash
            new_current_receipt = $newReceipt
            terminal_evidence = $terminalEvidence.record
            research_only = $true
            broker_execution_enabled = $false
        }
        $completionPath = [string]$intent.payload.completion_path
        $null = Write-DawnstrikeStateBoundaryProtectedJson `
            -Payload $completionPayload -Path $completionPath
        $currentPath = Join-Path $EvidenceRoot 'state-boundary-current.json'
        $boundary.locks[0].Dispose()
        $boundary.locks = @($boundary.locks | Select-Object -Skip 1)
        $currentWrite = Write-DawnstrikeStateBoundaryProtectedJson -Payload $newReceipt -Path $currentPath
        if ($currentWrite.sha256 -cne $newHash) {
            throw 'Completed StateRoot task binding receipt hash mismatch.'
        }
        $sealed = Assert-DawnstrikeStateRootBoundary `
            -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot `
            -AllowedTaskMutationOperationId $OperationId
        try {
            if ([string]$sealed.receipt_sha256 -cne $newHash) {
                throw 'Completed StateRoot task binding did not read back exactly.'
            }
        }
        finally {
            foreach ($lock in @($sealed.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
        Remove-Item -LiteralPath ([string]$intent.path) -Force -ErrorAction Stop
        return [pscustomobject]@{
            status = 'COMPLETE'
            operation_id = $OperationId
            mode = $Mode
            already_completed = $false
            receipt_path = $currentPath
            receipt_sha256 = $newHash
            completion_path = $completionPath
            research_only = $true
            broker_execution_enabled = $false
        }
    }
    finally {
        foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        foreach ($lock in @($terminalEvidence.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
    }
}

function Cancel-DawnstrikeStateBoundaryTaskMutationIfUnchanged {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId
    )
    $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $EvidenceRoot
    if ($null -eq $intent -or [string]$intent.payload.operation_id -cne $OperationId) { return $false }
    $boundary = Assert-DawnstrikeStateRootBoundary `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot `
        -AllowedTaskMutationOperationId $OperationId -AllowTaskDefinitionDrift
    try {
        if ([string]$boundary.receipt_sha256 -cne [string]$intent.payload.old_current_receipt_sha256 -or
            [string]$boundary.receipt.task_binding_sha256 -cne [string]$intent.payload.old_task_binding_sha256) {
            throw 'StateRoot task-mutation cancellation does not match its predecessor receipt.'
        }
        $live = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $affected = @($intent.payload.affected_tasks | ForEach-Object { [string]$_ })
        try {
            $null = Assert-DawnstrikeStateBoundaryTaskMutationScope `
                -ExpectedTasks $boundary.receipt.task_definitions_and_principals `
                -LiveTasks $live -AffectedTasks $affected
            $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
                -ExpectedTasks $boundary.receipt.task_definitions_and_principals `
                -LiveTasks $live -WriterSids @($boundary.writer_sids)
            $currentTerminalPairs = @(
                Get-DawnstrikeStateBoundaryTerminalEvidencePairs `
                    -StateRoot $StateRoot -Mode ([string]$intent.payload.mode) `
                    -ExpectedSha ([string]$intent.payload.expected_sha)
            )
            $postIntentTerminalPairs = @($currentTerminalPairs | Where-Object {
                $_ -notin @($intent.payload.predecessor_terminal_evidence_pairs)
            })
            if ($postIntentTerminalPairs.Count -gt 0) {
                # A terminal-named tuple can only preserve this intent. The
                # next launcher admission must dispatch the exact governed
                # mode; no writer-owned tuple is promoted or treated as proof.
                return $false
            }
            Remove-Item -LiteralPath ([string]$intent.path) -Force -ErrorAction Stop
            return $true
        }
        catch {
            Disable-DawnstrikeStateBoundaryAffectedTasks -TaskNames $affected
            return $false
        }
    }
    finally {
        foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
    }
}

function Get-DawnstrikeStateBoundaryAclSddl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    return $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All)
}

function New-DawnstrikeStateBoundaryAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][bool]$Directory,
        [Parameter(Mandatory = $true)][string[]]$WriterSids,
        [switch]$AnchorDirectory
    )

    $acl = if ($Directory) {
        [Security.AccessControl.DirectorySecurity]::new()
    }
    else { [Security.AccessControl.FileSecurity]::new() }
    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $inheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    else { [Security.AccessControl.InheritanceFlags]::None }
    $propagation = [Security.AccessControl.PropagationFlags]::None
    foreach ($sid in @($administrators, $system)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            $sid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            $inheritance,
            $propagation,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    $writerRights = [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::Synchronize
    $writerDirectoryRights = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Synchronize
    foreach ($writerSid in @($WriterSids | Sort-Object -Unique)) {
        if ($writerSid -in @($administrators.Value, $system.Value)) { continue }
        $sid = [Security.Principal.SecurityIdentifier]::new($writerSid)
        if ($Directory -and $AnchorDirectory) {
            # The directory itself permits traversal and child creation but
            # not DELETE, DELETE_CHILD, WRITE_DAC, or WRITE_OWNER.  Descendant
            # objects inherit Modify so normal task-owned file lifecycles work
            # without making StateRoot/locks renameable.
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $sid, $writerDirectoryRights,
                [Security.AccessControl.InheritanceFlags]::None,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            ))
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $sid, $writerRights, $inheritance,
                [Security.AccessControl.PropagationFlags]::InheritOnly,
                [Security.AccessControl.AccessControlType]::Allow
            ))
        }
        else {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $sid, $writerRights, $inheritance, $propagation,
                [Security.AccessControl.AccessControlType]::Allow
            ))
        }
    }
    return $acl
}

function Assert-DawnstrikeStateBoundaryAclObject {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Acl,
        [Parameter(Mandatory = $true)][bool]$Directory,
        [Parameter(Mandatory = $true)][string[]]$WriterSids,
        [switch]$AnchorDirectory,
        [string]$Label = 'StateRoot path'
    )

    $administrators = 'S-1-5-32-544'
    $system = 'S-1-5-18'
    $owner = try {
        $Acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    }
    catch { '' }
    if ($owner -cne $administrators) { throw "$Label is not owned by BUILTIN\Administrators." }
    if (-not $Acl.AreAccessRulesProtected) { throw "$Label inherits its DACL." }
    if (-not $Acl.AreAccessRulesCanonical) { throw "$Label DACL is not canonical." }

    $expectedInheritance = if ($Directory) {
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
    }
    else { [Security.AccessControl.InheritanceFlags]::None }
    $expectedRules = @{}
    foreach ($sid in @($administrators, $system)) {
        $key = @(
            $sid,
            [int64][Security.AccessControl.FileSystemRights]::FullControl,
            [int][Security.AccessControl.AccessControlType]::Allow,
            [int]$expectedInheritance,
            [int][Security.AccessControl.PropagationFlags]::None
        ) -join '|'
        $expectedRules[$key] = $true
    }
    $writerRights = [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::Synchronize
    $writerDirectoryRights = [Security.AccessControl.FileSystemRights]::ReadAndExecute -bor
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Synchronize
    foreach ($writerSid in @($WriterSids | Sort-Object -Unique)) {
        if ($writerSid -in @($administrators, $system)) { continue }
        if ($Directory -and $AnchorDirectory) {
            $directKey = @(
                $writerSid, [int64]$writerDirectoryRights,
                [int][Security.AccessControl.AccessControlType]::Allow,
                [int][Security.AccessControl.InheritanceFlags]::None,
                [int][Security.AccessControl.PropagationFlags]::None
            ) -join '|'
            $inheritKey = @(
                $writerSid, [int64]$writerRights,
                [int][Security.AccessControl.AccessControlType]::Allow,
                [int]$expectedInheritance,
                [int][Security.AccessControl.PropagationFlags]::InheritOnly
            ) -join '|'
            $expectedRules[$directKey] = $true
            $expectedRules[$inheritKey] = $true
        }
        else {
            $key = @(
                $writerSid, [int64]$writerRights,
                [int][Security.AccessControl.AccessControlType]::Allow,
                [int]$expectedInheritance,
                [int][Security.AccessControl.PropagationFlags]::None
            ) -join '|'
            $expectedRules[$key] = $true
        }
    }

    $rules = @($Acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne $expectedRules.Count) {
        throw "$Label DACL does not contain the exact approved ACE set."
    }
    $seen = @{}
    foreach ($rule in $rules) {
        $sid = [string]$rule.IdentityReference.Value
        $key = @(
            $sid, [int64]$rule.FileSystemRights, [int]$rule.AccessControlType,
            [int]$rule.InheritanceFlags, [int]$rule.PropagationFlags
        ) -join '|'
        if (-not $expectedRules.ContainsKey($key) -or $seen.ContainsKey($key)) {
            throw "$Label grants an unapproved or duplicate principal."
        }
        if (
            $rule.IsInherited -or
            $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow
        ) {
            throw "$Label contains an overbroad or malformed access rule."
        }
        $seen[$key] = $true
    }
    foreach ($key in $expectedRules.Keys) {
        if (-not $seen.ContainsKey($key)) { throw "$Label omits a required access rule." }
    }
    return $true
}

function Assert-DawnstrikeStateBoundaryPathAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Directory,
        [Parameter(Mandatory = $true)][string[]]$WriterSids,
        [switch]$AnchorDirectory,
        [string]$Label = 'StateRoot path'
    )
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $null = Assert-DawnstrikeStateBoundaryAclObject `
        -Acl $acl -Directory $Directory -WriterSids $WriterSids `
        -AnchorDirectory:$AnchorDirectory -Label $Label
    return $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All)
}

function Set-DawnstrikeStateBoundaryPathAcl {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Directory,
        [Parameter(Mandatory = $true)][string[]]$WriterSids,
        [switch]$AnchorDirectory
    )
    $acl = New-DawnstrikeStateBoundaryAcl -Directory $Directory `
        -WriterSids $WriterSids -AnchorDirectory:$AnchorDirectory
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
    return Assert-DawnstrikeStateBoundaryPathAcl `
        -Path $Path -Directory $Directory -WriterSids $WriterSids `
        -AnchorDirectory:$AnchorDirectory
}

function Get-DawnstrikeStateBoundaryTreeSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $root = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $rootPrefix = $root + '\'
    $queue = New-Object 'System.Collections.Generic.Queue[string]'
    $queue.Enqueue($root)
    $paths = @($root)
    while ($queue.Count -gt 0) {
        $directory = $queue.Dequeue()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'StateRoot tree contains a reparse point.'
            }
            $full = [IO.Path]::GetFullPath($child.FullName)
            if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'StateRoot tree enumeration escaped the fixed root.'
            }
            $paths += $full
            if ($child.PSIsContainer) { $queue.Enqueue($full) }
        }
    }

    $records = @()
    try {
        foreach ($path in @($paths | Sort-Object -Unique)) {
            $bound = Open-DawnstrikeStateBoundaryPath -Path $path -Label 'StateRoot snapshot path'
            $relative = if ([string]::Equals($path, $root, [StringComparison]::OrdinalIgnoreCase)) {
                '.'
            }
            else { $path.Substring($rootPrefix.Length).Replace('\', '/') }
            $sddl = Get-DawnstrikeStateBoundaryAclSddl $path
            $records += [pscustomobject]@{
                relative_path = $relative
                path = $path
                is_directory = [bool]$bound.is_directory
                identity = [string]$bound.identity
                sddl = $sddl
                sddl_sha256 = Get-DawnstrikeStateBoundarySha256Text $sddl
                handle = $bound.handle
            }
        }
        return @($records)
    }
    catch {
        foreach ($record in $records) {
            if ($null -ne $record.handle) { $record.handle.Dispose() }
        }
        throw
    }
}

function Set-DawnstrikeStateBoundaryEvidenceFileAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $none = [Security.AccessControl.InheritanceFlags]::None
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $administrators, 'FullControl', $none, $propagation, 'Allow'
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $system, 'FullControl', $none, $propagation, 'Allow'
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $users, 'ReadAndExecute', $none, $propagation, 'Allow'
    ))
    Set-Acl -LiteralPath $Path -AclObject $acl -ErrorAction Stop
}

function Assert-DawnstrikeStateBoundaryEvidenceAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $null = Assert-DawnstrikeStateBoundaryNoReparse -Path $Path -Label 'Protected StateRoot evidence'
    $acl = Get-Acl -LiteralPath $Path -ErrorAction Stop
    $protectedPrincipalSids = @{
        'S-1-5-18' = $true
        'S-1-5-32-544' = $true
        'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' = $true
    }
    try {
        $ownerSid = [string]([Security.Principal.NTAccount]::new(
            [string]$acl.Owner
        )).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch { throw 'Protected StateRoot evidence owner cannot be translated to an exact SID.' }
    if (-not $protectedPrincipalSids.ContainsKey($ownerSid)) {
        throw 'Protected StateRoot evidence is not administrator-owned.'
    }
    if (-not $acl.AreAccessRulesProtected) { throw 'Protected StateRoot evidence inherits its DACL.' }
    $unsafe = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    foreach ($rule in @($acl.Access)) {
        try {
            $ruleSid = [string]$rule.IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        catch { throw 'Protected StateRoot evidence access principal cannot be translated to an exact SID.' }
        if (
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            -not $protectedPrincipalSids.ContainsKey($ruleSid) -and
            ($rule.FileSystemRights -band $unsafe) -ne 0
        ) {
            throw 'Protected StateRoot evidence grants non-administrator write authority.'
        }
    }
}

function Write-DawnstrikeStateBoundaryProtectedJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Payload,
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$NoReplace
    )

    $fullPath = [IO.Path]::GetFullPath($Path)
    $directory = Split-Path -Parent $fullPath
    $directoryLease = Open-DawnstrikeStateBoundaryPath `
        -Path $directory -Label 'Protected StateRoot evidence root'
    $json = $Payload | ConvertTo-Json -Depth 20
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json + "`r`n")
    $expectedHash = Get-DawnstrikeStateBoundarySha256Bytes $bytes
    $temporary = Join-Path $directory ('.state-boundary-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        if (Test-Path -LiteralPath $fullPath) {
            $existingLease = Open-DawnstrikeStateBoundaryPath `
                -Path $fullPath -Label 'Existing protected StateRoot evidence'
            try {
                if ($existingLease.is_directory) {
                    throw 'Protected StateRoot evidence destination is not a regular file.'
                }
                Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $fullPath
                if ($NoReplace) {
                    $existingHash = Get-DawnstrikeStateBoundarySha256File -Path $fullPath
                    if ($existingHash -cne $expectedHash) {
                        throw 'Protected StateRoot evidence no-replace destination already differs.'
                    }
                    return [pscustomobject]@{
                        path = $fullPath
                        sha256 = $expectedHash
                        byte_count = $bytes.Length
                        bytes = $bytes
                        reused = $true
                    }
                }
            }
            finally { $existingLease.handle.Dispose() }
        }
        $temporaryStream = [IO.File]::Open(
            $temporary,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $temporaryStream.Write($bytes, 0, $bytes.Length)
            $temporaryStream.Flush($true)
        }
        finally { $temporaryStream.Dispose() }
        Set-DawnstrikeStateBoundaryEvidenceFileAcl -Path $temporary
        Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $temporary
        if ($NoReplace) {
            [Dawnstrike.StateBoundary.AtomicFile]::MoveNoReplace($temporary, $fullPath)
        }
        else {
            [Dawnstrike.StateBoundary.AtomicFile]::Replace($temporary, $fullPath)
        }
        $writtenLease = Open-DawnstrikeStateBoundaryPath `
            -Path $fullPath -Label 'Written protected StateRoot evidence'
        try {
            if ($writtenLease.is_directory) {
                throw 'Protected StateRoot evidence replacement is not a regular file.'
            }
            Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $fullPath
            $writtenHash = Get-DawnstrikeStateBoundarySha256File -Path $fullPath
            if ($writtenHash -cne $expectedHash) {
                throw 'Protected StateRoot evidence atomic replacement did not read back exactly.'
            }
        }
        finally { $writtenLease.handle.Dispose() }
        return [pscustomobject]@{
            path = $fullPath
            sha256 = $expectedHash
            byte_count = $bytes.Length
            bytes = $bytes
            reused = $false
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        $directoryLease.handle.Dispose()
    }
}

function Read-DawnstrikeStateBoundaryProtectedJson {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $pathLease = Open-DawnstrikeStateBoundaryPath -Path $Path -Label 'Protected StateRoot evidence'
    $stream = $null
    try {
        Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $Path
        $stream = [IO.File]::Open(
            $Path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $memory = [IO.MemoryStream]::new()
        try { $stream.CopyTo($memory); $bytes = $memory.ToArray() }
        finally { $memory.Dispose() }
        try { $payload = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json }
        catch { throw 'Protected StateRoot evidence is not valid JSON.' }
        return [pscustomobject]@{
            payload = $payload
            bytes = $bytes
            sha256 = Get-DawnstrikeStateBoundarySha256Bytes $bytes
            stream = [Dawnstrike.StateBoundary.DisposableGroup]::new(
                [IDisposable[]]@($stream, $pathLease.handle)
            )
        }
    }
    catch {
        if ($null -ne $stream) { $stream.Dispose() }
        $pathLease.handle.Dispose()
        throw
    }
}

function Open-DawnstrikeStateBoundaryExactFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $lease = Open-DawnstrikeStateBoundaryPath -Path $Path -Label $Label
    $stream = $null
    try {
        if ($lease.is_directory) { throw "$Label is not a regular file." }
        $stream = [IO.File]::Open(
            [string]$lease.path,
            [IO.FileMode]::Open,
            [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        $memory = [IO.MemoryStream]::new()
        try { $stream.CopyTo($memory); $bytes = $memory.ToArray() }
        finally { $memory.Dispose(); $stream.Position = 0 }
        if (
            (Get-DawnstrikeStateBoundarySha256Bytes $bytes) -cne
                $ExpectedSha256.ToLowerInvariant() -or
            (Get-DawnstrikeStateBoundarySha256File -Path ([string]$lease.path)) -cne
                $ExpectedSha256.ToLowerInvariant()
        ) { throw "$Label hash differs from its sealed identity." }
        $result = [pscustomobject]@{
            path = [string]$lease.path
            bytes = $bytes
            stream = $stream
            lease = $lease.handle
        }
        $stream = $null
        $lease = $null
        return $result
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $lease -and $null -ne $lease.handle) {
            $lease.handle.Dispose()
        }
    }
}

function Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Authorization,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet('BOOTSTRAP', 'ACTIVATE')]
        [string]$ExpectedOperationType,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedTree
    )

    if (
        [string]$Authorization.status -cne 'AUTHORIZED' -or
        [string]$Authorization.operation_type -cne $ExpectedOperationType -or
        [string]$Authorization.runtime_sha -cne $ExpectedSha.ToLowerInvariant() -or
        [string]$Authorization.runtime_tree -cne $ExpectedTree.ToLowerInvariant()
    ) { throw 'Protected runtime authorization is not the exact baseline predecessor.' }
    $material = $Authorization.material
    $state = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $statePrefix = $state + '\'
    $release = [IO.Path]::GetFullPath([string]$material.protected_release_root).TrimEnd('\')
    $dependency = [IO.Path]::GetFullPath([string]$material.dependency_root).TrimEnd('\')
    $locks = @()
    try {
        foreach ($directoryContract in @(
            [pscustomobject]@{ path = $release; label = 'Authorized baseline release root' },
            [pscustomobject]@{ path = $dependency; label = 'Authorized baseline dependency root' }
        )) {
            Assert-DawnstrikeStateBoundaryEvidenceAcl -Path ([string]$directoryContract.path)
            $directoryLease = Open-DawnstrikeStateBoundaryPath `
                -Path ([string]$directoryContract.path) `
                -Label ([string]$directoryContract.label)
            if (-not $directoryLease.is_directory) {
                $directoryLease.handle.Dispose()
                throw "$([string]$directoryContract.label) is not a directory."
            }
            $locks += $directoryLease.handle
        }
        $fileContracts = @(
            [pscustomobject]@{
                path = [string]$material.release_admission_path
                sha256 = [string]$material.release_admission_sha256
                label = 'Authorized baseline release admission'
                protected = $true
            },
            [pscustomobject]@{
                path = [string]$material.python_path
                sha256 = [string]$material.python_sha256
                label = 'Authorized baseline Python executable'
                protected = $true
            },
            [pscustomobject]@{
                path = [string]$material.python_boundary_manifest_path
                sha256 = [string]$material.python_boundary_manifest_sha256
                label = 'Authorized baseline Python boundary manifest'
                protected = $true
            },
            [pscustomobject]@{
                path = [string]$material.dependency_manifest_path
                sha256 = [string]$material.dependency_manifest_sha256
                label = 'Authorized baseline dependency manifest'
                protected = $true
            }
        )
        foreach ($manifest in @($material.launch_manifests)) {
            $fileContracts += [pscustomobject]@{
                path = [string]$manifest.path
                sha256 = [string]$manifest.sha256
                label = 'Authorized baseline launch manifest'
                protected = $false
            }
        }
        foreach ($terminalContract in @(
            [pscustomobject]@{
                relative = [string]$Authorization.contract.terminal_receipt_relative_path
                sha256 = [string]$Authorization.contract.terminal_receipt_sha256
                label = 'Authorized baseline terminal receipt'
            },
            [pscustomobject]@{
                relative = [string]$Authorization.contract.terminal_journal_relative_path
                sha256 = [string]$Authorization.contract.terminal_journal_sha256
                label = 'Authorized baseline terminal journal'
            }
        )) {
            $terminalPath = [IO.Path]::GetFullPath(
                (Join-Path $state ([string]$terminalContract.relative -replace '/', '\'))
            )
            if (-not $terminalPath.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Authorized baseline terminal evidence escaped StateRoot.'
            }
            $fileContracts += [pscustomobject]@{
                path = $terminalPath
                sha256 = [string]$terminalContract.sha256
                label = [string]$terminalContract.label
                protected = $false
            }
        }
        $releaseAdmission = $null
        foreach ($contract in $fileContracts) {
            if ([bool]$contract.protected) {
                Assert-DawnstrikeStateBoundaryEvidenceAcl -Path ([string]$contract.path)
            }
            $opened = Open-DawnstrikeStateBoundaryExactFile `
                -Path ([string]$contract.path) -ExpectedSha256 ([string]$contract.sha256) `
                -Label ([string]$contract.label)
            $locks += @($opened.stream, $opened.lease)
            if ([string]$contract.label -ceq 'Authorized baseline release admission') {
                try {
                    $releaseAdmission = [Text.Encoding]::UTF8.GetString($opened.bytes) |
                        ConvertFrom-Json
                }
                catch { throw 'Authorized baseline release admission is invalid JSON.' }
            }
        }
        if (
            [string]$releaseAdmission.schema_version -cne 'dawnstrike.release_admission.v1' -or
            [string]$releaseAdmission.candidate_sha -cne $ExpectedSha.ToLowerInvariant() -or
            [string]$releaseAdmission.candidate_tree -cne $ExpectedTree.ToLowerInvariant() -or
            $releaseAdmission.research_only -ne $true -or
            $releaseAdmission.broker_execution_enabled -ne $false
        ) { throw 'Authorized baseline release admission identity is invalid.' }
        return [pscustomobject]@{ locks = @($locks) }
    }
    catch {
        foreach ($lock in @($locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Open-DawnstrikeStateBoundaryCandidateMigrationBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedBoundarySha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedBoundaryTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedRuntimeSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedRuntimeTree,
        [Parameter(Mandatory = $true)][string]$ExpectedHelperPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$ExpectedHelperSha256,
        [string]$ExpectedReceiptSha256 = ''
    )

    $fixed = Assert-DawnstrikeStateBoundaryFixedPath `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    $state = [string]$fixed.state_root
    $evidence = [string]$fixed.evidence_root
    Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state
    $currentPath = Join-Path $evidence 'state-boundary-current.json'
    $read = Read-DawnstrikeStateBoundaryProtectedJson -Path $currentPath
    $locks = @($read.stream)
    try {
        $receipt = $read.payload
        if ($ExpectedReceiptSha256 -and $read.sha256 -cne $ExpectedReceiptSha256) {
            throw 'StateRoot candidate migration current receipt hash changed.'
        }
        if (
            [string]$receipt.schema_version -cne 'dawnstrike.state_boundary_installation.v2' -or
            [string]$receipt.status -cne 'PASS' -or
            [string]$receipt.candidate_sha -cne $ExpectedBoundarySha.ToLowerInvariant() -or
            [string]$receipt.candidate_tree -cne $ExpectedBoundaryTree.ToLowerInvariant() -or
            [string]$receipt.state_root -cne $state -or
            $receipt.research_only -ne $true -or
            $receipt.broker_execution_enabled -ne $false
        ) { throw 'StateRoot candidate migration boundary identity is invalid.' }
        $activationLineage = Get-DawnstrikeStateBoundaryActivationLineage -Receipt $receipt
        $currentAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $receipt -Kind current -StateRoot $state
        $rollbackAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $receipt -Kind rollback -StateRoot $state
        if ([string]$currentAuthorization.status -cne 'AUTHORIZED' -or
            [string]$currentAuthorization.runtime_sha -cne $ExpectedRuntimeSha.ToLowerInvariant() -or
            [string]$currentAuthorization.runtime_tree -cne $ExpectedRuntimeTree.ToLowerInvariant()) {
            throw 'StateRoot candidate migration current runtime authorization is not exact.'
        }
        $bootstrapAuthorization =
            [string]$currentAuthorization.operation_type -ceq 'BOOTSTRAP' -and
            [string]$activationLineage.status -ceq 'NONE' -and
            [string]$rollbackAuthorization.status -ceq 'NONE'
        $activeAuthorization =
            [string]$currentAuthorization.operation_type -ceq 'ACTIVATE' -and
            [string]$activationLineage.status -ceq 'ACTIVE' -and
            [string]$rollbackAuthorization.status -ceq 'AUTHORIZED' -and
            [string]$activationLineage.activation_id -ceq
                [string]$currentAuthorization.terminal_id -and
            [string]$activationLineage.receipt_relative_path -ceq
                [string]$currentAuthorization.contract.terminal_receipt_relative_path -and
            [string]$activationLineage.receipt_sha256 -ceq
                [string]$currentAuthorization.contract.terminal_receipt_sha256 -and
            [string]$activationLineage.journal_relative_path -ceq
                [string]$currentAuthorization.contract.terminal_journal_relative_path -and
            [string]$activationLineage.journal_sha256 -ceq
                [string]$currentAuthorization.contract.terminal_journal_sha256
        if (-not $bootstrapAuthorization -and -not $activeAuthorization) {
            throw 'StateRoot candidate migration requires an exact bootstrap or active runtime lineage.'
        }
        $authorizationState = if ($bootstrapAuthorization) { 'BOOTSTRAP_DISABLED' }
            else { 'ACTIVE_READY' }
        $expectedHelper = [IO.Path]::GetFullPath($ExpectedHelperPath)
        $receiptHelper = [IO.Path]::GetFullPath([string]$receipt.installed_helper_path)
        $canonicalHelper = [IO.Path]::GetFullPath(
            "C:\Program Files\Dawnstrike\releases\$($ExpectedBoundarySha.ToLowerInvariant())\scripts\state_root_boundary.ps1"
        )
        if (
            -not [string]::Equals($expectedHelper, $canonicalHelper, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals($receiptHelper, $expectedHelper, [StringComparison]::OrdinalIgnoreCase) -or
            [string]$receipt.installed_helper_sha256 -cne $ExpectedHelperSha256.ToLowerInvariant()
        ) { throw 'StateRoot candidate migration helper identity is invalid.' }
        Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $expectedHelper
        $helper = Open-DawnstrikeStateBoundaryExactFile `
            -Path $expectedHelper -ExpectedSha256 $ExpectedHelperSha256 `
            -Label 'Protected candidate-boundary helper'
        $locks += @($helper.stream, $helper.lease)
        $runtimeHelperPath = if (
            $ExpectedBoundarySha -ceq $ExpectedRuntimeSha
        ) { $expectedHelper } else {
            [IO.Path]::GetFullPath([string]$receipt.candidate_migration_runtime_helper_path)
        }
        $runtimeHelperSha256 = if (
            $ExpectedBoundarySha -ceq $ExpectedRuntimeSha
        ) { $ExpectedHelperSha256.ToLowerInvariant() } else {
            [string]$receipt.candidate_migration_runtime_helper_sha256
        }
        $expectedRuntimeHelperPath = [IO.Path]::GetFullPath(
            "C:\Program Files\Dawnstrike\releases\$($ExpectedRuntimeSha.ToLowerInvariant())\scripts\state_root_boundary.ps1"
        )
        if (
            -not [string]::Equals(
                $runtimeHelperPath, $expectedRuntimeHelperPath,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            $runtimeHelperSha256 -notmatch '^[0-9a-f]{64}$' -or
            ($ExpectedBoundarySha -cne $ExpectedRuntimeSha -and (
                [string]$receipt.candidate_migration_runtime_sha -cne
                    $ExpectedRuntimeSha.ToLowerInvariant() -or
                [string]$receipt.candidate_migration_runtime_tree -cne
                    $ExpectedRuntimeTree.ToLowerInvariant()
            ))
        ) { throw 'StateRoot candidate migration runtime helper identity is invalid.' }
        if ($ExpectedBoundarySha -cne $ExpectedRuntimeSha) {
            Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $runtimeHelperPath
            $runtimeHelper = Open-DawnstrikeStateBoundaryExactFile `
                -Path $runtimeHelperPath -ExpectedSha256 $runtimeHelperSha256 `
                -Label 'Protected authorized-runtime helper'
            $locks += @($runtimeHelper.stream, $runtimeHelper.lease)
        }

        $writerSids = @($receipt.writer_sids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        if ($writerSids.Count -lt 1 -or
            @($writerSids | Where-Object { $_ -notmatch '^S-1-' }).Count -ne 0) {
            throw 'StateRoot candidate migration writer SID set is invalid.'
        }
        $rootBound = Open-DawnstrikeStateBoundaryPath -Path $state -Label 'Production StateRoot'
        $locksRoot = Join-Path $state 'locks'
        $locksBound = Open-DawnstrikeStateBoundaryPath `
            -Path $locksRoot -Label 'Production StateRoot lock root'
        $locks += @($rootBound.handle, $locksBound.handle)
        $rootSddl = Assert-DawnstrikeStateBoundaryPathAcl -Path $state -Directory $true `
            -WriterSids $writerSids -AnchorDirectory
        $locksSddl = Assert-DawnstrikeStateBoundaryPathAcl -Path $locksRoot -Directory $true `
            -WriterSids $writerSids -AnchorDirectory
        if (
            [string]$rootBound.identity -cne [string]$receipt.state_root_identity -or
            [string]$locksBound.identity -cne [string]$receipt.locks_root_identity -or
            (Get-DawnstrikeStateBoundarySha256Text $rootSddl) -cne
                [string]$receipt.state_root_sddl_sha256 -or
            (Get-DawnstrikeStateBoundarySha256Text $locksSddl) -cne
                [string]$receipt.locks_root_sddl_sha256
        ) { throw 'StateRoot candidate migration root identity or DACL changed.' }
        if ((Get-DawnstrikeStateBoundaryTaskBindingHash `
                -Tasks $receipt.task_definitions_and_principals) -cne
                [string]$receipt.task_binding_sha256) {
            throw 'StateRoot candidate migration task binding hash is invalid.'
        }
        $liveTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
            -ExpectedTasks $receipt.task_definitions_and_principals `
            -LiveTasks $liveTasks -WriterSids $writerSids
        $canonical = @($liveTasks | Where-Object { [bool]$_.canonical })
        $expectedTaskState = if ($bootstrapAuthorization) { 'Disabled' } else { 'Ready' }
        $expectedTaskDisposition = if ($bootstrapAuthorization) {
            'DISABLED_BY_GOVERNED_BOOTSTRAP_BASELINE_RESEAL'
        }
        else { 'ENABLED_BY_GOVERNED_ACTIVATE_RESEAL' }
        if (
            $canonical.Count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
            @($canonical | Where-Object {
                [string]$_.state -cne $expectedTaskState
            }).Count -ne 0 -or
            [string]$receipt.canonical_task_disposition -cne $expectedTaskDisposition -or
            [string]$canonical[0].canonical_task_definition_contract_sha256 -cne
                [string]$currentAuthorization.material.canonical_task_definition_contract_sha256 -or
            [string]$canonical[0].canonical_task_action_contract_sha256 -cne
                [string]$currentAuthorization.material.canonical_task_action_contract_sha256
        ) { throw 'StateRoot candidate migration task contract is not exact for its runtime lineage.' }
        $authorizationEvidence = Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence `
            -Authorization $currentAuthorization -StateRoot $state `
            -ExpectedOperationType ([string]$currentAuthorization.operation_type) `
            -ExpectedSha $ExpectedRuntimeSha `
            -ExpectedTree $ExpectedRuntimeTree
        $locks += @($authorizationEvidence.locks)
        if ($activeAuthorization) {
            $rollbackEvidence = Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence `
                -Authorization $rollbackAuthorization -StateRoot $state `
                -ExpectedOperationType ([string]$rollbackAuthorization.operation_type) `
                -ExpectedSha ([string]$rollbackAuthorization.runtime_sha) `
                -ExpectedTree ([string]$rollbackAuthorization.runtime_tree)
            $locks += @($rollbackEvidence.locks)
        }
        return [pscustomobject]@{
            receipt = $receipt
            receipt_sha256 = [string]$read.sha256
            receipt_path = $currentPath
            current_runtime_authorization = $currentAuthorization
            rollback_runtime_authorization = $rollbackAuthorization
            activation_lineage = $activationLineage
            authorization_state = $authorizationState
            runtime_helper_path = $runtimeHelperPath
            runtime_helper_sha256 = $runtimeHelperSha256
            live_tasks = @($liveTasks)
            writer_sids = @($writerSids)
            locks = @($locks)
        }
    }
    catch {
        foreach ($lock in @($locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Assert-DawnstrikeStateBoundaryCandidateMigrationIntent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Intent,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedBoundarySha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedBoundaryTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedRuntimeSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedRuntimeTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$RequestContractSha256,
        [Parameter(Mandatory = $true)][string]$InstalledHelperPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$InstalledHelperSha256,
        [Parameter(Mandatory = $true)][string]$CandidateAdmissionPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$CandidateAdmissionSha256
    )

    $payload = $Intent.payload
    $required = @(
        'schema_version', 'operation_id', 'created_at_utc', 'state_root',
        'from_candidate_sha', 'from_candidate_tree', 'runtime_sha', 'runtime_tree',
        'candidate_sha', 'candidate_tree',
        'request_contract_sha256', 'old_current_receipt_sha256',
        'authorization_state', 'current_runtime_authorization_sha256',
        'rollback_runtime_authorization_sha256', 'activation_lineage_id',
        'predecessor_helper_path', 'predecessor_helper_sha256',
        'runtime_helper_path', 'runtime_helper_sha256',
        'installed_helper_path',
        'installed_helper_sha256', 'candidate_admission_path',
        'candidate_admission_sha256', 'new_current_receipt_sha256',
        'new_current_receipt', 'completion_path', 'historical_receipt_path',
        'research_only', 'broker_execution_enabled'
    )
    $names = @($payload.PSObject.Properties.Name | ForEach-Object { [string]$_ })
    $operationId = [string]$payload.operation_id
    $expectedCompletionPath = Join-Path $EvidenceRoot (
        "state-boundary-candidate-migration-$operationId.json"
    )
    $expectedHistoricalPath = Join-Path $EvidenceRoot (
        'state-boundary-' + $CandidateSha.ToLowerInvariant() + '.json'
    )
    $newReceiptHash = Get-DawnstrikeStateBoundarySha256Text (
        ($payload.new_current_receipt | ConvertTo-Json -Depth 20) + "`r`n"
    )
    $newReceipt = $payload.new_current_receipt
    if (
        ($names.Count -ne $required.Count) -or
        (($names | Sort-Object) -join "`n") -cne (($required | Sort-Object) -join "`n") -or
        [string]$payload.schema_version -cne
            'dawnstrike.state_boundary_candidate_migration_intent.v1' -or
        $operationId -notmatch '^[0-9a-f]{32}$' -or
        [string]$payload.state_root -cne [IO.Path]::GetFullPath($StateRoot).TrimEnd('\') -or
        [string]$payload.from_candidate_sha -cne $ExpectedBoundarySha.ToLowerInvariant() -or
        [string]$payload.from_candidate_tree -cne $ExpectedBoundaryTree.ToLowerInvariant() -or
        [string]$payload.runtime_sha -cne $ExpectedRuntimeSha.ToLowerInvariant() -or
        [string]$payload.runtime_tree -cne $ExpectedRuntimeTree.ToLowerInvariant() -or
        [string]$payload.candidate_sha -cne $CandidateSha.ToLowerInvariant() -or
        [string]$payload.candidate_tree -cne $CandidateTree.ToLowerInvariant() -or
        [string]$payload.request_contract_sha256 -cne $RequestContractSha256.ToLowerInvariant() -or
        [string]$payload.old_current_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.authorization_state -notin @('BOOTSTRAP_DISABLED', 'ACTIVE_READY') -or
        [string]$payload.current_runtime_authorization_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$payload.rollback_runtime_authorization_sha256 -notmatch
            '^(?:NONE|[0-9a-f]{64})$' -or
        [string]$payload.activation_lineage_id -notmatch '^(?:NONE|[0-9a-f]{24})$' -or
        ([string]$payload.authorization_state -ceq 'BOOTSTRAP_DISABLED' -and (
            [string]$payload.rollback_runtime_authorization_sha256 -cne 'NONE' -or
            [string]$payload.activation_lineage_id -cne 'NONE'
        )) -or
        ([string]$payload.authorization_state -ceq 'ACTIVE_READY' -and (
            [string]$payload.rollback_runtime_authorization_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$payload.activation_lineage_id -notmatch '^[0-9a-f]{24}$'
        )) -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath([string]$payload.predecessor_helper_path),
            [IO.Path]::GetFullPath(
                "C:\Program Files\Dawnstrike\releases\$($ExpectedBoundarySha.ToLowerInvariant())\scripts\state_root_boundary.ps1"
            ),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$payload.predecessor_helper_sha256 -notmatch '^[0-9a-f]{64}$' -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath([string]$payload.runtime_helper_path),
            [IO.Path]::GetFullPath(
                "C:\Program Files\Dawnstrike\releases\$($ExpectedRuntimeSha.ToLowerInvariant())\scripts\state_root_boundary.ps1"
            ),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$payload.runtime_helper_sha256 -notmatch '^[0-9a-f]{64}$' -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath([string]$payload.installed_helper_path),
            [IO.Path]::GetFullPath($InstalledHelperPath),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$payload.installed_helper_sha256 -cne
            $InstalledHelperSha256.ToLowerInvariant() -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath([string]$payload.candidate_admission_path),
            [IO.Path]::GetFullPath($CandidateAdmissionPath),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$payload.candidate_admission_sha256 -cne
            $CandidateAdmissionSha256.ToLowerInvariant() -or
        [string]$payload.new_current_receipt_sha256 -cne $newReceiptHash -or
        [string]$payload.completion_path -cne $expectedCompletionPath -or
        [string]$payload.historical_receipt_path -cne $expectedHistoricalPath -or
        [string]$newReceipt.candidate_sha -cne $CandidateSha.ToLowerInvariant() -or
        [string]$newReceipt.candidate_tree -cne $CandidateTree.ToLowerInvariant() -or
        [string]$newReceipt.current_runtime_authorization_sha256 -cne
            [string]$payload.current_runtime_authorization_sha256 -or
        [string]$newReceipt.rollback_runtime_authorization_sha256 -cne
            [string]$payload.rollback_runtime_authorization_sha256 -or
        [string]$newReceipt.last_activation_id -cne
            [string]$payload.activation_lineage_id -or
        [string]$newReceipt.candidate_migration_operation_id -cne $operationId -or
        [string]$newReceipt.candidate_migration_request_contract_sha256 -cne
            $RequestContractSha256.ToLowerInvariant() -or
        [string]$newReceipt.candidate_migration_predecessor_receipt_sha256 -cne
            [string]$payload.old_current_receipt_sha256 -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath(
                [string]$newReceipt.candidate_migration_predecessor_helper_path
            ),
            [IO.Path]::GetFullPath([string]$payload.predecessor_helper_path),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$newReceipt.candidate_migration_predecessor_helper_sha256 -cne
            [string]$payload.predecessor_helper_sha256 -or
        [string]$newReceipt.candidate_migration_runtime_sha -cne
            $ExpectedRuntimeSha.ToLowerInvariant() -or
        [string]$newReceipt.candidate_migration_runtime_tree -cne
            $ExpectedRuntimeTree.ToLowerInvariant() -or
        -not [string]::Equals(
            [IO.Path]::GetFullPath([string]$newReceipt.candidate_migration_runtime_helper_path),
            [IO.Path]::GetFullPath([string]$payload.runtime_helper_path),
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$newReceipt.candidate_migration_runtime_helper_sha256 -cne
            [string]$payload.runtime_helper_sha256 -or
        [string]$newReceipt.candidate_migration_authorization_state -cne
            [string]$payload.authorization_state -or
        $payload.research_only -ne $true -or
        $payload.broker_execution_enabled -ne $false
    ) { throw 'Protected StateRoot candidate-migration intent is invalid or remapped.' }
    return $true
}

function Complete-DawnstrikeStateBoundaryCandidateMigrationFileTransition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Intent,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][string]$CurrentReceiptPath,
        [ValidateSet('', 'after_completion', 'after_current')]
        [string]$TestCrashPoint = '',
        [switch]$KeepIntent
    )

    $payload = $Intent.payload
    $currentRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $CurrentReceiptPath
    try { $currentHash = [string]$currentRead.sha256 }
    finally { $currentRead.stream.Dispose() }
    if ($currentHash -notin @(
        [string]$payload.old_current_receipt_sha256,
        [string]$payload.new_current_receipt_sha256
    )) { throw 'Candidate migration current receipt is outside the sealed transition.' }
    $completionPayload = [ordered]@{
        schema_version = 'dawnstrike.state_boundary_candidate_migration_completion.v1'
        operation_id = [string]$payload.operation_id
        completed_at_utc = [string]$payload.created_at_utc
        state_root = [string]$payload.state_root
        from_candidate_sha = [string]$payload.from_candidate_sha
        from_candidate_tree = [string]$payload.from_candidate_tree
        runtime_sha = [string]$payload.runtime_sha
        runtime_tree = [string]$payload.runtime_tree
        candidate_sha = [string]$payload.candidate_sha
        candidate_tree = [string]$payload.candidate_tree
        request_contract_sha256 = [string]$payload.request_contract_sha256
        intent_sha256 = [string]$Intent.sha256
        old_current_receipt_sha256 = [string]$payload.old_current_receipt_sha256
        new_current_receipt_sha256 = [string]$payload.new_current_receipt_sha256
        authorization_state = [string]$payload.authorization_state
        current_runtime_authorization_sha256 =
            [string]$payload.current_runtime_authorization_sha256
        rollback_runtime_authorization_sha256 =
            [string]$payload.rollback_runtime_authorization_sha256
        activation_lineage_id = [string]$payload.activation_lineage_id
        predecessor_helper_path = [string]$payload.predecessor_helper_path
        predecessor_helper_sha256 = [string]$payload.predecessor_helper_sha256
        runtime_helper_path = [string]$payload.runtime_helper_path
        runtime_helper_sha256 = [string]$payload.runtime_helper_sha256
        installed_helper_path = [string]$payload.installed_helper_path
        installed_helper_sha256 = [string]$payload.installed_helper_sha256
        candidate_admission_path = [string]$payload.candidate_admission_path
        candidate_admission_sha256 = [string]$payload.candidate_admission_sha256
        research_only = $true
        broker_execution_enabled = $false
    }
    $completionWrite = Write-DawnstrikeStateBoundaryProtectedJson `
        -Payload $completionPayload -Path ([string]$payload.completion_path) -NoReplace
    if ($TestCrashPoint -eq 'after_completion') {
        throw 'DAWNSTRIKE_TEST_CANDIDATE_MIGRATION_CRASH:after_completion'
    }
    $historicalWrite = Write-DawnstrikeStateBoundaryProtectedJson `
        -Payload $payload.new_current_receipt `
        -Path ([string]$payload.historical_receipt_path) -NoReplace
    if ($historicalWrite.sha256 -cne [string]$payload.new_current_receipt_sha256) {
        throw 'StateRoot candidate migration historical receipt hash mismatch.'
    }
    if ($currentHash -ceq [string]$payload.old_current_receipt_sha256) {
        $currentWrite = Write-DawnstrikeStateBoundaryProtectedJson `
            -Payload $payload.new_current_receipt -Path $CurrentReceiptPath
        if ($currentWrite.sha256 -cne [string]$payload.new_current_receipt_sha256) {
            throw 'StateRoot candidate migration current receipt hash mismatch.'
        }
    }
    if ($TestCrashPoint -eq 'after_current') {
        throw 'DAWNSTRIKE_TEST_CANDIDATE_MIGRATION_CRASH:after_current'
    }
    foreach ($receiptPath in @(
        $CurrentReceiptPath,
        [string]$payload.historical_receipt_path
    )) {
        $sealedRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $receiptPath
        try {
            if ($sealedRead.sha256 -cne [string]$payload.new_current_receipt_sha256) {
                throw 'Candidate migration receipt did not converge on the sealed target.'
            }
        }
        finally { $sealedRead.stream.Dispose() }
    }
    if (-not $KeepIntent) {
        Remove-Item -LiteralPath ([string]$Intent.path) -Force -ErrorAction Stop
    }
    return [pscustomobject]@{
        completion_path = [string]$payload.completion_path
        completion_sha256 = [string]$completionWrite.sha256
        current_receipt_sha256 = [string]$payload.new_current_receipt_sha256
    }
}

function Migrate-DawnstrikeStateRootBoundaryCandidate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedBoundarySha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedBoundaryTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedRuntimeSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedRuntimeTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')]
        [string]$CandidateTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$RequestContractSha256,
        [Parameter(Mandatory = $true)][string]$InstalledHelperPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$InstalledHelperSha256,
        [Parameter(Mandatory = $true)][string]$CandidateAdmissionPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
        [string]$CandidateAdmissionSha256
    )

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'StateRoot candidate migration requires an elevated administrator process.'
    }
    if ($ExpectedBoundarySha -ceq $CandidateSha) {
        throw 'StateRoot candidate migration requires distinct predecessor and candidate SHAs.'
    }
    $fixed = Assert-DawnstrikeStateBoundaryFixedPath `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    $state = [string]$fixed.state_root
    $evidence = [string]$fixed.evidence_root
    $expectedBoundaryHelperPath = [IO.Path]::GetFullPath(
        "C:\Program Files\Dawnstrike\releases\$($ExpectedBoundarySha.ToLowerInvariant())\scripts\state_root_boundary.ps1"
    )
    $candidateRelease = [IO.Path]::GetFullPath(
        "C:\Program Files\Dawnstrike\releases\$($CandidateSha.ToLowerInvariant())"
    ).TrimEnd('\')
    $candidateHelper = [IO.Path]::GetFullPath($InstalledHelperPath)
    $expectedCandidateHelper = Join-Path $candidateRelease 'scripts\state_root_boundary.ps1'
    $candidateAdmission = [IO.Path]::GetFullPath($CandidateAdmissionPath)
    $expectedCandidateAdmission = Join-Path `
        (Join-Path $candidateRelease '.git') 'dawnstrike-host-admission-v1.json'
    if (
        -not [string]::Equals(
            $candidateHelper, $expectedCandidateHelper,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            $candidateAdmission, $expectedCandidateAdmission,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) { throw 'StateRoot candidate migration target paths are not canonical.' }
    $null = Assert-DawnstrikeStateBoundaryNoPendingRecovery -EvidenceRoot $evidence
    $null = Assert-DawnstrikeStateBoundaryNoTaskMutation -EvidenceRoot $evidence

    $intent = Get-DawnstrikeStateBoundaryCandidateMigrationIntent `
        -EvidenceRoot $evidence
    $probePath = Join-Path $evidence 'state-boundary-current.json'
    $probe = Read-DawnstrikeStateBoundaryProtectedJson -Path $probePath
    try {
        $probeSha = [string]$probe.payload.candidate_sha
        $probeTree = [string]$probe.payload.candidate_tree
        $probeHash = [string]$probe.sha256
    }
    finally { $probe.stream.Dispose() }

    if ($null -eq $intent -and $probeSha -ceq $CandidateSha.ToLowerInvariant() -and
        $probeTree -ceq $CandidateTree.ToLowerInvariant()) {
        $completed = Open-DawnstrikeStateBoundaryCandidateMigrationBoundary `
            -StateRoot $state -EvidenceRoot $evidence `
            -ExpectedBoundarySha $CandidateSha -ExpectedBoundaryTree $CandidateTree `
            -ExpectedRuntimeSha $ExpectedRuntimeSha -ExpectedRuntimeTree $ExpectedRuntimeTree `
            -ExpectedHelperPath $candidateHelper `
            -ExpectedHelperSha256 $InstalledHelperSha256 `
            -ExpectedReceiptSha256 $probeHash
        try {
            $receipt = $completed.receipt
            if (
                [string]$receipt.candidate_migration_from_sha -cne
                    $ExpectedBoundarySha.ToLowerInvariant() -or
                [string]$receipt.candidate_migration_from_tree -cne
                    $ExpectedBoundaryTree.ToLowerInvariant() -or
                [string]$receipt.candidate_migration_runtime_sha -cne
                    $ExpectedRuntimeSha.ToLowerInvariant() -or
                [string]$receipt.candidate_migration_runtime_tree -cne
                    $ExpectedRuntimeTree.ToLowerInvariant() -or
                [string]$receipt.candidate_migration_request_contract_sha256 -cne
                    $RequestContractSha256.ToLowerInvariant() -or
                [string]$receipt.candidate_migration_target_admission_sha256 -cne
                    $CandidateAdmissionSha256.ToLowerInvariant()
            ) { throw 'Completed StateRoot candidate migration belongs to another request.' }
            $historicalPath = Join-Path $evidence (
                'state-boundary-' + $CandidateSha.ToLowerInvariant() + '.json'
            )
            $historical = Read-DawnstrikeStateBoundaryProtectedJson -Path $historicalPath
            try {
                if ($historical.sha256 -cne $completed.receipt_sha256) {
                    throw 'Completed candidate migration historical/current receipts diverged.'
                }
            }
            finally { $historical.stream.Dispose() }
            return [pscustomobject]@{
                status = 'COMPLETE'
                operation_id = [string]$receipt.candidate_migration_operation_id
                already_completed = $true
                receipt = $receipt
                receipt_path = $historicalPath
                receipt_sha256 = [string]$completed.receipt_sha256
                current_receipt_path = $probePath
                research_only = $true
                broker_execution_enabled = $false
            }
        }
        finally {
            foreach ($lock in @($completed.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
    }
    if ($null -eq $intent -and (
        $probeSha -cne $ExpectedBoundarySha.ToLowerInvariant() -or
        $probeTree -cne $ExpectedBoundaryTree.ToLowerInvariant()
    )) { throw 'StateRoot candidate migration predecessor is neither exact source nor exact completed target.' }

    $source = $null
    $currentReceiptSha = $probeHash
    if ($null -eq $intent) {
        $source = Open-DawnstrikeStateBoundaryCandidateMigrationBoundary `
            -StateRoot $state -EvidenceRoot $evidence `
            -ExpectedBoundarySha $ExpectedBoundarySha `
            -ExpectedBoundaryTree $ExpectedBoundaryTree `
            -ExpectedRuntimeSha $ExpectedRuntimeSha -ExpectedRuntimeTree $ExpectedRuntimeTree `
            -ExpectedHelperPath $expectedBoundaryHelperPath `
            -ExpectedHelperSha256 ([string]$probe.payload.installed_helper_sha256) `
            -ExpectedReceiptSha256 $probeHash
        try {
            Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $candidateRelease
            Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $candidateHelper
            Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $candidateAdmission
            $candidateReleaseLease = Open-DawnstrikeStateBoundaryPath `
                -Path $candidateRelease -Label 'Candidate migration target release root'
            $source.locks += $candidateReleaseLease.handle
            $candidateHelperOpen = Open-DawnstrikeStateBoundaryExactFile `
                -Path $candidateHelper -ExpectedSha256 $InstalledHelperSha256 `
                -Label 'Candidate migration target helper'
            $source.locks += @($candidateHelperOpen.stream, $candidateHelperOpen.lease)
            $candidateAdmissionOpen = Open-DawnstrikeStateBoundaryExactFile `
                -Path $candidateAdmission -ExpectedSha256 $CandidateAdmissionSha256 `
                -Label 'Candidate migration target admission'
            $source.locks += @($candidateAdmissionOpen.stream, $candidateAdmissionOpen.lease)
            try {
                $candidateAdmissionPayload = [Text.Encoding]::UTF8.GetString(
                    $candidateAdmissionOpen.bytes
                ) | ConvertFrom-Json
            }
            catch { throw 'Candidate migration target admission is invalid JSON.' }
            if (
                [string]$candidateAdmissionPayload.schema_version -cne
                    'dawnstrike.release_admission.v1' -or
                [string]$candidateAdmissionPayload.candidate_sha -cne
                    $CandidateSha.ToLowerInvariant() -or
                [string]$candidateAdmissionPayload.candidate_tree -cne
                    $CandidateTree.ToLowerInvariant() -or
                $candidateAdmissionPayload.research_only -ne $true -or
                $candidateAdmissionPayload.broker_execution_enabled -ne $false
            ) { throw 'Candidate migration target admission identity is invalid.' }
            $operationId = [Guid]::NewGuid().ToString('N')
            $createdAt = [DateTime]::UtcNow.ToString('o')
            $newReceipt = Copy-DawnstrikeStateBoundaryReceipt -Receipt $source.receipt
            $newReceipt['candidate_sha'] = $CandidateSha.ToLowerInvariant()
            $newReceipt['candidate_tree'] = $CandidateTree.ToLowerInvariant()
            $newReceipt['installed_helper_path'] = $candidateHelper
            $newReceipt['installed_helper_sha256'] = $InstalledHelperSha256.ToLowerInvariant()
            $newReceipt['candidate_migration_schema_version'] =
                'dawnstrike.state_boundary_candidate_migration.v1'
            $newReceipt['candidate_migration_operation_id'] = $operationId
            $newReceipt['candidate_migration_completed_at_utc'] = $createdAt
            $newReceipt['candidate_migration_from_sha'] = $ExpectedBoundarySha.ToLowerInvariant()
            $newReceipt['candidate_migration_from_tree'] = $ExpectedBoundaryTree.ToLowerInvariant()
            $newReceipt['candidate_migration_runtime_sha'] = $ExpectedRuntimeSha.ToLowerInvariant()
            $newReceipt['candidate_migration_runtime_tree'] = $ExpectedRuntimeTree.ToLowerInvariant()
            $newReceipt['candidate_migration_request_contract_sha256'] =
                $RequestContractSha256.ToLowerInvariant()
            $newReceipt['candidate_migration_predecessor_receipt_sha256'] =
                [string]$source.receipt_sha256
            $newReceipt['candidate_migration_predecessor_helper_path'] =
                [IO.Path]::GetFullPath([string]$source.receipt.installed_helper_path)
            $newReceipt['candidate_migration_predecessor_helper_sha256'] =
                [string]$source.receipt.installed_helper_sha256
            $newReceipt['candidate_migration_authorization_state'] =
                [string]$source.authorization_state
            $newReceipt['candidate_migration_runtime_helper_path'] =
                [string]$source.runtime_helper_path
            $newReceipt['candidate_migration_runtime_helper_sha256'] =
                [string]$source.runtime_helper_sha256
            $newReceipt['candidate_migration_target_admission_path'] = $candidateAdmission
            $newReceipt['candidate_migration_target_admission_sha256'] =
                $CandidateAdmissionSha256.ToLowerInvariant()
            $newReceiptJson = $newReceipt | ConvertTo-Json -Depth 20
            $newReceiptHash = Get-DawnstrikeStateBoundarySha256Text ($newReceiptJson + "`r`n")
            $completionPath = Join-Path $evidence (
                "state-boundary-candidate-migration-$operationId.json"
            )
            $historicalPath = Join-Path $evidence (
                'state-boundary-' + $CandidateSha.ToLowerInvariant() + '.json'
            )
            $intentPayload = [ordered]@{
                schema_version = 'dawnstrike.state_boundary_candidate_migration_intent.v1'
                operation_id = $operationId
                created_at_utc = $createdAt
                state_root = $state
                from_candidate_sha = $ExpectedBoundarySha.ToLowerInvariant()
                from_candidate_tree = $ExpectedBoundaryTree.ToLowerInvariant()
                runtime_sha = $ExpectedRuntimeSha.ToLowerInvariant()
                runtime_tree = $ExpectedRuntimeTree.ToLowerInvariant()
                candidate_sha = $CandidateSha.ToLowerInvariant()
                candidate_tree = $CandidateTree.ToLowerInvariant()
                request_contract_sha256 = $RequestContractSha256.ToLowerInvariant()
                old_current_receipt_sha256 = [string]$source.receipt_sha256
                authorization_state = [string]$source.authorization_state
                current_runtime_authorization_sha256 =
                    [string]$source.current_runtime_authorization.sha256
                rollback_runtime_authorization_sha256 = if (
                    [string]$source.rollback_runtime_authorization.status -ceq 'AUTHORIZED'
                ) { [string]$source.rollback_runtime_authorization.sha256 } else { 'NONE' }
                activation_lineage_id = [string]$source.activation_lineage.activation_id
                predecessor_helper_path =
                    [IO.Path]::GetFullPath([string]$source.receipt.installed_helper_path)
                predecessor_helper_sha256 = [string]$source.receipt.installed_helper_sha256
                runtime_helper_path = [string]$source.runtime_helper_path
                runtime_helper_sha256 = [string]$source.runtime_helper_sha256
                installed_helper_path = $candidateHelper
                installed_helper_sha256 = $InstalledHelperSha256.ToLowerInvariant()
                candidate_admission_path = $candidateAdmission
                candidate_admission_sha256 = $CandidateAdmissionSha256.ToLowerInvariant()
                new_current_receipt_sha256 = $newReceiptHash
                new_current_receipt = $newReceipt
                completion_path = $completionPath
                historical_receipt_path = $historicalPath
                research_only = $true
                broker_execution_enabled = $false
            }
            $intentPath = Get-DawnstrikeStateBoundaryCandidateMigrationIntentPath `
                -EvidenceRoot $evidence
            $null = Write-DawnstrikeStateBoundaryProtectedJson `
                -Payload $intentPayload -Path $intentPath -NoReplace
            $intent = Get-DawnstrikeStateBoundaryCandidateMigrationIntent `
                -EvidenceRoot $evidence
        }
        finally {
            foreach ($lock in @($source.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
            $source = $null
        }
    }

    $null = Assert-DawnstrikeStateBoundaryCandidateMigrationIntent `
        -Intent $intent -StateRoot $state -EvidenceRoot $evidence `
        -ExpectedBoundarySha $ExpectedBoundarySha -ExpectedBoundaryTree $ExpectedBoundaryTree `
        -ExpectedRuntimeSha $ExpectedRuntimeSha -ExpectedRuntimeTree $ExpectedRuntimeTree `
        -CandidateSha $CandidateSha -CandidateTree $CandidateTree `
        -RequestContractSha256 $RequestContractSha256 `
        -InstalledHelperPath $candidateHelper -InstalledHelperSha256 $InstalledHelperSha256 `
        -CandidateAdmissionPath $candidateAdmission `
        -CandidateAdmissionSha256 $CandidateAdmissionSha256
    $payload = $intent.payload
    $currentReceiptSha = [string]$payload.old_current_receipt_sha256
    $boundarySha = if ($probeHash -ceq [string]$payload.new_current_receipt_sha256) {
        $CandidateSha
    }
    elseif ($probeHash -ceq [string]$payload.old_current_receipt_sha256) {
        $ExpectedBoundarySha
    }
    else { throw 'StateRoot candidate migration current receipt is outside its sealed transition.' }
    $boundaryTree = if ($boundarySha -ceq $CandidateSha) {
        $CandidateTree
    }
    else { $ExpectedBoundaryTree }
    $boundaryHelper = if ($boundarySha -ceq $CandidateSha) {
        $candidateHelper
    }
    else { $expectedBoundaryHelperPath }
    $boundaryHelperHash = if ($boundarySha -ceq $CandidateSha) {
        $InstalledHelperSha256
    }
    else { [string]$probe.payload.installed_helper_sha256 }
    $boundary = Open-DawnstrikeStateBoundaryCandidateMigrationBoundary `
        -StateRoot $state -EvidenceRoot $evidence `
        -ExpectedBoundarySha $boundarySha -ExpectedBoundaryTree $boundaryTree `
        -ExpectedRuntimeSha $ExpectedRuntimeSha -ExpectedRuntimeTree $ExpectedRuntimeTree `
        -ExpectedHelperPath $boundaryHelper -ExpectedHelperSha256 $boundaryHelperHash `
        -ExpectedReceiptSha256 $probeHash
    try {
        if ([string]$boundary.current_runtime_authorization.sha256 -cne
            [string]$payload.current_runtime_authorization_sha256) {
            throw 'StateRoot candidate migration runtime authorization changed under recovery.'
        }
        $boundaryRollbackSha = if (
            [string]$boundary.rollback_runtime_authorization.status -ceq 'AUTHORIZED'
        ) { [string]$boundary.rollback_runtime_authorization.sha256 } else { 'NONE' }
        if (
            [string]$boundary.authorization_state -cne [string]$payload.authorization_state -or
            $boundaryRollbackSha -cne [string]$payload.rollback_runtime_authorization_sha256 -or
            [string]$boundary.activation_lineage.activation_id -cne
                [string]$payload.activation_lineage_id -or
            -not [string]::Equals(
                [IO.Path]::GetFullPath([string]$boundary.runtime_helper_path),
                [IO.Path]::GetFullPath([string]$payload.runtime_helper_path),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$boundary.runtime_helper_sha256 -cne
                [string]$payload.runtime_helper_sha256
        ) { throw 'StateRoot candidate migration protected lineage changed under recovery.' }
        if ($boundarySha -ceq $ExpectedBoundarySha -and (
            -not [string]::Equals(
                [IO.Path]::GetFullPath([string]$boundary.receipt.installed_helper_path),
                [IO.Path]::GetFullPath([string]$payload.predecessor_helper_path),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$boundary.receipt.installed_helper_sha256 -cne
                [string]$payload.predecessor_helper_sha256
        )) { throw 'StateRoot candidate migration predecessor helper changed under recovery.' }
        Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $candidateRelease
        Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $candidateHelper
        Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $candidateAdmission
        $candidateReleaseLease = Open-DawnstrikeStateBoundaryPath `
            -Path $candidateRelease -Label 'Candidate migration target release root'
        $boundary.locks += $candidateReleaseLease.handle
        $candidateHelperOpen = Open-DawnstrikeStateBoundaryExactFile `
            -Path $candidateHelper -ExpectedSha256 $InstalledHelperSha256 `
            -Label 'Candidate migration target helper'
        $boundary.locks += @($candidateHelperOpen.stream, $candidateHelperOpen.lease)
        $candidateAdmissionOpen = Open-DawnstrikeStateBoundaryExactFile `
            -Path $candidateAdmission -ExpectedSha256 $CandidateAdmissionSha256 `
            -Label 'Candidate migration target admission'
        $boundary.locks += @($candidateAdmissionOpen.stream, $candidateAdmissionOpen.lease)
        # Once the shared mutation sentinel is durable, every newly launched
        # scheduled writer fails its StateRoot admission. Reprove that no task
        # admitted before the sentinel remains active immediately before the
        # protected current-pointer transition.
        Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state
        $boundary.locks[0].Dispose()
        $boundary.locks = @($boundary.locks | Select-Object -Skip 1)
        $transition = Complete-DawnstrikeStateBoundaryCandidateMigrationFileTransition `
            -Intent $intent -EvidenceRoot $evidence `
            -CurrentReceiptPath $probePath -KeepIntent
        $sealed = Open-DawnstrikeStateBoundaryCandidateMigrationBoundary `
            -StateRoot $state -EvidenceRoot $evidence `
            -ExpectedBoundarySha $CandidateSha -ExpectedBoundaryTree $CandidateTree `
            -ExpectedRuntimeSha $ExpectedRuntimeSha -ExpectedRuntimeTree $ExpectedRuntimeTree `
            -ExpectedHelperPath $candidateHelper `
            -ExpectedHelperSha256 $InstalledHelperSha256 `
            -ExpectedReceiptSha256 ([string]$payload.new_current_receipt_sha256)
        try {
            if ([string]$sealed.current_runtime_authorization.sha256 -cne
                [string]$payload.current_runtime_authorization_sha256) {
                throw 'StateRoot candidate migration did not preserve exact runtime authorization.'
            }
            $sealedRollbackSha = if (
                [string]$sealed.rollback_runtime_authorization.status -ceq 'AUTHORIZED'
            ) { [string]$sealed.rollback_runtime_authorization.sha256 } else { 'NONE' }
            if (
                [string]$sealed.authorization_state -cne [string]$payload.authorization_state -or
                $sealedRollbackSha -cne [string]$payload.rollback_runtime_authorization_sha256 -or
                [string]$sealed.activation_lineage.activation_id -cne
                    [string]$payload.activation_lineage_id -or
                -not [string]::Equals(
                    [IO.Path]::GetFullPath([string]$sealed.runtime_helper_path),
                    [IO.Path]::GetFullPath([string]$payload.runtime_helper_path),
                    [StringComparison]::OrdinalIgnoreCase
                ) -or
                [string]$sealed.runtime_helper_sha256 -cne
                    [string]$payload.runtime_helper_sha256
            ) { throw 'StateRoot candidate migration did not preserve exact rollback/activation lineage.' }
        }
        finally {
            foreach ($lock in @($sealed.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
        Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state
        Remove-Item -LiteralPath ([string]$intent.path) -Force -ErrorAction Stop
        return [pscustomobject]@{
            status = 'COMPLETE'
            operation_id = [string]$payload.operation_id
            already_completed = $false
            receipt = $payload.new_current_receipt
            receipt_path = [string]$payload.historical_receipt_path
            receipt_sha256 = [string]$payload.new_current_receipt_sha256
            current_receipt_path = $probePath
            completion_path = [string]$payload.completion_path
            completion_sha256 = [string]$transition.completion_sha256
            research_only = $true
            broker_execution_enabled = $false
        }
    }
    finally {
        foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
    }
}

function Set-DawnstrikeStateBoundaryTasksDisabled {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$TaskRecords)
    foreach ($record in @($TaskRecords)) {
        Disable-ScheduledTask -TaskName ([string]$record.task_name) `
            -TaskPath ([string]$record.task_path) -ErrorAction Stop | Out-Null
    }
    foreach ($record in @($TaskRecords)) {
        $task = @(Get-ScheduledTask -TaskName ([string]$record.task_name) -ErrorAction Stop)
        if ($task.Count -ne 1 -or [string]$task[0].State -ne 'Disabled') {
            throw 'StateRoot ACL migration could not hold every governed task Disabled.'
        }
    }
}

function Restore-DawnstrikeStateBoundaryTaskStates {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$TaskRecords,
        [switch]$SuccessfulInstallation
    )
    foreach ($record in @($TaskRecords)) {
        $name = [string]$record.task_name
        $path = [string]$record.task_path
        $prior = [string]$record.state
        $enable = $prior -eq 'Ready'
        if ($SuccessfulInstallation) {
            # Host-boundary installation proves identity and removes ambient
            # write authority; it does not bless preexisting task actions.
            # Governed Activate/Rebind must reseal exact candidate-bound XML
            # before enabling any production writer.
            $enable = $false
        }
        if ($enable) {
            Enable-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop | Out-Null
        }
        else {
            Disable-ScheduledTask -TaskName $name -TaskPath $path -ErrorAction Stop | Out-Null
        }
    }
}

function Restore-DawnstrikeStateBoundaryAcls {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)]$Entries
    )

    $root = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $prefix = $root + '\'
    foreach ($entry in @($Entries | Sort-Object { ([string]$_.relative_path).Length } -Descending)) {
        $relative = [string]$entry.relative_path
        $path = if ($relative -ceq '.') { $root } else {
            [IO.Path]::GetFullPath((Join-Path $root $relative.Replace('/', '\')))
        }
        if (-not [string]::Equals($path, $root, [StringComparison]::OrdinalIgnoreCase) -and
            -not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'StateRoot ACL rollback entry escaped the fixed root.'
        }
        $bound = Open-DawnstrikeStateBoundaryPath -Path $path -Label 'StateRoot ACL rollback path'
        try {
            if ([string]$bound.identity -cne [string]$entry.identity) {
                throw 'StateRoot ACL rollback identity changed.'
            }
            $security = if ([bool]$entry.is_directory) {
                [Security.AccessControl.DirectorySecurity]::new()
            }
            else { [Security.AccessControl.FileSecurity]::new() }
            $security.SetSecurityDescriptorSddlForm(
                [string]$entry.sddl,
                [Security.AccessControl.AccessControlSections]::All
            )
            Set-Acl -LiteralPath $path -AclObject $security -ErrorAction Stop
            $actual = Get-DawnstrikeStateBoundaryAclSddl $path
            if ((Get-DawnstrikeStateBoundarySha256Text $actual) -cne [string]$entry.sddl_sha256) {
                throw 'StateRoot ACL rollback SDDL verification failed.'
            }
        }
        finally { $bound.handle.Dispose() }
    }
}

function Get-DawnstrikeStateBoundaryManifestEntries {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Snapshot)
    return @($Snapshot | ForEach-Object {
        [ordered]@{
            relative_path = [string]$_.relative_path
            is_directory = [bool]$_.is_directory
            identity = [string]$_.identity
            sddl = [string]$_.sddl
            sddl_sha256 = [string]$_.sddl_sha256
        }
    })
}

function Assert-DawnstrikeStateBoundaryManifestRestored {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)]$Entries
    )

    $root = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $prefix = $root + '\'
    $expected = @{}
    foreach ($entry in @($Entries)) {
        $relative = [string]$entry.relative_path
        if ([string]::IsNullOrWhiteSpace($relative) -or $expected.ContainsKey($relative)) {
            throw 'StateRoot ACL rollback manifest contains an invalid or duplicate path.'
        }
        $path = if ($relative -ceq '.') { $root } else {
            [IO.Path]::GetFullPath((Join-Path $root $relative.Replace('/', '\')))
        }
        if (-not [string]::Equals($path, $root, [StringComparison]::OrdinalIgnoreCase) -and
            -not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'StateRoot ACL rollback manifest entry escaped the fixed root.'
        }
        if ($entry.is_directory -isnot [bool] -or
            [string]$entry.identity -notmatch '^[0-9a-f]{8}:[0-9a-f]{16}$' -or
            [string]$entry.sddl_sha256 -notmatch '^[0-9a-f]{64}$' -or
            (Get-DawnstrikeStateBoundarySha256Text ([string]$entry.sddl)) -cne
                [string]$entry.sddl_sha256) {
            throw 'StateRoot ACL rollback manifest entry is invalid.'
        }
        $expected[$relative] = $entry
    }

    $snapshot = @(Get-DawnstrikeStateBoundaryTreeSnapshot -StateRoot $root)
    try {
        if ($snapshot.Count -ne $expected.Count) {
            throw 'StateRoot ACL rollback did not restore the exact tree.'
        }
        foreach ($record in $snapshot) {
            $relative = [string]$record.relative_path
            if (-not $expected.ContainsKey($relative)) {
                throw 'StateRoot ACL rollback left an unexpected path.'
            }
            $entry = $expected[$relative]
            if ([bool]$record.is_directory -ne [bool]$entry.is_directory -or
                [string]$record.identity -cne [string]$entry.identity) {
                throw 'StateRoot ACL rollback did not restore the exact path identity.'
            }
            if ([string]$record.sddl -cne [string]$entry.sddl -or
                [string]$record.sddl_sha256 -cne [string]$entry.sddl_sha256) {
                throw 'StateRoot ACL rollback did not restore the exact SDDL manifest.'
            }
        }
    }
    finally {
        foreach ($record in $snapshot) {
            if ($null -ne $record.handle) { $record.handle.Dispose() }
        }
    }
    return $true
}

function Disable-DawnstrikeStateBoundaryTasksFailSafe {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$TaskRecords)
    foreach ($record in @($TaskRecords)) {
        try {
            Disable-ScheduledTask -TaskName ([string]$record.task_name) `
                -TaskPath ([string]$record.task_path) -ErrorAction Stop | Out-Null
        }
        catch { }
    }
}

function Invoke-DawnstrikeStateBoundaryPendingRecovery {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot
    )

    $pendingFiles = @(Get-ChildItem -LiteralPath $EvidenceRoot -Filter 'state-boundary-pending-*.json' -File -Force -ErrorAction Stop)
    if ($pendingFiles.Count -eq 0) { return @() }
    if ($pendingFiles.Count -ne 1) {
        throw 'Multiple StateRoot ACL recovery intents require operator investigation.'
    }
    $pendingRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $pendingFiles[0].FullName
    try { $pending = $pendingRead.payload }
    finally { $pendingRead.stream.Dispose() }
    if (
        [string]$pending.schema_version -cne 'dawnstrike.state_boundary_pending.v1' -or
        [string]$pending.state_root -cne [IO.Path]::GetFullPath($StateRoot).TrimEnd('\') -or
        [string]$pending.rollback_manifest_sha256 -notmatch '^[0-9a-f]{64}$'
    ) { throw 'StateRoot ACL recovery intent is invalid.' }
    $manifestPath = [IO.Path]::GetFullPath([string]$pending.rollback_manifest_path)
    $evidencePrefix = [IO.Path]::GetFullPath($EvidenceRoot).TrimEnd('\') + '\'
    $expectedManifestSuffix = '-' + [string]$pending.rollback_manifest_sha256 + '.json'
    if (-not $manifestPath.StartsWith($evidencePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        -not [IO.Path]::GetFileName($manifestPath).EndsWith($expectedManifestSuffix, [StringComparison]::Ordinal)) {
        throw 'StateRoot ACL rollback manifest escaped its content-addressed evidence root.'
    }
    $manifestRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $manifestPath
    try {
        if ($manifestRead.sha256 -cne [string]$pending.rollback_manifest_sha256) {
            throw 'StateRoot ACL rollback manifest hash mismatch.'
        }
        $manifest = $manifestRead.payload
    }
    finally { $manifestRead.stream.Dispose() }
    if (
        [string]$manifest.schema_version -cne 'dawnstrike.state_boundary_rollback.v1' -or
        [string]$manifest.operation_id -cne [string]$pending.operation_id -or
        [string]$manifest.state_root -cne [string]$pending.state_root -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false
    ) { throw 'StateRoot ACL rollback manifest identity is invalid.' }

    $status = 'RECOVERY_REQUIRED'
    $errorText = ''
    try {
        Set-DawnstrikeStateBoundaryTasksDisabled -TaskRecords $manifest.tasks
        Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $StateRoot
        Restore-DawnstrikeStateBoundaryAcls -StateRoot $StateRoot -Entries $manifest.state_entries
        $null = Assert-DawnstrikeStateBoundaryManifestRestored `
            -StateRoot $StateRoot -Entries $manifest.state_entries
        Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $StateRoot
        Restore-DawnstrikeStateBoundaryTaskStates -TaskRecords $manifest.tasks
        $restoredTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
        $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
            -ExpectedTasks $manifest.tasks -LiveTasks $restoredTasks `
            -WriterSids @($manifest.writer_sids)
        foreach ($expectedTask in @($manifest.tasks)) {
            $restoredTask = @($restoredTasks | Where-Object {
                [string]$_.task_name -ceq [string]$expectedTask.task_name
            })[0]
            if ([string]$restoredTask.state -cne [string]$expectedTask.state) {
                throw 'StateRoot ACL recovery did not restore the exact prior task state.'
            }
        }
        $status = 'ROLLED_BACK'
        Remove-Item -LiteralPath $pendingFiles[0].FullName -Force -ErrorAction Stop
    }
    catch {
        $errorText = $_.Exception.Message
        Disable-DawnstrikeStateBoundaryTasksFailSafe -TaskRecords $manifest.tasks
    }
    $recovery = [ordered]@{
        schema_version = 'dawnstrike.state_boundary_recovery.v1'
        operation_id = [string]$manifest.operation_id
        status = $status
        recovered_at_utc = [DateTime]::UtcNow.ToString('o')
        state_root = [string]$manifest.state_root
        rollback_manifest_path = [string]$pending.rollback_manifest_path
        rollback_manifest_sha256 = [string]$pending.rollback_manifest_sha256
        error = $errorText
        tasks_disabled_fail_safe = $status -ne 'ROLLED_BACK'
        research_only = $true
        broker_execution_enabled = $false
    }
    $recoveryPath = Join-Path $EvidenceRoot (
        'state-boundary-recovery-' + [string]$manifest.operation_id + '.json'
    )
    $null = Write-DawnstrikeStateBoundaryProtectedJson -Payload $recovery -Path $recoveryPath
    if ($status -ne 'ROLLED_BACK') {
        throw 'Interrupted StateRoot ACL migration could not be rolled back; governed tasks remain Disabled.'
    }
    return @([pscustomobject]$recovery)
}

function Install-DawnstrikeStateRootBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$EvidenceRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$InstalledHelperPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$InstalledHelperSha256
    )

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'StateRoot ACL installation requires an elevated administrator process.'
    }
    $fixed = Assert-DawnstrikeStateBoundaryFixedPath -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    $state = [string]$fixed.state_root
    $evidence = [string]$fixed.evidence_root
    $null = Assert-DawnstrikeStateBoundaryNoReparse -Path $state -Label 'Production StateRoot'
    $null = Assert-DawnstrikeStateBoundaryNoReparse -Path $evidence -Label 'Protected StateRoot evidence root'
    $null = Invoke-DawnstrikeStateBoundaryPendingRecovery -StateRoot $state -EvidenceRoot $evidence
    $null = Assert-DawnstrikeStateBoundaryNoTaskMutation -EvidenceRoot $evidence
    $null = Assert-DawnstrikeStateBoundaryNoCandidateMigration -EvidenceRoot $evidence
    if (Test-Path -LiteralPath (Join-Path $evidence 'state-boundary-current.json') -PathType Leaf) {
        throw 'An existing protected StateRoot boundary requires explicit candidate migration; reinstall is denied.'
    }
    Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state
    $locksRoot = Join-Path $state 'locks'
    if (-not (Test-Path -LiteralPath $locksRoot -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $locksRoot
    }
    $taskRecords = @(Get-DawnstrikeStateBoundaryTaskInventory -IncludeXml)
    # StateRoot write authority is derived only from the five canonical
    # scheduled writers.  The optional capture task may use one of those exact
    # identities, but can never expand the DACL by introducing another SID.
    $writerSids = @(
        $taskRecords |
            Where-Object { [bool]$_.canonical } |
            ForEach-Object { [string]$_.principal_sid } |
            Sort-Object -Unique
    )
    if ($writerSids.Count -lt 1) { throw 'StateRoot ACL installation resolved no scheduled writer SID.' }
    if (@($taskRecords | Where-Object { [string]$_.principal_sid -notin $writerSids }).Count -ne 0) {
        throw 'A noncanonical task principal is not an exact canonical StateRoot writer SID.'
    }
    $snapshot = @(Get-DawnstrikeStateBoundaryTreeSnapshot -StateRoot $state)
    $operationId = [Guid]::NewGuid().ToString('N')
    $manifestPayload = [ordered]@{
        schema_version = 'dawnstrike.state_boundary_rollback.v1'
        operation_id = $operationId
        created_at_utc = [DateTime]::UtcNow.ToString('o')
        candidate_sha = $CandidateSha.ToLowerInvariant()
        candidate_tree = $CandidateTree.ToLowerInvariant()
        state_root = $state
        state_entries = @(Get-DawnstrikeStateBoundaryManifestEntries -Snapshot $snapshot)
        tasks = @($taskRecords)
        writer_sids = @($writerSids)
        research_only = $true
        broker_execution_enabled = $false
    }
    $manifestJson = $manifestPayload | ConvertTo-Json -Depth 20
    $manifestHash = Get-DawnstrikeStateBoundarySha256Text ($manifestJson + "`r`n")
    $manifestPath = Join-Path $evidence ('state-boundary-rollback-' + $operationId + '-' + $manifestHash + '.json')
    $manifestWrite = Write-DawnstrikeStateBoundaryProtectedJson -Payload $manifestPayload -Path $manifestPath
    if ($manifestWrite.sha256 -cne $manifestHash) {
        foreach ($record in $snapshot) { $record.handle.Dispose() }
        throw 'StateRoot ACL rollback manifest serialization was not deterministic.'
    }
    $pendingPath = Join-Path $evidence ('state-boundary-pending-' + $operationId + '.json')
    $pendingPayload = [ordered]@{
        schema_version = 'dawnstrike.state_boundary_pending.v1'
        operation_id = $operationId
        created_at_utc = [DateTime]::UtcNow.ToString('o')
        state_root = $state
        candidate_sha = $CandidateSha.ToLowerInvariant()
        candidate_tree = $CandidateTree.ToLowerInvariant()
        rollback_manifest_path = $manifestPath
        rollback_manifest_sha256 = $manifestHash
        research_only = $true
        broker_execution_enabled = $false
    }
    $receiptWrite = $null
    try {
        # Arm recovery before the first task or ACL mutation.  Recovery is safe
        # even at this phase: it first holds every governed task Disabled, then
        # reapplies the exact manifest SDDL and prior task enablement.  Thus a
        # hard kill can neither strand Disabled tasks nor leave an unjournaled
        # partial ACL migration.
        $null = Write-DawnstrikeStateBoundaryProtectedJson -Payload $pendingPayload -Path $pendingPath
        Set-DawnstrikeStateBoundaryTasksDisabled -TaskRecords $taskRecords
        Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state
        foreach ($record in @($snapshot | Sort-Object { ([string]$_.path).Length } -Descending)) {
            $anchorDirectory = [bool]$record.is_directory -and (
                [string]::Equals([string]$record.path, $state, [StringComparison]::OrdinalIgnoreCase) -or
                [string]::Equals([string]$record.path, $locksRoot, [StringComparison]::OrdinalIgnoreCase)
            )
            $null = Set-DawnstrikeStateBoundaryPathAcl `
                -Path ([string]$record.path) -Directory ([bool]$record.is_directory) `
                -WriterSids $writerSids -AnchorDirectory:$anchorDirectory
            $check = Open-DawnstrikeStateBoundaryPath -Path ([string]$record.path) -Label 'Hardened StateRoot path'
            try {
                if ([string]$check.identity -cne [string]$record.identity) {
                    throw 'StateRoot identity changed during ACL hardening.'
                }
            }
            finally { $check.handle.Dispose() }
        }
        $finalSnapshot = @(Get-DawnstrikeStateBoundaryTreeSnapshot -StateRoot $state)
        try {
            $priorMap = @{}
            foreach ($record in $snapshot) { $priorMap[[string]$record.relative_path] = [string]$record.identity }
            if ($finalSnapshot.Count -ne $snapshot.Count) { throw 'StateRoot tree changed during ACL hardening.' }
            foreach ($record in $finalSnapshot) {
                $relative = [string]$record.relative_path
                if (-not $priorMap.ContainsKey($relative) -or $priorMap[$relative] -cne [string]$record.identity) {
                    throw 'StateRoot tree identity changed during ACL hardening.'
                }
                $anchorDirectory = [bool]$record.is_directory -and (
                    [string]::Equals([string]$record.path, $state, [StringComparison]::OrdinalIgnoreCase) -or
                    [string]::Equals([string]$record.path, $locksRoot, [StringComparison]::OrdinalIgnoreCase)
                )
                $null = Assert-DawnstrikeStateBoundaryPathAcl `
                    -Path ([string]$record.path) -Directory ([bool]$record.is_directory) `
                    -WriterSids $writerSids -AnchorDirectory:$anchorDirectory
            }
        }
        finally { foreach ($record in $finalSnapshot) { $record.handle.Dispose() } }

        Restore-DawnstrikeStateBoundaryTaskStates -TaskRecords $taskRecords -SuccessfulInstallation
        $finalTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
        foreach ($task in $finalTasks) {
            if ([string]$task.state -ne 'Disabled') {
                throw 'Host-boundary installation did not hold every unresealed production task Disabled.'
            }
        }
        $rootBound = Open-DawnstrikeStateBoundaryPath -Path $state -Label 'Hardened StateRoot'
        $locksBound = Open-DawnstrikeStateBoundaryPath -Path $locksRoot -Label 'Hardened StateRoot lock root'
        try {
            $rootSddl = Assert-DawnstrikeStateBoundaryPathAcl -Path $state -Directory $true `
                -WriterSids $writerSids -AnchorDirectory
            $locksSddl = Assert-DawnstrikeStateBoundaryPathAcl -Path $locksRoot -Directory $true `
                -WriterSids $writerSids -AnchorDirectory
            $stateIdentityText = (@(
                $snapshot | Sort-Object relative_path | ForEach-Object {
                    ([string]$_.relative_path + "`0" + [string]$_.identity + "`n")
                }
            ) -join '')
            $receipt = [ordered]@{
                schema_version = 'dawnstrike.state_boundary_installation.v2'
                status = 'PASS'
                operation_id = $operationId
                installed_at_utc = [DateTime]::UtcNow.ToString('o')
                installer_principal = [string]$identity.Name
                candidate_sha = $CandidateSha.ToLowerInvariant()
                candidate_tree = $CandidateTree.ToLowerInvariant()
                state_root = $state
                state_root_identity = [string]$rootBound.identity
                state_root_sddl = $rootSddl
                state_root_sddl_sha256 = Get-DawnstrikeStateBoundarySha256Text $rootSddl
                locks_root = $locksRoot
                locks_root_identity = [string]$locksBound.identity
                locks_root_sddl = $locksSddl
                locks_root_sddl_sha256 = Get-DawnstrikeStateBoundarySha256Text $locksSddl
                writer_sids = @($writerSids)
                task_definitions_and_principals = @($finalTasks)
                task_binding_sha256 = Get-DawnstrikeStateBoundaryTaskBindingHash -Tasks $finalTasks
                preinstallation_task_definitions_and_principals = @($taskRecords | ForEach-Object {
                    [ordered]@{
                        task_name = [string]$_.task_name
                        task_path = [string]$_.task_path
                        state = [string]$_.state
                        principal_user_id = [string]$_.principal_user_id
                        principal_sid = [string]$_.principal_sid
                        logon_type = [string]$_.logon_type
                        run_level = [string]$_.run_level
                        definition_sha256 = [string]$_.definition_sha256
                        canonical = [bool]$_.canonical
                    }
                })
                state_entry_count = $snapshot.Count
                state_identity_contract_sha256 = Get-DawnstrikeStateBoundarySha256Text $stateIdentityText
                rollback_manifest_path = $manifestPath
                rollback_manifest_sha256 = $manifestHash
                installed_helper_path = [IO.Path]::GetFullPath($InstalledHelperPath)
                installed_helper_sha256 = $InstalledHelperSha256.ToLowerInvariant()
                canonical_task_disposition = 'DISABLED_PENDING_GOVERNED_ACTIVATE_RESEAL'
                auxiliary_capture_disposition = 'DISABLED_PENDING_GOVERNED_HARDEN_CAPTURE_REBIND'
                last_activation_id = 'NONE'
                last_activation_terminal_receipt_relative_path = 'NONE'
                last_activation_terminal_receipt_sha256 = 'NONE'
                last_activation_terminal_journal_relative_path = 'NONE'
                last_activation_terminal_journal_sha256 = 'NONE'
                current_runtime_authorization_contract = 'NONE'
                current_runtime_authorization_sha256 = 'NONE'
                rollback_runtime_authorization_contract = 'NONE'
                rollback_runtime_authorization_sha256 = 'NONE'
                research_only = $true
                broker_execution_enabled = $false
            }
        }
        finally {
            $locksBound.handle.Dispose()
            $rootBound.handle.Dispose()
        }
        $receiptPath = Join-Path $evidence ('state-boundary-' + $CandidateSha.ToLowerInvariant() + '.json')
        $receiptWrite = Write-DawnstrikeStateBoundaryProtectedJson -Payload $receipt -Path $receiptPath
        $currentPath = Join-Path $evidence 'state-boundary-current.json'
        $currentWrite = Write-DawnstrikeStateBoundaryProtectedJson -Payload $receipt -Path $currentPath
        if ($currentWrite.sha256 -cne $receiptWrite.sha256) {
            throw 'StateRoot installation receipt copies diverged.'
        }
        Remove-Item -LiteralPath $pendingPath -Force -ErrorAction Stop
        return [pscustomobject]@{
            receipt = [pscustomobject]$receipt
            receipt_path = $receiptPath
            receipt_sha256 = [string]$receiptWrite.sha256
            current_receipt_path = $currentPath
            rollback_manifest_path = $manifestPath
            rollback_manifest_sha256 = $manifestHash
        }
    }
    catch {
        $failure = $_.Exception.Message
        $rolledBack = $false
        $rollbackError = ''
        try {
            Set-DawnstrikeStateBoundaryTasksDisabled -TaskRecords $taskRecords
            Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state
            Restore-DawnstrikeStateBoundaryAcls -StateRoot $state -Entries $manifestPayload.state_entries
            $null = Assert-DawnstrikeStateBoundaryManifestRestored `
                -StateRoot $state -Entries $manifestPayload.state_entries
            Assert-DawnstrikeStateBoundaryQuiescent -StateRoot $state
            Restore-DawnstrikeStateBoundaryTaskStates -TaskRecords $taskRecords
            $restoredTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
            $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
                -ExpectedTasks $taskRecords -LiveTasks $restoredTasks -WriterSids $writerSids
            foreach ($expectedTask in $taskRecords) {
                $restoredTask = @($restoredTasks | Where-Object {
                    [string]$_.task_name -ceq [string]$expectedTask.task_name
                })[0]
                if ([string]$restoredTask.state -cne [string]$expectedTask.state) {
                    throw 'StateRoot ACL rollback did not restore the exact prior task state.'
                }
            }
            if (Test-Path -LiteralPath $pendingPath -PathType Leaf) {
                Remove-Item -LiteralPath $pendingPath -Force -ErrorAction Stop
            }
            $rolledBack = $true
        }
        catch {
            $rollbackError = $_.Exception.Message
            Disable-DawnstrikeStateBoundaryTasksFailSafe -TaskRecords $taskRecords
        }
        $failureReceipt = [ordered]@{
            schema_version = 'dawnstrike.state_boundary_recovery.v1'
            operation_id = $operationId
            status = if ($rolledBack) { 'ROLLED_BACK' } else { 'RECOVERY_REQUIRED' }
            recovered_at_utc = [DateTime]::UtcNow.ToString('o')
            state_root = $state
            rollback_manifest_path = $manifestPath
            rollback_manifest_sha256 = $manifestHash
            installation_error = $failure
            rollback_error = $rollbackError
            tasks_disabled_fail_safe = -not $rolledBack
            research_only = $true
            broker_execution_enabled = $false
        }
        $failurePath = Join-Path $evidence ('state-boundary-recovery-' + $operationId + '.json')
        $null = Write-DawnstrikeStateBoundaryProtectedJson -Payload $failureReceipt -Path $failurePath
        if ($rolledBack) {
            throw "StateRoot ACL installation failed and was rolled back: $failure"
        }
        throw "StateRoot ACL installation failed; governed tasks remain Disabled: $failure; rollback=$rollbackError"
    }
    finally {
        foreach ($record in $snapshot) {
            if ($null -ne $record.handle) { $record.handle.Dispose() }
        }
    }
}

function Assert-DawnstrikeStateRootBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [string]$AllowedTaskMutationOperationId = '',
        [switch]$AllowTaskDefinitionDrift
    )

    $fixed = Assert-DawnstrikeStateBoundaryFixedPath -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    $state = [string]$fixed.state_root
    $evidence = [string]$fixed.evidence_root
    $null = Assert-DawnstrikeStateBoundaryNoPendingRecovery -EvidenceRoot $evidence
    $null = Assert-DawnstrikeStateBoundaryNoCandidateMigration -EvidenceRoot $evidence
    $taskMutation = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $evidence
    if ($null -ne $taskMutation) {
        if (
            [string]::IsNullOrWhiteSpace($AllowedTaskMutationOperationId) -or
            [string]$taskMutation.payload.schema_version -cne 'dawnstrike.state_boundary_task_mutation.v1' -or
            [string]$taskMutation.payload.operation_id -cne $AllowedTaskMutationOperationId
        ) {
            throw 'StateRoot task binding has an unresolved protected mutation intent; dispatch is denied.'
        }
    }
    $receiptPath = Join-Path $evidence 'state-boundary-current.json'
    $read = Read-DawnstrikeStateBoundaryProtectedJson -Path $receiptPath
    $locks = @($read.stream)
    try {
        $receipt = $read.payload
        if (
            [string]$receipt.schema_version -cne 'dawnstrike.state_boundary_installation.v2' -or
            [string]$receipt.status -cne 'PASS' -or
            [string]$receipt.candidate_sha -notmatch '^[0-9a-f]{40}$' -or
            [string]$receipt.candidate_tree -notmatch '^[0-9a-f]{40}$' -or
            [string]$receipt.state_root -cne $state -or
            $receipt.research_only -ne $true -or
            $receipt.broker_execution_enabled -ne $false
        ) { throw 'StateRoot installation receipt safety identity is invalid.' }
        $activationLineage = Get-DawnstrikeStateBoundaryActivationLineage -Receipt $receipt
        $currentRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $receipt -Kind current -StateRoot $state
        $rollbackRuntimeAuthorization = Get-DawnstrikeStateBoundaryRuntimeAuthorization `
            -Receipt $receipt -Kind rollback -StateRoot $state
        $writerSids = @($receipt.writer_sids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        if ($writerSids.Count -lt 1 -or @($writerSids | Where-Object { $_ -notmatch '^S-1-' }).Count -ne 0) {
            throw 'StateRoot installation receipt writer SID set is invalid.'
        }
        $installedHelper = [IO.Path]::GetFullPath([string]$receipt.installed_helper_path)
        $loadedHelper = [IO.Path]::GetFullPath($script:DawnstrikeStateBoundaryInstalledHelper)
        $loadedIsInstalled = [string]::Equals(
            $installedHelper, $loadedHelper, [StringComparison]::OrdinalIgnoreCase
        )
        if (-not $loadedIsInstalled) {
            $authorizedRuntimeHelper = [IO.Path]::GetFullPath(
                [string]$receipt.candidate_migration_runtime_helper_path
            )
            $preservedBootstrap =
                [string]$receipt.candidate_migration_authorization_state -ceq
                    'BOOTSTRAP_DISABLED' -and
                [string]$currentRuntimeAuthorization.operation_type -ceq 'BOOTSTRAP' -and
                [string]$rollbackRuntimeAuthorization.status -ceq 'NONE' -and
                [string]$activationLineage.status -ceq 'NONE'
            $preservedActive =
                [string]$receipt.candidate_migration_authorization_state -ceq
                    'ACTIVE_READY' -and
                [string]$currentRuntimeAuthorization.operation_type -ceq 'ACTIVATE' -and
                [string]$rollbackRuntimeAuthorization.status -ceq 'AUTHORIZED' -and
                [string]$activationLineage.status -ceq 'ACTIVE' -and
                [string]$activationLineage.activation_id -ceq
                    [string]$currentRuntimeAuthorization.terminal_id
            if (
                [string]$receipt.candidate_migration_schema_version -cne
                    'dawnstrike.state_boundary_candidate_migration.v1' -or
                [string]$receipt.candidate_migration_operation_id -notmatch '^[0-9a-f]{32}$' -or
                [string]$receipt.candidate_migration_request_contract_sha256 -notmatch
                    '^[0-9a-f]{64}$' -or
                [string]$receipt.candidate_migration_predecessor_receipt_sha256 -notmatch
                    '^[0-9a-f]{64}$' -or
                [string]$receipt.candidate_migration_predecessor_helper_sha256 -notmatch
                    '^[0-9a-f]{64}$' -or
                [string]$receipt.candidate_migration_runtime_helper_sha256 -notmatch
                    '^[0-9a-f]{64}$' -or
                [string]$receipt.candidate_migration_target_admission_sha256 -notmatch
                    '^[0-9a-f]{64}$' -or
                [string]$currentRuntimeAuthorization.status -cne 'AUTHORIZED' -or
                [string]$currentRuntimeAuthorization.runtime_sha -cne
                    [string]$receipt.candidate_migration_runtime_sha -or
                [string]$currentRuntimeAuthorization.runtime_tree -cne
                    [string]$receipt.candidate_migration_runtime_tree -or
                (-not $preservedBootstrap -and -not $preservedActive) -or
                -not [string]::Equals(
                    $authorizedRuntimeHelper, $loadedHelper,
                    [StringComparison]::OrdinalIgnoreCase
                )
            ) {
                throw 'StateRoot installation receipt does not authorize the loaded protected helper.'
            }
            Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $loadedHelper
            if ((Get-DawnstrikeStateBoundarySha256File $loadedHelper) -cne
                [string]$receipt.candidate_migration_runtime_helper_sha256) {
                throw 'Loaded authorized-runtime StateRoot helper bytes changed after candidate migration.'
            }
        }
        Assert-DawnstrikeStateBoundaryEvidenceAcl -Path $installedHelper
        if ((Get-DawnstrikeStateBoundarySha256File $installedHelper) -cne [string]$receipt.installed_helper_sha256) {
            throw 'Protected StateRoot helper bytes differ from the installation receipt.'
        }
        $rootBound = Open-DawnstrikeStateBoundaryPath -Path $state -Label 'Production StateRoot'
        $locksRoot = Join-Path $state 'locks'
        $locksBound = Open-DawnstrikeStateBoundaryPath -Path $locksRoot -Label 'Production StateRoot lock root'
        $locks += @($rootBound.handle, $locksBound.handle)
        $rootSddl = Assert-DawnstrikeStateBoundaryPathAcl -Path $state -Directory $true `
            -WriterSids $writerSids -AnchorDirectory
        $lockSddl = Assert-DawnstrikeStateBoundaryPathAcl -Path $locksRoot -Directory $true `
            -WriterSids $writerSids -AnchorDirectory
        if (
            [string]$rootBound.identity -cne [string]$receipt.state_root_identity -or
            [string]$locksBound.identity -cne [string]$receipt.locks_root_identity -or
            (Get-DawnstrikeStateBoundarySha256Text $rootSddl) -cne [string]$receipt.state_root_sddl_sha256 -or
            (Get-DawnstrikeStateBoundarySha256Text $lockSddl) -cne [string]$receipt.locks_root_sddl_sha256
        ) { throw 'Live StateRoot identity or DACL differs from the protected installation receipt.' }

        if ((Get-DawnstrikeStateBoundaryTaskBindingHash -Tasks $receipt.task_definitions_and_principals) -cne [string]$receipt.task_binding_sha256) {
            throw 'Protected StateRoot task binding hash is invalid.'
        }
        $liveTasks = @(Get-DawnstrikeStateBoundaryTaskInventory)
        if ($AllowTaskDefinitionDrift) {
            $null = Assert-DawnstrikeStateBoundaryTaskPrincipalsMatch `
                -ExpectedTasks $receipt.task_definitions_and_principals `
                -LiveTasks $liveTasks -WriterSids $writerSids
        }
        else {
            $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
                -ExpectedTasks $receipt.task_definitions_and_principals `
                -LiveTasks $liveTasks -WriterSids $writerSids
        }
        return [pscustomobject]@{
            status = 'PASS'
            candidate_sha = [string]$receipt.candidate_sha
            candidate_tree = [string]$receipt.candidate_tree
            state_root = $state
            writer_sids = @($writerSids)
            receipt_path = $receiptPath
            receipt_sha256 = [string]$read.sha256
            receipt = $receipt
            locks = @($locks)
            research_only = $true
            broker_execution_enabled = $false
        }
    }
    catch {
        foreach ($handle in $locks) { if ($null -ne $handle) { $handle.Dispose() } }
        throw
    }
}
