Set-StrictMode -Version Latest

function ConvertTo-DawnstrikeExactMarketDate {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Value)

    $parsed = [DateTime]::MinValue
    if (-not [DateTime]::TryParseExact(
        $Value,
        'yyyy-MM-dd',
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None,
        [ref]$parsed
    )) {
        throw 'Market date is not a real canonical calendar date.'
    }
    if ($parsed.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) -cne $Value) {
        throw 'Market date is not a real canonical calendar date.'
    }
    return $parsed.Date
}

function Get-DawnstrikeProtectedPathEntry {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path)
    $parent = Split-Path -Parent $full
    if ([string]::IsNullOrWhiteSpace($parent) -or
        -not (Test-Path -LiteralPath $parent -PathType Container)) {
        return $null
    }
    $matches = @(
        Get-ChildItem -LiteralPath $parent -Force -ErrorAction Stop |
            Where-Object {
                [string]::Equals(
                    [IO.Path]::GetFullPath($_.FullName),
                    $full,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    if ($matches.Count -gt 1) {
        throw 'Protected state path has an ambiguous filesystem identity.'
    }
    if ($matches.Count -eq 0) { return $null }
    return $matches[0]
}

function Assert-DawnstrikeProtectedPathNoReparse {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [ValidateSet('Any', 'Directory', 'File')][string]$ExpectedType = 'Any',
        [switch]$AllowMissing
    )

    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw "$Label is not an absolute filesystem path."
    }
    $rootItem = Get-Item -LiteralPath $root -Force -ErrorAction Stop
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label contains a reparse point."
    }
    $cursor = $root
    $relative = $full.Substring($root.Length).TrimStart('\', '/')
    $segments = if ([string]::IsNullOrWhiteSpace($relative)) {
        @()
    }
    else { @($relative -split '[\\/]') }
    for ($index = 0; $index -lt $segments.Count; $index++) {
        $cursor = Join-Path $cursor ([string]$segments[$index])
        $item = Get-DawnstrikeProtectedPathEntry -Path $cursor
        if ($null -eq $item) {
            if ($AllowMissing) { return $full }
            throw "$Label is missing."
        }
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse point."
        }
        if ($index -lt ($segments.Count - 1) -and -not $item.PSIsContainer) {
            throw "$Label has a non-directory parent component."
        }
    }
    $target = if ($segments.Count -eq 0) { $rootItem } else {
        Get-DawnstrikeProtectedPathEntry -Path $full
    }
    if ($null -eq $target) {
        if ($AllowMissing) { return $full }
        throw "$Label is missing."
    }
    if ($ExpectedType -eq 'Directory' -and -not $target.PSIsContainer) {
        throw "$Label is not a regular directory."
    }
    if ($ExpectedType -eq 'File' -and $target.PSIsContainer) {
        throw "$Label is not a regular file."
    }
    return $full
}

function Assert-DawnstrikeUniverseStateBoundary {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $state = Assert-DawnstrikeProtectedPathNoReparse `
        -Path $StateRoot -Label 'Core-universe bootstrap state root' `
        -ExpectedType Directory
    $null = Assert-DawnstrikeProtectedPathNoReparse `
        -Path (Join-Path $state 'logs') `
        -Label 'Core-universe bootstrap log root' `
        -ExpectedType Directory -AllowMissing
    $locks = Join-Path $state 'locks'
    $null = Assert-DawnstrikeProtectedPathNoReparse `
        -Path $locks -Label 'Core-universe bootstrap lock root' `
        -ExpectedType Directory -AllowMissing
    $null = Assert-DawnstrikeProtectedPathNoReparse `
        -Path (Join-Path $locks '.dawnstrike-daily-lock-boundary.v1') `
        -Label 'Core-universe bootstrap lock boundary marker' `
        -ExpectedType File -AllowMissing
    $config = Join-Path $state 'config'
    $null = Assert-DawnstrikeProtectedPathNoReparse `
        -Path $config -Label 'Core-universe bootstrap config root' `
        -ExpectedType Directory -AllowMissing
    $null = Assert-DawnstrikeProtectedPathNoReparse `
        -Path (Join-Path $config '.luna_core_universe.refresh.lock') `
        -Label 'Core-universe bootstrap refresh lock' -ExpectedType File -AllowMissing
    $null = Assert-DawnstrikeProtectedPathNoReparse `
        -Path (Join-Path $config 'luna_core_universe.json') `
        -Label 'Core-universe bootstrap active pointer' -ExpectedType File -AllowMissing
    $null = Assert-DawnstrikeProtectedPathNoReparse `
        -Path (Join-Path $config 'luna_core_universe_generations') `
        -Label 'Core-universe bootstrap generation root' `
        -ExpectedType Directory -AllowMissing
    return $state
}

function Open-DawnstrikeProtectedDirectoryHandle {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowWriteShare
    )

    if (-not ('Dawnstrike.Security.ProtectedDirectoryNative' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Security {
    public static class ProtectedDirectoryNative {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern SafeFileHandle CreateFileW(
            string path,
            UInt32 desiredAccess,
            UInt32 shareMode,
            IntPtr securityAttributes,
            UInt32 creationDisposition,
            UInt32 flagsAndAttributes,
            IntPtr templateFile
        );
    }
}
'@
    }
    $full = Assert-DawnstrikeProtectedPathNoReparse `
        -Path $Path -Label $Label -ExpectedType Directory
    $shareMode = [uint32]0x00000001
    if ($AllowWriteShare) { $shareMode = $shareMode -bor [uint32]0x00000002 }
    # DELETE sharing is never admitted. WRITE sharing is reserved for a
    # directory whose exact marker is already held immutable and non-deletable.
    $handle = [Dawnstrike.Security.ProtectedDirectoryNative]::CreateFileW(
        $full,
        [uint32]2147483648,
        $shareMode,
        [IntPtr]::Zero,
        [uint32]3,
        [uint32]0x02200000,
        [IntPtr]::Zero
    )
    if ($null -eq $handle -or $handle.IsInvalid) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($null -ne $handle) { $handle.Dispose() }
        throw "$Label could not be held against replacement (Windows error $errorCode)."
    }
    try {
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $full -Label $Label -ExpectedType Directory
        return $handle
    }
    catch {
        $handle.Dispose()
        throw
    }
}

function Test-DawnstrikeProtectedExactBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][byte[]]$Actual,
        [Parameter(Mandatory = $true)][byte[]]$Expected
    )

    if ($Actual.Length -ne $Expected.Length) { return $false }
    for ($index = 0; $index -lt $Actual.Length; $index++) {
        if ($Actual[$index] -ne $Expected[$index]) { return $false }
    }
    return $true
}

function Open-DawnstrikeProtectedFileReadHandle {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = Assert-DawnstrikeProtectedPathNoReparse `
        -Path $Path -Label $Label -ExpectedType File
    $handle = [Dawnstrike.Security.ProtectedDirectoryNative]::CreateFileW(
        $full,
        [uint32]2147483648,
        [uint32]0x00000001,
        [IntPtr]::Zero,
        [uint32]3,
        [uint32]0x00200000,
        [IntPtr]::Zero
    )
    if ($null -eq $handle -or $handle.IsInvalid) {
        $errorCode = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($null -ne $handle) { $handle.Dispose() }
        throw "$Label could not be held immutable (Windows error $errorCode)."
    }
    try {
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $full -Label $Label -ExpectedType File
        return $handle
    }
    catch {
        $handle.Dispose()
        throw
    }
}

function Open-DawnstrikeProtectedWriteDirectoryBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $strictHandle = Open-DawnstrikeProtectedDirectoryHandle -Path $Path -Label $Label
    $markerHandle = $null
    $directoryHandle = $null
    try {
        $full = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $Path -Label $Label -ExpectedType Directory
        $markerPath = Join-Path $full '.dawnstrike-daily-lock-boundary.v1'
        $expectedBytes = [Text.Encoding]::UTF8.GetBytes(
            "dawnstrike.daily_lock.directory_boundary.v1`n"
        )
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $markerPath -Label "$Label boundary marker" `
            -ExpectedType File -AllowMissing
        if (-not (Test-Path -LiteralPath $markerPath)) {
            try {
                $stream = [IO.File]::Open(
                    $markerPath,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                try {
                    $stream.Write($expectedBytes, 0, $expectedBytes.Length)
                    $stream.Flush($true)
                }
                finally { $stream.Dispose() }
            }
            catch [IO.IOException] {
                if (-not (Test-Path -LiteralPath $markerPath)) { throw }
            }
        }
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $markerPath -Label "$Label boundary marker" -ExpectedType File
        if (-not (Test-DawnstrikeProtectedExactBytes `
            -Actual ([IO.File]::ReadAllBytes($markerPath)) -Expected $expectedBytes)) {
            throw "$Label boundary marker has unexpected bytes."
        }
        $markerHandle = Open-DawnstrikeProtectedFileReadHandle `
            -Path $markerPath -Label "$Label boundary marker"
        if (-not (Test-DawnstrikeProtectedExactBytes `
            -Actual ([IO.File]::ReadAllBytes($markerPath)) -Expected $expectedBytes)) {
            throw "$Label boundary marker changed during admission."
        }
        $directoryHandle = Open-DawnstrikeProtectedDirectoryHandle `
            -Path $full -Label $Label -AllowWriteShare
        $strictHandle.Dispose()
        $strictHandle = $null
        return [pscustomobject]@{
            path = $full
            marker_path = $markerPath
            marker_handle = $markerHandle
            directory_handle = $directoryHandle
        }
    }
    catch {
        if ($null -ne $directoryHandle) { $directoryHandle.Dispose() }
        if ($null -ne $markerHandle) { $markerHandle.Dispose() }
        if ($null -ne $strictHandle) { $strictHandle.Dispose() }
        throw
    }
}

function Close-DawnstrikeProtectedWriteDirectoryBoundary {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Boundary)

    $strictHandle = $null
    try {
        $strictHandle = Open-DawnstrikeProtectedDirectoryHandle `
            -Path ([string]$Boundary.path) -Label 'Protected write-boundary directory cleanup'
        if ($null -ne $Boundary.directory_handle) { $Boundary.directory_handle.Dispose() }
        $expectedBytes = [Text.Encoding]::UTF8.GetBytes(
            "dawnstrike.daily_lock.directory_boundary.v1`n"
        )
        if (-not (Test-DawnstrikeProtectedExactBytes `
            -Actual ([IO.File]::ReadAllBytes([string]$Boundary.marker_path)) `
            -Expected $expectedBytes)) {
            throw 'Protected write-boundary marker changed before cleanup.'
        }
        if ($null -ne $Boundary.marker_handle) { $Boundary.marker_handle.Dispose() }
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path ([string]$Boundary.marker_path) `
            -Label 'Protected write-boundary marker cleanup' -ExpectedType File
        if (-not (Test-DawnstrikeProtectedExactBytes `
            -Actual ([IO.File]::ReadAllBytes([string]$Boundary.marker_path)) `
            -Expected $expectedBytes)) {
            throw 'Protected write-boundary marker changed during cleanup.'
        }
        Remove-Item -LiteralPath ([string]$Boundary.marker_path) -Force -ErrorAction Stop
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path ([string]$Boundary.marker_path) `
            -Label 'Protected write-boundary marker cleanup' `
            -ExpectedType File -AllowMissing
        if (Test-Path -LiteralPath ([string]$Boundary.marker_path)) {
            throw 'Protected write-boundary marker remained after cleanup.'
        }
    }
    finally {
        if ($null -ne $Boundary.directory_handle) { $Boundary.directory_handle.Dispose() }
        if ($null -ne $Boundary.marker_handle) { $Boundary.marker_handle.Dispose() }
        if ($null -ne $strictHandle) { $strictHandle.Dispose() }
    }
}

function New-DawnstrikeUniverseLogDirectorySecurity {
    [CmdletBinding()]
    param()

    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor `
        [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $none = [Security.AccessControl.PropagationFlags]::None
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $administrators, 'FullControl', $inheritance, $none, 'Allow'
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $system, 'FullControl', $inheritance, $none, 'Allow'
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $users, 'ReadAndExecute', $inheritance, $none, 'Allow'
    ))
    return $acl
}

function Assert-DawnstrikeUniverseLogDirectoryAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $applied = Get-Acl -LiteralPath $Path -ErrorAction Stop
    if (-not $applied.AreAccessRulesProtected -or
        $applied.GetOwner([Security.Principal.SecurityIdentifier]).Value -cne $administrators.Value) {
        throw 'Core-universe protected log directory ACL did not become inheritance-protected.'
    }
    $writeLike = [Security.AccessControl.FileSystemRights]::Write -bor `
        [Security.AccessControl.FileSystemRights]::Delete -bor `
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor `
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor `
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    $rules = @($applied.GetAccessRules(
        $true,
        $false,
        [Security.Principal.SecurityIdentifier]
    ))
    $administratorWriter = $false
    $systemWriter = $false
    foreach ($rule in $rules) {
        $sid = [string]$rule.IdentityReference.Value
        $isAllow = [string]$rule.AccessControlType -ceq 'Allow'
        $hasWrite = ([int64]$rule.FileSystemRights -band [int64]$writeLike) -ne 0
        if ($isAllow -and $hasWrite) {
            if ($sid -ceq $administrators.Value) { $administratorWriter = $true; continue }
            if ($sid -ceq $system.Value) { $systemWriter = $true; continue }
            throw 'Core-universe protected log directory grants write access outside administrators/SYSTEM.'
        }
    }
    if (-not $administratorWriter -or -not $systemWriter) {
        throw 'Core-universe protected log directory lacks its exact privileged writers.'
    }
}

function Open-DawnstrikeUniverseBootstrapLogBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$MarketDate
    )

    $state = Assert-DawnstrikeUniverseStateBoundary -StateRoot $StateRoot
    $stateHandle = Open-DawnstrikeProtectedDirectoryHandle `
        -Path $state -Label 'Core-universe bootstrap state root'
    $locksBoundary = $null
    $logsHandle = $null
    $operationHandle = $null
    try {
        $locks = Join-Path $state 'locks'
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $locks -Label 'Core-universe bootstrap lock root' `
            -ExpectedType Directory -AllowMissing
        if (-not (Test-Path -LiteralPath $locks)) {
            New-Item -ItemType Directory -Path $locks -ErrorAction Stop | Out-Null
        }
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $locks -Label 'Core-universe bootstrap lock root' -ExpectedType Directory
        $locksBoundary = Open-DawnstrikeProtectedWriteDirectoryBoundary `
            -Path $locks -Label 'Core-universe bootstrap lock root'

        $logs = Join-Path $state 'logs'
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $logs -Label 'Core-universe bootstrap log root' `
            -ExpectedType Directory -AllowMissing
        if (-not (Test-Path -LiteralPath $logs)) {
            New-Item -ItemType Directory -Path $logs -ErrorAction Stop | Out-Null
        }
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $logs -Label 'Core-universe bootstrap log root' -ExpectedType Directory
        $logsHandle = Open-DawnstrikeProtectedDirectoryHandle `
            -Path $logs -Label 'Core-universe bootstrap log root'

        $operationRoot = Join-Path $logs (
            'protected-luna-core-bootstrap-' + $MarketDate + '-' +
            [Guid]::NewGuid().ToString('N')
        )
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $operationRoot -Label 'Core-universe bootstrap operation log root' `
            -ExpectedType Directory -AllowMissing
        $operationAcl = New-DawnstrikeUniverseLogDirectorySecurity
        # Apply the non-inherited privileged DACL as part of directory
        # creation.  Creating first and tightening later would let an
        # unprivileged watcher retain a write handle acquired in between.
        [IO.Directory]::CreateDirectory($operationRoot, $operationAcl) | Out-Null
        $operationHandle = Open-DawnstrikeProtectedDirectoryHandle `
            -Path $operationRoot -Label 'Core-universe bootstrap operation log root'
        Assert-DawnstrikeUniverseLogDirectoryAcl -Path $operationRoot
        $null = Assert-DawnstrikeProtectedPathNoReparse `
            -Path $operationRoot -Label 'Core-universe bootstrap operation log root' `
            -ExpectedType Directory
        if (@(Get-ChildItem -LiteralPath $operationRoot -Force -ErrorAction Stop).Count -ne 0) {
            throw 'Core-universe bootstrap operation log root was not empty after protection.'
        }
        return [pscustomobject]@{
            path = $operationRoot
            state_handle = $stateHandle
            locks_boundary = $locksBoundary
            logs_handle = $logsHandle
            operation_handle = $operationHandle
        }
    }
    catch {
        if ($null -ne $operationHandle) { $operationHandle.Dispose() }
        if ($null -ne $logsHandle) { $logsHandle.Dispose() }
        try {
            if ($null -ne $locksBoundary) {
                Close-DawnstrikeProtectedWriteDirectoryBoundary -Boundary $locksBoundary
            }
        }
        finally {
            $stateHandle.Dispose()
        }
        throw
    }
}

function Close-DawnstrikeUniverseBootstrapLogBoundary {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Boundary)

    try {
        foreach ($name in @('operation_handle', 'logs_handle')) {
            $handle = $Boundary.$name
            if ($null -ne $handle) { $handle.Dispose() }
        }
        if ($null -ne $Boundary.locks_boundary) {
            Close-DawnstrikeProtectedWriteDirectoryBoundary -Boundary $Boundary.locks_boundary
        }
    }
    finally {
        if ($null -ne $Boundary.state_handle) { $Boundary.state_handle.Dispose() }
    }
}

function Assert-DawnstrikeUniverseBootstrapBoundarySnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RequestedMarketDate,
        [Parameter(Mandatory = $true)][DateTime]$Now,
        [Parameter(Mandatory = $true)][object[]]$CanonicalTasks,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][object[]]$CaptureTasks
    )

    $parsedDate = ConvertTo-DawnstrikeExactMarketDate -Value $RequestedMarketDate
    if ($parsedDate -ne $Now.Date) {
        throw 'Core-universe bootstrap is allowed only for the host current date.'
    }

    $expectedNames = @(
        'Dawnstrike AlphaOps Morning',
        'Dawnstrike AlphaOps Monitor 5m',
        'Dawnstrike AlphaOps EOD Full Report',
        'Dawnstrike AlphaOps V6 Weekly Training',
        'Dawnstrike 10of10 Daily Finalize'
    )
    if (@($CanonicalTasks).Count -ne $expectedNames.Count) {
        throw 'Core-universe bootstrap requires exactly five canonical task snapshots.'
    }
    $byName = @{}
    foreach ($task in @($CanonicalTasks)) {
        $name = [string]$task.name
        if ($name -cnotin $expectedNames -or $byName.ContainsKey($name)) {
            throw 'Core-universe bootstrap found an unknown or duplicate canonical task.'
        }
        if ([string]$task.state -cne 'Ready') {
            throw "Core-universe bootstrap requires a unique Ready canonical task: $name"
        }
        $nextRun = [DateTime]$task.next_run_time
        if ($nextRun -ne [DateTime]::MinValue -and $nextRun.Date -eq $Now.Date) {
            throw "Core-universe bootstrap is blocked by a pending same-day canonical trigger: $name"
        }
        $byName[$name] = $task
    }
    foreach ($name in $expectedNames) {
        if (-not $byName.ContainsKey($name)) {
            throw "Core-universe bootstrap is missing canonical task: $name"
        }
    }

    $captures = @($CaptureTasks)
    if ($captures.Count -gt 1) {
        throw 'Core-universe bootstrap requires the optional delayed-SIP task to be absent or quiescent.'
    }
    if ($captures.Count -eq 1 -and [string]$captures[0].state -cnotin @('Ready', 'Disabled')) {
        throw 'Core-universe bootstrap requires the optional delayed-SIP task to be absent or quiescent.'
    }

    $eod = $byName['Dawnstrike AlphaOps EOD Full Report']
    $finalizer = $byName['Dawnstrike 10of10 Daily Finalize']
    $weekly = $byName['Dawnstrike AlphaOps V6 Weekly Training']
    $eodLast = [DateTime]$eod.last_run_time
    $finalizerLast = [DateTime]$finalizer.last_run_time
    # Host mutations are deliberately post-Finalizer only.  Task Scheduler and
    # source-admission RPCs occur before the bounded child process and cannot be
    # assigned an honest pre-Morning deadline; admitting that window could make
    # the one-shot Morning task observe a release lock and exit.
    $afterFinalizer = (
        $eodLast.Date -eq $Now.Date -and
        $finalizerLast.Date -eq $Now.Date -and
        $eodLast -le $Now -and
        $eodLast -le $finalizerLast -and
        $finalizerLast -le $Now
    )
    if (-not $afterFinalizer) {
        throw 'Core-universe bootstrap requires the post-finalizer quiescent window.'
    }
    if ($Now.DayOfWeek -eq [DayOfWeek]::Monday) {
        $weeklyNext = [DateTime]$weekly.next_run_time
        $weeklyLast = [DateTime]$weekly.last_run_time
        if (
            $weeklyNext.Date -eq $Now.Date -or
            $weeklyLast.Date -ne $Now.Date -or
            $weeklyLast -gt $Now
        ) {
            throw 'Monday core-universe bootstrap requires the same-day Weekly task to finish first.'
        }
    }
    return $true
}

function Assert-DawnstrikeExactProductionAliases {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Actual,
        [Parameter(Mandatory = $true)][string[]]$Expected
    )

    $actualJson = ConvertTo-Json @($Actual | ForEach-Object { [string]$_ }) -Compress
    $expectedJson = ConvertTo-Json @($Expected) -Compress
    if ($actualJson -cne $expectedJson) {
        throw 'Vercel recovery result aliases do not match the governed production aliases.'
    }
}

function Assert-DawnstrikeVercelRecoveryResult {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Result,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][string]$ExpectedMarketDate,
        [Parameter(Mandatory = $true)][string]$ExpectedProjectId,
        [Parameter(Mandatory = $true)][string]$ExpectedProjectName,
        [Parameter(Mandatory = $true)][string]$ExpectedProviderScope,
        [Parameter(Mandatory = $true)][string[]]$ExpectedAliases
    )

    $null = ConvertTo-DawnstrikeExactMarketDate -Value $ExpectedMarketDate
    if ($Result.research_only -ne $true -or $Result.broker_execution_enabled -ne $false) {
        throw 'Vercel publication recovery result crossed the research-only boundary.'
    }
    if (
        [string]$Result.project_id -cne $ExpectedProjectId -or
        [string]$Result.provider_scope -cne $ExpectedProviderScope
    ) {
        throw 'Vercel publication recovery result target identity is invalid.'
    }
    Assert-DawnstrikeExactProductionAliases `
        -Actual @($Result.production_aliases) -Expected $ExpectedAliases

    $schema = [string]$Result.schema_version
    $status = [string]$Result.status
    if ($schema -ceq 'dawnstrike.vercel_publication_recovery.v1') {
        if ([string]$Result.project_name -cne $ExpectedProjectName) {
            throw 'Vercel publication recovery result project name is invalid.'
        }
        $null = ConvertTo-DawnstrikeExactMarketDate -Value ([string]$Result.market_date)
        if (
            $status -notin @(
                'NO_NONTERMINAL_CURRENT_OPERATION',
                'ARCHIVED_COMPENSATED',
                'COMPENSATED'
            ) -or
            [string]$Result.market_date -cne $ExpectedMarketDate
        ) {
            throw 'Vercel publication recovery result identity is invalid.'
        }
        if ($status -in @('ARCHIVED_COMPENSATED', 'COMPENSATED') -and
            [string]$Result.archived_journal_sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw 'Vercel publication recovery archive identity is invalid.'
        }
    }
    elseif ($schema -ceq 'dawnstrike.daily_deployment.v1') {
        $null = ConvertTo-DawnstrikeExactMarketDate -Value ([string]$Result.market_date)
        $null = ConvertTo-DawnstrikeExactMarketDate -Value ([string]$Result.expected_market_date)
        if (
            $status -cne 'PRODUCTION_VERIFIED' -or
            [string]$Result.source_sha -cne $ExpectedSha -or
            [string]$Result.market_date -cne $ExpectedMarketDate -or
            [string]$Result.expected_market_date -cne $ExpectedMarketDate -or
            $Result.promoted -ne $true -or
            [int]$Result.readiness_http_status -ne 200
        ) {
            throw 'Vercel completed publication recovery result identity is invalid.'
        }
    }
    else {
        throw 'Vercel publication recovery returned an unsupported schema.'
    }
    return $true
}
