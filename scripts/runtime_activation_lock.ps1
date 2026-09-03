# Shared, strict activation-lock implementation. Callers set operation and
# immutable source identity before acquisition; credentials are never accepted.
$script:DawnstrikeApprovedPythonPath='C:\Program Files\Dawnstrike\Python313\python.exe'
$script:DawnstrikeApprovedPythonSha256='ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1'
$script:DawnstrikeApprovedPythonSubject='CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US'
$script:DawnstrikeApprovedPythonThumbprint='9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48'
$script:DawnstrikeApprovedGitPath='C:\Program Files\Git\cmd\git.exe'
$script:DawnstrikeApprovedGitSha256='37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9'
$script:DawnstrikeApprovedGitSubject='CN=Johannes Schindelin, O=Johannes Schindelin, S=Nordrhein-Westfalen, C=DE'
$script:DawnstrikeApprovedGitThumbprint='3EB14A3AEF84B7153E139397F0A49E2FAC662B0E'

if (-not (Get-Command Invoke-DawnstrikeJobProcess -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot 'dawnstrike_job_process.ps1')
}

function Invoke-DawnstrikeRuntimeContractProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [Parameter(Mandatory=$true)][string]$Label,
        [Parameter(Mandatory=$true)][ValidateRange(1,1800)][int]$TimeoutSeconds
    )
    $result = Invoke-DawnstrikeJobProcess `
        -FilePath $PythonPath -ArgumentList $ArgumentList `
        -WorkingDirectory $PSScriptRoot -Label $Label `
        -TimeoutSeconds $TimeoutSeconds -OutputDrainTimeoutSeconds 5 `
        -EnvironmentOverrides @{ PYTHONDONTWRITEBYTECODE = '1' }
    if ([int]$result.ExitCode -ne 0) {
        throw "$Label failed with exit code $([int]$result.ExitCode)."
    }
    return [string]$result.Stdout
}

function Get-DawnstrikeApprovedLockInterpreter {
    Assert-DawnstrikeSharedLockNoReparse $script:DawnstrikeApprovedPythonPath 'Approved lock-contract interpreter'
    if(-not (Test-Path -LiteralPath $script:DawnstrikeApprovedPythonPath -PathType Leaf)){throw 'Approved lock-contract interpreter is missing.'}
    if((Get-DawnstrikeRuntimeLockHash $script:DawnstrikeApprovedPythonPath)-ne $script:DawnstrikeApprovedPythonSha256){throw 'Approved lock-contract interpreter hash changed.'}
    try{$certificate=[Security.Cryptography.X509Certificates.X509Certificate2]::new([Security.Cryptography.X509Certificates.X509Certificate]::CreateFromSignedFile($script:DawnstrikeApprovedPythonPath))}
    catch{throw 'Approved lock-contract interpreter has no readable Authenticode signer.'}
    if($certificate.Subject -ne $script:DawnstrikeApprovedPythonSubject -or $certificate.Thumbprint -ne $script:DawnstrikeApprovedPythonThumbprint){throw 'Approved lock-contract interpreter signer is invalid.'}
    return [pscustomobject]@{path=$script:DawnstrikeApprovedPythonPath;sha256=$script:DawnstrikeApprovedPythonSha256}
}

function Assert-DawnstrikeSharedLockNoReparse([string]$Path,[string]$Label){
    $full=[IO.Path]::GetFullPath($Path);$cursor=[IO.FileInfo]::new($full)
    if(-not $cursor.Exists){$cursor=[IO.DirectoryInfo]::new((Split-Path $full -Parent))}
    while($null-ne $cursor){
        if($cursor.Exists-and($cursor.Attributes-band [IO.FileAttributes]::ReparsePoint)){throw "$Label contains a reparse point."}
        $cursor=if($cursor-is [IO.FileInfo]){$cursor.Directory}else{$cursor.Parent}
    }
}
function Get-DawnstrikeSharedLockSha256Text([string]$Text) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-DawnstrikeRuntimeLockHash([string]$Path) {
    $sha=[Security.Cryptography.SHA256]::Create();$stream=$null
    $share=[IO.FileShare]::ReadWrite-bor[IO.FileShare]::Delete
    try{$stream=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share);return ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}
    finally{if($null-ne $stream){$stream.Dispose()};$sha.Dispose()}
}

function Initialize-DawnstrikeRuntimeLockNative {
    if ('Dawnstrike.Locking.RuntimeLockNative' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Locking {
    [StructLayout(LayoutKind.Sequential)]
    public struct RuntimeFileDispositionInfo {
        [MarshalAs(UnmanagedType.Bool)]
        public bool DeleteFile;
    }

    public static class RuntimeLockNative {
        private const UInt32 GenericRead = 0x80000000;
        private const UInt32 GenericWrite = 0x40000000;
        private const UInt32 DeleteAccess = 0x00010000;
        private const UInt32 FileReadAttributes = 0x00000080;
        private const UInt32 FileShareRead = 0x00000001;
        private const UInt32 FileShareWrite = 0x00000002;
        private const UInt32 CreateNew = 1;
        private const UInt32 OpenExisting = 3;
        private const UInt32 FileAttributeNormal = 0x00000080;
        private const UInt32 FileFlagBackupSemantics = 0x02000000;
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

        private static SafeFileHandle Open(
            string path,
            UInt32 access,
            UInt32 share,
            UInt32 disposition,
            UInt32 flags,
            string label
        ) {
            SafeFileHandle handle = CreateFileW(
                path, access, share, IntPtr.Zero, disposition, flags, IntPtr.Zero
            );
            if (handle == null || handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                if (handle != null) handle.Dispose();
                throw new Win32Exception(error, label);
            }
            return handle;
        }

        public static SafeFileHandle CreateNewRetained(string path) {
            return Open(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                CreateNew,
                FileAttributeNormal,
                "Retained runtime lock creation failed"
            );
        }

        public static SafeFileHandle OpenExistingRetained(string path) {
            return Open(
                path,
                GenericRead | GenericWrite | DeleteAccess,
                FileShareRead,
                OpenExisting,
                FileAttributeNormal | FileFlagOpenReparsePoint,
                "Retained runtime lock open failed"
            );
        }

        public static SafeFileHandle OpenDirectoryRetained(string path) {
            return Open(
                path,
                FileReadAttributes,
                FileShareRead | FileShareWrite,
                OpenExisting,
                FileFlagBackupSemantics | FileFlagOpenReparsePoint,
                "Retained runtime lock root open failed"
            );
        }

        public static void MarkDelete(SafeFileHandle handle) {
            RuntimeFileDispositionInfo disposition = new RuntimeFileDispositionInfo();
            disposition.DeleteFile = true;
            int size = Marshal.SizeOf(typeof(RuntimeFileDispositionInfo));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                Marshal.StructureToPtr(disposition, buffer, false);
                if (!SetFileInformationByHandle(
                    handle, FileDispositionInformation, buffer, (UInt32)size
                )) {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Retained runtime lock exact deletion failed"
                    );
                }
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public static void RenameNoReplace(SafeFileHandle handle, string destination) {
            byte[] name = Encoding.Unicode.GetBytes(destination);
            int rootOffset = IntPtr.Size;
            int lengthOffset = rootOffset + IntPtr.Size;
            int nameOffset = lengthOffset + 4;
            // FileNameLength excludes the terminator, but Windows still reads
            // the variable tail as a Unicode string on supported Desktop
            // builds.  Reserve and zero an explicit terminator so the rename
            // cannot acquire allocator-tail bytes in its directory entry.
            int size = nameOffset + name.Length + 2;
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try {
                for (int index = 0; index < size; index++) Marshal.WriteByte(buffer, index, 0);
                Marshal.WriteByte(buffer, 0, 0);
                Marshal.WriteIntPtr(buffer, rootOffset, IntPtr.Zero);
                Marshal.WriteInt32(buffer, lengthOffset, name.Length);
                Marshal.Copy(name, 0, IntPtr.Add(buffer, nameOffset), name.Length);
                if (!SetFileInformationByHandle(
                    handle, FileRenameInformation, buffer, (UInt32)size
                )) {
                    throw new Win32Exception(
                        Marshal.GetLastWin32Error(),
                        "Retained runtime lock exact rename failed"
                    );
                }
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        public static string GetFinalPath(SafeFileHandle handle) {
            StringBuilder path = new StringBuilder(32768);
            UInt32 length = GetFinalPathNameByHandleW(
                handle, path, (UInt32)path.Capacity, 0
            );
            if (length == 0 || length >= path.Capacity) {
                throw new Win32Exception(
                    Marshal.GetLastWin32Error(),
                    "Retained runtime lock path lookup failed"
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

function Test-DawnstrikeRuntimeLockPathEqual([string]$Left,[string]$Right) {
    return [string]::Equals(
        [IO.Path]::GetFullPath($Left).TrimEnd('\'),
        [IO.Path]::GetFullPath($Right).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Open-DawnstrikeRetainedRuntimeLockRoot([string]$Path) {
    $full=[IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeSharedLockNoReparse $full 'Runtime activation lock root'
    Initialize-DawnstrikeRuntimeLockNative
    $handle=[Dawnstrike.Locking.RuntimeLockNative]::OpenDirectoryRetained($full)
    try {
        $actual=[Dawnstrike.Locking.RuntimeLockNative]::GetFinalPath($handle)
        if(-not(Test-DawnstrikeRuntimeLockPathEqual $full $actual)){throw 'Runtime activation lock root handle is bound to a different path.'}
        Assert-DawnstrikeSharedLockNoReparse $full 'Runtime activation lock root'
        return $handle
    } catch { $handle.Dispose(); throw }
}

function Open-DawnstrikeRetainedRuntimeLockFile([string]$Path,[switch]$CreateNew) {
    $full=[IO.Path]::GetFullPath($Path)
    Assert-DawnstrikeSharedLockNoReparse $full 'Runtime activation lock'
    Initialize-DawnstrikeRuntimeLockNative
    $native=if($CreateNew){
        [Dawnstrike.Locking.RuntimeLockNative]::CreateNewRetained($full)
    }else{
        [Dawnstrike.Locking.RuntimeLockNative]::OpenExistingRetained($full)
    }
    try {
        $stream=[IO.FileStream]::new($native,[IO.FileAccess]::ReadWrite,4096,$false)
        $actual=[Dawnstrike.Locking.RuntimeLockNative]::GetFinalPath($stream.SafeFileHandle)
        if(-not(Test-DawnstrikeRuntimeLockPathEqual $full $actual)){throw 'Runtime activation lock handle is bound to a different path.'}
        return $stream
    } catch {
        if($null-ne$stream){$stream.Dispose()}else{$native.Dispose()}
        throw
    }
}

function Get-DawnstrikeRetainedRuntimeLockSnapshot([IO.FileStream]$Handle,[string]$ExpectedPath) {
    Initialize-DawnstrikeRuntimeLockNative
    if($null-eq$Handle-or$Handle.SafeFileHandle.IsClosed-or-not$Handle.CanRead){throw 'Runtime activation lock retained handle is no longer valid.'}
    $actual=[Dawnstrike.Locking.RuntimeLockNative]::GetFinalPath($Handle.SafeFileHandle)
    if(-not(Test-DawnstrikeRuntimeLockPathEqual $ExpectedPath $actual)){throw 'Runtime activation lock retained handle path changed.'}
    $Handle.Flush($true)
    if($Handle.Length-gt16384){throw 'Runtime activation lock retained bytes exceed the lock ceiling.'}
    $position=$Handle.Position
    try {
        $Handle.Position=0
        $bytes=[byte[]]::new([int]$Handle.Length);$offset=0
        while($offset-lt$bytes.Length){$count=$Handle.Read($bytes,$offset,$bytes.Length-$offset);if($count-le0){throw 'Runtime activation lock retained read was incomplete.'};$offset+=$count}
    } finally { $Handle.Position=$position }
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$hash=([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()}
    finally{$sha.Dispose()}
    return [pscustomobject]@{path=$actual;bytes=$bytes;raw_file_sha256=$hash}
}

function New-DawnstrikeRetainedRuntimeLockObject {
    param([string]$Path,[string]$Token,[string]$BytesSha256,[string]$Operation,[string]$PythonPath,[string]$PythonSha256,[IO.FileStream]$Handle,[object]$RootHandle,[string]$JournalPath='',[string]$JournalSha256='',[string]$StaleArchive='')
    $fields=[ordered]@{path=$Path;token=$Token;bytes_sha256=$BytesSha256;operation=$Operation;python_path=$PythonPath;python_sha256=$PythonSha256;acquired=$true;retained=$true;retained_handle=$Handle;root_handle=$RootHandle}
    if($JournalPath){$fields.journal_path=$JournalPath}
    if($JournalSha256){$fields.journal_sha256=$JournalSha256}
    if($StaleArchive){$fields.stale_archive=$StaleArchive}
    return [pscustomobject]$fields
}

function Confirm-DawnstrikeGovernedRuntimeLock([object]$Lock,[ValidateRange(1,1800)][int]$TimeoutSeconds=300) {
    if($null-eq$Lock-or-not[bool]$Lock.acquired){throw 'Runtime activation lock is not acquired.'}
    $handleProperty=$Lock.PSObject.Properties['retained_handle'];$rootProperty=$Lock.PSObject.Properties['root_handle']
    if($null-eq$handleProperty-or$null-eq$handleProperty.Value-or$null-eq$rootProperty-or$null-eq$rootProperty.Value){throw 'Runtime activation lock has no retained handle identity.'}
    $root=[Microsoft.Win32.SafeHandles.SafeFileHandle]$rootProperty.Value
    if($root.IsClosed-or$root.IsInvalid){throw 'Runtime activation lock root handle is no longer valid.'}
    $expectedRoot=[IO.Path]::GetFullPath((Split-Path ([string]$Lock.path) -Parent))
    $actualRoot=[Dawnstrike.Locking.RuntimeLockNative]::GetFinalPath($root)
    if(-not(Test-DawnstrikeRuntimeLockPathEqual $expectedRoot $actualRoot)){throw 'Runtime activation lock root handle path changed.'}
    $retained=Get-DawnstrikeRetainedRuntimeLockSnapshot ([IO.FileStream]$handleProperty.Value) ([string]$Lock.path)
    if($retained.raw_file_sha256-ne[string]$Lock.bytes_sha256){throw 'Runtime activation lock retained bytes changed; lock retained.'}
    $strict=Get-DawnstrikeStrictRuntimeLock ([string]$Lock.path) ([string]$Lock.python_path) ([string]$Lock.python_sha256) ([IO.FileStream]$handleProperty.Value) $TimeoutSeconds
    if($strict.raw_file_sha256-ne$retained.raw_file_sha256-or[string]$strict.payload.lock_token-ne[string]$Lock.token){throw 'Runtime activation lock path is not bound to its retained handle.'}
    return $strict
}

function Move-DawnstrikeRetainedRuntimeLockNoReplace([IO.FileStream]$Handle,[string]$Destination) {
    $full=[IO.Path]::GetFullPath($Destination)
    Assert-DawnstrikeSharedLockNoReparse $full 'Runtime activation lock move destination'
    if(Test-Path -LiteralPath $full){throw 'Runtime activation lock move destination already exists.'}
    [Dawnstrike.Locking.RuntimeLockNative]::RenameNoReplace($Handle.SafeFileHandle,$full)
    $actual=[Dawnstrike.Locking.RuntimeLockNative]::GetFinalPath($Handle.SafeFileHandle)
    if(-not(Test-DawnstrikeRuntimeLockPathEqual $full $actual)){throw 'Runtime activation lock handle-bound move was not proven.'}
}

function Close-DawnstrikeRuntimeLockHandles([AllowNull()][object]$Lock) {
    if($null-eq$Lock){return}
    $fileProperty=$Lock.PSObject.Properties['retained_handle']
    if($null-ne$fileProperty-and$null-ne$fileProperty.Value){$fileProperty.Value.Dispose();$Lock.retained_handle=$null}
    $rootProperty=$Lock.PSObject.Properties['root_handle']
    if($null-ne$rootProperty-and$null-ne$rootProperty.Value){$rootProperty.Value.Dispose();$Lock.root_handle=$null}
}

function Enter-DawnstrikeRuntimeLockMutex {
    $created = $false
    $mutex = [Threading.Mutex]::new($false, "Global\DawnstrikeRuntimeActivationLockV2", [ref]$created)
    $script:DawnstrikeLockMutexAbandoned = $false
    try { $owned=$mutex.WaitOne([TimeSpan]::FromSeconds(30)) }
    catch [Threading.AbandonedMutexException] { $owned=$true; $script:DawnstrikeLockMutexAbandoned=$true }
    if (-not $owned) { $mutex.Dispose(); throw "Runtime activation lock mutex timed out." }
    return $mutex
}

function Exit-DawnstrikeRuntimeLockMutex([Threading.Mutex]$Mutex) {
    try { $Mutex.ReleaseMutex() } finally { $Mutex.Dispose() }
}

function Assert-DawnstrikeRuntimeLockStateRoot([string]$StateRoot) {
    if (-not [IO.Path]::IsPathRooted($StateRoot)) { throw "StateRoot must be drive-qualified." }
    $full = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $root = [IO.Path]::GetPathRoot($full).TrimEnd('\')
    if ([string]::Equals($full, $root, [StringComparison]::OrdinalIgnoreCase)) { throw "StateRoot cannot be a drive root." }
    if (Test-Path -LiteralPath $full -PathType Leaf) { throw "StateRoot cannot be a file." }
    $cursor = [IO.DirectoryInfo]::new($full)
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "StateRoot contains a reparse point." }
        $cursor = $cursor.Parent
    }
    return $full
}

function Get-DawnstrikeStrictRuntimeLock([string]$Path,[string]$PythonPath,[string]$PythonSha256,[AllowNull()][IO.FileStream]$RetainedHandle=$null,[ValidateRange(1,1800)][int]$TimeoutSeconds=300) {
    $contract = Join-Path $PSScriptRoot "runtime_activation_lock_contract.py"
    Assert-DawnstrikeSharedLockNoReparse $contract 'Runtime activation lock contract'
    Assert-DawnstrikeSharedLockNoReparse $Path 'Runtime activation lock'
    if (-not [IO.Path]::IsPathRooted($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "Approved lock-contract interpreter is invalid." }
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if(($item.Attributes-band [IO.FileAttributes]::ReparsePoint)-or $item.Length -gt 16384){throw 'Runtime activation lock leaf is unsafe.'}
    if($PythonPath -ne $script:DawnstrikeApprovedPythonPath -or $PythonSha256 -ne $script:DawnstrikeApprovedPythonSha256){throw 'Lock-contract interpreter is not the approved exact identity.'}
    if ((Get-DawnstrikeRuntimeLockHash $PythonPath) -ne $PythonSha256) { throw "Approved lock-contract interpreter hash changed." }
    $validationHandle=$RetainedHandle;$closeValidation=$false
    if($null-eq$validationHandle){
        $share=[IO.FileShare]::ReadWrite-bor[IO.FileShare]::Delete
        $validationHandle=[IO.File]::Open($Path,[IO.FileMode]::Open,[IO.FileAccess]::Read,$share)
        $closeValidation=$true
    }
    try{
        $snapshot=Get-DawnstrikeRetainedRuntimeLockSnapshot $validationHandle $Path
        $captured=[Convert]::ToBase64String([byte[]]$snapshot.bytes)
        $arguments=@('-I','-B','-S',$contract,'--captured-base64',$captured)
        try {
            $output = Invoke-DawnstrikeRuntimeContractProcess `
                -PythonPath $PythonPath -ArgumentList $arguments `
                -Label 'Runtime activation lock validation' -TimeoutSeconds $TimeoutSeconds
        }
        catch { throw "Runtime activation lock is malformed, unsafe, or timed out." }
        try { $validated=([string]$output) | ConvertFrom-Json }
        catch { throw "Runtime activation lock validator returned invalid output." }
        if([string]$validated.raw_file_sha256-ne[string]$snapshot.raw_file_sha256){throw 'Runtime activation lock validator did not consume the held exact bytes.'}
        return $validated
    }finally{if($closeValidation){$validationHandle.Dispose()}}
}

function Test-DawnstrikeRuntimeLockOwnerDead([object]$Payload) {
    $process = Get-Process -Id ([int]$Payload.process_id) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $true }
    $actual = $process.StartTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
    if ($actual -eq [string]$Payload.process_started_at_utc) { return $false }
    # A different OS process start proves this PID was reused and the recorded
    # owner is dead. Inability to read the start time raises and fails closed.
    return $true
}

function Convert-DawnstrikeCanonicalOriginIdentity([string]$Origin) {
    $value=$Origin.Trim()
    if ($value -match '^(?:https://|ssh://git@)github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$' -or $value -match '^git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$') {
        $identity=("github.com/{0}/{1}" -f $Matches[1].ToLowerInvariant(),$Matches[2].ToLowerInvariant())
        if($identity -ne 'github.com/mattfren/dawnstrike'){throw 'Origin is not the governed Dawnstrike repository.'}
        return $identity
    }
    throw "Origin URL cannot be reduced to an approved canonical identity."
}

function New-DawnstrikeRuntimeLockPayload([string]$Operation,[string]$CandidateSha,[string]$CandidateTree,[string]$OriginIdentity,[string]$Token) {
    $originHash = Get-DawnstrikeSharedLockSha256Text $OriginIdentity
    return [ordered]@{
        schema_version = "dawnstrike.runtime_activation_lock.v2"
        operation = $Operation
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        origin_identity = $OriginIdentity
        origin_identity_sha256 = $originHash
        process_id = [int]$PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
        acquired_at_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffffffZ")
        lock_token = $Token
        research_only = $true
        broker_execution_enabled = $false
    }
}

function Enter-DawnstrikeGovernedRuntimeLock {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
[ValidateSet("capture_task_hardening","capture_task_rebind","runtime_activation","runtime_rollback","state_preparation","recovery")][string]$Operation = "runtime_activation",
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha = $script:DawnstrikeLockCandidateSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree = $script:DawnstrikeLockCandidateTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity,
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256
    )
    if ([string]::IsNullOrWhiteSpace($CandidateSha) -or [string]::IsNullOrWhiteSpace($CandidateTree) -or [string]::IsNullOrWhiteSpace($OriginIdentity)) { throw "Runtime activation lock source identity is incomplete." }
    $state = Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $lockRoot = Join-Path $state "locks"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    $null = Assert-DawnstrikeRuntimeLockStateRoot $state
    $lockRootItem=Get-Item -LiteralPath $lockRoot -Force
    if($lockRootItem.Attributes-band [IO.FileAttributes]::ReparsePoint){throw 'Runtime activation lock root is unsafe.'}
    $path = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    $mutex = Enter-DawnstrikeRuntimeLockMutex
    $rootHandle=$null;$handle=$null;$returned=$false
    try {
        $rootHandle=Open-DawnstrikeRetainedRuntimeLockRoot $lockRoot
        if($script:DawnstrikeLockMutexAbandoned){$script:DawnstrikeLockMutexAbandoned=$false;throw 'Runtime activation lock mutex was abandoned; only exact stale-lock recovery is permitted.'}
        $daily=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($daily.Count){throw "A daily run lock exists; runtime activation is not permitted."}
        if (Test-Path -LiteralPath $path) { $null = Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256; throw "A runtime activation lock already exists and requires governed recovery." }
        $token = [guid]::NewGuid().ToString("N")
        $json = (New-DawnstrikeRuntimeLockPayload $Operation $CandidateSha $CandidateTree $OriginIdentity $token) | ConvertTo-Json -Compress
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json)
        $handle=Open-DawnstrikeRetainedRuntimeLockFile $path -CreateNew
        $handle.Write($bytes,0,$bytes.Length);$handle.Flush($true)
        $strict = Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256 $handle
        if ($strict.payload.lock_token -ne $token) { throw "Runtime activation lock read-back token mismatch." }
        $owned=New-DawnstrikeRetainedRuntimeLockObject $path $token ([string]$strict.raw_file_sha256) $Operation $PythonPath $PythonSha256 $handle $rootHandle
        $null=Confirm-DawnstrikeGovernedRuntimeLock $owned
        $dailyAfter=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($dailyAfter.Count){
            $null=Confirm-DawnstrikeGovernedRuntimeLock $owned
            [Dawnstrike.Locking.RuntimeLockNative]::MarkDelete($handle.SafeFileHandle)
            $handle.Dispose();$handle=$null;$owned.retained_handle=$null
            if(Test-Path -LiteralPath $path){throw 'Owned activation lock could not be relinquished after daily-lock race.'}
            throw 'A daily run lock appeared during activation lock acquisition.'
        }
        $returned=$true
        return $owned
    } finally {
        if(-not$returned){if($null-ne$handle){$handle.Dispose()};if($null-ne$rootHandle){$rootHandle.Dispose()}}
        Exit-DawnstrikeRuntimeLockMutex $mutex
    }
}

function Adopt-DawnstrikeGovernedRuntimeLock {
    [CmdletBinding()]
param([string]$StateRoot,[string]$ExpectedToken,[string]$ExpectedFileSha256,[string]$ExpectedOperation,[string]$CandidateSha,[string]$CandidateTree,[string]$OriginIdentity,[string]$PythonPath,[string]$PythonSha256,[ValidateSet("capture_task_rebind","runtime_activation","runtime_rollback","state_preparation","recovery")][string]$RecoveryOperation="recovery")
    $state = Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $path = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
    $mutex = Enter-DawnstrikeRuntimeLockMutex
    $rootHandle=$null;$staleHandle=$null;$nextHandle=$null;$returned=$false
    try {
        $script:DawnstrikeLockMutexAbandoned=$false
        $preview=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
        if ($preview.payload.lock_token -ne $ExpectedToken -or $preview.raw_file_sha256 -ne $ExpectedFileSha256 -or $preview.payload.operation -ne $ExpectedOperation -or $preview.payload.candidate_sha -ne $CandidateSha -or $preview.payload.candidate_tree -ne $CandidateTree -or $preview.payload.origin_identity -ne $OriginIdentity) { throw "Stale runtime lock does not match the PREPARED contract." }
        if (-not (Test-DawnstrikeRuntimeLockOwnerDead $preview.payload)) { throw "Runtime activation lock owner is still active." }
        $rootHandle=Open-DawnstrikeRetainedRuntimeLockRoot (Split-Path $path -Parent)
        $staleHandle=Open-DawnstrikeRetainedRuntimeLockFile $path
        $stale = Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256 $staleHandle
        if ($stale.payload.lock_token -ne $ExpectedToken -or $stale.raw_file_sha256 -ne $ExpectedFileSha256 -or $stale.payload.operation -ne $ExpectedOperation -or $stale.payload.candidate_sha -ne $CandidateSha -or $stale.payload.candidate_tree -ne $CandidateTree -or $stale.payload.origin_identity -ne $OriginIdentity) { throw "Stale runtime lock does not match the PREPARED contract." }
        if (-not (Test-DawnstrikeRuntimeLockOwnerDead $stale.payload)) { throw "Runtime activation lock owner is still active." }
        $archive = Join-Path (Split-Path $path -Parent) ("recovered-stale-" + $ExpectedFileSha256 + ".lock")
        Assert-DawnstrikeSharedLockNoReparse $archive 'Stale lock archive'
        if (Test-Path -LiteralPath $archive) { throw "Stale lock archive already exists; adoption is ambiguous." }
        $token=[guid]::NewGuid().ToString("N")
        $json=(New-DawnstrikeRuntimeLockPayload $RecoveryOperation $CandidateSha $CandidateTree $OriginIdentity $token)|ConvertTo-Json -Compress
        $temp=Join-Path (Split-Path $path -Parent) (".lock-recovery-"+[guid]::NewGuid().ToString('N')+".tmp")
        Assert-DawnstrikeSharedLockNoReparse $temp 'Recovery lock temporary file'
        $bytes=[Text.UTF8Encoding]::new($false).GetBytes($json)
        $nextHandle=Open-DawnstrikeRetainedRuntimeLockFile $temp -CreateNew
        $nextHandle.Write($bytes,0,$bytes.Length);$nextHandle.Flush($true)
        if((Get-DawnstrikeRetainedRuntimeLockSnapshot $nextHandle $temp).raw_file_sha256-ne(Get-DawnstrikeSharedLockSha256Text $json)){throw 'Prepared recovery lock bytes changed.'}
        Move-DawnstrikeRetainedRuntimeLockNoReplace $staleHandle $archive
        Move-DawnstrikeRetainedRuntimeLockNoReplace $nextHandle $path
        if ((Get-DawnstrikeRuntimeLockHash $archive) -ne $ExpectedFileSha256) { throw "Stale lock archive changed during atomic adoption." }
        $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256 $nextHandle
        $staleHandle.Dispose();$staleHandle=$null
        $owned=New-DawnstrikeRetainedRuntimeLockObject $path $token ([string]$current.raw_file_sha256) $RecoveryOperation $PythonPath $PythonSha256 $nextHandle $rootHandle '' '' $archive
        $null=Confirm-DawnstrikeGovernedRuntimeLock $owned
        $returned=$true
        return $owned
    } finally {
        if(-not$returned){if($null-ne$nextHandle){$nextHandle.Dispose()};if($null-ne$staleHandle){$staleHandle.Dispose()};if($null-ne$rootHandle){$rootHandle.Dispose()}}
        Exit-DawnstrikeRuntimeLockMutex $mutex
    }
}

function Get-DawnstrikeStrictRuntimeOperationJournal {
    param([string]$Path,[string]$PythonPath,[string]$PythonSha256,[ValidateRange(1,1800)][int]$TimeoutSeconds=300)
    $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
    Assert-DawnstrikeSharedLockNoReparse $contract 'Runtime operation journal contract'
    Assert-DawnstrikeSharedLockNoReparse $Path 'Runtime operation journal'
    if($PythonPath-ne $script:DawnstrikeApprovedPythonPath-or $PythonSha256-ne $script:DawnstrikeApprovedPythonSha256){throw 'Journal interpreter is not approved.'}
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if($item.PSIsContainer-or($item.Attributes-band [IO.FileAttributes]::ReparsePoint)-or$item.Length-gt 65536){throw 'Runtime operation journal leaf is unsafe.'}
    $state=(Split-Path (Split-Path (Split-Path $Path -Parent) -Parent) -Parent)
    try {
        $output=Invoke-DawnstrikeRuntimeContractProcess `
            -PythonPath $PythonPath `
            -ArgumentList @('-I','-B','-S',$contract,'verify',$Path,'--state-root',$state) `
            -Label 'Runtime operation journal validation' -TimeoutSeconds $TimeoutSeconds
    }
    catch { throw 'Runtime operation journal is malformed, unsafe, or timed out.' }
    try{return ([string]$output)|ConvertFrom-Json}catch{throw 'Runtime operation journal validator returned invalid output.'}
}

function Get-DawnstrikeApprovedGit {
    Assert-DawnstrikeSharedLockNoReparse $script:DawnstrikeApprovedGitPath 'Approved Git executable'
    if(-not(Test-Path -LiteralPath $script:DawnstrikeApprovedGitPath -PathType Leaf)){throw 'Approved Git executable is missing.'}
    if((Get-DawnstrikeRuntimeLockHash $script:DawnstrikeApprovedGitPath)-ne$script:DawnstrikeApprovedGitSha256){throw 'Approved Git executable hash changed.'}
    # Windows PowerShell exposes Get-AuthenticodeSignature directly, while
    # PowerShell 7 on Windows may not import the Microsoft.PowerShell.Security
    # module in a non-interactive runner.  Keep the cmdlet path where present
    # and use the same embedded Authenticode certificate fallback as the
    # pinned Python check otherwise; both paths require the exact signer.
    $signatureCommand=Get-Command Get-AuthenticodeSignature -CommandType Cmdlet -ErrorAction SilentlyContinue
    if($null-ne$signatureCommand){
        $signature=Get-AuthenticodeSignature -LiteralPath $script:DawnstrikeApprovedGitPath -ErrorAction Stop
        if(
            [string]$signature.Status-ne'Valid'-or
            $null-eq$signature.SignerCertificate-or
            [string]$signature.SignerCertificate.Subject-ne$script:DawnstrikeApprovedGitSubject-or
            [string]$signature.SignerCertificate.Thumbprint-ne$script:DawnstrikeApprovedGitThumbprint
        ){throw 'Approved Git executable signer is invalid.'}
    }else{
        try{$certificate=[Security.Cryptography.X509Certificates.X509Certificate2]::new([Security.Cryptography.X509Certificates.X509Certificate]::CreateFromSignedFile($script:DawnstrikeApprovedGitPath))}
        catch{throw 'Approved Git executable has no readable Authenticode signer.'}
        if($certificate.Subject-ne$script:DawnstrikeApprovedGitSubject-or$certificate.Thumbprint-ne$script:DawnstrikeApprovedGitThumbprint){throw 'Approved Git executable signer is invalid.'}
    }
    return [pscustomobject]@{path=$script:DawnstrikeApprovedGitPath;sha256=$script:DawnstrikeApprovedGitSha256}
}

function Move-DawnstrikeBoundRuntimeOperationTemps {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [Parameter(Mandatory=$true)][string]$JournalRoot,
        [Parameter(Mandatory=$true)][string]$LockRoot,
        [Parameter(Mandatory=$true)][object]$Lock,
        [Parameter(Mandatory=$true)][string]$Operation,
        [Parameter(Mandatory=$true)][string]$CandidateSha,
        [Parameter(Mandatory=$true)][string]$CandidateTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity
    )

    # Crash-left journal inputs/transitions are deliberately not parsed as
    # journals.  Only a small, regular, hash/owner-bound temporary may be
    # quarantined, and only while the global mutex is held.  Malformed or
    # foreign bytes remain untouched for operator review and never poison the
    # canonical-journal scan.
    if (-not (Test-DawnstrikeRuntimeLockOwnerDead $Lock.payload)) { return }
    $referencedNext = @{}
    foreach ($canonicalItem in @(Get-ChildItem -LiteralPath $JournalRoot -File -Force -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^runtime-activation-[0-9a-f]{24}\.json$' -or
        $_.Name -match '^runtime-rollback-[0-9a-f]{24}\.json$' -or
        $_.Name -match '^capture-task-rebind-[0-9a-f]{40}\.json$' -or
        $_.Name -match '^capture-task-hardening-[0-9a-f]{40}\.json$' -or
        $_.Name -match '^state-preparation-[0-9a-f]{40}\.json$'
    })) {
        try {
            $canonicalPayload = [IO.File]::ReadAllText($canonicalItem.FullName) | ConvertFrom-Json
            if ([string]$canonicalPayload.adoption_state -eq 'ADOPTION_PREPARED' -and
                [string]$canonicalPayload.next_lock_relative_path -ne 'NONE' -and
                [string]$canonicalPayload.next_lock_file_sha256 -match '^[0-9a-f]{64}$') {
                $referencedNext[[string]$canonicalPayload.next_lock_relative_path.Replace('/', '\')] = $true
            }
        } catch { }
    }
    $candidates = @(
        Get-ChildItem -LiteralPath $JournalRoot -File -Force -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -match '^\.journal-(?:input|transition|init)-[0-9a-f]{32}\.json$'
            }
    )
    $candidates += @(
        Get-ChildItem -LiteralPath $LockRoot -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^\.next-runtime-lock-[0-9a-f]{64}\.tmp$' }
    )
    foreach ($item in $candidates) {
        Assert-DawnstrikeSharedLockNoReparse $item.FullName 'Runtime operation temporary evidence'
        if ($item.Length -gt 65536 -or $item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
        $bound = $false
        try {
            $raw = [IO.File]::ReadAllText($item.FullName)
            if ($item.Name -match '^\.next-runtime-lock-([0-9a-f]{64})\.tmp$') {
                $relativeName = 'locks\' + $item.Name
                if ($referencedNext.ContainsKey($relativeName)) { continue }
                $bound = (
                    (Get-DawnstrikeSharedLockSha256Text $raw) -eq [string]$Matches[1] -and
                    (($raw | ConvertFrom-Json).operation -eq $Operation) -and
                    (($raw | ConvertFrom-Json).candidate_sha -eq $CandidateSha) -and
                    (($raw | ConvertFrom-Json).candidate_tree -eq $CandidateTree) -and
                    (($raw | ConvertFrom-Json).origin_identity -eq $OriginIdentity)
                )
            }
            else {
                $tempPayload = $raw | ConvertFrom-Json
                $bound = (
                    [string]$tempPayload.operation -eq $Operation -and
                    [string]$tempPayload.candidate_sha -eq $CandidateSha -and
                    [string]$tempPayload.candidate_tree -eq $CandidateTree -and
                    [string]$tempPayload.origin_identity -eq $OriginIdentity
                )
            }
        }
        catch { $bound = $false }
        if (-not $bound) { continue }
        $quarantineRoot = Join-Path $StateRoot 'recovery-quarantine'
        Assert-DawnstrikeSharedLockNoReparse $quarantineRoot 'Runtime operation temporary quarantine'
        New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
        Assert-DawnstrikeSharedLockNoReparse $quarantineRoot 'Runtime operation temporary quarantine'
        $stamp = [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
        $destination = Join-Path $quarantineRoot ('runtime-operation-' + $stamp + '-' + $item.Name.TrimStart('.'))
        Assert-DawnstrikeSharedLockNoReparse $destination 'Runtime operation temporary quarantine destination'
        if (Test-Path -LiteralPath $destination) { continue }
        [IO.File]::Move($item.FullName, $destination)
        if ((Test-Path -LiteralPath $item.FullName) -or -not (Test-Path -LiteralPath $destination -PathType Leaf)) {
            throw 'Runtime operation temporary quarantine was not proven.'
        }
    }
}

function Get-DawnstrikeAdvancedOriginRecoveryAdmission {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [ValidateSet('','runtime_activation','runtime_rollback','capture_task_rebind','capture_task_hardening','state_preparation')][string]$Operation = '',
        [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$CandidateSha = '',
        [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$CandidateTree = '',
        [string]$OriginIdentity = '',
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256
    )

    $mutex = Enter-DawnstrikeRuntimeLockMutex
    try {
        $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
        $lockPath=Join-Path $state 'locks\dawnstrike-runtime-activation.lock'
        if(-not(Test-Path -LiteralPath $lockPath -PathType Leaf)){return $null}
        $lock=Get-DawnstrikeStrictRuntimeLock $lockPath $PythonPath $PythonSha256
        if(-not(Test-DawnstrikeRuntimeLockOwnerDead $lock.payload)){
            throw 'Advanced-origin recovery lock owner is still active.'
        }
        # In recovery-discovery mode the sealed journal is the authority.  Any
        # caller-supplied identity is only an optional consistency filter; it
        # can never select a journal or manufacture an old candidate/date.
        $lockOperation = [string]$lock.payload.operation
        $lockCandidateSha = [string]$lock.payload.candidate_sha
        $lockCandidateTree = [string]$lock.payload.candidate_tree
        $lockOriginIdentity = [string]$lock.payload.origin_identity
        if (($Operation -and $lockOperation -cne $Operation) -or
            ($CandidateSha -and $lockCandidateSha -cne $CandidateSha) -or
            ($CandidateTree -and $lockCandidateTree -cne $CandidateTree) -or
            ($OriginIdentity -and $lockOriginIdentity -cne $OriginIdentity)) {
            throw 'Advanced-origin recovery lock identity is not the requested exact transaction.'
        }
        $discoveryOperation = if ($Operation) { $Operation } else { $lockOperation }
        $discoveryCandidateSha = if ($CandidateSha) { $CandidateSha } else { $lockCandidateSha }
        $discoveryCandidateTree = if ($CandidateTree) { $CandidateTree } else { $lockCandidateTree }
        $discoveryOriginIdentity = if ($OriginIdentity) { $OriginIdentity } else { $lockOriginIdentity }
        $journalRoot=Join-Path $state 'receipts\runtime-operation'
        Assert-DawnstrikeSharedLockNoReparse $journalRoot 'Runtime operation journal root'
        if(-not(Test-Path -LiteralPath $journalRoot -PathType Container)){
            throw 'Advanced-origin recovery lock has no operation journal root.'
        }
        Move-DawnstrikeBoundRuntimeOperationTemps `
            -StateRoot $state -JournalRoot $journalRoot -LockRoot (Join-Path $state 'locks') -Lock $lock -Operation $discoveryOperation `
            -CandidateSha $discoveryCandidateSha -CandidateTree $discoveryCandidateTree -OriginIdentity $discoveryOriginIdentity
        $matches=@()
        # Only canonical operation journals are admissible.  In particular,
        # crash-left .journal-input/.journal-transition files must never be
        # strict-parsed as independent operations.
        $canonical = @(Get-ChildItem -LiteralPath $journalRoot -File -Force -ErrorAction Stop | Where-Object {
            $_.Name -match '^runtime-activation-[0-9a-f]{24}\.json$' -or
            $_.Name -match '^runtime-rollback-[0-9a-f]{24}\.json$' -or
            $_.Name -match '^capture-task-rebind-[0-9a-f]{40}\.json$' -or
            $_.Name -match '^capture-task-hardening-[0-9a-f]{40}\.json$' -or
            $_.Name -match '^state-preparation-[0-9a-f]{40}\.json$'
        })
        foreach($item in $canonical){
            Assert-DawnstrikeSharedLockNoReparse $item.FullName 'Canonical runtime operation journal'
            $journal=Get-DawnstrikeStrictRuntimeOperationJournal $item.FullName $PythonPath $PythonSha256
            $payload=$journal.payload
            $nameOperation = if ($item.Name -like 'runtime-activation-*') { 'runtime_activation' }
                elseif ($item.Name -like 'runtime-rollback-*') { 'runtime_rollback' }
                elseif ($item.Name -like 'capture-task-rebind-*') { 'capture_task_rebind' }
                elseif ($item.Name -like 'capture-task-hardening-*') { 'capture_task_hardening' }
                else { 'state_preparation' }
            if ([string]$payload.operation -ne $nameOperation) { throw 'Canonical runtime operation journal filename binding is invalid.' }
            $bound=$false
            if([string]$payload.adoption_state-in@('NONE','ADOPTED')){
                $bound=(
                    [string]$payload.lock_token-eq[string]$lock.payload.lock_token-and
                    [string]$payload.lock_file_sha256-eq[string]$lock.raw_file_sha256
                )
            }elseif([string]$payload.adoption_state-eq'ADOPTION_PREPARED'){
                $oldBound=(
                    [string]$payload.old_lock_token-eq[string]$lock.payload.lock_token-and
                    [string]$payload.old_lock_file_sha256-eq[string]$lock.raw_file_sha256
                )
                $nextBound=(
                    [string]$payload.next_lock_token-eq[string]$lock.payload.lock_token-and
                    [string]$payload.next_lock_file_sha256-eq[string]$lock.raw_file_sha256
                )
                $bound=($oldBound-or$nextBound)
            }
            if($bound){
                if(
                    [string]$payload.operation-ne$lockOperation-or
                    [string]$payload.candidate_sha-ne$lockCandidateSha-or
                    [string]$payload.candidate_tree-ne$lockCandidateTree-or
                    [string]$payload.origin_identity-ne$lockOriginIdentity
                ){throw 'Advanced-origin recovery journal and lock identities disagree.'}
                $matches+=,[pscustomobject]@{path=$item.FullName;journal=$journal}
            }
        }
        if($matches.Count-ne1){throw 'Advanced-origin recovery requires exactly one lock-bound operation journal.'}
        $selected = $matches[0]
        $selectedPayload = $selected.journal.payload
        # Re-apply optional filters to the selected sealed journal, and expose
        # the complete recovery identity so callers do not derive operation,
        # phase, receipt paths, or market-date context from the stale request.
        if (($Operation -and [string]$selectedPayload.operation -cne $Operation) -or
            ($CandidateSha -and [string]$selectedPayload.candidate_sha -cne $CandidateSha) -or
            ($CandidateTree -and [string]$selectedPayload.candidate_tree -cne $CandidateTree) -or
            ($OriginIdentity -and [string]$selectedPayload.origin_identity -cne $OriginIdentity)) {
            throw 'Advanced-origin recovery journal does not match the requested exact transaction.'
        }
        return [pscustomobject]@{
            path = $selected.path
            journal = $selected.journal
            operation = [string]$selectedPayload.operation
            candidate_sha = [string]$selectedPayload.candidate_sha
            candidate_tree = [string]$selectedPayload.candidate_tree
            previous_sha = [string]$selectedPayload.previous_sha
            previous_tree = [string]$selectedPayload.previous_tree
            current_sha = [string]$selectedPayload.current_sha
            current_tree = [string]$selectedPayload.current_tree
            phase = [string]$selectedPayload.phase
            origin_identity = [string]$selectedPayload.origin_identity
            prepared_receipt_relative_path = [string]$selectedPayload.prepared_receipt_relative_path
            complete_receipt_relative_path = [string]$selectedPayload.complete_receipt_relative_path
            compensation_receipt_relative_path = [string]$selectedPayload.compensation_receipt_relative_path
            market_date = if ($selectedPayload.PSObject.Properties.Name -contains 'market_date') { [string]$selectedPayload.market_date } else { 'NONE' }
        }
    }
    finally { Exit-DawnstrikeRuntimeLockMutex $mutex }
}

function Save-DawnstrikeRuntimeOperationJournalAdoptionPredecessor {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][object]$Journal,[Parameter(Mandatory=$true)][string]$JournalPath)

    $expectedHash=[string]$Journal.raw_file_sha256
    if($expectedHash-notmatch'^[0-9a-f]{64}$'){throw 'Runtime operation journal adoption predecessor hash is invalid.'}
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    Assert-DawnstrikeSharedLockNoReparse $journalFull 'Runtime operation journal adoption predecessor'
    if(-not(Test-Path -LiteralPath $journalFull -PathType Leaf)){throw 'Runtime operation journal adoption predecessor is missing.'}
    $journalItem=Get-Item -LiteralPath $journalFull -Force -ErrorAction Stop
    if(($journalItem.Attributes-band[IO.FileAttributes]::ReparsePoint)-or$journalItem.Length-gt65536){throw 'Runtime operation journal adoption predecessor leaf is unsafe.'}
    $journalBytes=[IO.File]::ReadAllBytes($journalFull)
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$actualHash=([BitConverter]::ToString($sha.ComputeHash($journalBytes))).Replace('-','').ToLowerInvariant()}
    finally{$sha.Dispose()}
    if($actualHash-ne$expectedHash){throw 'Runtime operation journal changed before adoption lineage was recorded.'}

    # The journal's prior_journal_file_sha256 remains phase-transition lineage.
    # Preserve every exact adoption predecessor separately so terminal
    # compensation can retain its immutable compensation-receipt binding.
    $lineageRoot=Join-Path (Split-Path $journalFull -Parent) 'adoption-lineage'
    Assert-DawnstrikeSharedLockNoReparse $lineageRoot 'Runtime operation journal adoption lineage root'
    New-Item -ItemType Directory -Path $lineageRoot -Force|Out-Null
    Assert-DawnstrikeSharedLockNoReparse $lineageRoot 'Runtime operation journal adoption lineage root'
    $lineagePath=Join-Path $lineageRoot ('adoption-predecessor-'+$expectedHash+'.json')
    Assert-DawnstrikeSharedLockNoReparse $lineagePath 'Runtime operation journal adoption lineage'
    if(Test-Path -LiteralPath $lineagePath){
        $lineageItem=Get-Item -LiteralPath $lineagePath -Force -ErrorAction Stop
        if(-not(Test-Path -LiteralPath $lineagePath -PathType Leaf)-or
            ($lineageItem.Attributes-band[IO.FileAttributes]::ReparsePoint)-or
            $lineageItem.Length-ne$journalBytes.Length-or
            (Get-DawnstrikeRuntimeLockHash $lineagePath)-ne$expectedHash){
            throw 'Runtime operation journal adoption lineage archive is ambiguous.'
        }
        return [pscustomobject]@{path=$lineagePath;raw_file_sha256=$expectedHash}
    }

    $temporary=Join-Path $lineageRoot ('.adoption-predecessor-'+[guid]::NewGuid().ToString('N')+'.tmp')
    Assert-DawnstrikeSharedLockNoReparse $temporary 'Runtime operation journal adoption lineage temporary'
    $stream=[IO.File]::Open($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($journalBytes,0,$journalBytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    try{[IO.File]::Move($temporary,$lineagePath)}finally{if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary -Force}}
    Assert-DawnstrikeSharedLockNoReparse $lineagePath 'Runtime operation journal adoption lineage'
    if(-not(Test-Path -LiteralPath $lineagePath -PathType Leaf)-or(Get-DawnstrikeRuntimeLockHash $lineagePath)-ne$expectedHash){throw 'Runtime operation journal adoption lineage archive was not proven.'}
    return [pscustomobject]@{path=$lineagePath;raw_file_sha256=$expectedHash}
}

function Set-DawnstrikeRuntimeOperationJournalAdoption {
    param([object]$Journal,[string]$JournalPath,[string]$State,[string]$CurrentToken,
        [string]$CurrentHash,[string]$OldToken,[string]$OldHash,[string]$NextToken,
        [string]$NextHash,[string]$ArchiveRelative,[string]$NextRelative,
        [string]$PythonPath,[string]$PythonSha256,[ValidateRange(1,1800)][int]$ProcessTimeoutSeconds=300)
    $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
    $payload=[ordered]@{}
    foreach($property in $Journal.payload.PSObject.Properties){
        if($property.Name-ne'journal_self_sha256'){$payload[$property.Name]=$property.Value}
    }
    # Adoption is also the compatibility boundary for journals written by
    # older candidates.  New terminal recovery always uses the v2 exact key
    # set, while ordinary v1 journals remain readable until adopted.
    if(-not $payload.Contains('compensation_receipt_relative_path')){$payload.compensation_receipt_relative_path='NONE'}
    if(-not $payload.Contains('compensation_receipt_sha256')){$payload.compensation_receipt_sha256=(Get-DawnstrikeSharedLockSha256Text '')}
    $payload.schema_version='dawnstrike.runtime_operation_journal.v2'
    $payload.adoption_state=$State;$payload.lock_token=$CurrentToken
    $payload.lock_file_sha256=$CurrentHash;$payload.old_lock_token=$OldToken
    $payload.old_lock_file_sha256=$OldHash;$payload.next_lock_token=$NextToken
    $payload.next_lock_file_sha256=$NextHash
    $payload.old_lock_archive_relative_path=$ArchiveRelative
    $payload.next_lock_relative_path=$NextRelative
    if([string]$Journal.payload.phase-eq'COMPENSATED'){
        # This is the compensation receipt's immutable predecessor binding,
        # not scratch space for later lock-adoption transitions.
        $payload.prior_journal_file_sha256=[string]$Journal.payload.prior_journal_file_sha256
    }else{
        $payload.prior_journal_file_sha256=[string]$Journal.raw_file_sha256
    }
    $payload.recorded_at_utc=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
    $input=Join-Path (Split-Path $JournalPath -Parent) ('.journal-input-'+[guid]::NewGuid().ToString('N')+'.json')
    Assert-DawnstrikeSharedLockNoReparse $input 'Runtime operation journal input'
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($payload|ConvertTo-Json -Depth 8 -Compress))
    $stream=[IO.File]::Open($input,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    try{
        $null=Save-DawnstrikeRuntimeOperationJournalAdoptionPredecessor $Journal $JournalPath
        $state=(Split-Path (Split-Path (Split-Path $JournalPath -Parent) -Parent) -Parent)
        try {
            $output=Invoke-DawnstrikeRuntimeContractProcess `
                -PythonPath $PythonPath `
                -ArgumentList @('-I','-B','-S',$contract,'seal',$input,$JournalPath,'--state-root',$state) `
                -Label 'Runtime operation journal adoption seal' -TimeoutSeconds $ProcessTimeoutSeconds
        }
        catch { throw 'Runtime operation journal adoption seal failed or timed out.' }
        try{return ([string]$output)|ConvertFrom-Json}catch{throw 'Journal seal returned invalid output.'}
    }finally{if(Test-Path -LiteralPath $input){Remove-Item -LiteralPath $input -Force}}
}

function Adopt-DawnstrikeGovernedRuntimeLockWithJournal {
    [CmdletBinding()]
    param([string]$StateRoot,[string]$JournalPath,[string]$CandidateSha,
        [string]$CandidateTree,[string]$OriginIdentity,[string]$PythonPath,
        [string]$PythonSha256,[ValidateSet('after_prepared','after_archive','after_replace')][string]$TestCrashPoint='')
    if($TestCrashPoint-and$env:DAWNSTRIKE_TEST_LOCK_JOURNAL-ne'1'){throw 'Journal crash injection is test-only.'}
    $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $statePrefix=$state.TrimEnd('\')+'\'
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    if(-not$journalFull.StartsWith($statePrefix,[StringComparison]::OrdinalIgnoreCase)){throw 'Journal must be inside StateRoot.'}
    $path=Join-Path $state 'locks\dawnstrike-runtime-activation.lock'
    $lockRoot=Split-Path $path -Parent
    $mutex=Enter-DawnstrikeRuntimeLockMutex
    $rootHandle=$null;$currentHandle=$null;$nextHandle=$null;$archiveHandle=$null;$returned=$false
    try{
        $rootHandle=Open-DawnstrikeRetainedRuntimeLockRoot $lockRoot
        while($true){
            if($null-ne$currentHandle){$currentHandle.Dispose();$currentHandle=$null}
            if($null-ne$nextHandle){$nextHandle.Dispose();$nextHandle=$null}
            if($null-ne$archiveHandle){$archiveHandle.Dispose();$archiveHandle=$null}
            $journal=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
            $payload=$journal.payload
            if($payload.candidate_sha-ne$CandidateSha-or$payload.candidate_tree-ne$CandidateTree-or$payload.origin_identity-ne$OriginIdentity){throw 'Journal source identity does not match recovery.'}
            $ownerStart=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
            $needsNewRound=$false;$shape=''

            if($payload.adoption_state-eq'NONE'-or$payload.adoption_state-eq'ADOPTED'){
                if(-not(Test-Path -LiteralPath $path -PathType Leaf)){throw 'Journal-bound runtime activation lock is missing.'}
                $preview=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256
                $previewOwnedByCurrent=([int]$preview.payload.process_id-eq[int]$PID-and[string]$preview.payload.process_started_at_utc-eq$ownerStart)
                if(-not$previewOwnedByCurrent-and-not(Test-DawnstrikeRuntimeLockOwnerDead $preview.payload)){throw 'Runtime activation lock owner is still active.'}
                $currentHandle=Open-DawnstrikeRetainedRuntimeLockFile $path
                $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256 $currentHandle
                if((Get-DawnstrikeRetainedRuntimeLockSnapshot $currentHandle $path).raw_file_sha256-ne$current.raw_file_sha256){throw 'Journal-bound runtime activation lock changed while opening its retained handle.'}
                if($payload.lock_token-ne$current.payload.lock_token-or$payload.lock_file_sha256-ne$current.raw_file_sha256){throw 'Journal does not bind the stale lock.'}
                $ownedByCurrent=([int]$current.payload.process_id-eq[int]$PID-and[string]$current.payload.process_started_at_utc-eq$ownerStart)
                if($ownedByCurrent){
                    $owned=New-DawnstrikeRetainedRuntimeLockObject $path ([string]$current.payload.lock_token) ([string]$current.raw_file_sha256) ([string]$current.payload.operation) $PythonPath $PythonSha256 $currentHandle $rootHandle $journalFull ([string]$journal.raw_file_sha256)
                    $null=Confirm-DawnstrikeGovernedRuntimeLock $owned
                    $returned=$true;return $owned
                }
                if(-not(Test-DawnstrikeRuntimeLockOwnerDead $current.payload)){throw 'Runtime activation lock owner is still active.'}
                $needsNewRound=$true
            }elseif($payload.adoption_state-eq'ADOPTION_PREPARED'){
                $oldPath=Join-Path $state ([string]$payload.old_lock_archive_relative_path).Replace('/','\')
                $nextPath=Join-Path $state ([string]$payload.next_lock_relative_path).Replace('/','\')
                if(Test-Path -LiteralPath $path -PathType Leaf){
                    $currentHandle=Open-DawnstrikeRetainedRuntimeLockFile $path
                    $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256 $currentHandle
                    if((Get-DawnstrikeRetainedRuntimeLockSnapshot $currentHandle $path).raw_file_sha256-ne$current.raw_file_sha256){throw 'Prepared adoption canonical lock changed while opening its retained handle.'}
                    if($current.raw_file_sha256-eq[string]$payload.old_lock_file_sha256){
                        if(Test-Path -LiteralPath $oldPath){throw 'Prepared adoption old archive already exists while the old canonical lock remains.'}
                        $shape='OLD_CANONICAL'
                    }elseif($current.raw_file_sha256-eq[string]$payload.next_lock_file_sha256){
                        if(Test-Path -LiteralPath $nextPath){throw 'Prepared adoption retained both the installed and temporary next lock.'}
                        if(-not(Test-Path -LiteralPath $oldPath -PathType Leaf)){throw 'Prepared adoption archive is missing.'}
                        $archiveHandle=Open-DawnstrikeRetainedRuntimeLockFile $oldPath
                        $archived=Get-DawnstrikeStrictRuntimeLock $oldPath $PythonPath $PythonSha256 $archiveHandle
                        if($archived.raw_file_sha256-ne[string]$payload.old_lock_file_sha256-or(Get-DawnstrikeRetainedRuntimeLockSnapshot $archiveHandle $oldPath).raw_file_sha256-ne$archived.raw_file_sha256){throw 'Prepared adoption archive is missing or changed.'}
                        $archiveHandle.Dispose();$archiveHandle=$null
                        if(-not(Test-DawnstrikeRuntimeLockOwnerDead $current.payload)){throw 'Prepared next runtime activation lock owner is still active.'}
                        # The installed next lock belongs to the process that
                        # died after its handle-bound install.  Begin another
                        # durable round so the returned lock names this process.
                        $needsNewRound=$true
                    }else{throw 'Prepared adoption canonical lock hash is invalid.'}
                }else{
                    if(-not(Test-Path -LiteralPath $oldPath -PathType Leaf)-or-not(Test-Path -LiteralPath $nextPath -PathType Leaf)){throw 'Prepared adoption missing-canonical shape is incomplete.'}
                    $archiveHandle=Open-DawnstrikeRetainedRuntimeLockFile $oldPath
                    $archived=Get-DawnstrikeStrictRuntimeLock $oldPath $PythonPath $PythonSha256 $archiveHandle
                    if($archived.raw_file_sha256-ne[string]$payload.old_lock_file_sha256-or(Get-DawnstrikeRetainedRuntimeLockSnapshot $archiveHandle $oldPath).raw_file_sha256-ne$archived.raw_file_sha256){throw 'Prepared adoption archive is missing or changed.'}
                    $shape='OLD_ARCHIVED'
                }
            }else{throw 'Journal adoption state is not recoverable.'}

            if($needsNewRound){
                $oldToken=[string]$current.payload.lock_token;$oldHash=[string]$current.raw_file_sha256
                $nextToken=[guid]::NewGuid().ToString('N')
                $nextJson=(New-DawnstrikeRuntimeLockPayload ([string]$current.payload.operation) $CandidateSha $CandidateTree $OriginIdentity $nextToken)|ConvertTo-Json -Compress
                $nextBytes=[Text.UTF8Encoding]::new($false).GetBytes($nextJson)
                $nextHash=Get-DawnstrikeSharedLockSha256Text $nextJson
                $nextName='.next-runtime-lock-'+$nextHash+'.tmp';$nextPath=Join-Path $lockRoot $nextName
                $nextHandle=Open-DawnstrikeRetainedRuntimeLockFile $nextPath -CreateNew
                $nextHandle.Write($nextBytes,0,$nextBytes.Length);$nextHandle.Flush($true)
                if((Get-DawnstrikeRetainedRuntimeLockSnapshot $nextHandle $nextPath).raw_file_sha256-ne$nextHash){throw 'Prepared next-lock bytes changed while held.'}
                $archiveName='recovered-stale-'+$oldHash+'.lock'
                $journal=Set-DawnstrikeRuntimeOperationJournalAdoption $journal $journalFull 'ADOPTION_PREPARED' $oldToken $oldHash $oldToken $oldHash $nextToken $nextHash ('locks/'+$archiveName) ('locks/'+$nextName) $PythonPath $PythonSha256
                if($TestCrashPoint-eq'after_prepared'){exit 137}
                $payload=$journal.payload;$oldPath=Join-Path $lockRoot $archiveName;$shape='OLD_CANONICAL'
            }else{
                if($null-eq$nextHandle){
                    $nextHandle=Open-DawnstrikeRetainedRuntimeLockFile $nextPath
                    $next=Get-DawnstrikeStrictRuntimeLock $nextPath $PythonPath $PythonSha256 $nextHandle
                    if($next.raw_file_sha256-ne[string]$payload.next_lock_file_sha256-or(Get-DawnstrikeRetainedRuntimeLockSnapshot $nextHandle $nextPath).raw_file_sha256-ne$next.raw_file_sha256){throw 'Prepared next-lock bytes are missing or changed.'}
                }
            }

            if($shape-eq'OLD_CANONICAL'){
                Move-DawnstrikeRetainedRuntimeLockNoReplace $currentHandle $oldPath
                $archiveHandle=$currentHandle;$currentHandle=$null
                if($TestCrashPoint-eq'after_archive'){exit 137}
            }elseif($shape-ne'OLD_ARCHIVED'){throw 'Prepared adoption filesystem shape is invalid.'}

            Move-DawnstrikeRetainedRuntimeLockNoReplace $nextHandle $path
            $currentHandle=$nextHandle;$nextHandle=$null
            if($TestCrashPoint-eq'after_replace'){exit 137}
            $current=Get-DawnstrikeStrictRuntimeLock $path $PythonPath $PythonSha256 $currentHandle
            if((Get-DawnstrikeRetainedRuntimeLockSnapshot $currentHandle $path).raw_file_sha256-ne$current.raw_file_sha256){throw 'Installed runtime activation lock changed while held.'}
            $journal=Set-DawnstrikeRuntimeOperationJournalAdoption $journal $journalFull 'ADOPTED' ([string]$current.payload.lock_token) ([string]$current.raw_file_sha256) ([string]$payload.old_lock_token) ([string]$payload.old_lock_file_sha256) ([string]$payload.next_lock_token) ([string]$payload.next_lock_file_sha256) ([string]$payload.old_lock_archive_relative_path) 'NONE' $PythonPath $PythonSha256
            if($null-ne$archiveHandle){$archiveHandle.Dispose();$archiveHandle=$null}
            if($current.payload.process_id-ne[int]$PID-or$current.payload.process_started_at_utc-ne$ownerStart){throw 'Recovered runtime lock is not owned by this exact process.'}
            $owned=New-DawnstrikeRetainedRuntimeLockObject $path ([string]$current.payload.lock_token) ([string]$current.raw_file_sha256) ([string]$current.payload.operation) $PythonPath $PythonSha256 $currentHandle $rootHandle $journalFull ([string]$journal.raw_file_sha256)
            $null=Confirm-DawnstrikeGovernedRuntimeLock $owned
            $returned=$true;return $owned
        }
    }finally{
        if(-not$returned){if($null-ne$nextHandle){$nextHandle.Dispose()};if($null-ne$currentHandle){$currentHandle.Dispose()};if($null-ne$archiveHandle){$archiveHandle.Dispose()};if($null-ne$rootHandle){$rootHandle.Dispose()}}
        Exit-DawnstrikeRuntimeLockMutex $mutex
    }
}

function Set-DawnstrikeRuntimeOperationJournalPhase {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [Parameter(Mandatory=$true)][string]$JournalPath,
        [Parameter(Mandatory=$true)][object]$Lock,
[ValidateSet('runtime_activation','runtime_rollback','capture_task_rebind','capture_task_hardening','state_preparation')][string]$Operation,
 [ValidateSet('INIT','PRE_QUIESCE','PRE_SWAP','POST_SWAP','POST_SWAP_READY','PRE_ENABLE','POST_ENABLE','PRE_TASK_UPDATE','POST_TASK_UPDATE','PREPARE','COMPLETE','COMPENSATED')][string]$Phase,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity,
        [Parameter(Mandatory=$true)][string]$PreparedReceiptRelativePath,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$PreparedReceiptSha256,
        [Parameter(Mandatory=$true)][string]$CompleteReceiptRelativePath,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$CompleteReceiptSha256,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$BackupContractSha256,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$TaskContractSha256,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$RuntimeStageContractSha256,
        [string]$CompensationReceiptRelativePath='NONE',
        [string]$CompensationReceiptSha256='',
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256,
        [ValidateRange(1,1800)][int]$ProcessTimeoutSeconds=300
    )
    $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    $journalRoot=[IO.Path]::GetFullPath((Join-Path $state 'receipts\runtime-operation')).TrimEnd('\')+'\'
    if(-not$journalFull.StartsWith($journalRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Runtime operation journal must be inside its governed receipt root.'}
    Assert-DawnstrikeSharedLockNoReparse $journalFull 'Runtime operation journal'
    $current=Confirm-DawnstrikeGovernedRuntimeLock $Lock -TimeoutSeconds $ProcessTimeoutSeconds
    $processStart=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
    if($current.payload.lock_token-ne$Lock.token-or$current.raw_file_sha256-ne$Lock.bytes_sha256-or[int]$current.payload.process_id-ne[int]$PID-or[string]$current.payload.process_started_at_utc-ne$processStart){throw 'Journal transition requires the exact live lock owned by this process.'}
    if($current.payload.operation-ne$Operation-or$current.payload.candidate_sha-ne$CandidateSha-or$current.payload.candidate_tree-ne$CandidateTree-or$current.payload.origin_identity-ne$OriginIdentity){throw 'Journal transition lock identity does not match the operation.'}
    $phases=@{
runtime_activation=@('INIT','PRE_QUIESCE','PRE_SWAP','POST_SWAP','POST_SWAP_READY','COMPLETE','COMPENSATED')
runtime_rollback=@('INIT','PRE_SWAP','POST_SWAP','POST_SWAP_READY','COMPLETE','COMPENSATED')
capture_task_rebind=@('INIT','PRE_ENABLE','POST_ENABLE','COMPLETE','COMPENSATED')
capture_task_hardening=@('INIT','PRE_TASK_UPDATE','POST_TASK_UPDATE','COMPLETE','COMPENSATED')
        state_preparation=@('INIT','PREPARE','COMPLETE','COMPENSATED')
    }
    $sequence=[array]::IndexOf([object[]]$phases[$Operation],$Phase)
    if($sequence-lt 0){throw 'Journal phase is invalid for the operation.'}
    $empty=Get-DawnstrikeSharedLockSha256Text ''
    $priorHash=$empty
    $initOwnerProcessId=[int]$current.payload.process_id
    $initOwnerStartedAtUtc=[string]$current.payload.process_started_at_utc
    if($Phase-ne'INIT'){
        $prior=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
        $priorHash=[string]$prior.raw_file_sha256
        $initOwnerProcessId=[int]$prior.payload.init_owner_process_id
        $initOwnerStartedAtUtc=[string]$prior.payload.init_owner_started_at_utc
    }elseif(Test-Path -LiteralPath $journalFull){throw 'INIT journal already exists.'}
    if($Phase -eq 'COMPENSATED'){
        if($CompensationReceiptRelativePath -eq 'NONE' -or $CompensationReceiptSha256 -notmatch '^[0-9a-f]{64}$' -or $CompensationReceiptSha256 -eq $empty){throw 'Compensated journal requires an exact compensation receipt.'}
    }elseif($CompensationReceiptRelativePath -ne 'NONE' -or $CompensationReceiptSha256 -ne ''){throw 'Non-compensated journal cannot carry compensation proof.'}
    $payload=[ordered]@{
        schema_version='dawnstrike.runtime_operation_journal.v2';operation=$Operation;phase=$Phase;sequence=$sequence
        candidate_sha=$CandidateSha;candidate_tree=$CandidateTree;current_sha=$CurrentSha;current_tree=$CurrentTree
        previous_sha=$PreviousSha;previous_tree=$PreviousTree;origin_identity=$OriginIdentity
        origin_identity_sha256=Get-DawnstrikeSharedLockSha256Text $OriginIdentity
        state_root_sha256=Get-DawnstrikeSharedLockSha256Text $state.ToLowerInvariant()
        lock_token=[string]$current.payload.lock_token;lock_file_sha256=[string]$current.raw_file_sha256
        prior_journal_file_sha256=$priorHash
        prepared_receipt_relative_path=$PreparedReceiptRelativePath;prepared_receipt_sha256=$PreparedReceiptSha256
        complete_receipt_relative_path=$CompleteReceiptRelativePath;complete_receipt_sha256=$CompleteReceiptSha256
        backup_contract_sha256=$BackupContractSha256;task_contract_sha256=$TaskContractSha256
        runtime_stage_contract_sha256=$RuntimeStageContractSha256;recorded_at_utc=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        compensation_receipt_relative_path=$CompensationReceiptRelativePath;compensation_receipt_sha256=$(if($Phase -eq 'COMPENSATED'){$CompensationReceiptSha256}else{$empty})
        research_only=$true;broker_execution_enabled=$false;adoption_state='NONE'
        old_lock_token=[string]$current.payload.lock_token;old_lock_file_sha256=[string]$current.raw_file_sha256
        next_lock_token=[string]$current.payload.lock_token;next_lock_file_sha256=[string]$current.raw_file_sha256
        old_lock_archive_relative_path='NONE';next_lock_relative_path='NONE'
        init_owner_process_id=$initOwnerProcessId;init_owner_started_at_utc=$initOwnerStartedAtUtc
    }
    New-Item -ItemType Directory -Path (Split-Path $journalFull -Parent) -Force|Out-Null
    $input=Join-Path (Split-Path $journalFull -Parent) ('.journal-transition-'+[guid]::NewGuid().ToString('N')+'.json')
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes(($payload|ConvertTo-Json -Depth 8 -Compress))
    $stream=[IO.File]::Open($input,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
    try{
        $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
        $arguments=@('-I','-B','-S',$contract,'transition',$input,$journalFull,'--state-root',$state)
        if($Phase-ne'INIT'){$arguments+=@('--previous',$journalFull)}
        try {
            $output=Invoke-DawnstrikeRuntimeContractProcess `
                -PythonPath $PythonPath -ArgumentList $arguments `
                -Label 'Runtime operation journal phase transition' -TimeoutSeconds $ProcessTimeoutSeconds
        }
        catch { throw 'Runtime operation journal phase transition failed or timed out.' }
        try{return ([string]$output)|ConvertFrom-Json}catch{throw 'Journal transition returned invalid output.'}
    }finally{if(Test-Path -LiteralPath $input){Remove-Item -LiteralPath $input -Force}}
}

function Clear-DawnstrikeCompensatedJournalTombstone {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [Parameter(Mandatory=$true)][string]$JournalPath,
[Parameter(Mandatory=$true)][ValidateSet('capture_task_rebind','capture_task_hardening','runtime_activation','runtime_rollback','state_preparation')][string]$Operation,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity,
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256
    )
    $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    Assert-DawnstrikeSharedLockNoReparse $journalFull 'Compensated journal tombstone'
    $lockPath=Join-Path $state 'locks\dawnstrike-runtime-activation.lock'
    $mutex=Enter-DawnstrikeRuntimeLockMutex
    try {
        if(Test-Path -LiteralPath $lockPath -PathType Leaf){throw 'Cannot clear a compensated journal while its runtime lock exists.'}
        $journal=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
        if([string]$journal.payload.operation -ne $Operation -or [string]$journal.payload.phase -ne 'COMPENSATED' -or
            [string]$journal.payload.candidate_sha -ne $CandidateSha -or [string]$journal.payload.candidate_tree -ne $CandidateTree -or
            [string]$journal.payload.origin_identity -ne $OriginIdentity){throw 'Compensated journal tombstone identity is invalid.'}
        $relative=[string]$journal.payload.compensation_receipt_relative_path
        $receipt=Join-Path $state ($relative.Replace('/','\'))
        Assert-DawnstrikeSharedLockNoReparse $receipt 'Compensation receipt'
        if(-not(Test-Path -LiteralPath $receipt -PathType Leaf) -or (Get-DawnstrikeRuntimeLockHash $receipt) -ne [string]$journal.payload.compensation_receipt_sha256){throw 'Compensation receipt changed or is missing.'}
        $archiveRoot=Join-Path $state 'receipts\runtime-operation\archive'
        New-Item -ItemType Directory -Path $archiveRoot -Force|Out-Null
        Assert-DawnstrikeSharedLockNoReparse $archiveRoot 'Compensated journal archive root'
        $archive=Join-Path $archiveRoot ('compensated-'+[string]$journal.raw_file_sha256+'.json')
        Assert-DawnstrikeSharedLockNoReparse $archive 'Compensated journal archive'
        if(Test-Path -LiteralPath $archive){throw 'Compensated journal archive already exists; tombstone cleanup is ambiguous.'}
        [IO.File]::Move($journalFull,$archive)
        if((Test-Path -LiteralPath $journalFull) -or -not(Test-Path -LiteralPath $archive) -or (Get-DawnstrikeRuntimeLockHash $archive) -ne [string]$journal.raw_file_sha256){throw 'Compensated journal archive was not proven.'}
        return [pscustomobject]@{archived_path=$archive;raw_file_sha256=[string]$journal.raw_file_sha256}
    } finally { Exit-DawnstrikeRuntimeLockMutex $mutex }
}

function Enter-DawnstrikeGovernedRuntimeLockWithJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$StateRoot,
        [Parameter(Mandatory=$true)][string]$JournalPath,
[ValidateSet('runtime_activation','runtime_rollback','capture_task_rebind','capture_task_hardening','state_preparation')][string]$Operation,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$CurrentTree,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousSha,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$PreviousTree,
        [Parameter(Mandatory=$true)][string]$OriginIdentity,
        [Parameter(Mandatory=$true)][string]$PreparedReceiptRelativePath,
        [Parameter(Mandatory=$true)][string]$CompleteReceiptRelativePath,
        [ValidatePattern('^[0-9a-f]{64}$')][string]$TaskContractSha256,
        [Parameter(Mandatory=$true)][string]$PythonPath,
        [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-f]{64}$')][string]$PythonSha256,
        [ValidateRange(1,1800)][int]$ProcessTimeoutSeconds=300,
        [ValidateSet('after_init','after_lock')][string]$TestCrashPoint='',
        [switch]$TestInjectDailyLockRace
    )
    if($TestCrashPoint-and$env:DAWNSTRIKE_TEST_LOCK_JOURNAL-ne'1'){throw 'Journal acquisition crash injection is test-only.'}
    $state=Assert-DawnstrikeRuntimeLockStateRoot $StateRoot
    $journalFull=[IO.Path]::GetFullPath($JournalPath)
    $journalRoot=[IO.Path]::GetFullPath((Join-Path $state 'receipts\runtime-operation')).TrimEnd('\')+'\'
    if(-not$journalFull.StartsWith($journalRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'Runtime operation journal must be inside its governed receipt root.'}
    if($TaskContractSha256-eq(Get-DawnstrikeSharedLockSha256Text '')){throw 'Journal acquisition requires a nonempty task contract hash.'}
    Assert-DawnstrikeSharedLockNoReparse $journalFull 'Runtime operation journal'
    $lockRoot=Join-Path $state 'locks';New-Item -ItemType Directory -Path $lockRoot -Force|Out-Null
    Assert-DawnstrikeSharedLockNoReparse $lockRoot 'Runtime activation lock root'
    $lockRootItem=Get-Item -LiteralPath $lockRoot -Force
    if($lockRootItem.Attributes-band[IO.FileAttributes]::ReparsePoint){throw 'Runtime activation lock root is unsafe.'}
    $lockPath=Join-Path $lockRoot 'dawnstrike-runtime-activation.lock'
    $mutex=Enter-DawnstrikeRuntimeLockMutex
    $rootHandle=$null;$lockHandle=$null;$returned=$false
    try{
        $rootHandle=Open-DawnstrikeRetainedRuntimeLockRoot $lockRoot
        $abandoned=[bool]$script:DawnstrikeLockMutexAbandoned
        $script:DawnstrikeLockMutexAbandoned=$false
        $dailyBefore=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($dailyBefore.Count){throw 'A daily run lock exists; runtime lock initialization is not permitted.'}
        $hasJournal=Test-Path -LiteralPath $journalFull -PathType Leaf
        $hasLock=Test-Path -LiteralPath $lockPath -PathType Leaf
        if($hasJournal){
            $journal=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
            if([string]$journal.payload.operation-ne$Operation-or[string]$journal.payload.candidate_sha-ne$CandidateSha-or[string]$journal.payload.candidate_tree-ne$CandidateTree-or[string]$journal.payload.origin_identity-ne$OriginIdentity-or[string]$journal.payload.prepared_receipt_relative_path-ne$PreparedReceiptRelativePath-or[string]$journal.payload.complete_receipt_relative_path-ne$CompleteReceiptRelativePath-or[string]$journal.payload.task_contract_sha256-ne$TaskContractSha256){throw 'Existing journal identity is invalid.'}
            if([string]$journal.payload.phase -eq 'COMPENSATED'){
                if($hasLock){
                    $lock=Get-DawnstrikeStrictRuntimeLock $lockPath $PythonPath $PythonSha256
                    if($journal.payload.lock_token-ne$lock.payload.lock_token-or$journal.payload.lock_file_sha256-ne$lock.raw_file_sha256){throw 'Compensated journal and lock do not match.'}
                    if(-not(Test-DawnstrikeRuntimeLockOwnerDead $lock.payload)){throw 'Runtime activation lock owner is still active.'}
                    $adopted=Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state -JournalPath $journalFull -CandidateSha $CandidateSha -CandidateTree $CandidateTree -OriginIdentity $OriginIdentity -PythonPath $PythonPath -PythonSha256 $PythonSha256
                    return $adopted
                }
                return [pscustomobject]@{path=$lockPath;token='';bytes_sha256='';operation=$Operation;python_path=$PythonPath;python_sha256=$PythonSha256;acquired=$false;terminal_tombstone=$true;journal_path=$journalFull;journal_sha256=[string]$journal.raw_file_sha256}
            }
            if([string]$journal.payload.phase-ne'INIT'-or[string]$journal.payload.current_sha-ne$CurrentSha-or[string]$journal.payload.current_tree-ne$CurrentTree-or[string]$journal.payload.previous_sha-ne$PreviousSha-or[string]$journal.payload.previous_tree-ne$PreviousTree){throw 'Existing INIT journal identity is invalid.'}
            if(-not$hasLock){
                $initOwner=[pscustomobject]@{process_id=[int]$journal.payload.init_owner_process_id;process_started_at_utc=[string]$journal.payload.init_owner_started_at_utc}
                if(-not$abandoned-and-not(Test-DawnstrikeRuntimeLockOwnerDead $initOwner)){throw 'Orphan INIT journal owner is still active.'}
                $before=[string]$journal.raw_file_sha256
                if((Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256).raw_file_sha256-ne$before){throw 'Orphan INIT journal changed during recovery.'}
                Remove-Item -LiteralPath $journalFull -Force
                if(Test-Path -LiteralPath $journalFull){throw 'Orphan INIT journal cleanup failed.'}
                $hasJournal=$false
            }else{
                $lock=Get-DawnstrikeStrictRuntimeLock $lockPath $PythonPath $PythonSha256
                if($journal.payload.lock_token-ne$lock.payload.lock_token-or$journal.payload.lock_file_sha256-ne$lock.raw_file_sha256){throw 'INIT journal and lock do not match.'}
                if(-not(Test-DawnstrikeRuntimeLockOwnerDead $lock.payload)){throw 'Runtime activation lock owner is still active.'}
                $adopted=Adopt-DawnstrikeGovernedRuntimeLockWithJournal -StateRoot $state -JournalPath $journalFull -CandidateSha $CandidateSha -CandidateTree $CandidateTree -OriginIdentity $OriginIdentity -PythonPath $PythonPath -PythonSha256 $PythonSha256
                return $adopted
            }
        }elseif($hasLock){throw 'Runtime lock exists without its exact INIT journal.'}
        $token=[guid]::NewGuid().ToString('N')
        $lockJson=(New-DawnstrikeRuntimeLockPayload $Operation $CandidateSha $CandidateTree $OriginIdentity $token)|ConvertTo-Json -Compress
        $lockBytes=[Text.UTF8Encoding]::new($false).GetBytes($lockJson)
        $lockHash=Get-DawnstrikeSharedLockSha256Text $lockJson
        $empty=Get-DawnstrikeSharedLockSha256Text ''
        $payload=[ordered]@{
            schema_version='dawnstrike.runtime_operation_journal.v2';operation=$Operation;phase='INIT';sequence=0
            candidate_sha=$CandidateSha;candidate_tree=$CandidateTree;current_sha=$CurrentSha;current_tree=$CurrentTree
            previous_sha=$PreviousSha;previous_tree=$PreviousTree;origin_identity=$OriginIdentity
            origin_identity_sha256=Get-DawnstrikeSharedLockSha256Text $OriginIdentity
            state_root_sha256=Get-DawnstrikeSharedLockSha256Text $state.ToLowerInvariant()
            lock_token=$token;lock_file_sha256=$lockHash;prior_journal_file_sha256=$empty
            prepared_receipt_relative_path=$PreparedReceiptRelativePath;prepared_receipt_sha256=$empty
            complete_receipt_relative_path=$CompleteReceiptRelativePath;complete_receipt_sha256=$empty
            backup_contract_sha256=$empty;task_contract_sha256=$TaskContractSha256;runtime_stage_contract_sha256=$empty
            compensation_receipt_relative_path='NONE';compensation_receipt_sha256=$empty
            recorded_at_utc=[DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ');research_only=$true;broker_execution_enabled=$false
            adoption_state='NONE';old_lock_token=$token;old_lock_file_sha256=$lockHash;next_lock_token=$token;next_lock_file_sha256=$lockHash
            old_lock_archive_relative_path='NONE';next_lock_relative_path='NONE'
            init_owner_process_id=[int]$PID;init_owner_started_at_utc=(Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffffffZ')
        }
        New-Item -ItemType Directory -Path (Split-Path $journalFull -Parent) -Force|Out-Null
        $input=Join-Path (Split-Path $journalFull -Parent) ('.journal-init-'+[guid]::NewGuid().ToString('N')+'.json')
        $inputBytes=[Text.UTF8Encoding]::new($false).GetBytes(($payload|ConvertTo-Json -Depth 8 -Compress))
        $inputStream=[IO.File]::Open($input,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
        try{$inputStream.Write($inputBytes,0,$inputBytes.Length);$inputStream.Flush($true)}finally{$inputStream.Dispose()}
        try{
            $contract=Join-Path $PSScriptRoot 'runtime_operation_journal.py'
            try {
                $null=Invoke-DawnstrikeRuntimeContractProcess `
                    -PythonPath $PythonPath `
                    -ArgumentList @('-I','-B','-S',$contract,'transition',$input,$journalFull,'--state-root',$state) `
                    -Label 'INIT runtime journal sealing' -TimeoutSeconds $ProcessTimeoutSeconds
            }
            catch { throw 'INIT journal sealing failed or timed out.' }
        }finally{if(Test-Path $input){Remove-Item $input -Force}}
        if($TestCrashPoint-eq'after_init'){Stop-Process -Id $PID -Force}
        $lockHandle=Open-DawnstrikeRetainedRuntimeLockFile $lockPath -CreateNew
        $lockHandle.Write($lockBytes,0,$lockBytes.Length);$lockHandle.Flush($true)
        $strict=Get-DawnstrikeStrictRuntimeLock $lockPath $PythonPath $PythonSha256 $lockHandle $ProcessTimeoutSeconds
        if($strict.raw_file_sha256-ne$lockHash-or$strict.payload.lock_token-ne$token){throw 'Created lock does not match INIT journal.'}
        $owned=New-DawnstrikeRetainedRuntimeLockObject $lockPath $token $lockHash $Operation $PythonPath $PythonSha256 $lockHandle $rootHandle $journalFull
        $null=Confirm-DawnstrikeGovernedRuntimeLock $owned -TimeoutSeconds $ProcessTimeoutSeconds
        if($TestCrashPoint-eq'after_lock'){Stop-Process -Id $PID -Force}
        if($TestInjectDailyLockRace){
            if($env:DAWNSTRIKE_TEST_LOCK_JOURNAL-ne'1'){throw 'Daily-lock race injection is test-only.'}
            [IO.File]::WriteAllText((Join-Path $lockRoot 'dawnstrike-daily-injected.lock'),'test')
        }
        $dailyAfter=@(Get-ChildItem -LiteralPath $lockRoot -Filter 'dawnstrike-daily-*.lock' -File -Force -ErrorAction SilentlyContinue)
        if($dailyAfter.Count){
            $ownedLock=Confirm-DawnstrikeGovernedRuntimeLock $owned
            $ownedJournal=Get-DawnstrikeStrictRuntimeOperationJournal $journalFull $PythonPath $PythonSha256
            if($ownedLock.payload.lock_token-ne$token-or$ownedLock.raw_file_sha256-ne$lockHash-or$ownedJournal.payload.lock_token-ne$token-or$ownedJournal.payload.lock_file_sha256-ne$lockHash){throw 'Initialized lock or journal changed during daily-lock race; evidence retained.'}
            [Dawnstrike.Locking.RuntimeLockNative]::MarkDelete($lockHandle.SafeFileHandle)
            $lockHandle.Dispose();$lockHandle=$null;$owned.retained_handle=$null
            Remove-Item -LiteralPath $journalFull -Force
            if((Test-Path $lockPath)-or(Test-Path $journalFull)){throw 'Owned initialization evidence could not be cleaned after daily-lock race.'}
            throw 'A daily run lock appeared during runtime lock initialization.'
        }
        $returned=$true;return $owned
    }finally{
        if(-not$returned){if($null-ne$lockHandle){$lockHandle.Dispose()};if($null-ne$rootHandle){$rootHandle.Dispose()}}
        Exit-DawnstrikeRuntimeLockMutex $mutex
    }
}

function Exit-DawnstrikeGovernedRuntimeLock {
    param([AllowNull()][object]$Lock)
    if ($null -eq $Lock) { return }
    if(-not[bool]$Lock.acquired){return}
    $mutex=Enter-DawnstrikeRuntimeLockMutex
    $deleted=$false
    try {
        $current=Confirm-DawnstrikeGovernedRuntimeLock $Lock
        if ($current.payload.lock_token -ne $Lock.token -or $current.raw_file_sha256 -ne $Lock.bytes_sha256) { throw "Runtime activation lock ownership changed; lock retained." }
        $handle=[IO.FileStream]$Lock.retained_handle
        [Dawnstrike.Locking.RuntimeLockNative]::MarkDelete($handle.SafeFileHandle)
        $handle.Dispose();$Lock.retained_handle=$null;$deleted=$true
        if (Test-Path -LiteralPath $Lock.path) { throw "Runtime activation lock release was not proven." }
    } finally {
        if($deleted-and$null-ne$Lock.root_handle){$Lock.root_handle.Dispose();$Lock.root_handle=$null}
        Exit-DawnstrikeRuntimeLockMutex $mutex
    }
}
