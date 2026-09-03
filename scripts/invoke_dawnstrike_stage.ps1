[CmdletBinding()]
param()

if (
    [int]$PSVersionTable.PSVersion.Major -lt 5 -or
    [string]$PSVersionTable.PSEdition -ne "Desktop"
) {
    throw "Dawnstrike stage locking requires Windows PowerShell 5.1 or later (Desktop edition)."
}

function Enter-DawnstrikeLockOperationMutex {
    [CmdletBinding()]
    param([int]$TimeoutMilliseconds = 60000)

    $mutex = New-Object System.Threading.Mutex($false, "Global\Dawnstrike.LockHandshake.v1")
    try {
        if (-not $mutex.WaitOne($TimeoutMilliseconds)) {
            $mutex.Dispose()
            throw "Dawnstrike lock handshake mutex could not be acquired."
        }
        return $mutex
    }
    catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-DawnstrikeLockOperationMutex {
    [CmdletBinding()]
    param([AllowNull()][object]$Mutex)

    if ($null -eq $Mutex) { return }
    try { $Mutex.ReleaseMutex() } catch { }
    $Mutex.Dispose()
}

function Assert-DawnstrikeLockNoReparseComponents {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label contains a reparse-point path component."
    }
    $parentPath = if ($null -ne $item -and $item.PSIsContainer) {
        $item.FullName
    }
    else {
        Split-Path -Parent $full
    }
    $current = [System.IO.DirectoryInfo]::new($parentPath)
    while ($null -ne $current) {
        $currentItem = Get-Item -LiteralPath $current.FullName -Force -ErrorAction SilentlyContinue
        if ($null -ne $currentItem -and ($currentItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point path component."
        }
        if ([string]::Equals(
            $current.FullName.TrimEnd('\'),
            $current.Root.FullName.TrimEnd('\'),
            [System.StringComparison]::OrdinalIgnoreCase
        )) { break }
        $current = $current.Parent
    }
}

function Get-DawnstrikeLockBytesSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Read-DawnstrikeLockFileBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    # A retained creator requests WRITE and DELETE access, so a cooperating
    # read must share both.  The creator itself shares READ only and therefore
    # still denies every outside write, unlink, replace, and rename attempt.
    $share = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        $share
    )
    try {
        if ($stream.Length -gt 1048576) {
            throw "$Label exceeds the lock byte ceiling."
        }
        $bytes = [byte[]]::new([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $count = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($count -le 0) { throw "$Label ended before its exact bytes were read." }
            $offset += $count
        }
        return $bytes
    }
    finally { $stream.Dispose() }
}

function Get-DawnstrikeLockSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowMissing
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeLockNoReparseComponents $full $Label
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) {
        if ($AllowMissing) {
            return [pscustomobject]@{
                path = $full
                present = $false
                bytes_sha256 = $null
                byte_count = 0
                lock_token = $null
                payload = $null
            }
        }
        throw "$Label is missing."
    }
    if (
        $item.PSIsContainer -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) { throw "$Label is not a regular non-reparse file." }
    $first = Read-DawnstrikeLockFileBytes -Path $full -Label $Label
    $firstHash = Get-DawnstrikeLockBytesSha256 $first
    Assert-DawnstrikeLockNoReparseComponents $full $Label
    $second = Read-DawnstrikeLockFileBytes -Path $full -Label $Label
    $secondHash = Get-DawnstrikeLockBytesSha256 $second
    if ($firstHash -ne $secondHash -or $first.Length -ne $second.Length) {
        throw "$Label changed while taking its same-byte snapshot."
    }
    try {
        $payload = [System.Text.Encoding]::UTF8.GetString($first) | ConvertFrom-Json
    }
    catch { throw "$Label is not valid JSON." }
    if (
        $null -eq $payload -or
        [string]::IsNullOrWhiteSpace([string]$payload.lock_token) -or
        [string]$payload.research_only -ne 'True' -or
        [string]$payload.broker_execution_enabled -ne 'False'
    ) { throw "$Label does not carry the exact safety identity." }
    return [pscustomobject]@{
        path = $full
        present = $true
        bytes_sha256 = $firstHash
        byte_count = $first.Length
        lock_token = [string]$payload.lock_token
        payload = $payload
    }
}

function Remove-DawnstrikeOwnedLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock, [Parameter(Mandatory = $true)][string]$Label)

    if (-not [bool]$Lock.acquired) { return }
    $retainedProperty = $Lock.PSObject.Properties['retained_handle']
    if ($null -ne $retainedProperty -and $null -ne $retainedProperty.Value) {
        $null = Confirm-DawnstrikeRetainedDailyRunLock -Lock $Lock
        $handle = [System.IO.FileStream]$retainedProperty.Value
        [Dawnstrike.Locking.DailyLockNative]::MarkDelete($handle.SafeFileHandle)
        $handle.Dispose()
        $Lock.retained_handle = $null
        $afterRetained = Get-DawnstrikeLockSnapshot `
            -Path ([string]$Lock.lock_path) -Label $Label -AllowMissing
        if ($afterRetained.present) {
            throw "$Label path was repopulated after its exact retained handle was released."
        }
        return
    }
    $current = Get-DawnstrikeLockSnapshot -Path ([string]$Lock.lock_path) -Label $Label
    if (
        -not $current.present -or
        [string]$current.lock_token -ne [string]$Lock.lock_token -or
        [string]$current.bytes_sha256 -ne [string]$Lock.bytes_sha256
    ) { throw "$Label ownership changed; refusing to remove it." }
    Remove-Item -LiteralPath ([string]$Lock.lock_path) -Force -ErrorAction Stop
    $after = Get-DawnstrikeLockSnapshot -Path ([string]$Lock.lock_path) -Label $Label -AllowMissing
    if ($after.present) { throw "$Label could not be removed after ownership was proven." }
}

function Move-DawnstrikeDeadDailyLockToArchive {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Snapshot,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not [bool]$Snapshot.present) {
        throw "$Label cannot be archived because its exact snapshot is absent."
    }
    $sourcePath = [System.IO.Path]::GetFullPath([string]$Snapshot.path)
    $sourceHash = [string]$Snapshot.bytes_sha256
    if ($sourceHash -notmatch '^[0-9a-f]{64}$') {
        throw "$Label does not carry an exact SHA-256 identity."
    }
    $archivePath = "$sourcePath.stale-dead-$sourceHash"
    Assert-DawnstrikeLockNoReparseComponents $sourcePath $Label
    Assert-DawnstrikeLockNoReparseComponents $archivePath "$Label archive"
    if (Test-Path -LiteralPath $archivePath) {
        throw "$Label deterministic stale archive already exists; refusing an ambiguous overwrite."
    }
    $handle = Open-DawnstrikeRetainedExistingDailyRunLock -Path $sourcePath -Label $Label
    try {
        $heldLock = [pscustomobject]@{
            acquired = $true
            lock_path = $sourcePath
            lock_token = [string]$Snapshot.lock_token
            bytes_sha256 = $sourceHash
            retained = $true
            retained_handle = $handle
        }
        $null = Confirm-DawnstrikeRetainedDailyRunLock -Lock $heldLock
        [Dawnstrike.Locking.DailyLockNative]::RenameNoReplace(
            $handle.SafeFileHandle,
            $archivePath
        )
        $heldLock.lock_path = $archivePath
        $null = Confirm-DawnstrikeRetainedDailyRunLock -Lock $heldLock
        $afterSource = Get-DawnstrikeLockSnapshot `
            -Path $sourcePath -Label $Label -AllowMissing
        if ($afterSource.present) {
            throw "$Label archival did not remove the exact original lock path."
        }
        return [pscustomobject]@{
            path = $archivePath
            bytes_sha256 = $sourceHash
            lock_token = [string]$Snapshot.lock_token
        }
    }
    finally {
        $handle.Dispose()
    }
}

function Resolve-DawnstrikeForeignDailyLocksCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LockRoot,
        [Parameter(Mandatory = $true)][string]$CurrentLockPath
    )

    $currentFull = [System.IO.Path]::GetFullPath($CurrentLockPath)
    $records = @()
    $foreign = @(
        Get-ChildItem -LiteralPath $LockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
            Where-Object {
                [System.IO.Path]::GetFullPath($_.FullName) -ne $currentFull
            } |
            Sort-Object -Property FullName
    )
    foreach ($item in $foreign) {
        if ([string]$item.Name -notmatch '^dawnstrike-daily-(\d{4}-\d{2}-\d{2})\.lock$') {
            throw "Counterpart daily run lock has a noncanonical filename."
        }
        $pathDate = [string]$Matches[1]
        $snapshot = Get-DawnstrikeLockSnapshot -Path $item.FullName -Label "Counterpart daily run lock"
        if (
            [string]$snapshot.payload.market_date -ne $pathDate -or
            [string]::IsNullOrWhiteSpace([string]$snapshot.payload.owner)
        ) {
            throw "Counterpart daily run lock is not bound to its canonical date and owner."
        }
        $ownerActive = Test-DawnstrikeLockOwnerActive -LockPath $item.FullName
        $verified = Get-DawnstrikeLockSnapshot -Path $item.FullName -Label "Counterpart daily run lock"
        if (
            [string]$verified.bytes_sha256 -ne [string]$snapshot.bytes_sha256 -or
            [string]$verified.lock_token -ne [string]$snapshot.lock_token
        ) {
            throw "Counterpart daily run lock changed while its owner identity was checked."
        }
        $records += [pscustomobject]@{
            snapshot = $verified
            owner_active = [bool]$ownerActive
        }
    }
    $active = @($records | Where-Object { $_.owner_active })
    if ($active.Count -gt 0) {
        return [pscustomobject]@{
            blocked = $true
            active_count = $active.Count
            archived_count = 0
        }
    }
    $archived = 0
    foreach ($record in $records) {
        $null = Move-DawnstrikeDeadDailyLockToArchive `
            -Snapshot $record.snapshot `
            -Label "Counterpart daily run lock"
        $archived += 1
    }
    return [pscustomobject]@{
        blocked = $false
        active_count = 0
        archived_count = $archived
    }
}

function Initialize-DawnstrikeDailyLockNative {
    [CmdletBinding()]
    param()

    if ('Dawnstrike.Locking.DailyLockNative' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Locking {
    [StructLayout(LayoutKind.Sequential)]
    public struct FileDispositionInfo {
        [MarshalAs(UnmanagedType.Bool)]
        public bool DeleteFile;
    }

    public static class DailyLockNative {
        private const UInt32 GenericRead = 0x80000000;
        private const UInt32 GenericWrite = 0x40000000;
        private const UInt32 DeleteAccess = 0x00010000;
        private const UInt32 FileShareRead = 0x00000001;
        private const UInt32 CreateNew = 1;
        private const UInt32 OpenExisting = 3;
        private const UInt32 FileAttributeNormal = 0x00000080;
        private const UInt32 FileFlagOpenReparsePoint = 0x00200000;
        private const int FileRenameInformation = 3;
        private const int FileDispositionInformation = 4;

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFileW(
            string path,
            UInt32 desiredAccess,
            UInt32 shareMode,
            IntPtr securityAttributes,
            UInt32 creationDisposition,
            UInt32 flagsAndAttributes,
            IntPtr templateFile
        );

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(
            SafeFileHandle file,
            int fileInformationClass,
            ref FileDispositionInfo fileInformation,
            UInt32 bufferSize
        );

        [DllImport(
            "kernel32.dll",
            EntryPoint = "SetFileInformationByHandle",
            SetLastError = true
        )]
        private static extern bool SetFileInformationByHandleRaw(
            SafeFileHandle file,
            int fileInformationClass,
            IntPtr fileInformation,
            UInt32 bufferSize
        );

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern UInt32 GetFinalPathNameByHandleW(
            SafeFileHandle file,
            StringBuilder path,
            UInt32 pathLength,
            UInt32 flags
        );

        public static SafeFileHandle CreateNewRetained(string path) {
            // DELETE access lets explicit Exit mark this exact open file for
            // deletion. There is deliberately no delete-on-close flag: a hard
            // process crash must leave durable dead-owner recovery evidence.
            SafeFileHandle handle = CreateFileW(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                IntPtr.Zero,
                CreateNew,
                FileAttributeNormal,
                IntPtr.Zero
            );
            if (handle == null || handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                if (handle != null) handle.Dispose();
                throw new Win32Exception(error, "Retained daily lock creation failed");
            }
            return handle;
        }

        public static SafeFileHandle OpenExistingRetained(string path) {
            SafeFileHandle handle = CreateFileW(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                IntPtr.Zero,
                OpenExisting,
                FileAttributeNormal | FileFlagOpenReparsePoint,
                IntPtr.Zero
            );
            if (handle == null || handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                if (handle != null) handle.Dispose();
                throw new Win32Exception(error, "Retained daily lock open failed");
            }
            return handle;
        }

        public static void MarkDelete(SafeFileHandle handle) {
            FileDispositionInfo disposition = new FileDispositionInfo();
            disposition.DeleteFile = true;
            UInt32 size = (UInt32)Marshal.SizeOf(typeof(FileDispositionInfo));
            if (!SetFileInformationByHandle(
                handle,
                FileDispositionInformation,
                ref disposition,
                size
            )) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Retained daily lock exact deletion failed"
                );
            }
        }

        public static void RenameNoReplace(SafeFileHandle handle, string destination) {
            byte[] name = Encoding.Unicode.GetBytes(destination);
            int rootOffset = IntPtr.Size;
            int lengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = lengthOffset + 4;
            int size = nameOffset + name.Length + 2;
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                for (int index = 0; index < size; index++) Marshal.WriteByte(buffer, index, 0);
                Marshal.WriteByte(buffer, 0, 0); // ReplaceIfExists = false
                Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
                Marshal.WriteInt32(buffer, lengthOffset, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
                if (!SetFileInformationByHandleRaw(
                    handle,
                    FileRenameInformation,
                    buffer,
                    (UInt32)size
                )) {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Retained daily lock exact no-replace rename failed"
                    );
                }
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public static string GetFinalPath(SafeFileHandle handle) {
            StringBuilder path = new StringBuilder(32768);
            UInt32 length = GetFinalPathNameByHandleW(
                handle,
                path,
                (UInt32)path.Capacity,
                0
            );
            if (length == 0 || length >= path.Capacity) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Retained daily lock path lookup failed"
                );
            }
            string value = path.ToString();
            if (value.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)) {
                return @"\\" + value.Substring(8);
            }
            if (value.StartsWith(@"\\?\", StringComparison.OrdinalIgnoreCase)) {
                return value.Substring(4);
            }
            return value;
        }
    }
}
'@
}

function Open-DawnstrikeRetainedDailyRunLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeLockNoReparseComponents $full $Label
    Initialize-DawnstrikeDailyLockNative
    $nativeHandle = [Dawnstrike.Locking.DailyLockNative]::CreateNewRetained($full)
    try {
        return [System.IO.FileStream]::new(
            $nativeHandle,
            [System.IO.FileAccess]::ReadWrite,
            4096,
            $false
        )
    }
    catch {
        $nativeHandle.Dispose()
        throw
    }
}

function Open-DawnstrikeRetainedExistingDailyRunLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeLockNoReparseComponents $full $Label
    Initialize-DawnstrikeDailyLockNative
    $nativeHandle = [Dawnstrike.Locking.DailyLockNative]::OpenExistingRetained($full)
    try {
        $stream = [System.IO.FileStream]::new(
            $nativeHandle,
            [System.IO.FileAccess]::ReadWrite,
            4096,
            $false
        )
        $actual = [System.IO.Path]::GetFullPath(
            [Dawnstrike.Locking.DailyLockNative]::GetFinalPath($stream.SafeFileHandle)
        )
        if (-not [string]::Equals(
            $full,
            $actual,
            [System.StringComparison]::OrdinalIgnoreCase
        )) { throw "$Label retained handle is bound to a different path." }
        return $stream
    }
    catch {
        if ($null -ne $stream) { $stream.Dispose() }
        else { $nativeHandle.Dispose() }
        throw
    }
}

function Get-DawnstrikeRetainedDailyRunLockSnapshot {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    $property = $Lock.PSObject.Properties['retained_handle']
    if ($null -eq $property -or $null -eq $property.Value) {
        throw 'Daily run lock has no retained handle.'
    }
    $handle = [System.IO.FileStream]$property.Value
    if (-not $handle.CanRead -or -not $handle.CanWrite -or $handle.SafeFileHandle.IsClosed) {
        throw 'Daily run lock retained handle is no longer valid.'
    }
    $expectedPath = [System.IO.Path]::GetFullPath([string]$Lock.lock_path)
    $handlePath = [System.IO.Path]::GetFullPath(
        [Dawnstrike.Locking.DailyLockNative]::GetFinalPath($handle.SafeFileHandle)
    )
    if (-not [string]::Equals(
        $expectedPath,
        $handlePath,
        [System.StringComparison]::OrdinalIgnoreCase
    )) { throw 'Daily run lock retained handle is bound to a different path.' }
    Assert-DawnstrikeLockNoReparseComponents $expectedPath 'Daily run lock'
    $handle.Flush($true)
    if ($handle.Length -gt 1048576) { throw 'Daily run lock exceeds the lock byte ceiling.' }
    $position = $handle.Position
    try {
        $handle.Position = 0
        $bytes = [byte[]]::new([int]$handle.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $count = $handle.Read($bytes, $offset, $bytes.Length - $offset)
            if ($count -le 0) { throw 'Daily run lock retained handle returned incomplete bytes.' }
            $offset += $count
        }
    }
    finally { $handle.Position = $position }
    $bytesSha256 = Get-DawnstrikeLockBytesSha256 $bytes
    try { $payload = [System.Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json }
    catch { throw 'Daily run lock retained bytes are not valid JSON.' }
    if (
        [string]$payload.lock_token -ne [string]$Lock.lock_token -or
        $bytesSha256 -ne [string]$Lock.bytes_sha256 -or
        [string]$payload.research_only -ne 'True' -or
        [string]$payload.broker_execution_enabled -ne 'False'
    ) { throw 'Daily run lock retained bytes do not match its exact ownership identity.' }
    return [pscustomobject]@{
        path = $handlePath
        bytes_sha256 = $bytesSha256
        lock_token = [string]$payload.lock_token
        payload = $payload
    }
}

function Confirm-DawnstrikeRetainedDailyRunLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    $retained = Get-DawnstrikeRetainedDailyRunLockSnapshot -Lock $Lock
    $pathSnapshot = Get-DawnstrikeLockSnapshot `
        -Path ([string]$Lock.lock_path) -Label 'Daily run lock'
    if (
        [string]$retained.path -ne [string]$pathSnapshot.path -or
        [string]$retained.lock_token -ne [string]$pathSnapshot.lock_token -or
        [string]$retained.bytes_sha256 -ne [string]$pathSnapshot.bytes_sha256
    ) { throw 'Daily run lock path is not bound to its retained handle.' }
    return $true
}

function Enter-DawnstrikeDailyRunLockCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$Owner,
        [int]$StaleAfterMinutes = 240,
        [switch]$RetainHandle
    )

    if ($StaleAfterMinutes -le 0) {
        throw "StaleAfterMinutes must be positive."
    }
    $lockRoot = Join-Path $StateRoot "locks"
    Assert-DawnstrikeLockNoReparseComponents $lockRoot "Daily lock root"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    Assert-DawnstrikeLockNoReparseComponents $lockRoot "Daily lock root"
    $activationLockPath = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    $lockPath = Join-Path $lockRoot ("dawnstrike-daily-" + $MarketDate + ".lock")
    $activationBefore = Get-DawnstrikeLockSnapshot -Path $activationLockPath -Label "Runtime activation lock" -AllowMissing
    if ($Owner -notin @("runtime_activation", "runtime_rollback") -and $activationBefore.present) {
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "runtime_activation_lock"
            age_minutes = $null
        }
    }
    $existingDaily = Get-DawnstrikeLockSnapshot -Path $lockPath -Label "Daily run lock" -AllowMissing
    if ($existingDaily.present) {
        $age = ((Get-Date).ToUniversalTime() - (Get-Item -LiteralPath $lockPath).LastWriteTimeUtc).TotalMinutes
        $ownerActive = Test-DawnstrikeLockOwnerActive -LockPath $lockPath
        # Wall-clock age is diagnostic only.  A long-running owner must never
        # be evicted merely because it crossed StaleAfterMinutes; doing so
        # permits two daily runs to mutate the same research ledger.  Recovery
        # is allowed only when PID/start-time proof says the owner is dead.
        if (-not $ownerActive) {
            $again = Get-DawnstrikeLockSnapshot -Path $lockPath -Label "Daily run lock"
            if ($again.bytes_sha256 -ne $existingDaily.bytes_sha256) {
                return [pscustomobject]@{
                    acquired = $false
                    lock_path = $lockPath
                    reason = "concurrent_lock_change"
                    age_minutes = [math]::Round($age, 2)
                }
            }
            $null = Move-DawnstrikeDeadDailyLockToArchive `
                -Snapshot $again `
                -Label "Daily run lock"
        } else {
            return [pscustomobject]@{
                acquired = $false
                lock_path = $lockPath
                reason = "active_lock"
                age_minutes = [math]::Round($age, 2)
            }
        }
    }
    $foreignBefore = Resolve-DawnstrikeForeignDailyLocksCore `
        -LockRoot $lockRoot `
        -CurrentLockPath $lockPath
    if ($foreignBefore.blocked) {
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "active_foreign_daily_lock"
            age_minutes = $null
        }
    }
    $lockToken = [guid]::NewGuid().ToString("N")
    $payload = [ordered]@{
        schema_version = "dawnstrike.daily_run_lock.v3"
        market_date = $MarketDate
        owner = $Owner
        acquired_at = [DateTime]::UtcNow.ToString("o")
        process_id = $PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
        lock_token = $lockToken
        research_only = $true
        broker_execution_enabled = $false
    } | ConvertTo-Json -Depth 3
    $handle = $null
    try {
        if ($RetainHandle) {
            $handle = Open-DawnstrikeRetainedDailyRunLock `
                -Path $lockPath -Label 'Daily run lock'
        }
        else {
            $handle = [System.IO.File]::Open(
                $lockPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
        }
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $handle.Write($bytes, 0, $bytes.Length)
        $handle.Flush($true)
    } catch [System.ComponentModel.Win32Exception] {
        if ($null -ne $handle) { $handle.Dispose() }
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "concurrent_lock_acquisition"
            age_minutes = $null
        }
    }
    catch [System.IO.IOException] {
        if ($null -ne $handle) { $handle.Dispose() }
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "concurrent_lock_acquisition"
            age_minutes = $null
        }
    }
    catch {
        if ($null -ne $handle) { $handle.Dispose() }
        throw
    }
    finally {
        if (-not $RetainHandle -and $null -ne $handle) {
            $handle.Dispose()
            $handle = $null
        }
    }
    Assert-DawnstrikeLockNoReparseComponents $lockPath "Daily run lock"
    $own = Get-DawnstrikeLockSnapshot -Path $lockPath -Label "Daily run lock"
    if (-not $own.present -or $own.lock_token -ne $lockToken) {
        if ($RetainHandle -and $null -ne $handle) { $handle.Dispose() }
        throw "Daily run lock could not be read back with its own token."
    }
    $ownedLockFields = [ordered]@{
        acquired = $true
        lock_path = $lockPath
        reason = "acquired"
        age_minutes = 0
        lock_token = $lockToken
        bytes_sha256 = $own.bytes_sha256
    }
    if ($RetainHandle) {
        $ownedLockFields.retained = $true
        $ownedLockFields.retained_handle = $handle
    }
    $ownedLock = [pscustomobject]$ownedLockFields
    if ($RetainHandle) {
        try { $null = Confirm-DawnstrikeRetainedDailyRunLock -Lock $ownedLock }
        catch {
            $handle.Dispose()
            $ownedLock.retained_handle = $null
            throw
        }
    }
    # Cooperative two-lock handshake: the pre-create check is only a
    # fast-path.  The post-create same-byte snapshot is authoritative.  If a
    # runtime activation/rollback lock appeared in the race window, release
    # only this unchanged daily lock and fail before any stage work begins.
    $activationAfter = Get-DawnstrikeLockSnapshot -Path $activationLockPath -Label "Runtime activation lock" -AllowMissing
    if ($activationAfter.present -and $Owner -notin @("runtime_activation", "runtime_rollback")) {
        Remove-DawnstrikeOwnedLock -Lock $ownedLock -Label "Daily run lock"
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "runtime_activation_lock_race"
            age_minutes = $null
        }
    }
    if ($Owner -in @("runtime_activation", "runtime_rollback") -and -not $activationAfter.present) {
        Remove-DawnstrikeOwnedLock -Lock $ownedLock -Label "Daily run lock"
        throw "Runtime activation lock disappeared during the daily lock handshake."
    }
    try {
        $foreignAfter = Resolve-DawnstrikeForeignDailyLocksCore `
            -LockRoot $lockRoot `
            -CurrentLockPath $lockPath
    }
    catch {
        Remove-DawnstrikeOwnedLock -Lock $ownedLock -Label "Daily run lock"
        throw
    }
    if ($foreignAfter.blocked) {
        Remove-DawnstrikeOwnedLock -Lock $ownedLock -Label "Daily run lock"
        return [pscustomobject]@{
            acquired = $false
            lock_path = $lockPath
            reason = "active_foreign_daily_lock_race"
            age_minutes = $null
        }
    }
    return $ownedLock
}

function Enter-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$Owner,
        [int]$StaleAfterMinutes = 240,
        [switch]$RetainHandle
    )

    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        return Enter-DawnstrikeDailyRunLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Confirm-DawnstrikeActivationDailyLockHandshakeCore {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][object]$ActivationLock,
        [Parameter(Mandatory = $true)][object]$DailyLock
    )

    if (-not $ActivationLock.acquired -or -not $DailyLock.acquired) {
        throw "Activation/daily lock handshake requires both owned locks."
    }
    $retainedProperty = $DailyLock.PSObject.Properties['retained_handle']
    if ($null -ne $retainedProperty -and $null -ne $retainedProperty.Value) {
        $null = Confirm-DawnstrikeRetainedDailyRunLock -Lock $DailyLock
    }
    $lockRoot = Join-Path $StateRoot "locks"
    Assert-DawnstrikeLockNoReparseComponents $lockRoot "Activation lock root"
    $activation = Get-DawnstrikeLockSnapshot `
        -Path ([string]$ActivationLock.path) `
        -Label "Runtime activation lock"
    if (
        $activation.lock_token -ne [string]$ActivationLock.token -or
        $activation.bytes_sha256 -ne [string]$ActivationLock.bytes_sha256
    ) {
        throw "Runtime activation lock changed during the lock handshake."
    }
    $daily = Get-DawnstrikeLockSnapshot `
        -Path ([string]$DailyLock.lock_path) `
        -Label "Daily run lock"
    if (
        $daily.lock_token -ne [string]$DailyLock.lock_token -or
        $daily.bytes_sha256 -ne [string]$DailyLock.bytes_sha256
    ) {
        throw "Daily run lock changed during the lock handshake."
    }
    $foreign = Resolve-DawnstrikeForeignDailyLocksCore `
        -LockRoot $lockRoot `
        -CurrentLockPath ([string]$DailyLock.lock_path)
    if ($foreign.blocked) {
        Remove-DawnstrikeOwnedLock -Lock $DailyLock -Label "Daily run lock"
        throw "An active counterpart daily run lock appeared during the activation handshake."
    }
    return $true
}

function Confirm-DawnstrikeActivationDailyLockHandshake {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][object]$ActivationLock,
        [Parameter(Mandatory = $true)][object]$DailyLock
    )

    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        return Confirm-DawnstrikeActivationDailyLockHandshakeCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Test-DawnstrikeLockOwnerActive {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$LockPath)

    try {
        $snapshot = Get-DawnstrikeLockSnapshot -Path $LockPath -Label "Lock owner record"
        $payload = $snapshot.payload
        $processId = 0
        if (-not [int]::TryParse([string]$payload.process_id, [ref]$processId) -or $processId -le 0) {
            throw "Lock owner process_id is invalid."
        }
        $schemaVersion = [string]$payload.schema_version
        $recordedIdentity = [DateTimeOffset]::MinValue
        if ($schemaVersion -eq "dawnstrike.daily_run_lock.v3") {
            $startedProperty = $payload.PSObject.Properties["process_started_at_utc"]
            if (
                $null -eq $startedProperty -or
                [string]::IsNullOrWhiteSpace([string]$startedProperty.Value) -or
                -not [DateTimeOffset]::TryParse(
                    [string]$startedProperty.Value,
                    [ref]$recordedIdentity
                )
            ) {
                throw "Lock owner process-start identity is invalid."
            }
            # A reused PID has a different creation time.  Compare exact
            # process start identity rather than mutable lock-file age.
        }
        elseif ($schemaVersion -eq "dawnstrike.daily_run_lock.v2") {
            # v2 locks predate the exact process-start field.  Retain the old
            # acquired_at relationship for compatibility, but require that
            # timestamp to be valid before deciding whether an owner is dead.
            if (-not [DateTimeOffset]::TryParse(
                [string]$payload.acquired_at,
                [ref]$recordedIdentity
            )) {
                throw "Legacy lock owner acquisition identity is invalid."
            }
        }
        else {
            throw "Daily run lock schema is unsupported for owner recovery."
        }
        try {
            $ownerProcess = [System.Diagnostics.Process]::GetProcessById($processId)
        }
        catch [System.ArgumentException] {
            return $false
        }
        $processStarted = [DateTimeOffset]$ownerProcess.StartTime.ToUniversalTime()
        $recordedIdentity = $recordedIdentity.ToUniversalTime()
        if ($schemaVersion -eq "dawnstrike.daily_run_lock.v3") {
            return $processStarted.UtcDateTime.Ticks -eq $recordedIdentity.UtcDateTime.Ticks
        }
        return $processStarted.UtcDateTime.Ticks -le $recordedIdentity.UtcDateTime.AddTicks(
            [TimeSpan]::TicksPerSecond * 5
        ).Ticks
    }
    catch {
        throw "Lock owner identity is ambiguous; refusing stale recovery. $($_.Exception.Message)"
    }
}

function Exit-DawnstrikeDailyRunLockCore {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    if (-not $Lock.acquired) { return }
    Remove-DawnstrikeOwnedLock -Lock $Lock -Label "Daily run lock"
}

function Exit-DawnstrikeDailyRunLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Lock)

    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        Exit-DawnstrikeDailyRunLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Write-DawnstrikeLockDenialReceipt {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$Owner,
        [Parameter(Mandatory = $true)][object]$Lock
    )

    try {
        $receiptRoot = Join-Path $StateRoot "receipts\lock-denials"
        New-Item -ItemType Directory -Path $receiptRoot -Force | Out-Null
        $recordedAt = [DateTimeOffset]::UtcNow
        $payload = [ordered]@{
            schema_version = "dawnstrike.lock_denial.v1"
            market_date = $MarketDate
            owner = $Owner
            reason = [string]$Lock.reason
            lock_path = [string]$Lock.lock_path
            age_minutes = $Lock.age_minutes
            recorded_at = $recordedAt.ToString("o")
            research_only = $true
            broker_execution_enabled = $false
        } | ConvertTo-Json -Depth 4
        $name = "$MarketDate-$Owner-$($recordedAt.ToString('yyyyMMddTHHmmssfffZ')).json"
        $path = Join-Path $receiptRoot $name
        $temporary = "$path.$([guid]::NewGuid().ToString('N')).tmp"
        [System.IO.File]::WriteAllText($temporary, $payload, [System.Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $path
        return $true
    }
    catch {
        return $false
    }
}
