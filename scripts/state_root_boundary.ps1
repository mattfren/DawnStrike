Set-StrictMode -Version Latest

$script:DawnstrikeStateBoundaryFixedRoot = 'C:\r\dawnstrike-state'
$script:DawnstrikeStateBoundaryEvidenceRoot = 'C:\ProgramData\Dawnstrike'
$script:DawnstrikeStateBoundaryInstalledHelper = 'C:\Program Files\Dawnstrike\bin\state_root_boundary.ps1'
$script:DawnstrikeStateBoundaryCanonicalTasks = @(
    'Dawnstrike AlphaOps Morning',
    'Dawnstrike AlphaOps Monitor 5m',
    'Dawnstrike AlphaOps EOD Full Report',
    'Dawnstrike AlphaOps V6 Weekly Training',
    'Dawnstrike 10of10 Daily Finalize'
)
$script:DawnstrikeStateBoundaryAuxiliaryTask = 'Dawnstrike Delayed SIP Capture'

if (-not ('Dawnstrike.StateBoundary.BoundPath' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.StateBoundary {
    [StructLayout(LayoutKind.Sequential)]
    internal struct ByHandleFileInformation {
        public uint FileAttributes;
        public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        public uint VolumeSerialNumber;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint NumberOfLinks;
        public uint FileIndexHigh;
        public uint FileIndexLow;
    }

    internal static class NativeMethods {
        internal const uint GENERIC_READ = 0x80000000;
        internal const uint OPEN_EXISTING = 3;
        internal const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
        internal const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;
        internal const uint FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400;
        internal const uint MOVEFILE_REPLACE_EXISTING = 0x00000001;
        internal const uint MOVEFILE_WRITE_THROUGH = 0x00000008;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        internal static extern SafeFileHandle CreateFileW(
            string fileName,
            uint desiredAccess,
            FileShare shareMode,
            IntPtr securityAttributes,
            uint creationDisposition,
            uint flagsAndAttributes,
            IntPtr templateFile);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileInformationByHandle(
            SafeFileHandle handle,
            out ByHandleFileInformation information);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool MoveFileExW(
            string existingFileName,
            string newFileName,
            uint flags);
    }

    public sealed class BoundPath : IDisposable {
        public SafeFileHandle Handle { get; private set; }
        public string Identity { get; private set; }
        public uint Attributes { get; private set; }

        private BoundPath(SafeFileHandle handle, string identity, uint attributes) {
            Handle = handle;
            Identity = identity;
            Attributes = attributes;
        }

        public static BoundPath Open(string path) {
            SafeFileHandle handle = NativeMethods.CreateFileW(
                path,
                // A metadata-only READ_CONTROL handle does not prevent a
                // modern Windows directory rename. GENERIC_READ combined
                // with a share mask that omits Delete does, while remaining
                // available to the admitted read/execute principal.
                NativeMethods.GENERIC_READ,
                FileShare.Read | FileShare.Write,
                IntPtr.Zero,
                NativeMethods.OPEN_EXISTING,
                NativeMethods.FILE_FLAG_BACKUP_SEMANTICS |
                    NativeMethods.FILE_FLAG_OPEN_REPARSE_POINT,
                IntPtr.Zero);
            if (handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "Could not bind StateRoot path: " + path);
            }
            ByHandleFileInformation information;
            if (!NativeMethods.GetFileInformationByHandle(handle, out information)) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "Could not identify StateRoot path: " + path);
            }
            if ((information.FileAttributes & NativeMethods.FILE_ATTRIBUTE_REPARSE_POINT) != 0) {
                handle.Dispose();
                throw new InvalidOperationException("StateRoot boundary contains a reparse point: " + path);
            }
            string identity = information.VolumeSerialNumber.ToString("x8") + ":" +
                information.FileIndexHigh.ToString("x8") +
                information.FileIndexLow.ToString("x8");
            return new BoundPath(handle, identity, information.FileAttributes);
        }

        public void Dispose() {
            if (Handle != null) {
                Handle.Dispose();
                Handle = null;
            }
        }
    }

    public sealed class BoundPathChain : IDisposable {
        private List<BoundPath> paths;
        public string Identity { get; private set; }
        public uint Attributes { get; private set; }

        private BoundPathChain(List<BoundPath> paths) {
            this.paths = paths;
            BoundPath terminal = paths[paths.Count - 1];
            Identity = terminal.Identity;
            Attributes = terminal.Attributes;
        }

        public static BoundPathChain Open(string path) {
            string full = Path.GetFullPath(path);
            string root = Path.GetPathRoot(full);
            if (String.IsNullOrEmpty(root)) {
                throw new InvalidOperationException("StateRoot boundary path has no trusted volume anchor.");
            }
            List<BoundPath> opened = new List<BoundPath>();
            try {
                string cursor = root;
                opened.Add(BoundPath.Open(cursor));
                string tail = full.Substring(root.Length);
                string[] components = tail.Split(
                    new char[] { Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar },
                    StringSplitOptions.RemoveEmptyEntries);
                foreach (string component in components) {
                    cursor = Path.Combine(cursor, component);
                    opened.Add(BoundPath.Open(cursor));
                }
                return new BoundPathChain(opened);
            }
            catch {
                for (int index = opened.Count - 1; index >= 0; index--) {
                    opened[index].Dispose();
                }
                throw;
            }
        }

        public void Dispose() {
            if (paths == null) { return; }
            for (int index = paths.Count - 1; index >= 0; index--) {
                paths[index].Dispose();
            }
            paths = null;
        }
    }

    public sealed class DisposableGroup : IDisposable {
        private IDisposable[] items;
        public DisposableGroup(IDisposable[] items) { this.items = items; }
        public void Dispose() {
            if (items == null) { return; }
            for (int index = items.Length - 1; index >= 0; index--) {
                if (items[index] != null) { items[index].Dispose(); }
            }
            items = null;
        }
    }

    public static class AtomicFile {
        public static void Replace(string source, string destination) {
            if (!NativeMethods.MoveFileExW(
                    source,
                    destination,
                    NativeMethods.MOVEFILE_REPLACE_EXISTING |
                        NativeMethods.MOVEFILE_WRITE_THROUGH)) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Could not atomically replace protected StateRoot evidence: " + destination);
            }
        }

        public static void MoveNoReplace(string source, string destination) {
            if (!NativeMethods.MoveFileExW(
                    source,
                    destination,
                    NativeMethods.MOVEFILE_WRITE_THROUGH)) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Could not atomically create protected StateRoot evidence: " + destination);
            }
        }
    }
}
'@
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
        'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
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

function Get-DawnstrikeStateBoundaryTaskMutationIntent {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$EvidenceRoot)
    $path = Get-DawnstrikeStateBoundaryTaskMutationIntentPath -EvidenceRoot $EvidenceRoot
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $read = Read-DawnstrikeStateBoundaryProtectedJson -Path $path
    try { return [pscustomobject]@{ path = $path; payload = $read.payload; sha256 = $read.sha256 } }
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

function Assert-DawnstrikeStateBoundaryTaskMutationIntent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Intent,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$RequestContractSha256 = ''
    )
    $payload = $Intent.payload
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
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)]$TerminalRecord,
        [Parameter(Mandatory = $true)]$LiveTasks
    )

    if ($Mode -in @('Activate', 'Rollback')) {
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
                [string]$task.state -cne 'Ready'
            ) { throw 'Terminal canonical task inventory is inconsistent or not Ready.' }
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
            required_state = 'Ready'
        }
        if ($Mode -eq 'Activate') { return [pscustomobject]$contract }

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

function Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
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
        $terminalTaskContract = [ordered]@{ mode = $Mode }
        if ($Mode -in @('Activate', 'Rollback')) {
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
                'dawnstrike.runtime_operation_journal.v2'
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
            }
            locks = @($locks)
        }
    }
    catch {
        foreach ($lock in $locks) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Get-DawnstrikeStateBoundaryTerminalCandidatePaths {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
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
    $receiptFolderName = if ($Mode -eq 'Activate') { 'runtime-activation' } else { 'runtime-rollback' }
    $receiptStem = if ($Mode -eq 'Activate') { 'runtime-activation-' } else { 'runtime-rollback-' }
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
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
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

function Find-DawnstrikeStateBoundaryExactTerminalEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [Parameter(Mandatory = $true)]$LiveTasks,
        [string[]]$ExcludedEvidencePairs = @()
    )

    # Hardening intentionally has no automatic adoption path: its recovery
    # contract remains fail-closed Disabled until the governed mode reruns.
    if ($Mode -eq 'HardenCapture') { return $null }
    $state = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $candidates = @(Get-DawnstrikeStateBoundaryTerminalCandidatePaths `
        -StateRoot $state -Mode $Mode -ExpectedSha $ExpectedSha)

    $matches = @()
    foreach ($candidate in @($candidates)) {
        if (-not (Test-Path -LiteralPath ([string]$candidate.receipt) -PathType Leaf) -or
            -not (Test-Path -LiteralPath ([string]$candidate.journal) -PathType Leaf)) {
            continue
        }
        try {
            $evidence = Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `
                -StateRoot $state -Mode $Mode -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree `
                -ReceiptPath ([string]$candidate.receipt) -JournalPath ([string]$candidate.journal)
            try {
                $evidencePair = [string]$evidence.record.receipt_sha256 + ':' +
                    [string]$evidence.record.journal_sha256
                if ($evidencePair -in @($ExcludedEvidencePairs)) { continue }
                $null = Assert-DawnstrikeStateBoundaryTerminalTaskContract `
                    -Mode $Mode -TerminalRecord $evidence.record.task_contract `
                    -LiveTasks $LiveTasks
                $matches += [pscustomobject]@{
                    receipt_path = [string]$candidate.receipt
                    journal_path = [string]$candidate.journal
                    receipt_sha256 = [string]$evidence.record.receipt_sha256
                    journal_sha256 = [string]$evidence.record.journal_sha256
                }
            }
            finally {
                foreach ($lock in @($evidence.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
            }
        }
        catch {
            # Only an exact, fully self-bound receipt+journal+live-task tuple is
            # adoptable. Invalid or partial crash artifacts remain fail-closed.
        }
    }
    if ($matches.Count -gt 1) {
        throw 'Multiple exact terminal task-mutation evidence tuples are ambiguous.'
    }
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Complete-DawnstrikeStateBoundaryExistingTerminal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
        [Parameter(Mandatory = $true)]$Terminal
    )
    $completion = Complete-DawnstrikeStateBoundaryTaskMutation `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree -OperationId $OperationId `
        -TerminalReceiptPath ([string]$Terminal.receipt_path) `
        -TerminalJournalPath ([string]$Terminal.journal_path)
    $sealed = Assert-DawnstrikeStateRootBoundary `
        -StateRoot $StateRoot -EvidenceRoot $EvidenceRoot
    return [pscustomobject]@{
        status = 'ADOPTED_EXISTING_TERMINAL'
        operation_id = $OperationId
        mode = $Mode
        already_completed = $true
        resumed = $true
        receipt_path = [string]$sealed.receipt_path
        receipt_sha256 = [string]$completion.receipt_sha256
        completion_path = [string]$completion.completion_path
        terminal_receipt_path = [string]$Terminal.receipt_path
        terminal_journal_path = [string]$Terminal.journal_path
        writer_sids = @($sealed.writer_sids)
        locks = @($sealed.locks)
        research_only = $true
        broker_execution_enabled = $false
    }
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
    $terminalEvidence = Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `
        -StateRoot $StateRoot -Mode ([string]$intentPayload.mode) `
        -ExpectedSha ([string]$intentPayload.expected_sha) `
        -ExpectedTree ([string]$intentPayload.expected_tree) `
        -ReceiptPath ([string]$terminalRecord.receipt_path) `
        -JournalPath ([string]$terminalRecord.journal_path) `
        -ExpectedReceiptSha256 ([string]$terminalRecord.receipt_sha256) `
        -ExpectedJournalSha256 ([string]$terminalRecord.journal_sha256)
    if (
        (([pscustomobject]$terminalRecord.task_contract | ConvertTo-Json -Compress) -cne
            ($terminalEvidence.record.task_contract | ConvertTo-Json -Compress))
    ) { throw 'Protected completion terminal task contract differs from the exact terminal receipt.' }
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

function Get-DawnstrikeStateBoundaryTaskMutationReadAdmission {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [string]$EvidenceRoot = $script:DawnstrikeStateBoundaryEvidenceRoot,
        [Parameter(Mandatory = $true)][ValidateSet(
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
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
            $validatedCompletion = Complete-DawnstrikeStateBoundaryTaskMutationAdoption `
                -Intent $intent -Completion $completion `
                -StateRoot $state -EvidenceRoot $evidence -ValidationOnly
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
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
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
        return Complete-DawnstrikeStateBoundaryTaskMutationAdoption `
            -Intent $intent -Completion $completionObject -StateRoot $state -EvidenceRoot $evidence
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
        # A task-mutating mode may have durably sealed its terminal receipt and
        # journal immediately before the launcher was killed. Prove and adopt
        # that exact live terminal tuple before any fail-safe disable or rerun.
        $existingTerminal = Find-DawnstrikeStateBoundaryExactTerminalEvidence `
            -StateRoot $state -Mode $Mode -ExpectedSha $ExpectedSha `
            -ExpectedTree $ExpectedTree -LiveTasks $liveTasks `
            -ExcludedEvidencePairs @($intent.payload.predecessor_terminal_evidence_pairs)
        if ($null -ne $existingTerminal) {
            foreach ($lock in @($boundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
            $boundary.locks = @()
            return Complete-DawnstrikeStateBoundaryExistingTerminal `
                -StateRoot $state -EvidenceRoot $evidence -Mode $Mode `
                -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree `
                -OperationId $operationId -Terminal $existingTerminal
        }
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
        return [pscustomobject]@{
            status = 'RESUME_REQUIRED'
            operation_id = $operationId
            mode = $Mode
            intent_path = [string]$intent.path
            intent_sha256 = [string]$intent.sha256
            already_completed = $false
            resumed = $true
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
            'HardenCapture', 'Activate', 'RebindCapture', 'Rollback'
        )][string]$Mode,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
        [Parameter(Mandatory = $true)][string]$TerminalReceiptPath,
        [Parameter(Mandatory = $true)][string]$TerminalJournalPath
    )
    $intent = Get-DawnstrikeStateBoundaryTaskMutationIntent -EvidenceRoot $EvidenceRoot
    if ($null -eq $intent -or [string]$intent.payload.operation_id -cne $OperationId) {
        throw 'Protected StateRoot task-mutation intent is missing or changed.'
    }
    $null = Assert-DawnstrikeStateBoundaryTaskMutationIntent `
        -Intent $intent -StateRoot $StateRoot -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree
    $terminalEvidence = Get-DawnstrikeStateBoundaryTaskMutationTerminalEvidence `
        -StateRoot $StateRoot -Mode $Mode -ExpectedSha $ExpectedSha -ExpectedTree $ExpectedTree `
        -ReceiptPath $TerminalReceiptPath -JournalPath $TerminalJournalPath
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
        if ($Mode -in @('Activate', 'Rollback')) {
            $canonical = @($liveTasks | Where-Object { [bool]$_.canonical })
            if ($canonical.Count -ne $script:DawnstrikeStateBoundaryCanonicalTasks.Count -or
                @($canonical | Where-Object { [string]$_.state -ne 'Ready' }).Count -ne 0) {
                throw 'Governed activation/rollback did not leave every canonical task Ready.'
            }
            $newReceipt['canonical_task_disposition'] = 'ENABLED_BY_GOVERNED_' + $Mode.ToUpperInvariant() + '_RESEAL'
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
            $exactTerminal = Find-DawnstrikeStateBoundaryExactTerminalEvidence `
                -StateRoot $StateRoot -Mode ([string]$intent.payload.mode) `
                -ExpectedSha ([string]$intent.payload.expected_sha) `
                -ExpectedTree ([string]$intent.payload.expected_tree) -LiveTasks $live `
                -ExcludedEvidencePairs @($intent.payload.predecessor_terminal_evidence_pairs)
            if ($null -ne $exactTerminal) {
                # Preserve the protected intent and exact terminal task state.
                # Enter will seal it (or adopt an already-written completion)
                # on the next launcher admission instead of disabling/rerunning.
                return $false
            }
            $null = Assert-DawnstrikeStateBoundaryTaskInventoryMatches `
                -ExpectedTasks $boundary.receipt.task_definitions_and_principals `
                -LiveTasks $live -WriterSids @($boundary.writer_sids)
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
    if ([string]$acl.Owner -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$') {
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
        if (
            $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
            [string]$rule.IdentityReference -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$' -and
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
        $writerSids = @($receipt.writer_sids | ForEach-Object { [string]$_ } | Sort-Object -Unique)
        if ($writerSids.Count -lt 1 -or @($writerSids | Where-Object { $_ -notmatch '^S-1-' }).Count -ne 0) {
            throw 'StateRoot installation receipt writer SID set is invalid.'
        }
        $installedHelper = [IO.Path]::GetFullPath([string]$receipt.installed_helper_path)
        if (-not [string]::Equals($installedHelper, $script:DawnstrikeStateBoundaryInstalledHelper, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'StateRoot installation receipt does not bind the protected helper path.'
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
