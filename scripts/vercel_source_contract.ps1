[CmdletBinding()]
param()

# Publication is built from a generated artifact, but the function entrypoints
# are executable source.  Keep their identity tied to one clean, immutable Git
# commit so a dirty/racing checkout cannot silently change the deployed code.
$script:VercelApprovedGitPath = 'C:\Program Files\Dawnstrike\Git-2.55.0.5\cmd\git.exe'
$script:VercelApprovedGitSha256 = '78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f'
$script:VercelApprovedGitSubject = 'CN=Johannes Schindelin, O=Johannes Schindelin, L=Bruehl, C=DE'
$script:VercelApprovedGitThumbprint = '2A1E97CBF0DFCDA15B0DA0AC9745014F989D4AD0'
$script:VercelGeneratedRuntimeAuthorityPath = Join-Path `
    (Split-Path -Parent $PSScriptRoot) 'config\vercel_generated_runtime_authority.v1.json'
$script:VercelGeneratedRuntimeAuthoritySha256 = `
    '3629e094ef8d8c7f64e4ebd0111e76af9e848fab03cd61355d215f2d387cc4a4'
$script:VercelGeneratedRuntimeAuthorityContractSha256 = `
    '26d187b155c3b30486457cef3a3bd3c0830a1bf882a039c76a04e3059de198e3'
$script:VercelPinnedWheelContracts = @(
    [pscustomobject]@{
        distribution = 'pip-26.2.1'
        uri = 'https://files.pythonhosted.org/packages/f3/6e/1736e5b4ae2b778ef2f81c47d797de9f891d4d8acb047a24ca37a60294dd/pip-26.2.1-py3-none-any.whl'
        length = 1816632L
        sha256 = '71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e'
        archive_prefixes = @('pip/', 'pip-26.2.1.dist-info/')
    },
    [pscustomobject]@{
        distribution = 'vercel_runtime-0.22.1'
        uri = 'https://files.pythonhosted.org/packages/9f/52/44e1dcea1d974c4b7011549cfb687f23507dd8f95247f32ad79e7df1e441/vercel_runtime-0.22.1-py3-none-any.whl'
        length = 525108L
        sha256 = 'c9c1d74ae41199a48478a3082e6d7c42c21cdc096710e17f30e0292f71816fec'
        archive_prefixes = @('vercel_runtime/', 'vercel_runtime-0.22.1.dist-info/')
    }
)

function Get-VercelApprovedGitPath {
    if (Get-Command Get-DawnstrikeApprovedGit -ErrorAction SilentlyContinue) {
        return [string](Get-DawnstrikeApprovedGit).path
    }
    $cursor = [System.IO.FileInfo]::new($script:VercelApprovedGitPath)
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw "Approved Vercel Git path contains a reparse point."
        }
        $cursor = if ($cursor -is [System.IO.FileInfo]) { $cursor.Directory } else { $cursor.Parent }
    }
    if (-not (Test-Path -LiteralPath $script:VercelApprovedGitPath -PathType Leaf) -or
        (Get-FileHash -LiteralPath $script:VercelApprovedGitPath -Algorithm SHA256).Hash.ToLowerInvariant() -cne
            $script:VercelApprovedGitSha256) {
        throw "Approved Vercel Git executable identity changed."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $script:VercelApprovedGitPath -ErrorAction Stop
    if ([string]$signature.Status -cne 'Valid' -or $null -eq $signature.SignerCertificate -or
        [string]$signature.SignerCertificate.Subject -cne $script:VercelApprovedGitSubject -or
        [string]$signature.SignerCertificate.Thumbprint -cne $script:VercelApprovedGitThumbprint) {
        throw "Approved Vercel Git executable signer changed."
    }
    return $script:VercelApprovedGitPath
}

# Windows PowerShell's ConvertFrom-Json keeps the last value for a duplicate
# object key.  That is unsafe for a receipt/config boundary: an attacker can
# append a second handler or route and rely on a different parser downstream.
# Keep a tiny dependency-free JSON grammar guard beside the contract so every
# JSON object is rejected before PowerShell materializes it.
# Native runtime support is precompiled and loaded by the protected entrypoint.

if ($false) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace Dawnstrike.Native {
    [StructLayout(LayoutKind.Sequential)]
    internal struct VercelPackageByHandleInformation {
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

    internal static class VercelPackageNativeMethods {
        internal const uint GENERIC_READ = 0x80000000;
        internal const uint OPEN_EXISTING = 3;
        internal const uint FILE_FLAG_BACKUP_SEMANTICS = 0x02000000;
        internal const uint FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000;
        internal const uint FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400;

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
            out VercelPackageByHandleInformation information);
    }

    public sealed class VercelPackageDirectoryLease : IDisposable {
        private SafeFileHandle handle;
        public string Identity { get; private set; }
        public long LastWriteFileTime { get; private set; }

        private VercelPackageDirectoryLease(
            SafeFileHandle value,
            string identity,
            long lastWriteFileTime) {
            handle = value;
            Identity = identity;
            LastWriteFileTime = lastWriteFileTime;
        }

        public static VercelPackageDirectoryLease Open(string path) {
            SafeFileHandle handle = VercelPackageNativeMethods.CreateFileW(
                path,
                VercelPackageNativeMethods.GENERIC_READ,
                // Deny new write/delete handles for the admitted directory.
                FileShare.Read,
                IntPtr.Zero,
                VercelPackageNativeMethods.OPEN_EXISTING,
                VercelPackageNativeMethods.FILE_FLAG_BACKUP_SEMANTICS |
                    VercelPackageNativeMethods.FILE_FLAG_OPEN_REPARSE_POINT,
                IntPtr.Zero);
            if (handle.IsInvalid) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "Could not retain Vercel package directory: " + path);
            }
            VercelPackageByHandleInformation information;
            if (!VercelPackageNativeMethods.GetFileInformationByHandle(handle, out information)) {
                int error = Marshal.GetLastWin32Error();
                handle.Dispose();
                throw new Win32Exception(error, "Could not identify Vercel package directory: " + path);
            }
            if ((information.FileAttributes & VercelPackageNativeMethods.FILE_ATTRIBUTE_REPARSE_POINT) != 0) {
                handle.Dispose();
                throw new InvalidOperationException("Vercel package directory is a reparse point: " + path);
            }
            string identity = information.VolumeSerialNumber.ToString("x8") + ":" +
                information.FileIndexHigh.ToString("x8") + information.FileIndexLow.ToString("x8");
            long lastWrite = ((long)information.LastWriteTime.dwHighDateTime << 32) |
                (uint)information.LastWriteTime.dwLowDateTime;
            return new VercelPackageDirectoryLease(handle, identity, lastWrite);
        }

        public void Dispose() {
            if (handle != null) {
                handle.Dispose();
                handle = null;
            }
        }
    }

    public sealed class VercelPackageMutationWatch : IDisposable {
        private readonly FileSystemWatcher watcher;
        private int changed;
        private int overflowed;

        public VercelPackageMutationWatch(string path) {
            watcher = new FileSystemWatcher(path);
            watcher.IncludeSubdirectories = true;
            watcher.NotifyFilter = NotifyFilters.FileName | NotifyFilters.DirectoryName |
                NotifyFilters.LastWrite | NotifyFilters.Size | NotifyFilters.Attributes |
                NotifyFilters.CreationTime | NotifyFilters.Security;
            watcher.InternalBufferSize = 65536;
            watcher.Changed += delegate { Interlocked.Exchange(ref changed, 1); };
            watcher.Created += delegate { Interlocked.Exchange(ref changed, 1); };
            watcher.Deleted += delegate { Interlocked.Exchange(ref changed, 1); };
            watcher.Renamed += delegate { Interlocked.Exchange(ref changed, 1); };
            watcher.Error += delegate { Interlocked.Exchange(ref overflowed, 1); };
            watcher.EnableRaisingEvents = true;
        }

        public void AssertStable() {
            // Give completion notifications already issued by the kernel a
            // bounded opportunity to reach the watcher callback.
            Thread.Sleep(100);
            Thread.MemoryBarrier();
            if (Volatile.Read(ref overflowed) != 0) {
                throw new InvalidOperationException("Vercel package mutation watcher overflowed.");
            }
            if (Volatile.Read(ref changed) != 0) {
                throw new InvalidOperationException("Vercel package changed after deployment admission.");
            }
        }

        public void Dispose() {
            watcher.EnableRaisingEvents = false;
            watcher.Dispose();
        }
    }
}
'@
}

function Get-VercelDeploymentBoundarySnapshotHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Directories,
        [Parameter(Mandatory = $true)][object[]]$Files
    )

    $lines = @(
        $Directories | Sort-Object relative_path | ForEach-Object {
            'D|' + [string]$_.relative_path + '|' + [string]$_.identity + '|' +
                [string][long]$_.last_write_file_time
        }
        $Files | Sort-Object relative_path | ForEach-Object {
            'F|' + [string]$_.relative_path + '|' + [string][long]$_.length + '|' +
                [string]$_.sha1 + '|' + [string]$_.sha256 + '|' + [string][int]$_.mode
        }
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
        ))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-VercelDeploymentFrozenMapHash {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Files)

    $lines = @(
        $Files | Sort-Object relative_path -CaseSensitive | ForEach-Object {
            'F|' + [string]$_.relative_path + '|' + [string][long]$_.length + '|' +
                [string]$_.sha1 + '|' + [string]$_.sha256 + '|' + [string][int]$_.mode
        }
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash(
            [Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
        ))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Assert-VercelDeploymentProviderRelativePath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not $Path -or $Path.Length -gt 1024 -or $Path -match '[\x00-\x1f]' -or
        $Path.Contains('\') -or $Path.StartsWith('/') -or $Path.EndsWith('/') -or
        $Path -match '(^|/)\.\.?(/|$)' -or $Path -notmatch '^[A-Za-z0-9._/-]+$') {
        throw "$Label is not a canonical bounded provider-relative path."
    }
}

function Get-VercelDeploymentProviderDirectories {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Files)

    $directories = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($file in @($Files)) {
        $segments = @(([string]$file.relative_path).Split('/'))
        for ($count = 1; $count -lt $segments.Count; $count++) {
            $null = $directories.Add([string]::Join('/', $segments[0..($count - 1)]))
        }
    }
    return @($directories | Sort-Object -CaseSensitive)
}

function Read-VercelDeploymentBoundedStreamBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][System.IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][long]$MaximumBytes,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not $Stream.CanRead -or -not $Stream.CanSeek) {
        throw "$Label is not a retained seekable read stream."
    }
    if ($Stream.Length -lt 0 -or $Stream.Length -gt $MaximumBytes -or
        $Stream.Length -gt [int]::MaxValue) {
        throw "$Label exceeds the bounded in-memory deployment limit."
    }
    $Stream.Position = 0
    $bytes = New-Object byte[] ([int]$Stream.Length)
    $offset = 0
    while ($offset -lt $bytes.Length) {
        $read = $Stream.Read($bytes, $offset, $bytes.Length - $offset)
        if ($read -le 0) { throw "$Label ended before its retained length." }
        $offset += $read
    }
    if ($Stream.ReadByte() -ne -1) { throw "$Label grew during retained admission." }
    $Stream.Position = 0
    return ,$bytes
}

function Get-VercelDeploymentBytesHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][ValidateSet('SHA1', 'SHA256')][string]$Algorithm
    )

    $hash = if ($Algorithm -eq 'SHA1') {
        [Security.Cryptography.SHA1]::Create()
    }
    else { [Security.Cryptography.SHA256]::Create() }
    try {
        return ([BitConverter]::ToString($hash.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $hash.Dispose() }
}

function Assert-VercelFrozenPackageManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][byte[]]$ManifestBytes,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedManifestSha256,
        [Parameter(Mandatory = $true)][object[]]$Directories,
        [Parameter(Mandatory = $true)][object[]]$Files
    )

    $manifestHash = Get-VercelDeploymentBytesHash -Bytes $ManifestBytes -Algorithm SHA256
    if ($manifestHash -cne $ExpectedManifestSha256.ToLowerInvariant()) {
        throw 'Vercel package manifest bytes changed before frozen deployment admission.'
    }
    try {
        $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
        $raw = $strictUtf8.GetString($ManifestBytes)
    }
    catch { throw 'Vercel package manifest is not strict UTF-8.' }
    Assert-VercelJsonObjectKeysUnique -RawJson $raw
    try { $manifest = $raw | ConvertFrom-Json }
    catch { throw 'Vercel package manifest is not valid JSON.' }
    $manifestKeys = @(
        $manifest.PSObject.Properties | ForEach-Object { [string]$_.Name } |
            Sort-Object -CaseSensitive
    )
    $expectedManifestKeys = @(
        'bindings', 'directories', 'files', 'generated_runtime_authority_sha256',
        'schema_version'
    ) |
        Sort-Object -CaseSensitive
    if (@(Compare-Object -ReferenceObject $expectedManifestKeys `
            -DifferenceObject $manifestKeys -CaseSensitive).Count -ne 0 -or
        [string]$manifest.schema_version -cne 'dawnstrike.vercel_package_manifest.v1' -or
        [string]$manifest.generated_runtime_authority_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $null -eq $manifest.files -or $null -eq $manifest.directories -or
        $null -eq $manifest.bindings) {
        throw 'Vercel package manifest schema is invalid.'
    }

    $expectedDirectories = @(
        $Directories | Where-Object { [string]$_.relative_path -cne '.vercel/output' } |
            ForEach-Object { [string]$_.relative_path } | Sort-Object -CaseSensitive
    )
    $manifestDirectories = @($manifest.directories | ForEach-Object { [string]$_ } |
        Sort-Object -CaseSensitive)
    if ($expectedDirectories.Count -ne $manifestDirectories.Count) {
        throw 'Frozen Vercel package directory set differs from its sealed manifest.'
    }
    for ($index = 0; $index -lt $expectedDirectories.Count; $index++) {
        if ($expectedDirectories[$index] -cne $manifestDirectories[$index]) {
            throw 'Frozen Vercel package directory set differs from its sealed manifest.'
        }
    }

    $manifestFiles = @($manifest.files.PSObject.Properties)
    if ($manifestFiles.Count -ne $Files.Count) {
        throw 'Frozen Vercel package file set differs from its sealed manifest.'
    }
    $frozenByPath = [Collections.Generic.Dictionary[string, object]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($file in @($Files)) {
        if ($frozenByPath.ContainsKey([string]$file.relative_path)) {
            throw 'Frozen Vercel package contains a duplicate deployment path.'
        }
        $frozenByPath.Add([string]$file.relative_path, $file)
    }
    foreach ($property in $manifestFiles) {
        $name = [string]$property.Name
        if (-not $frozenByPath.ContainsKey($name)) {
            throw 'Frozen Vercel package file set differs from its sealed manifest.'
        }
        $file = $frozenByPath[$name]
        if ([string]$property.Value.sha256 -cne [string]$file.sha256 -or
            [long]$property.Value.size -ne [long]$file.length) {
            throw "Frozen Vercel package bytes differ from the sealed manifest for $name."
        }
    }
    return [pscustomobject]@{
        sha256 = $manifestHash
        manifest = $manifest
    }
}

function Open-VercelDeploymentPackageBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedPackageManifestSha256,
        [ValidateRange(1, 20000)][int]$MaximumFileCount = 10000,
        [ValidateRange(1048576, 2147483647)][long]$MaximumFileBytes = 67108864,
        [ValidateRange(1048576, 2147483647)][long]$MaximumTotalBytes = 536870912
    )

    $root = [IO.Path]::GetFullPath($StageRoot).TrimEnd('\')
    $packageRoot = [IO.Path]::GetFullPath((Join-Path $root '.vercel\output')).TrimEnd('\')
    $packageManifestPath = [IO.Path]::GetFullPath(
        (Join-Path $root 'vercel-package-manifest.json')
    )
    Assert-VercelContainedPathNoReparse -Root (Split-Path -Parent $root) -Target $root `
        -Label 'Vercel deploy package root'
    Assert-VercelContainedPathNoReparse -Root $root -Target $packageRoot `
        -Label 'Vercel prebuilt output root'
    $directoryRecords = @()
    $outputFileRecords = @()
    $providerFileRecords = @()
    $rootLease = $null
    $manifestLease = $null
    $manifestStream = $null
    $manifestBytes = $null
    $watch = $null
    try {
        if (-not (Get-Command Open-DawnstrikeStateBoundaryPath -ErrorAction SilentlyContinue)) {
            throw 'Vercel deploy package boundary requires the protected namespace helper.'
        }
        $rootLease = Open-DawnstrikeStateBoundaryPath `
            -Path $packageRoot -Label 'Vercel deploy package root namespace'
        $manifestLease = Open-DawnstrikeStateBoundaryPath `
            -Path $packageManifestPath -Label 'Vercel package manifest namespace'
        $manifestStream = [IO.File]::Open(
            $packageManifestPath, [IO.FileMode]::Open, [IO.FileAccess]::Read,
            [IO.FileShare]::Read
        )
        [byte[]]$manifestBytes = Read-VercelDeploymentBoundedStreamBytes `
            -Stream $manifestStream -MaximumBytes 16777216 -Label 'Vercel package manifest'
        $directories = @(
            (Get-Item -LiteralPath $packageRoot -Force -ErrorAction Stop)
            Get-ChildItem -LiteralPath $packageRoot -Directory -Recurse -Force -ErrorAction Stop
        ) | Sort-Object FullName
        foreach ($directory in $directories) {
            if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Vercel deploy package contains a reparse directory.'
            }
            $directoryRecords += [pscustomobject]@{
                path = [string]$directory.FullName
                relative_path = if ([string]::Equals(
                    [string]$directory.FullName, $packageRoot, [StringComparison]::OrdinalIgnoreCase
                )) { '.vercel/output' } else {
                    '.vercel/output/' +
                        [string]$directory.FullName.Substring($packageRoot.Length + 1).Replace('\', '/')
                }
                # Directory namespace changes cannot alter the explicit CAS
                # request: every provider file comes from retained file bytes.
                identity = ''
                last_write_file_time = 0L
                lease = $null
            }
        }
        [long]$totalBytes = 0
        foreach ($file in @(
            Get-ChildItem -LiteralPath $packageRoot -File -Recurse -Force -ErrorAction Stop |
                Sort-Object FullName
        )) {
            if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Vercel deploy package contains a reparse file.'
            }
            $stream = [IO.File]::Open(
                [string]$file.FullName,
                [IO.FileMode]::Open,
                [IO.FileAccess]::Read,
                [IO.FileShare]::Read
            )
            if ($providerFileRecords.Count -ge $MaximumFileCount) {
                $stream.Dispose()
                throw 'Vercel deploy package exceeds the bounded file-count limit.'
            }
            [byte[]]$data = Read-VercelDeploymentBoundedStreamBytes `
                -Stream $stream -MaximumBytes $MaximumFileBytes `
                -Label ('Vercel deploy package file ' + [string]$file.FullName)
            $sha1 = Get-VercelDeploymentBytesHash -Bytes $data -Algorithm SHA1
            $sha256 = Get-VercelDeploymentBytesHash -Bytes $data -Algorithm SHA256
            $relativePath = '.vercel/output/' +
                [string]$file.FullName.Substring($packageRoot.Length + 1).Replace('\', '/')
            Assert-VercelDeploymentProviderRelativePath `
                -Path $relativePath -Label 'Vercel prebuilt output file'
            $record = [pscustomobject]@{
                path = [string]$file.FullName
                relative_path = $relativePath
                logical_sources = @($relativePath)
                target_relative_path = $relativePath
                origin = 'OUTPUT'
                route_bindings = @()
                length = [long]$stream.Length
                mode = 33206
                sha1 = $sha1
                sha256 = $sha256
                data = $data
                stream = $stream
            }
            $outputFileRecords += $record
            $providerFileRecords += $record
            $totalBytes += [long]$record.length
            if ($totalBytes -gt $MaximumTotalBytes) {
                throw 'Vercel deploy package exceeds the bounded aggregate byte limit.'
            }
        }
        if ($outputFileRecords.Count -lt 1) {
            throw 'Vercel deploy package boundary contains no files.'
        }
        $manifestAdmission = Assert-VercelFrozenPackageManifest `
            -ManifestBytes $manifestBytes `
            -ExpectedManifestSha256 $ExpectedPackageManifestSha256 `
            -Directories $directoryRecords -Files $outputFileRecords
        $manifestHash = [string]$manifestAdmission.sha256

        # Vercel CLI 59.11.2 deploy --prebuilt uploads both .vercel/output/**
        # and every distinct project-relative filePathMap target referenced by
        # the retained .vc-config.json files. The sealed package manifest binds
        # those source -> target mappings. Re-open every target once under a
        # deny-write/delete FileStream, compare its retained bytes to the sealed
        # binding, and build the provider request only from this exact union.
        $bindings = @($manifestAdmission.manifest.bindings)
        if ($bindings.Count -lt 1 -or $bindings.Count -gt ($MaximumFileCount * 4)) {
            throw 'Vercel package manifest has an invalid bounded binding count.'
        }
        $bindingGroups = [Collections.Generic.Dictionary[string, object]]::new(
            [StringComparer]::Ordinal
        )
        $foldedBindingTargets = [Collections.Generic.Dictionary[string, string]]::new(
            [StringComparer]::OrdinalIgnoreCase
        )
        foreach ($binding in $bindings) {
            $bindingKeys = @(
                $binding.PSObject.Properties | ForEach-Object { [string]$_.Name } |
                    Sort-Object -CaseSensitive
            )
            $expectedBindingKeys = @('route', 'sha256', 'size', 'source', 'target') |
                Sort-Object -CaseSensitive
            if (@(Compare-Object -ReferenceObject $expectedBindingKeys `
                    -DifferenceObject $bindingKeys -CaseSensitive).Count -ne 0) {
                throw 'Vercel package manifest contains an invalid binding schema.'
            }
            $route = [string]$binding.route
            $source = [string]$binding.source
            $target = [string]$binding.target
            Assert-VercelDeploymentProviderRelativePath `
                -Path $route -Label 'Vercel package binding route'
            Assert-VercelDeploymentProviderRelativePath `
                -Path $source -Label 'Vercel package binding logical source'
            Assert-VercelDeploymentProviderRelativePath `
                -Path $target -Label 'Vercel package binding target'
            $bindingSha256 = [string]$binding.sha256
            [long]$bindingSize = [long]$binding.size
            if ($bindingSha256 -cnotmatch '^[0-9a-f]{64}$' -or $bindingSize -lt 0 -or
                $bindingSize -gt $MaximumFileBytes) {
                throw 'Vercel package manifest contains an invalid binding byte identity.'
            }
            $existingTargetSpelling = $null
            if ($foldedBindingTargets.TryGetValue($target, [ref]$existingTargetSpelling) -and
                $existingTargetSpelling -cne $target) {
                throw 'Vercel package manifest contains case-colliding binding targets.'
            }
            if (-not $bindingGroups.ContainsKey($target)) {
                $foldedBindingTargets[$target] = $target
                $bindingGroups.Add($target, [pscustomobject]@{
                    target = $target
                    sha256 = $bindingSha256
                    size = $bindingSize
                    pairs = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                    routes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                    sources = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
                })
            }
            $group = $bindingGroups[$target]
            if ([string]$group.sha256 -cne $bindingSha256 -or
                [long]$group.size -ne $bindingSize) {
                throw 'Vercel package manifest gives one binding target conflicting byte identities.'
            }
            $pair = $route + "`0" + $source
            if (-not $group.pairs.Add($pair)) {
                throw 'Vercel package manifest contains a duplicate route/source binding.'
            }
            $null = $group.routes.Add($route)
            $null = $group.sources.Add($source)
        }

        $providerByPath = [Collections.Generic.Dictionary[string, object]]::new(
            [StringComparer]::Ordinal
        )
        $providerPathSpellings = [Collections.Generic.Dictionary[string, string]]::new(
            [StringComparer]::OrdinalIgnoreCase
        )
        foreach ($record in @($providerFileRecords)) {
            $providerByPath.Add([string]$record.relative_path, $record)
            $providerPathSpellings.Add([string]$record.relative_path, [string]$record.relative_path)
        }
        foreach ($group in @($bindingGroups.Values | Sort-Object target -CaseSensitive)) {
            $target = [string]$group.target
            $existingSpelling = $null
            if ($providerPathSpellings.TryGetValue($target, [ref]$existingSpelling) -and
                $existingSpelling -cne $target) {
                throw 'Vercel deployment union contains case-colliding provider paths.'
            }
            if ($providerByPath.ContainsKey($target)) {
                $existing = $providerByPath[$target]
                if ([string]$existing.sha256 -cne [string]$group.sha256 -or
                    [long]$existing.length -ne [long]$group.size) {
                    throw 'Vercel filePathMap target conflicts with a prebuilt output provider path.'
                }
                $existing.origin = 'OUTPUT_AND_FILE_PATH_MAP'
                $existing.logical_sources = @(
                    @($existing.logical_sources) + @($group.sources) |
                        Sort-Object -Unique -CaseSensitive
                )
                $existing.route_bindings = @($group.routes | Sort-Object -CaseSensitive)
                continue
            }
            if ($providerFileRecords.Count -ge $MaximumFileCount) {
                throw 'Vercel deployment union exceeds the bounded file-count limit.'
            }
            $targetPath = [IO.Path]::GetFullPath((Join-Path $root ($target -replace '/', '\')))
            Assert-VercelContainedPathNoReparse -Root $root -Target $targetPath `
                -Label 'Vercel filePathMap target'
            $normalizedTarget = (Get-VercelRelativePath -Root $root -Path $targetPath).Replace('\', '/')
            if ($normalizedTarget -cne $target) {
                throw 'Vercel filePathMap target is not canonical beneath the stage root.'
            }
            $targetItem = Get-Item -LiteralPath $targetPath -Force -ErrorAction Stop
            if ($targetItem.PSIsContainer -or
                ($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Vercel filePathMap target is not an ordinary file.'
            }
            $stream = [IO.File]::Open(
                $targetPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
            )
            [byte[]]$data = Read-VercelDeploymentBoundedStreamBytes `
                -Stream $stream -MaximumBytes $MaximumFileBytes `
                -Label ('Vercel retained filePathMap target ' + $target)
            $sha256 = Get-VercelDeploymentBytesHash -Bytes $data -Algorithm SHA256
            if ([long]$stream.Length -ne [long]$group.size -or
                $sha256 -cne [string]$group.sha256) {
                [Array]::Clear($data, 0, $data.Length)
                $stream.Dispose()
                throw 'Vercel retained filePathMap target bytes differ from the sealed binding.'
            }
            $record = [pscustomobject]@{
                path = $targetPath
                relative_path = $target
                logical_sources = @($group.sources | Sort-Object -CaseSensitive)
                target_relative_path = $target
                origin = 'FILE_PATH_MAP'
                route_bindings = @($group.routes | Sort-Object -CaseSensitive)
                length = [long]$stream.Length
                mode = 33206
                sha1 = Get-VercelDeploymentBytesHash -Bytes $data -Algorithm SHA1
                sha256 = $sha256
                data = $data
                stream = $stream
            }
            $providerFileRecords += $record
            $providerByPath.Add($target, $record)
            $providerPathSpellings.Add($target, $target)
            $totalBytes += [long]$record.length
            if ($totalBytes -gt $MaximumTotalBytes) {
                throw 'Vercel deployment union exceeds the bounded aggregate byte limit.'
            }
        }

        $providerDirectories = @(
            Get-VercelDeploymentProviderDirectories -Files $providerFileRecords
        )
        $caseFoldedPaths = [Collections.Generic.HashSet[string]]::new(
            [StringComparer]::OrdinalIgnoreCase
        )
        foreach ($directory in $providerDirectories) {
            if (-not $caseFoldedPaths.Add([string]$directory)) {
                throw 'Vercel deployment union contains case-colliding provider directories.'
            }
        }
        foreach ($record in @($providerFileRecords)) {
            if (-not $caseFoldedPaths.Add([string]$record.relative_path)) {
                throw 'Vercel deployment union contains a provider file/directory collision.'
            }
        }
        $frozenRuntimeAuthoritySha256 = Assert-VercelFrozenGeneratedRuntimeAuthority `
            -Boundary ([pscustomobject]@{ files = @($providerFileRecords) })
        if ($frozenRuntimeAuthoritySha256 -cne
            [string]$manifestAdmission.manifest.generated_runtime_authority_sha256) {
            throw 'Frozen Vercel generated-runtime authority differs from the sealed package manifest.'
        }
        $apiFiles = @(
            $providerFileRecords | Sort-Object relative_path -CaseSensitive | ForEach-Object {
                [pscustomobject][ordered]@{
                    file = [string]$_.relative_path
                    sha = [string]$_.sha1
                    size = [long]$_.length
                    mode = [int]$_.mode
                }
            }
        )
        $uploadsBySha1 = [Collections.Generic.Dictionary[string, object]]::new(
            [StringComparer]::Ordinal
        )
        foreach ($record in @($providerFileRecords)) {
            if ($uploadsBySha1.ContainsKey([string]$record.sha1)) {
                $existing = $uploadsBySha1[[string]$record.sha1]
                if ([string]$existing.sha256 -cne [string]$record.sha256 -or
                    [long]$existing.length -ne [long]$record.length) {
                    throw 'Vercel deploy package contains a conflicting SHA-1 content identity.'
                }
            }
            else { $uploadsBySha1.Add([string]$record.sha1, $record) }
        }
        $snapshot = Get-VercelDeploymentBoundarySnapshotHash `
            -Directories $directoryRecords -Files $providerFileRecords
        $mapHash = Get-VercelDeploymentFrozenMapHash -Files $providerFileRecords
        return [pscustomobject]@{
            root = $root
            package_root = $packageRoot
            package_root_relative_path = '.vercel/output'
            root_namespace_lease = $rootLease.handle
            manifest_namespace_lease = $manifestLease.handle
            manifest_stream = $manifestStream
            manifest_bytes = $manifestBytes
            package_manifest_sha256 = $manifestHash
            generated_runtime_authority_sha256 = $frozenRuntimeAuthoritySha256
            directories = @($directoryRecords)
            provider_directories = @($providerDirectories)
            output_files = @($outputFileRecords)
            files = @($providerFileRecords)
            api_files = @($apiFiles)
            unique_uploads = @($uploadsBySha1.Values | Sort-Object sha1)
            file_count = $providerFileRecords.Count
            output_file_count = $outputFileRecords.Count
            file_path_map_target_count = $providerFileRecords.Count - $outputFileRecords.Count
            total_bytes = $totalBytes
            watcher = $null
            snapshot_sha256 = $snapshot
            map_sha256 = $mapHash
        }
    }
    catch {
        if ($null -ne $watch) { $watch.Dispose() }
        foreach ($record in @($providerFileRecords)) {
            if ($null -ne $record.data) { [Array]::Clear($record.data, 0, $record.data.Length) }
            if ($null -ne $record.stream) { $record.stream.Dispose() }
        }
        foreach ($record in @($directoryRecords)) {
            if ($null -ne $record.lease) { $record.lease.Dispose() }
        }
        if ($null -ne $manifestBytes) { [Array]::Clear($manifestBytes, 0, $manifestBytes.Length) }
        if ($null -ne $manifestStream) { $manifestStream.Dispose() }
        if ($null -ne $manifestLease -and $null -ne $manifestLease.handle) {
            $manifestLease.handle.Dispose()
        }
        if ($null -ne $rootLease -and $null -ne $rootLease.handle) { $rootLease.handle.Dispose() }
        throw
    }
}

function Assert-VercelFrozenDeploymentFileStable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$File)

    [byte[]]$bytes = Read-VercelDeploymentBoundedStreamBytes `
        -Stream $File.stream -MaximumBytes ([long]$File.length) `
        -Label ('Retained Vercel deploy file ' + [string]$File.relative_path)
    $sha1 = Get-VercelDeploymentBytesHash -Bytes $bytes -Algorithm SHA1
    $sha256 = Get-VercelDeploymentBytesHash -Bytes $bytes -Algorithm SHA256
    if ([long]$File.stream.Length -ne [long]$File.length -or
        $sha1 -cne [string]$File.sha1 -or $sha256 -cne [string]$File.sha256 -or
        (Get-VercelDeploymentBytesHash -Bytes ([byte[]]$File.data) -Algorithm SHA1) -cne
            [string]$File.sha1 -or
        (Get-VercelDeploymentBytesHash -Bytes ([byte[]]$File.data) -Algorithm SHA256) -cne
            [string]$File.sha256) {
        throw 'Vercel deploy package retained handle or frozen bytes changed after admission.'
    }
    return $true
}

function Assert-VercelDeploymentPackageBoundaryStable {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Boundary)

    foreach ($record in @($Boundary.files)) {
        $null = Assert-VercelFrozenDeploymentFileStable -File $record
    }
    $manifestHash = Get-VercelDeploymentBytesHash `
        -Bytes (Read-VercelDeploymentBoundedStreamBytes `
            -Stream $Boundary.manifest_stream -MaximumBytes 16777216 `
            -Label 'Retained Vercel package manifest') `
        -Algorithm SHA256
    if ($manifestHash -cne [string]$Boundary.package_manifest_sha256) {
        throw 'Vercel package manifest bytes changed after admission.'
    }
    $snapshot = Get-VercelDeploymentBoundarySnapshotHash `
        -Directories @($Boundary.directories) -Files @($Boundary.files)
    if ($snapshot -cne [string]$Boundary.snapshot_sha256) {
        throw 'Vercel deploy package snapshot identity changed after admission.'
    }
    return $snapshot
}

function Close-VercelDeploymentPackageBoundary {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Boundary)

    foreach ($record in @($Boundary.files)) {
        if ($null -ne $record.data) { [Array]::Clear($record.data, 0, $record.data.Length) }
        if ($null -ne $record.stream) { $record.stream.Dispose() }
    }
    if ($null -ne $Boundary.manifest_bytes) {
        [Array]::Clear($Boundary.manifest_bytes, 0, $Boundary.manifest_bytes.Length)
    }
    if ($null -ne $Boundary.manifest_stream) { $Boundary.manifest_stream.Dispose() }
    if ($null -ne $Boundary.manifest_namespace_lease) {
        $Boundary.manifest_namespace_lease.Dispose()
    }
    foreach ($record in @($Boundary.directories)) {
        if ($null -ne $record.lease) { $record.lease.Dispose() }
    }
    if ($null -ne $Boundary.root_namespace_lease) { $Boundary.root_namespace_lease.Dispose() }
}

function Throw-VercelJsonParseFailure {
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][string]$Message
    )
    throw "Invalid JSON at offset $($State.index): $Message"
}

function Skip-VercelJsonWhitespace {
    param([Parameter(Mandatory = $true)][hashtable]$State)
    while ($State.index -lt $State.text.Length -and
        " `t`r`n".IndexOf($State.text[$State.index]) -ge 0) {
        $State.index++
    }
}

function Test-VercelJsonTakeCharacter {
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][char]$Character
    )
    if ($State.index -lt $State.text.Length -and
        $State.text[$State.index] -ceq $Character) {
        $State.index++
        return $true
    }
    return $false
}

function Read-VercelJsonStringToken {
    param([Parameter(Mandatory = $true)][hashtable]$State)
    if (-not (Test-VercelJsonTakeCharacter -State $State -Character '"')) {
        Throw-VercelJsonParseFailure -State $State -Message 'string is missing'
    }
    $value = [Text.StringBuilder]::new()
    while ($State.index -lt $State.text.Length) {
        [char]$current = $State.text[$State.index]
        $State.index++
        if ($current -ceq '"') { return $value.ToString() }
        if ([int]$current -lt 32) {
            Throw-VercelJsonParseFailure -State $State -Message 'control character in string'
        }
        if ($current -cne '\') {
            $null = $value.Append($current)
            continue
        }
        if ($State.index -ge $State.text.Length) {
            Throw-VercelJsonParseFailure -State $State -Message 'truncated string escape'
        }
        [char]$escaped = $State.text[$State.index]
        $State.index++
        switch ([string]$escaped) {
            '"' { $null = $value.Append('"') }
            '\' { $null = $value.Append('\') }
            '/' { $null = $value.Append('/') }
            'b' { $null = $value.Append([char]8) }
            'f' { $null = $value.Append([char]12) }
            'n' { $null = $value.Append("`n") }
            'r' { $null = $value.Append("`r") }
            't' { $null = $value.Append("`t") }
            'u' {
                if ($State.index + 4 -gt $State.text.Length) {
                    Throw-VercelJsonParseFailure -State $State -Message 'truncated unicode escape'
                }
                $hex = $State.text.Substring($State.index, 4)
                if ($hex -cnotmatch '^[0-9a-fA-F]{4}$') {
                    Throw-VercelJsonParseFailure -State $State -Message 'invalid unicode escape'
                }
                $null = $value.Append([char][Convert]::ToInt32($hex, 16))
                $State.index += 4
            }
            default {
                Throw-VercelJsonParseFailure -State $State -Message 'invalid string escape'
            }
        }
    }
    Throw-VercelJsonParseFailure -State $State -Message 'unterminated string'
}

function Read-VercelJsonNumberToken {
    param([Parameter(Mandatory = $true)][hashtable]$State)
    $start = $State.index
    $null = Test-VercelJsonTakeCharacter -State $State -Character '-'
    if (-not (Test-VercelJsonTakeCharacter -State $State -Character '0')) {
        if ($State.index -ge $State.text.Length -or
            $State.text[$State.index] -lt '1' -or $State.text[$State.index] -gt '9') {
            Throw-VercelJsonParseFailure -State $State -Message 'number digits are missing'
        }
        while ($State.index -lt $State.text.Length -and
            $State.text[$State.index] -ge '0' -and $State.text[$State.index] -le '9') {
            $State.index++
        }
    }
    if (Test-VercelJsonTakeCharacter -State $State -Character '.') {
        $fractionStart = $State.index
        while ($State.index -lt $State.text.Length -and
            $State.text[$State.index] -ge '0' -and $State.text[$State.index] -le '9') {
            $State.index++
        }
        if ($State.index -eq $fractionStart) {
            Throw-VercelJsonParseFailure -State $State -Message 'fraction digits are missing'
        }
    }
    if ($State.index -lt $State.text.Length -and
        $State.text[$State.index] -in @('e', 'E')) {
        $State.index++
        if ($State.index -lt $State.text.Length -and
            $State.text[$State.index] -in @('+', '-')) { $State.index++ }
        $exponentStart = $State.index
        while ($State.index -lt $State.text.Length -and
            $State.text[$State.index] -ge '0' -and $State.text[$State.index] -le '9') {
            $State.index++
        }
        if ($State.index -eq $exponentStart) {
            Throw-VercelJsonParseFailure -State $State -Message 'exponent digits are missing'
        }
    }
    if ($State.index -eq $start) {
        Throw-VercelJsonParseFailure -State $State -Message 'invalid number'
    }
}

function Read-VercelJsonValueToken {
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [ValidateRange(0, 128)][int]$Depth = 0
    )
    if ($Depth -ge 128) {
        Throw-VercelJsonParseFailure -State $State -Message 'nesting depth exceeded'
    }
    Skip-VercelJsonWhitespace -State $State
    if ($State.index -ge $State.text.Length) {
        Throw-VercelJsonParseFailure -State $State -Message 'value is missing'
    }
    [char]$current = $State.text[$State.index]
    if ($current -ceq '"') { $null = Read-VercelJsonStringToken -State $State; return }
    if ($current -ceq '{') {
        $State.index++
        $keys = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        Skip-VercelJsonWhitespace -State $State
        if (Test-VercelJsonTakeCharacter -State $State -Character '}') { return }
        while ($true) {
            Skip-VercelJsonWhitespace -State $State
            $key = Read-VercelJsonStringToken -State $State
            if (-not $keys.Add($key)) {
                Throw-VercelJsonParseFailure -State $State -Message "duplicate object key: $key"
            }
            Skip-VercelJsonWhitespace -State $State
            if (-not (Test-VercelJsonTakeCharacter -State $State -Character ':')) {
                Throw-VercelJsonParseFailure -State $State -Message "expected ':'"
            }
            Read-VercelJsonValueToken -State $State -Depth ($Depth + 1)
            Skip-VercelJsonWhitespace -State $State
            if (Test-VercelJsonTakeCharacter -State $State -Character '}') { return }
            if (-not (Test-VercelJsonTakeCharacter -State $State -Character ',')) {
                Throw-VercelJsonParseFailure -State $State -Message "expected ','"
            }
        }
    }
    if ($current -ceq '[') {
        $State.index++
        Skip-VercelJsonWhitespace -State $State
        if (Test-VercelJsonTakeCharacter -State $State -Character ']') { return }
        while ($true) {
            Read-VercelJsonValueToken -State $State -Depth ($Depth + 1)
            Skip-VercelJsonWhitespace -State $State
            if (Test-VercelJsonTakeCharacter -State $State -Character ']') { return }
            if (-not (Test-VercelJsonTakeCharacter -State $State -Character ',')) {
                Throw-VercelJsonParseFailure -State $State -Message "expected ','"
            }
        }
    }
    foreach ($literal in @('true', 'false', 'null')) {
        if ($State.text.Length - $State.index -ge $literal.Length -and
            $State.text.Substring($State.index, $literal.Length) -ceq $literal) {
            $State.index += $literal.Length
            return
        }
    }
    Read-VercelJsonNumberToken -State $State
}

function Assert-VercelJsonObjectKeysUnique {
    param([Parameter(Mandatory = $true)][string]$RawJson)
    try {
        $state = @{ text = $RawJson; index = 0 }
        Read-VercelJsonValueToken -State $state
        Skip-VercelJsonWhitespace -State $state
        if ($state.index -ne $state.text.Length) {
            Throw-VercelJsonParseFailure -State $state -Message 'trailing JSON content'
        }
    }
    catch {
        throw "Vercel JSON is invalid or contains duplicate object keys: $($_.Exception.Message)"
    }
}

function ConvertTo-VercelNativeArgument {
    param([AllowEmptyString()][Parameter(Mandatory = $true)][string]$Value)

    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $quoted = [Text.StringBuilder]::new()
    $null = $quoted.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            $null = $quoted.Append(('\' * (($backslashes * 2) + 1)))
            $null = $quoted.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            $null = $quoted.Append(('\' * $backslashes))
            $backslashes = 0
        }
        $null = $quoted.Append($character)
    }
    if ($backslashes -gt 0) { $null = $quoted.Append(('\' * ($backslashes * 2))) }
    $null = $quoted.Append('"')
    return $quoted.ToString()
}

function Invoke-VercelGitText {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = Get-VercelApprovedGitPath
    $startInfo.Arguments = (
        @(
            '-c', 'core.fsmonitor=false',
            '-c', 'core.hooksPath=NUL',
            '-c', 'protocol.ext.allow=never',
            '-c', 'submodule.recurse=false',
            '-C', $Root
        ) + @($Arguments) |
            ForEach-Object { ConvertTo-VercelNativeArgument -Value ([string]$_) }
    ) -join ' '
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($name in @($startInfo.EnvironmentVariables.Keys | ForEach-Object { [string]$_ })) {
        if ($name -like 'GIT_*') { $startInfo.EnvironmentVariables.Remove($name) }
    }
    $startInfo.EnvironmentVariables['GIT_CONFIG_NOSYSTEM'] = '1'
    $startInfo.EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "$Label could not start Git." }
    $bytes = $null
    try {
        $bytes = [System.IO.MemoryStream]::new()
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($bytes)
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            try { $process.Kill() } catch { }
            throw "$Label timed out."
        }
        $null = [System.Threading.Tasks.Task]::WaitAll(@($stdoutTask, $stderrTask), 30000)
        if ($process.ExitCode -ne 0) { throw "$Label failed: $($stderrTask.Result)" }
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        return $utf8.GetString($bytes.ToArray()).Trim()
    }
    finally {
        if ($null -ne $bytes) { $bytes.Dispose() }
        $process.Dispose()
    }
}

function Get-VercelIgnoredPublicationPaths {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$AllowedStageRoot = ""
    )
    $allowedPrefix = ""
    if ($AllowedStageRoot) {
        $allowedPrefix = [System.IO.Path]::GetFullPath($AllowedStageRoot).TrimEnd('\') + '\'
    }
    $ignored = Invoke-VercelGitText `
        -Root $Root `
        -Arguments @("ls-files", "--others", "--ignored", "--exclude-standard", "-z") `
        -Label "Ignored publication artifact verification"
    return @(
        ([string]$ignored).Split(
            [char[]]@([char]0),
            [System.StringSplitOptions]::RemoveEmptyEntries
        ) |
            Where-Object {
                $relative = [string]$_
                $full = [System.IO.Path]::GetFullPath((Join-Path $Root $relative))
                $allowed = $allowedPrefix -and $full.StartsWith(
                    $allowedPrefix,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
                if ($allowed) { return $false }
                $name = [System.IO.Path]::GetFileName($relative).ToLowerInvariant()
                $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
                $extension -in @(
                    ".ps1", ".psm1", ".py", ".pyc", ".pyd", ".dll", ".exe",
                    ".com", ".bat", ".cmd", ".sh", ".pth"
                ) -or $name -in @("sitecustomize.py", "usercustomize.py")
            }
    )
}

function Assert-VercelLocalGitConfigurationSafe {
    param([Parameter(Mandatory = $true)][string]$Root)
    $raw = Invoke-VercelGitText `
        -Root $Root `
        -Arguments @('config', '--local', '--no-includes', '--name-only', '--null', '--list') `
        -Label 'Publication local Git config verification'
    $seen = @{}
    $fixed = @{
        'core.repositoryformatversion' = '0'
        'core.filemode' = 'false'
        'core.bare' = 'false'
        'core.logallrefupdates' = 'true'
        'core.symlinks' = 'false'
        'core.ignorecase' = 'true'
        'remote.origin.url' = 'https://github.com/mattfren/DawnStrike.git'
        'remote.origin.fetch' = '+refs/heads/*:refs/remotes/origin/*'
        'lfs.repositoryformatversion' = '0'
    }
    $nonExecuting = @('user.email', 'user.name')
    foreach ($record in ([string]$raw).Split(
        [char[]]@([char]0), [System.StringSplitOptions]::RemoveEmptyEntries
    )) {
        $key = ([string]$record).ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw 'Publication local Git config contains a duplicate key.' }
        $seen[$key] = $true
        $value = Invoke-VercelGitText `
            -Root $Root `
            -Arguments @('config', '--local', '--no-includes', '--get-all', $key) `
            -Label "Publication local Git config value verification for $key"
        if ([string]$value -match "[`r`n]") {
            throw 'Publication local Git config contains a duplicate or multiline value.'
        }
        if ($fixed.ContainsKey($key)) {
            if ([string]$fixed[$key] -cne $value) {
                throw "Publication local Git config value is not governed: $key"
            }
            continue
        }
        if ($key -in $nonExecuting -and $value.Length -ge 1 -and $value.Length -le 512) {
            continue
        }
        if ($key -match '^branch\.([a-z0-9._/-]+)\.(remote|merge)$') {
            $branchName = $Matches[1]
            $field = $Matches[2]
            $expected = if ($field -eq 'remote') { 'origin' } else { "refs/heads/$branchName" }
            if ($value -cne $expected) {
                throw "Publication branch Git config value is not governed: $key"
            }
            continue
        }
        throw "Publication local Git config key is not governed: $key"
    }
}

function Get-VercelGitSourceContract {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$AllowedStageRoot = ""
    )
    Assert-VercelLocalGitConfigurationSafe -Root $Root
    $top = Invoke-VercelGitText -Root $Root -Arguments @("rev-parse", "--show-toplevel") `
        -Label "Publication Git root verification"
    if (-not [System.String]::Equals(
        [System.IO.Path]::GetFullPath($top).TrimEnd('\'),
        [System.IO.Path]::GetFullPath($Root).TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Publication Git root does not match the requested project root."
    }
    $head = (Invoke-VercelGitText -Root $Root -Arguments @("rev-parse", "HEAD") `
        -Label "Publication Git HEAD verification").ToLowerInvariant()
    $tree = (Invoke-VercelGitText -Root $Root -Arguments @("rev-parse", "HEAD^{tree}") `
        -Label "Publication Git tree verification").ToLowerInvariant()
    if ($head -notmatch '^[0-9a-f]{40}$' -or $tree -notmatch '^[0-9a-f]{40}$') {
        throw "Publication Git identity is invalid."
    }
    $status = Invoke-VercelGitText `
        -Root $Root `
        -Arguments @("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none") `
        -Label "Publication Git cleanliness verification"
    if ($status) {
        throw "Publication Git checkout is not clean."
    }
    $forbiddenIgnored = @(Get-VercelIgnoredPublicationPaths `
        -Root $Root `
        -AllowedStageRoot $AllowedStageRoot)
    if ($forbiddenIgnored.Count -gt 0) {
        throw "Publication Git checkout contains ignored executable or Python-startup artifacts: $($forbiddenIgnored -join ', ')"
    }
    return [pscustomobject]@{ head = $head; tree = $tree }
}

function Assert-VercelGitSourceStable {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree,
        [string]$AllowedStageRoot = ""
    )
    $actual = Get-VercelGitSourceContract -Root $Root -AllowedStageRoot $AllowedStageRoot
    if ($actual.head -ne $ExpectedSourceSha.ToLowerInvariant()) {
        throw "Publication source HEAD changed during staging or deployment."
    }
    if ($actual.tree -ne $ExpectedSourceTree.ToLowerInvariant()) {
        throw "Publication source Git tree changed during staging or deployment."
    }
    return $actual
}

function Write-VercelGitBlob {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    Assert-VercelLocalGitConfigurationSafe -Root $Root
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = Get-VercelApprovedGitPath
    $startInfo.Arguments = (
        @(
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=NUL",
            "-c", "protocol.ext.allow=never",
            "-c", "submodule.recurse=false",
            "-C", $Root, "cat-file", "blob", "$Commit`:$RelativePath"
        ) |
            ForEach-Object { ConvertTo-VercelNativeArgument -Value ([string]$_) }
    ) -join " "
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($name in @($startInfo.EnvironmentVariables.Keys | ForEach-Object { [string]$_ })) {
        if ($name -like 'GIT_*') { $startInfo.EnvironmentVariables.Remove($name) }
    }
    $startInfo.EnvironmentVariables['GIT_CONFIG_NOSYSTEM'] = '1'
    $startInfo.EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Could not start Git blob extraction for $RelativePath." }
    try {
        $bytes = [System.IO.MemoryStream]::new()
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($bytes)
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            try { $process.Kill() } catch { }
            throw "Git blob extraction timed out for $RelativePath."
        }
        $null = [System.Threading.Tasks.Task]::WaitAll(@($stdoutTask, $stderrTask), 30000)
        $stderr = $stderrTask.Result
        if ($process.ExitCode -ne 0) {
            throw "Git blob extraction failed for $RelativePath`: $stderr"
        }
        [System.IO.File]::WriteAllBytes($Destination, $bytes.ToArray())
        $bytes.Dispose()
    }
    finally { $process.Dispose() }
}

function Get-VercelFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Expected publication file is missing: $Path"
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Get-VercelUtf8TextSha256 {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
        )).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Assert-VercelGeneratedRuntimeObjectKeys {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string[]]$Expected,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($null -eq $Object) { throw "$Label is missing." }
    $actual = @(
        $Object.PSObject.Properties | ForEach-Object { [string]$_.Name } |
            Sort-Object -CaseSensitive
    )
    $expectedSorted = @($Expected | Sort-Object -CaseSensitive)
    if (@(Compare-Object -ReferenceObject $expectedSorted -DifferenceObject $actual `
            -CaseSensitive).Count -ne 0) {
        throw "$Label contains an unexpected schema."
    }
}

function Assert-VercelGeneratedRuntimeStringArray {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$AllowEmpty
    )
    $items = @($Value)
    if (-not $AllowEmpty -and $items.Count -lt 1) { throw "$Label is empty." }
    $sorted = @($items | ForEach-Object { [string]$_ } | Sort-Object -Unique -CaseSensitive)
    if ($items.Count -ne $sorted.Count) { throw "$Label contains a duplicate value." }
    for ($index = 0; $index -lt $items.Count; $index++) {
        if ([string]$items[$index] -cne [string]$sorted[$index]) {
            throw "$Label is not canonical ordinal order."
        }
        Assert-VercelDeploymentProviderRelativePath `
            -Path ([string]$items[$index]) -Label $Label
    }
}

function Get-VercelGeneratedRuntimeMapSha256 {
    param([Parameter(Mandatory = $true)][object[]]$Records)
    $lines = @(
        foreach ($record in @($Records)) {
            @(
                [string]$record.kind
                [string]$record.target
                (@($record.logical_sources) -join ',')
                (@($record.route_bindings) -join ',')
                [string][long]$record.size
                [string]$record.sha256
            ) -join '|'
        }
    )
    return Get-VercelUtf8TextSha256 -Text ($lines -join "`n")
}

function Get-VercelGeneratedRuntimeAuthority {
    $repositoryRoot = Split-Path -Parent $PSScriptRoot
    $authorityPath = [IO.Path]::GetFullPath($script:VercelGeneratedRuntimeAuthorityPath)
    Assert-VercelContainedPathNoReparse -Root $repositoryRoot -Target $authorityPath `
        -Label 'Vercel generated-runtime authority'
    if (-not (Test-Path -LiteralPath $authorityPath -PathType Leaf)) {
        throw 'Vercel generated-runtime authority is missing.'
    }
    [byte[]]$bytes = [IO.File]::ReadAllBytes($authorityPath)
    $rawHash = Get-VercelDeploymentBytesHash -Bytes $bytes -Algorithm SHA256
    if ($rawHash -cne $script:VercelGeneratedRuntimeAuthoritySha256) {
        throw 'Vercel generated-runtime authority bytes changed.'
    }
    try {
        $strictUtf8 = New-Object Text.UTF8Encoding($false, $true)
        $raw = $strictUtf8.GetString($bytes)
    }
    catch { throw 'Vercel generated-runtime authority is not strict UTF-8.' }
    Assert-VercelJsonObjectKeysUnique -RawJson $raw
    try { $authority = $raw | ConvertFrom-Json }
    catch { throw 'Vercel generated-runtime authority is not valid JSON.' }
    Assert-VercelGeneratedRuntimeObjectKeys -Object $authority -Expected @(
        'authority_contract_sha256', 'broker_execution_enabled', 'generated_files',
        'generated_map_sha256', 'node', 'python_runtime', 'research_only',
        'schema_version', 'uv', 'vendor_files', 'vendor_map_sha256', 'vercel_cli'
    ) -Label 'Vercel generated-runtime authority'
    if ([string]$authority.schema_version -cne 'dawnstrike.vercel_generated_runtime_authority.v1' -or
        [string]$authority.python_runtime -cne 'python3.13' -or
        $authority.research_only -isnot [bool] -or [bool]$authority.research_only -ne $true -or
        $authority.broker_execution_enabled -isnot [bool] -or
        [bool]$authority.broker_execution_enabled -ne $false) {
        throw 'Vercel generated-runtime authority safety contract is invalid.'
    }
    Assert-VercelGeneratedRuntimeObjectKeys -Object $authority.node -Expected @(
        'archive_sha256', 'archive_size', 'executable_sha256', 'executable_size', 'version'
    ) -Label 'Vercel generated-runtime Node authority'
    Assert-VercelGeneratedRuntimeObjectKeys -Object $authority.uv -Expected @(
        'executable_sha256', 'executable_size', 'version'
    ) -Label 'Vercel generated-runtime uv authority'
    Assert-VercelGeneratedRuntimeObjectKeys -Object $authority.vercel_cli -Expected @(
        'entry_sha256', 'tree_file_count', 'tree_sha256', 'version'
    ) -Label 'Vercel generated-runtime CLI authority'
    if ([string]$authority.node.version -cne '24.20.0' -or
        [long]$authority.node.archive_size -ne 37539751 -or
        [string]$authority.node.archive_sha256 -cne
            '6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba' -or
        [long]$authority.node.executable_size -ne 93381448 -or
        [string]$authority.node.executable_sha256 -cne
            '5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5' -or
        [string]$authority.uv.version -cne '0.12.9' -or
        [long]$authority.uv.executable_size -ne 41386496 -or
        [string]$authority.uv.executable_sha256 -cne
            'b5d230c79ffa3629422f48bfce0766e9827769608a79bbb4e4e540081f59d97c' -or
        [string]$authority.vercel_cli.version -cne '59.11.2' -or
        [long]$authority.vercel_cli.tree_file_count -ne 7131 -or
        [string]$authority.vercel_cli.tree_sha256 -cne
            '3bfb7509c4bf6a8fec920566c290a385c8160b9851b2350a655f0fd8b6c9e069' -or
        [string]$authority.vercel_cli.entry_sha256 -cne
            '2dd6e7c273a24bf4317af867d9b7bacb4db35487b42ea77912e2e7c33fa0c152') {
        throw 'Vercel generated-runtime authority toolchain identity changed.'
    }

    $generated = @($authority.generated_files)
    $vendor = @($authority.vendor_files)
    if ($generated.Count -ne 8 -or $vendor.Count -lt 1 -or $vendor.Count -gt 10000) {
        throw 'Vercel generated-runtime authority has an unexpected closed-world count.'
    }
    $targets = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $foldedTargets = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    foreach ($record in @($generated) + @($vendor)) {
        Assert-VercelGeneratedRuntimeObjectKeys -Object $record -Expected @(
            'kind', 'logical_sources', 'route_bindings', 'sha256', 'size', 'target'
        ) -Label 'Vercel generated-runtime record'
        $target = [string]$record.target
        Assert-VercelDeploymentProviderRelativePath -Path $target `
            -Label 'Vercel generated-runtime target'
        if (-not $targets.Add($target) -or -not $foldedTargets.Add($target)) {
            throw 'Vercel generated-runtime authority contains duplicate or case-colliding targets.'
        }
        Assert-VercelGeneratedRuntimeStringArray -Value @($record.logical_sources) `
            -Label 'Vercel generated-runtime logical source'
        Assert-VercelGeneratedRuntimeStringArray -Value @($record.route_bindings) `
            -Label 'Vercel generated-runtime route binding' -AllowEmpty
        if ([string]$record.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [long]$record.size -lt 0 -or [string][long]$record.size -cne [string]$record.size) {
            throw 'Vercel generated-runtime authority contains an invalid byte identity.'
        }
    }

    $routeNames = @('api/health.func', 'api/readiness.func')
    $expectedGenerated = [ordered]@{
        '.python-version' = @('runtime_support', @('.python-version'), $routeNames)
        '.vercel/output/config.json' = @(
            'deployment_config', @('.vercel/output/config.json'), @()
        )
        '.vercel/output/functions/api/health.func/.vc-config.json' = @(
            'route_config', @('.vercel/output/functions/api/health.func/.vc-config.json'),
            @('api/health.func')
        )
        '.vercel/output/functions/api/health.func/vc__handler__python.py' = @(
            'route_wrapper',
            @('.vercel/output/functions/api/health.func/vc__handler__python.py'),
            @('api/health.func')
        )
        '.vercel/output/functions/api/readiness.func/.vc-config.json' = @(
            'route_config', @('.vercel/output/functions/api/readiness.func/.vc-config.json'),
            @('api/readiness.func')
        )
        '.vercel/output/functions/api/readiness.func/vc__handler__python.py' = @(
            'route_wrapper',
            @('.vercel/output/functions/api/readiness.func/vc__handler__python.py'),
            @('api/readiness.func')
        )
        'api/public_state.py' = @('runtime_support', @('api/public_state.py'), $routeNames)
        'uv.lock' = @('runtime_support', @('uv.lock'), $routeNames)
    }
    foreach ($record in $generated) {
        $expected = $expectedGenerated[[string]$record.target]
        if ($null -eq $expected -or [string]$record.kind -cne [string]$expected[0] -or
            (@($record.logical_sources) -join "`0") -cne (@($expected[1]) -join "`0") -or
            (@($record.route_bindings) -join "`0") -cne (@($expected[2]) -join "`0")) {
            throw 'Vercel generated-runtime authority contains an unexpected generated file.'
        }
    }
    foreach ($record in $vendor) {
        $sources = @($record.logical_sources)
        if ([string]$record.kind -cne 'vendor_file' -or $sources.Count -ne 1 -or
            [string]$sources[0] -cnotmatch
                '^_vendor/(?:pip|pip-26\.2\.1\.dist-info|vercel_runtime|vercel_runtime-0\.22\.1\.dist-info)/' -or
            -not ([string]$record.target).StartsWith(
                '.vercel/python/.venv/Lib/site-packages/', [StringComparison]::Ordinal
            ) -or
            (@($record.route_bindings) -join "`0") -cne ($routeNames -join "`0")) {
            throw 'Vercel generated-runtime authority contains an unexpected vendor mapping.'
        }
    }
    $generatedHash = Get-VercelGeneratedRuntimeMapSha256 -Records $generated
    $vendorHash = Get-VercelGeneratedRuntimeMapSha256 -Records $vendor
    if ($generatedHash -cne [string]$authority.generated_map_sha256 -or
        $vendorHash -cne [string]$authority.vendor_map_sha256) {
        throw 'Vercel generated-runtime authority map hash is invalid.'
    }
    $nodeAuthorityLine = 'node|' + (@(
            [string]$authority.node.archive_sha256
            [string][long]$authority.node.archive_size
            [string]$authority.node.executable_sha256
            [string][long]$authority.node.executable_size
            [string]$authority.node.version
        ) -join '|')
    $uvAuthorityLine = 'uv|' + (@(
            [string]$authority.uv.executable_sha256
            [string][long]$authority.uv.executable_size
            [string]$authority.uv.version
        ) -join '|')
    $vercelAuthorityLine = 'vercel|' + (@(
            [string]$authority.vercel_cli.entry_sha256
            [string][long]$authority.vercel_cli.tree_file_count
            [string]$authority.vercel_cli.tree_sha256
            [string]$authority.vercel_cli.version
        ) -join '|')
    $authorityLines = @(
        'dawnstrike.vercel_generated_runtime_authority.v1'
        $nodeAuthorityLine
        $uvAuthorityLine
        $vercelAuthorityLine
        'python_runtime|python3.13'
        "generated|$generatedHash"
        "vendor|$vendorHash"
        'research_only|true'
        'broker_execution_enabled|false'
    )
    $contractHash = Get-VercelUtf8TextSha256 -Text ($authorityLines -join "`n")
    if ($contractHash -cne [string]$authority.authority_contract_sha256 -or
        $contractHash -cne $script:VercelGeneratedRuntimeAuthorityContractSha256) {
        throw 'Vercel generated-runtime authority contract hash is invalid.'
    }
    return [pscustomobject]@{
        payload = $authority
        path = $authorityPath
        sha256 = $rawHash
    }
}

function Assert-VercelGeneratedRuntimeAuthority {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][object[]]$ReferencedFiles
    )
    $authority = Get-VercelGeneratedRuntimeAuthority
    $routeNames = @('api/health.func', 'api/readiness.func')
    $bySource = [Collections.Generic.Dictionary[string, object]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($reference in @($ReferencedFiles)) {
        $source = [string]$reference.source
        $target = [string]$reference.target
        $route = [string]$reference.route
        if (-not $bySource.ContainsKey($source)) {
            $bySource.Add($source, [pscustomobject]@{
                target = $target
                sha256 = [string]$reference.sha256
                size = [long]$reference.size
                routes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
            })
        }
        $group = $bySource[$source]
        if ([string]$group.target -cne $target -or
            [string]$group.sha256 -cne [string]$reference.sha256 -or
            [long]$group.size -ne [long]$reference.size -or
            -not $group.routes.Add($route)) {
            throw 'Vercel generated-runtime filePathMap has conflicting or duplicate bindings.'
        }
    }
    foreach ($record in @($authority.payload.generated_files) + @($authority.payload.vendor_files)) {
        $target = [string]$record.target
        $targetPath = Join-Path $StageRoot ($target -replace '/', '\')
        if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
            throw "Vercel generated-runtime authority target is missing: $target"
        }
        $actualTarget = (Get-VercelRelativePath -Root $StageRoot -Path $targetPath).Replace('\', '/')
        if ($actualTarget -cne $target -or
            (Get-VercelFileSha256 -Path $targetPath) -cne [string]$record.sha256 -or
            [long](Get-Item -LiteralPath $targetPath -Force).Length -ne [long]$record.size) {
            throw "Vercel generated-runtime authority bytes changed for $target."
        }
    }
    $mappedAuthority = @(
        @($authority.payload.generated_files) | Where-Object {
            [string]$_.kind -eq 'runtime_support'
        }
    ) + @($authority.payload.vendor_files)
    foreach ($record in $mappedAuthority) {
        $source = [string]@($record.logical_sources)[0]
        if (-not $bySource.ContainsKey($source)) {
            throw "Vercel generated-runtime authority binding is missing for $source."
        }
        $group = $bySource[$source]
        if ([string]$group.target -cne [string]$record.target -or
            [string]$group.sha256 -cne [string]$record.sha256 -or
            [long]$group.size -ne [long]$record.size -or
            (@($group.routes | Sort-Object -CaseSensitive) -join "`0") -cne
                ($routeNames -join "`0")) {
            throw "Vercel generated-runtime authority binding changed for $source."
        }
    }
    $authoritySources = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($record in @($authority.payload.vendor_files)) {
        $null = $authoritySources.Add([string]@($record.logical_sources)[0])
    }
    foreach ($source in @($bySource.Keys)) {
        if ($source.StartsWith('_vendor/', [StringComparison]::Ordinal) -and
            -not $authoritySources.Contains($source)) {
            throw "Vercel generated-runtime filePathMap contains an unauthorized vendor source: $source"
        }
    }
    return [string]$authority.sha256
}

function Assert-VercelFrozenGeneratedRuntimeAuthority {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Boundary)

    $authority = Get-VercelGeneratedRuntimeAuthority
    $frozenByTarget = [Collections.Generic.Dictionary[string, object]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($file in @($Boundary.files)) {
        $target = [string]$file.relative_path
        if ($frozenByTarget.ContainsKey($target)) {
            throw 'Frozen Vercel deployment boundary contains a duplicate authority target.'
        }
        $frozenByTarget.Add($target, $file)
    }
    foreach ($record in @($authority.payload.generated_files) + @($authority.payload.vendor_files)) {
        $target = [string]$record.target
        if (-not $frozenByTarget.ContainsKey($target)) {
            throw "Frozen Vercel deployment boundary is missing authority target $target."
        }
        $file = $frozenByTarget[$target]
        $null = Assert-VercelFrozenDeploymentFileStable -File $file
        if ([string]$file.sha256 -cne [string]$record.sha256 -or
            [long]$file.length -ne [long]$record.size) {
            throw "Frozen Vercel deployment bytes differ from authority for $target."
        }
        if ([string]$record.kind -in @('runtime_support', 'vendor_file')) {
            if ((@($file.logical_sources) -join "`0") -cne
                    (@($record.logical_sources) -join "`0") -or
                (@($file.route_bindings) -join "`0") -cne
                    (@($record.route_bindings) -join "`0")) {
                throw "Frozen Vercel deployment mapping differs from authority for $target."
            }
        }
    }
    $authorityVendorSources = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($record in @($authority.payload.vendor_files)) {
        $null = $authorityVendorSources.Add([string]@($record.logical_sources)[0])
    }
    foreach ($file in @($Boundary.files)) {
        foreach ($source in @($file.logical_sources)) {
            $sourceText = [string]$source
            if ($sourceText.StartsWith('_vendor/', [StringComparison]::Ordinal) -and
                -not $authorityVendorSources.Contains($sourceText)) {
                throw "Frozen Vercel deployment contains unauthorized vendor source $sourceText."
            }
        }
    }
    return [string]$authority.sha256
}

function Convert-VercelSourceManifestToCanonicalJson {
    param([Parameter(Mandatory = $true)][string]$RawJson)
    Assert-VercelJsonObjectKeysUnique -RawJson $RawJson
    try { $parsed = $RawJson | ConvertFrom-Json }
    catch { throw "Vercel source manifest is unreadable." }
    $health = $parsed.api_sha256.PSObject.Properties["api/health.py"]
    $readiness = $parsed.api_sha256.PSObject.Properties["api/readiness.py"]
    if (
        $parsed.schema_version -ne "dawnstrike.vercel_source_manifest.v1" -or
        [string]$parsed.source_sha -notmatch '^[0-9a-f]{40}$' -or
        [string]$parsed.source_tree -notmatch '^[0-9a-f]{40}$' -or
        $null -eq $health -or [string]$health.Value -notmatch '^[0-9a-f]{64}$' -or
        $null -eq $readiness -or [string]$readiness.Value -notmatch '^[0-9a-f]{64}$'
    ) {
        throw "Vercel source manifest has an invalid schema or hash."
    }
    # Rebuild from the exact allowlisted shape and key order.  Comparing this
    # to the raw bytes rejects duplicate keys, extra fields, and reordering.
    $canonical = [ordered]@{
        schema_version = "dawnstrike.vercel_source_manifest.v1"
        source_sha = [string]$parsed.source_sha
        source_tree = [string]$parsed.source_tree
        api_sha256 = [ordered]@{
            "api/health.py" = [string]$health.Value
            "api/readiness.py" = [string]$readiness.Value
        }
    }
    return ($canonical | ConvertTo-Json -Depth 8)
}

function Get-VercelSourceManifestCanonicalJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Vercel source manifest is missing: $Path"
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try { $raw = $utf8.GetString([System.IO.File]::ReadAllBytes($Path)) }
    catch { throw "Vercel source manifest is not valid UTF-8: $Path" }
    $canonical = Convert-VercelSourceManifestToCanonicalJson -RawJson $raw
    # Byte equality of the root/static/function manifests is asserted below;
    # tolerate PowerShell/Python newline conventions here after strict schema
    # and duplicate-key validation.
    return $canonical
}

function Assert-VercelSourceManifestJson {
    param(
        [Parameter(Mandatory = $true)][string]$RawJson,
        [Parameter(Mandatory = $true)][string]$ExpectedCanonicalJson,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $trimmed = $RawJson.Trim()
    $canonical = Convert-VercelSourceManifestToCanonicalJson -RawJson $trimmed
    if ($trimmed -cne $canonical) {
        throw "$Label source manifest is not the deterministic canonical encoding."
    }
    if ($canonical -cne $ExpectedCanonicalJson) {
        throw "$Label source manifest does not match the verified package manifest."
    }
}

function Assert-VercelManifestBytesEqual {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][string]$ActualPath,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $expected = [System.IO.File]::ReadAllBytes($ExpectedPath)
    $actual = [System.IO.File]::ReadAllBytes($ActualPath)
    if (
        $expected.Length -ne $actual.Length -or
        -not [System.Linq.Enumerable]::SequenceEqual([byte[]]$expected, [byte[]]$actual)
    ) {
        throw "$Label source manifest bytes do not match the root manifest."
    }
}

function Assert-VercelStagedSourceManifest {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree
    )
    $sourceManifestPath = Join-Path $StageRoot "vercel-source-manifest.json"
    $canonical = Get-VercelSourceManifestCanonicalJson -Path $sourceManifestPath
    $expectedCanonical = [ordered]@{
        schema_version = "dawnstrike.vercel_source_manifest.v1"
        source_sha = $ExpectedSourceSha
        source_tree = $ExpectedSourceTree
        api_sha256 = [ordered]@{
            "api/health.py" = $null
            "api/readiness.py" = $null
        }
    }
    $parsed = $canonical | ConvertFrom-Json
    $expectedCanonical.api_sha256["api/health.py"] = [string]$parsed.api_sha256.PSObject.Properties["api/health.py"].Value
    $expectedCanonical.api_sha256["api/readiness.py"] = [string]$parsed.api_sha256.PSObject.Properties["api/readiness.py"].Value
    if (($expectedCanonical | ConvertTo-Json -Depth 8) -cne $canonical) {
        throw "Vercel source manifest does not match the verified Git commit and tree."
    }
    Assert-VercelManifestBytesEqual -ExpectedPath $sourceManifestPath `
        -ActualPath (Join-Path $StageRoot "public\vercel-source-manifest.json") `
        -Label "Static package"
    Assert-VercelManifestBytesEqual -ExpectedPath $sourceManifestPath `
        -ActualPath (Join-Path $StageRoot "function_public\vercel-source-manifest.json") `
        -Label "Function public package"
    $sourceManifest = $parsed
    foreach ($apiPath in @("api/health.py", "api/readiness.py")) {
        $apiProperty = $sourceManifest.api_sha256.PSObject.Properties[$apiPath]
        if ($null -eq $apiProperty -or [string]$apiProperty.Value -notmatch '^[0-9a-f]{64}$') {
            throw "Vercel source manifest is missing a valid hash for $apiPath."
        }
        $stagedApiPath = Join-Path $StageRoot ($apiPath -replace "/", "\")
        $actualApiHash = Get-VercelFileSha256 -Path $stagedApiPath
        if ($actualApiHash -ne [string]$apiProperty.Value) {
            throw "Staged API bytes do not match the immutable Vercel source manifest for $apiPath."
        }
    }
}

function Get-VercelRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $rootFullPath = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
    $rootItem = Get-Item -LiteralPath $rootFullPath -Force -ErrorAction Stop
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Vercel package root must not be a reparse point."
    }
    $rootPrefix = $rootFullPath + "\"
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Vercel package path escaped its expected root."
    }
    $relativeSegments = $fullPath.Substring($rootPrefix.Length).Split("\")
    $currentPath = $rootFullPath
    foreach ($segment in $relativeSegments) {
        if (-not $segment) { continue }
        $currentPath = Join-Path $currentPath $segment
        $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Vercel package path must not traverse a reparse point."
        }
    }
    return $fullPath.Substring($rootPrefix.Length)
}

function Assert-VercelContainedPathNoReparse {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $targetFull = [System.IO.Path]::GetFullPath($Target)
    $prefix = $rootFull + '\'
    if (-not $targetFull.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escaped the governed project root."
    }
    $cursor = $targetFull
    while ($true) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse point."
            }
        }
        if ([string]::Equals($cursor, $rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or [string]::Equals($parent, $cursor, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "$Label does not descend from the governed project root."
        }
        $cursor = $parent
    }
}

function Get-VercelPackageDirectories {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $output = Join-Path $StageRoot ".vercel\output"
    return @(
        Get-ChildItem -LiteralPath $output -Recurse -Directory -Force -ErrorAction SilentlyContinue |
            ForEach-Object {
                (Get-VercelRelativePath -Root $StageRoot -Path $_.FullName).Replace("\", "/")
            } |
            Sort-Object
    )
}

function Get-VercelPackageInventory {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $output = Join-Path $StageRoot ".vercel\output"
    $inventory = [ordered]@{}
    $files = @(
        Get-ChildItem -LiteralPath $output -Recurse -File -Force -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName
    )
    foreach ($file in $files) {
        $relative = (Get-VercelRelativePath -Root $StageRoot -Path $file.FullName).Replace("\", "/")
        $inventory[$relative] = [ordered]@{
            sha256 = Get-VercelFileSha256 -Path $file.FullName
            size = [int64]$file.Length
        }
    }
    return $inventory
}

function Assert-VercelPackageInventory {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][object[]]$FunctionRouteDirs,
        [object[]]$ReferencedFiles = @(),
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{64}$')][string]$GeneratedRuntimeAuthoritySha256
    )
    $output = Join-Path $StageRoot ".vercel\output"
    $functions = Join-Path $output "functions"
    $static = Join-Path $output "static"
    $directFunctionFiles = @(
        Get-ChildItem -LiteralPath $functions -File -Force -ErrorAction SilentlyContinue
    )
    if ($directFunctionFiles.Count -gt 0) {
        throw "Vercel prebuilt function package contains files outside the expected function routes."
    }

    $publicSource = Join-Path $StageRoot "public"
    $expectedStaticFiles = @(
        Get-ChildItem -LiteralPath $publicSource -Recurse -File -Force -ErrorAction SilentlyContinue
    )
    $actualStaticFiles = @(
        Get-ChildItem -LiteralPath $static -Recurse -File -Force -ErrorAction SilentlyContinue
    )
    $expectedStaticRelative = @(
        $expectedStaticFiles | ForEach-Object {
            (Get-VercelRelativePath -Root $publicSource -Path $_.FullName).Replace("\", "/")
        }
    )
    $actualStaticRelative = @(
        $actualStaticFiles | ForEach-Object {
            (Get-VercelRelativePath -Root $static -Path $_.FullName).Replace("\", "/")
        }
    )
    if (@(Compare-Object `
            -ReferenceObject ($expectedStaticRelative | Sort-Object) `
            -DifferenceObject ($actualStaticRelative | Sort-Object)).Count -ne 0) {
        throw "Vercel prebuilt static package contains an unexpected or missing file."
    }
    foreach ($file in $expectedStaticFiles) {
        $relative = (Get-VercelRelativePath -Root $publicSource -Path $file.FullName).Replace("\", "/")
        $actualPath = Join-Path $static ($relative -replace "/", "\")
        if ((Get-VercelFileSha256 -Path $file.FullName) -ne (Get-VercelFileSha256 -Path $actualPath)) {
            throw "Vercel prebuilt static package bytes changed for $relative."
        }
    }

    $stageFiles = @(
        Get-ChildItem -LiteralPath $StageRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike "$(Join-Path $StageRoot '.vercel\output')*" }
    )
    foreach ($routeDir in $FunctionRouteDirs) {
        $routeFiles = @(
            Get-ChildItem -LiteralPath $routeDir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue
        )
        foreach ($file in $routeFiles) {
            if ($file.Name -in @(".vc-config.json", "vc__handler__python.py")) { continue }
            $routeRelative = (Get-VercelRelativePath -Root $routeDir.FullName -Path $file.FullName).Replace("\", "/")
            $allowed = @(
                $stageFiles | Where-Object {
                    $stageRelative = (Get-VercelRelativePath -Root $StageRoot -Path $_.FullName).Replace("\", "/")
                    $stageRelative -eq $routeRelative -or
                    $stageRelative.EndsWith("/$routeRelative", [System.StringComparison]::OrdinalIgnoreCase)
                }
            )
            if ($allowed.Count -ne 1) {
                throw "Vercel prebuilt function package contains an unexpected file: $routeRelative"
            }
            if ((Get-VercelFileSha256 -Path $file.FullName) -ne
                (Get-VercelFileSha256 -Path $allowed[0].FullName)) {
                throw "Vercel prebuilt function bytes changed for $routeRelative."
            }
        }
    }

    $packageManifestPath = Join-Path $StageRoot "vercel-package-manifest.json"
    $inventory = Get-VercelPackageInventory -StageRoot $StageRoot
    $expectedManifest = [ordered]@{
        schema_version = "dawnstrike.vercel_package_manifest.v1"
        generated_runtime_authority_sha256 = $GeneratedRuntimeAuthoritySha256.ToLowerInvariant()
        directories = @(Get-VercelPackageDirectories -StageRoot $StageRoot)
        files = $inventory
        bindings = @(
            @($ReferencedFiles) |
                Sort-Object -Property route, source, target |
                ForEach-Object {
                    [ordered]@{
                        route = [string]$_.route
                        source = [string]$_.source
                        target = [string]$_.target
                        sha256 = [string]$_.sha256
                        size = [int64]$_.size
                    }
                }
        )
    }
    $expectedJson = $expectedManifest | ConvertTo-Json -Depth 20
    if (Test-Path -LiteralPath $packageManifestPath -PathType Leaf) {
        $raw = Get-Content -Raw -LiteralPath $packageManifestPath
        Assert-VercelJsonObjectKeysUnique -RawJson $raw
        try { $parsed = $raw | ConvertFrom-Json }
        catch { throw "Vercel package inventory manifest is unreadable." }
        $canonical = $parsed | ConvertTo-Json -Depth 20
        if (
            $raw -cne $canonical -or
            $canonical -cne $expectedJson -or
            $parsed.schema_version -ne "dawnstrike.vercel_package_manifest.v1"
        ) {
            throw "Vercel prebuilt package inventory changed after the build."
        }
    }
    else {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($packageManifestPath, $expectedJson, $utf8NoBom)
    }
}

function Get-VercelPackageManifestSha256 {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $manifestPath = Join-Path $StageRoot "vercel-package-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Vercel package inventory manifest is missing."
    }
    return Get-VercelFileSha256 -Path $manifestPath
}

function Normalize-VercelGeneratedPipRecord {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    # pip's Windows launchers embed their absolute virtual-environment path.
    # Their three RECORD hashes therefore vary with the fresh StageRoot even
    # though the provider does not upload those Scripts/*.exe files.  Wheel
    # RECORD permits an empty hash/size for installed files.  Canonicalize only
    # those exact pinned-pip rows so every uploaded vendor byte has one stable,
    # independently committed authority value.
    $recordPath = Join-Path $StageRoot ".vercel\python\.venv\Lib\site-packages\pip-26.2.1.dist-info\RECORD"
    if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
        throw "Vercel generated pip RECORD is missing."
    }
    $relative = (Get-VercelRelativePath -Root $StageRoot -Path $recordPath).Replace("\", "/")
    if ($relative -cne ".vercel/python/.venv/Lib/site-packages/pip-26.2.1.dist-info/RECORD") {
        throw "Vercel generated pip RECORD escaped the stage root."
    }
    $utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
    try { $raw = $utf8Strict.GetString([System.IO.File]::ReadAllBytes($recordPath)) }
    catch { throw "Vercel generated pip RECORD is not valid UTF-8." }
    if ($raw.Contains("`r") -or -not $raw.EndsWith("`n", [System.StringComparison]::Ordinal)) {
        throw "Vercel generated pip RECORD has non-canonical line endings."
    }
    $lines = @($raw.Substring(0, $raw.Length - 1).Split("`n"))
    $expectedLaunchers = @("pip.exe", "pip3.exe", "pip3.13.exe")
    foreach ($launcher in $expectedLaunchers) {
        $prefix = "../../Scripts/$launcher"
        $matchingIndexes = @(
            for ($index = 0; $index -lt $lines.Count; $index++) {
                if ($lines[$index].StartsWith("$prefix,", [System.StringComparison]::Ordinal)) {
                    $index
                }
            }
        )
        if ($matchingIndexes.Count -ne 1) {
            throw "Vercel generated pip RECORD does not bind exactly one $launcher row."
        }
        $row = [string]$lines[$matchingIndexes[0]]
        if ($row -cne "$prefix,," -and
            $row -cnotmatch ('^' + [regex]::Escape($prefix) + ',sha256=[A-Za-z0-9_-]{43},46080$')) {
            throw "Vercel generated pip RECORD has an unexpected $launcher row."
        }
        $lines[$matchingIndexes[0]] = "$prefix,,"
    }
    $canonical = ($lines -join "`n") + "`n"
    if ($raw -cne $canonical) {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($recordPath, $canonical, $utf8NoBom)
    }
}

function Get-VercelPinnedWheelBytes {
    param([Parameter(Mandatory = $true)]$Contract)

    if (-not ('System.Net.Http.HttpClient' -as [type])) {
        $null = [Reflection.Assembly]::Load(
            'System.Net.Http, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a'
        )
    }
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $handler.UseDefaultCredentials = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(120)
    $request = [Net.Http.HttpRequestMessage]::new(
        [Net.Http.HttpMethod]::Get,
        [Uri]([string]$Contract.uri)
    )
    $request.Headers.UserAgent.ParseAdd('Dawnstrike-Deterministic-Vercel-Package/1')
    $response = $null
    $contentStream = $null
    $buffered = $null
    try {
        $response = $client.SendAsync(
            $request,
            [Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200) {
            throw "Pinned Vercel wheel download returned HTTP $([int]$response.StatusCode)."
        }
        if ($null -ne $response.Content.Headers.ContentLength -and
            [long]$response.Content.Headers.ContentLength -ne [long]$Contract.length) {
            throw 'Pinned Vercel wheel download length header changed.'
        }
        $contentStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $buffered = [IO.MemoryStream]::new([int][long]$Contract.length)
        [byte[]]$buffer = New-Object byte[] 65536
        while ($true) {
            $count = $contentStream.Read($buffer, 0, $buffer.Length)
            if ($count -eq 0) { break }
            if ($buffered.Length + $count -gt [long]$Contract.length) {
                throw 'Pinned Vercel wheel download exceeded its fixed byte length.'
            }
            $buffered.Write($buffer, 0, $count)
        }
        [byte[]]$bytes = $buffered.ToArray()
        if ($bytes.Length -ne [long]$Contract.length -or
            (Get-VercelDeploymentBytesHash -Bytes $bytes -Algorithm SHA256) -cne
                [string]$Contract.sha256) {
            throw 'Pinned Vercel wheel download identity changed.'
        }
        return ,$bytes
    }
    finally {
        if ($null -ne $contentStream) { $contentStream.Dispose() }
        if ($null -ne $buffered) { $buffered.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        $request.Dispose()
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-VercelPinnedWheelArchive {
    param([Parameter(Mandatory = $true)]$Contract)

    if (-not ('System.IO.Compression.ZipArchive' -as [type])) {
        $null = [Reflection.Assembly]::Load(
            'System.IO.Compression, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089'
        )
    }
    [byte[]]$wheelBytes = Get-VercelPinnedWheelBytes -Contract $Contract
    $memory = [IO.MemoryStream]::new($wheelBytes, $false)
    $archive = [IO.Compression.ZipArchive]::new(
        $memory,
        [IO.Compression.ZipArchiveMode]::Read,
        $false
    )
    $entries = [Collections.Generic.Dictionary[string, byte[]]]::new(
        [StringComparer]::Ordinal
    )
    $folded = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    [long]$expandedLength = 0
    try {
        foreach ($entry in $archive.Entries) {
            $name = [string]$entry.FullName
            if ([string]::IsNullOrWhiteSpace($name) -or $name.Contains('\') -or
                $name.StartsWith('/', [StringComparison]::Ordinal) -or
                $name.Contains(':') -or
                @($name.Split('/') | Where-Object { $_ -in @('', '.', '..') }).Count -gt 0) {
                if ($name.EndsWith('/', [StringComparison]::Ordinal) -and
                    @($name.TrimEnd('/').Split('/') | Where-Object {
                            $_ -in @('', '.', '..')
                        }).Count -eq 0) {
                    continue
                }
                throw 'Pinned Vercel wheel contains an unsafe archive path.'
            }
            if (-not (@($Contract.archive_prefixes | Where-Object {
                            $name.StartsWith([string]$_, [StringComparison]::Ordinal)
                        }).Count -eq 1)) {
                throw 'Pinned Vercel wheel contains an unexpected distribution path.'
            }
            if (-not $folded.Add($name) -or $entries.ContainsKey($name)) {
                throw 'Pinned Vercel wheel contains a duplicate or case-colliding path.'
            }
            if ([long]$entry.Length -lt 0 -or [long]$entry.Length -gt 16777216L -or
                $expandedLength + [long]$entry.Length -gt 67108864L) {
                throw 'Pinned Vercel wheel exceeds the fixed extraction bounds.'
            }
            $entryStream = $entry.Open()
            $entryBuffer = [IO.MemoryStream]::new([int][long]$entry.Length)
            try {
                $entryStream.CopyTo($entryBuffer)
                [byte[]]$entryBytes = $entryBuffer.ToArray()
            }
            finally {
                $entryBuffer.Dispose()
                $entryStream.Dispose()
            }
            if ($entryBytes.Length -ne [long]$entry.Length) {
                throw 'Pinned Vercel wheel entry length changed while it was read.'
            }
            $entries.Add($name, $entryBytes)
            $expandedLength += $entryBytes.Length
        }
    }
    finally {
        $archive.Dispose()
        $memory.Dispose()
    }
    if ($entries.Count -lt 1) { throw 'Pinned Vercel wheel contains no files.' }
    return [pscustomobject]@{ contract = $Contract; entries = $entries }
}

function Get-VercelWheelRecordDigest {
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return [Convert]::ToBase64String($sha.ComputeHash($Bytes)).TrimEnd('=').Replace(
            '+', '-'
        ).Replace('/', '_')
    }
    finally { $sha.Dispose() }
}

function Get-VercelDeterministicWheelRecordBytes {
    param([Parameter(Mandatory = $true)]$Wheel)

    $distribution = [string]$Wheel.contract.distribution
    $recordName = "$distribution.dist-info/RECORD"
    $rows = [Collections.Generic.Dictionary[string, string]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($name in @($Wheel.entries.Keys)) {
        [byte[]]$bytes = $Wheel.entries[$name]
        $rows[$name] = "$name,sha256=$(Get-VercelWheelRecordDigest -Bytes $bytes),$($bytes.Length)"
    }
    [byte[]]$installerBytes = [Text.Encoding]::UTF8.GetBytes('uv')
    $installerName = "$distribution.dist-info/INSTALLER"
    $rows[$installerName] = "$installerName,sha256=$(Get-VercelWheelRecordDigest -Bytes $installerBytes),2"
    $requestedName = "$distribution.dist-info/REQUESTED"
    $rows[$requestedName] = "$requestedName,sha256=$(Get-VercelWheelRecordDigest -Bytes @()),0"
    $rows[$recordName] = "$recordName,,"
    if ($distribution -ceq 'pip-26.2.1') {
        foreach ($launcher in @('pip.exe', 'pip3.13.exe', 'pip3.exe')) {
            $rows["../../Scripts/$launcher"] = "../../Scripts/$launcher,,"
        }
    }
    [string[]]$names = @($rows.Keys)
    [Array]::Sort($names, [StringComparer]::Ordinal)
    return ,[Text.Encoding]::UTF8.GetBytes(
        ((@($names | ForEach-Object { $rows[$_] }) -join "`n") + "`n")
    )
}

function Write-VercelDeterministicFile {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][byte[]]$Bytes
    )
    Assert-VercelDeploymentProviderRelativePath -Path $RelativePath `
        -Label 'Deterministic Vercel output path'
    $path = [IO.Path]::GetFullPath((Join-Path $StageRoot ($RelativePath -replace '/', '\')))
    Assert-VercelContainedPathNoReparse -Root $StageRoot -Target $path `
        -Label 'Deterministic Vercel output path'
    $parent = Split-Path -Parent $path
    $null = [IO.Directory]::CreateDirectory($parent)
    if (-not [string]::Equals(
            [IO.Path]::GetFullPath($parent).TrimEnd('\'),
            [IO.Path]::GetFullPath($StageRoot).TrimEnd('\'),
            [StringComparison]::OrdinalIgnoreCase
        )) {
        Assert-VercelContainedPathNoReparse -Root $StageRoot -Target $parent `
            -Label 'Deterministic Vercel output parent'
    }
    $stream = [IO.FileStream]::new(
        $path,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write,
        [IO.FileShare]::None
    )
    try {
        $stream.Write($Bytes, 0, $Bytes.Length)
        $stream.Flush($true)
    }
    finally { $stream.Dispose() }
    return $path
}

function Get-VercelDeterministicWrapperBytes {
    param([Parameter(Mandatory = $true)][ValidateSet('health', 'readiness')][string]$Route)
    $template = @'

import importlib
import os
import os.path
import site
import sys
import time

# Cold start baseline, read back by vercel_runtime.vc_init
_vc_boot_start_ms = int(time.monotonic() * 1000)

_here = os.path.dirname(__file__)

os.environ.update({
  "__VC_PY_BOOT_START_MS": str(_vc_boot_start_ms),
  "__VC_HANDLER_MODULE_NAME": "api.__DAWNSTRIKE_ROUTE__",
  "__VC_HANDLER_ENTRYPOINT": "api/__DAWNSTRIKE_ROUTE__.py",
  "__VC_HANDLER_ENTRYPOINT_ABS": os.path.join(_here, "api/__DAWNSTRIKE_ROUTE__.py"),
  "__VC_HANDLER_VENDOR_DIR": "_vendor",
  "__VC_HANDLER_VARIABLE_NAME": "handler"
})

_vendor_rel = '_vendor'
_vendor = os.path.normpath(os.path.join(_here, _vendor_rel))

if os.path.isdir(_vendor):
    # Process .pth files like a real site-packages dir
    site.addsitedir(_vendor)

    # Move _vendor to the front (after script dir if present)
    try:
        while _vendor in sys.path:
            sys.path.remove(_vendor)
    except ValueError:
        pass

    # Put vendored deps ahead of site-packages but after the script dir
    idx = 1 if (sys.path and sys.path[0] in ('', _here)) else 0
    sys.path.insert(idx, _vendor)

    importlib.invalidate_caches()

from vercel_runtime.vc_init import vc_handler
'@
    $text = $template.Replace("`r`n", "`n").Replace('__DAWNSTRIKE_ROUTE__', $Route)
    if (-not $text.EndsWith("`n", [StringComparison]::Ordinal)) { $text += "`n" }
    return ,[Text.Encoding]::UTF8.GetBytes($text)
}

function Get-VercelDeterministicUvLockBytes {
    $text = @'
version = 1
revision = 3
requires-python = ">=3.13"

[[package]]
name = "dawnstrike-public-stage"
version = "0.0.0"
source = { virtual = "." }
dependencies = [
    { name = "vercel-runtime" },
]

[package.metadata]
requires-dist = [{ name = "vercel-runtime", specifier = "==0.22.1" }]

[[package]]
name = "vercel-runtime"
version = "0.22.1"
source = { registry = "https://pypi.org/simple" }
sdist = { url = "https://files.pythonhosted.org/packages/34/0e/bb9b05b116371d146cf3b913632ca12c10ae965190b99e7c6efbfed9df41/vercel_runtime-0.22.1.tar.gz", hash = "sha256:5be8ab7420d8a57f99e3746eda0557336fb6875717f427111db73b79e055b37b", size = 420776, upload-time = "2026-08-26T17:06:59.581Z" }
wheels = [
    { url = "https://files.pythonhosted.org/packages/9f/52/44e1dcea1d974c4b7011549cfb687f23507dd8f95247f32ad79e7df1e441/vercel_runtime-0.22.1-py3-none-any.whl", hash = "sha256:c9c1d74ae41199a48478a3082e6d7c42c21cdc096710e17f30e0292f71816fec", size = 525108, upload-time = "2026-08-26T17:07:00.639Z" },
]
'@
    $text = $text.Replace("`r`n", "`n")
    if (-not $text.EndsWith("`n", [StringComparison]::Ordinal)) { $text += "`n" }
    return ,[Text.Encoding]::UTF8.GetBytes($text)
}

function Get-VercelDeterministicOutputConfigBytes {
    $headers = [ordered]@{
        'Content-Security-Policy' = "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; upgrade-insecure-requests"
        'X-Content-Type-Options' = 'nosniff'
        'Referrer-Policy' = 'no-referrer'
        'Permissions-Policy' = 'camera=(), microphone=(), geolocation=(), payment=(), usb=()'
        'X-Frame-Options' = 'DENY'
        'Cross-Origin-Opener-Policy' = 'same-origin'
        'Cross-Origin-Resource-Policy' = 'same-origin'
    }
    $config = [ordered]@{
        version = 3
        routes = @(
            [ordered]@{ src = '^(?:/(.*))$'; headers = $headers; continue = $true }
            [ordered]@{ handle = 'filesystem' }
            [ordered]@{ src = '^/api(/.*)?$'; status = 404 }
            [ordered]@{ handle = 'error' }
            [ordered]@{ src = '^(?!/api).*$'; dest = '/404.html'; status = 404 }
            [ordered]@{ handle = 'miss' }
            [ordered]@{ src = '^/api/(.+)(?:\.(?:py))$'; dest = '/api/$1'; check = $true }
        )
        crons = @()
    }
    return ,[Text.Encoding]::UTF8.GetBytes(($config | ConvertTo-Json -Depth 20 -Compress))
}

function New-VercelDeterministicBuiltPackage {
    param([Parameter(Mandatory = $true)][string]$StageRoot)

    $stage = [IO.Path]::GetFullPath($StageRoot)
    foreach ($required in @(
            'api/health.py', 'api/readiness.py', 'api/public_state.py',
            'function_public/vercel-source-manifest.json', 'public/vercel-source-manifest.json',
            'pyproject.toml', 'vercel.json'
        )) {
        $requiredPath = Join-Path $stage ($required -replace '/', '\')
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Deterministic Vercel package input is missing: $required"
        }
    }
    foreach ($generated in @('.vercel', '.python-version', 'uv.lock', 'vercel-package-manifest.json')) {
        if (Test-Path -LiteralPath (Join-Path $stage $generated)) {
            throw "Deterministic Vercel package output already exists: $generated"
        }
    }
    $authority = Get-VercelGeneratedRuntimeAuthority
    $wheels = [Collections.Generic.Dictionary[string, object]]::new(
        [StringComparer]::Ordinal
    )
    foreach ($contract in $script:VercelPinnedWheelContracts) {
        $wheel = Get-VercelPinnedWheelArchive -Contract $contract
        $wheels.Add([string]$contract.distribution, $wheel)
    }
    foreach ($record in @($authority.payload.vendor_files)) {
        $source = [string]@($record.logical_sources)[0]
        $archivePath = $source.Substring('_vendor/'.Length)
        $distribution = if ($archivePath.StartsWith('pip', [StringComparison]::Ordinal)) {
            'pip-26.2.1'
        }
        elseif ($archivePath.StartsWith('vercel_runtime', [StringComparison]::Ordinal)) {
            'vercel_runtime-0.22.1'
        }
        else { throw 'Generated Vercel vendor authority names an unsupported distribution.' }
        $wheel = $wheels[$distribution]
        if ($archivePath -ceq "$distribution.dist-info/REQUESTED") {
            [byte[]]$bytes = @()
        }
        elseif ($archivePath -ceq "$distribution.dist-info/RECORD") {
            [byte[]]$bytes = Get-VercelDeterministicWheelRecordBytes -Wheel $wheel
        }
        elseif ($wheel.entries.ContainsKey($archivePath)) {
            [byte[]]$bytes = $wheel.entries[$archivePath]
        }
        else { throw "Pinned wheel lacks authorized generated file $archivePath." }
        if ($bytes.Length -ne [long]$record.size -or
            (Get-VercelDeploymentBytesHash -Bytes $bytes -Algorithm SHA256) -cne
                [string]$record.sha256) {
            throw "Pinned wheel bytes differ from generated authority for $archivePath."
        }
        $null = Write-VercelDeterministicFile -StageRoot $stage `
            -RelativePath ([string]$record.target) -Bytes $bytes
    }

    $null = Write-VercelDeterministicFile -StageRoot $stage `
        -RelativePath '.python-version' -Bytes ([Text.Encoding]::UTF8.GetBytes("3.13`n"))
    $null = Write-VercelDeterministicFile -StageRoot $stage `
        -RelativePath 'uv.lock' -Bytes (Get-VercelDeterministicUvLockBytes)
    $publicRoot = Join-Path $stage 'public'
    foreach ($file in @(Get-ChildItem -LiteralPath $publicRoot -Recurse -File -Force |
            Sort-Object -Property FullName)) {
        $relative = (Get-VercelRelativePath -Root $publicRoot -Path $file.FullName).Replace('\', '/')
        $null = Write-VercelDeterministicFile -StageRoot $stage `
            -RelativePath ".vercel/output/static/$relative" `
            -Bytes ([IO.File]::ReadAllBytes($file.FullName))
    }

    $map = [Collections.Generic.Dictionary[string, string]]::new([StringComparer]::Ordinal)
    foreach ($source in @(
            '.python-version', 'api/health.py', 'api/public_state.py', 'api/readiness.py',
            'pyproject.toml', 'uv.lock', 'vercel.json'
        )) { $map.Add($source, $source) }
    $functionPublicRoot = Join-Path $stage 'function_public'
    foreach ($file in @(Get-ChildItem -LiteralPath $functionPublicRoot -Recurse -File -Force |
            Sort-Object -Property FullName)) {
        $relative = (Get-VercelRelativePath -Root $functionPublicRoot -Path $file.FullName).Replace('\', '/')
        $source = "function_public/$relative"
        if ($map.ContainsKey($source)) { throw 'Function public map contains a duplicate path.' }
        $map.Add($source, $source)
    }
    foreach ($record in @($authority.payload.vendor_files)) {
        $source = [string]@($record.logical_sources)[0]
        if ($map.ContainsKey($source)) { throw 'Generated vendor map contains a duplicate path.' }
        $map.Add($source, [string]$record.target)
    }
    $orderedMap = [ordered]@{}
    [string[]]$mapSources = @($map.Keys)
    [Array]::Sort($mapSources, [StringComparer]::Ordinal)
    foreach ($source in $mapSources) {
        $orderedMap[$source] = $map[$source]
    }
    foreach ($route in @('health', 'readiness')) {
        $config = [ordered]@{
            handler = 'vc__handler__python.vc_handler'
            runtime = 'python3.13'
            architecture = 'x86_64'
            maxDuration = 10
            environment = [ordered]@{
                PYTHONPATH = '_vendor'
                PYTHONDONTWRITEBYTECODE = '1'
            }
            supportsResponseStreaming = $true
            filePathMap = $orderedMap
        }
        $prefix = ".vercel/output/functions/api/$route.func"
        $null = Write-VercelDeterministicFile -StageRoot $stage `
            -RelativePath "$prefix/.vc-config.json" `
            -Bytes ([Text.Encoding]::UTF8.GetBytes(
                ($config | ConvertTo-Json -Depth 20 -Compress)
            ))
        $null = Write-VercelDeterministicFile -StageRoot $stage `
            -RelativePath "$prefix/vc__handler__python.py" `
            -Bytes (Get-VercelDeterministicWrapperBytes -Route $route)
    }
    $null = Write-VercelDeterministicFile -StageRoot $stage `
        -RelativePath '.vercel/output/config.json' `
        -Bytes (Get-VercelDeterministicOutputConfigBytes)
    Assert-VercelNoEnvironmentArtifacts -StageRoot $stage
}

function Add-VercelFunctionPublicBindings {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    # @vercel/python 59.11.2 does not materialize includeFiles in filePathMap
    # for this prebuilt layout. Add the explicitly governed function_public bytes
    # before sealing the package; the validator below then requires these
    # bindings in both functions and hashes every referenced stage file.
    Normalize-VercelGeneratedPipRecord -StageRoot $StageRoot
    $functions = Join-Path $StageRoot ".vercel\output\functions"
    $publicRoot = Join-Path $StageRoot "function_public"
    if (-not (Test-Path -LiteralPath $publicRoot -PathType Container)) {
        throw "Vercel function public package source is missing."
    }
    $publicFiles = @(
        Get-ChildItem -LiteralPath $publicRoot -Recurse -File -Force -ErrorAction Stop |
            Sort-Object -Property FullName
    )
    $routeDirs = @(
        Get-ChildItem -LiteralPath $functions -Recurse -Directory -Force -ErrorAction Stop |
            Where-Object { $_.Name -like "*.func" }
    )
    if ($routeDirs.Count -ne 2) {
        throw "Vercel function public package must contain exactly two function routes before binding."
    }
    foreach ($routeDir in $routeDirs) {
        $configPath = Join-Path $routeDir.FullName ".vc-config.json"
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            throw "Vercel function public package is missing .vc-config.json."
        }
        $raw = Get-Content -Raw -LiteralPath $configPath
        Assert-VercelJsonObjectKeysUnique -RawJson $raw
        try { $config = $raw | ConvertFrom-Json }
        catch { throw "Vercel function public package config is unreadable." }
        $map = $config.PSObject.Properties["filePathMap"]
        if ($null -eq $map -or $null -eq $map.Value) {
            throw "Vercel function public package config is missing filePathMap."
        }
        foreach ($file in $publicFiles) {
            $relative = (Get-VercelRelativePath -Root $publicRoot -Path $file.FullName).Replace("\", "/")
            $source = "function_public/$relative"
            if ($null -eq $map.Value.PSObject.Properties[$source]) {
                $map.Value | Add-Member -MemberType NoteProperty -Name $source -Value $source
            }
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 100), $utf8NoBom)
    }
}

function Assert-VercelNoEnvironmentArtifacts {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $vercelRoot = Join-Path $StageRoot ".vercel"
    if (-not (Test-Path -LiteralPath $vercelRoot -PathType Container)) { return }
    $environmentArtifacts = @(
        Get-ChildItem -LiteralPath $vercelRoot -Recurse -File -Force -ErrorAction Stop |
            Where-Object { $_.Name.StartsWith(".env", [System.StringComparison]::OrdinalIgnoreCase) }
    )
    if ($environmentArtifacts.Count -gt 0) {
        throw "Vercel build left environment artifacts in the publication stage."
    }
}

function Assert-VercelBuiltPackage {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree,
        [string]$ExpectedPackageManifestSha256 = ""
    )
    Assert-VercelStagedSourceManifest `
        -StageRoot $StageRoot `
        -ExpectedSourceSha $ExpectedSourceSha `
        -ExpectedSourceTree $ExpectedSourceTree
    $output = Join-Path $StageRoot ".vercel\output"
    $staticManifest = Join-Path $output "static\vercel-source-manifest.json"
    $functions = Join-Path $output "functions"
    if (-not (Test-Path -LiteralPath $output -PathType Container)) {
        throw "Vercel prebuilt output package is missing."
    }

    # The Build Output API package is deliberately a tiny closed-world
    # boundary.  Do not let an additional generated config, middleware, or
    # function become reachable merely because Vercel happened to accept it.
    $expectedOutputEntries = @("config.json", "functions", "static")
    $actualOutputEntries = @(
        Get-ChildItem -LiteralPath $output -Force -ErrorAction SilentlyContinue |
            ForEach-Object { [string]$_.Name }
    )
    $unexpectedOutputEntries = @(
        Compare-Object `
            -ReferenceObject ($expectedOutputEntries | Sort-Object) `
            -DifferenceObject ($actualOutputEntries | Sort-Object) |
            Where-Object { $_.SideIndicator -eq "=>" } |
            ForEach-Object { [string]$_.InputObject }
    )
    if ($unexpectedOutputEntries.Count -gt 0) {
        throw "Vercel prebuilt output contains unexpected entries: $($unexpectedOutputEntries -join ', ')"
    }
    foreach ($requiredEntry in $expectedOutputEntries) {
        if (-not (Test-Path -LiteralPath (Join-Path $output $requiredEntry))) {
            throw "Vercel prebuilt output is missing required entry: $requiredEntry"
        }
    }

    $configPath = Join-Path $output "config.json"
    try {
        $configRaw = Get-Content -Raw -LiteralPath $configPath
        Assert-VercelJsonObjectKeysUnique -RawJson $configRaw
        $config = $configRaw | ConvertFrom-Json
    }
    catch {
        throw "Vercel prebuilt output config is unreadable."
    }
    if ($config.version -ne 3) {
        throw "Vercel prebuilt output config must use Build Output API version 3."
    }
    $allowedConfigProperties = @("version", "routes", "crons")
    $unexpectedConfigProperties = @(
        $config.PSObject.Properties |
            Where-Object { $_.Name -notin $allowedConfigProperties } |
            ForEach-Object { [string]$_.Name }
    )
    if ($unexpectedConfigProperties.Count -gt 0) {
        throw "Vercel prebuilt output config contains unexpected properties: $($unexpectedConfigProperties -join ', ')"
    }
    $routesProperty = $config.PSObject.Properties["routes"]
    if ($null -eq $routesProperty -or $null -eq $routesProperty.Value) {
        throw "Vercel prebuilt output config is missing routes."
    }
    $cronsProperty = $config.PSObject.Properties["crons"]
    if ($null -eq $cronsProperty -or @($cronsProperty.Value).Count -ne 0) {
        throw "Vercel prebuilt output config must contain an empty crons list."
    }
    $routeKinds = @()
    $expectedSecurityHeaders = [ordered]@{
        "Content-Security-Policy" = "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; upgrade-insecure-requests"
        "X-Content-Type-Options" = "nosniff"
        "Referrer-Policy" = "no-referrer"
        "Permissions-Policy" = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        "X-Frame-Options" = "DENY"
        "Cross-Origin-Opener-Policy" = "same-origin"
        "Cross-Origin-Resource-Policy" = "same-origin"
    }
    foreach ($route in @($routesProperty.Value)) {
        $routeProperties = @($route.PSObject.Properties | ForEach-Object { [string]$_.Name })
        $handle = $route.PSObject.Properties["handle"]
        $destination = $route.PSObject.Properties["dest"]
        $headers = $route.PSObject.Properties["headers"]
        $continue = $route.PSObject.Properties["continue"]
        if ($null -ne $handle -and [string]$handle.Value -eq "filesystem") {
            if (@(Compare-Object `
                    -ReferenceObject @("handle") `
                    -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0) {
                throw "Vercel prebuilt output config contains an unexpected filesystem route field."
            }
            $routeKinds += "filesystem"
            continue
        }
        if ($null -ne $handle -and [string]$handle.Value -in @("error", "miss")) {
            if (@(Compare-Object `
                    -ReferenceObject @("handle") `
                    -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0) {
                throw "Vercel prebuilt output config contains an unexpected handle route field."
            }
            $routeKinds += [string]$handle.Value
            continue
        }
        if ($null -ne $headers) {
            if (@(Compare-Object `
                    -ReferenceObject @("continue", "headers", "src") `
                    -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0 -or
                $null -eq $continue -or [bool]$continue.Value -ne $true -or
                $null -eq $route.PSObject.Properties["src"] -or
                [string]$route.src -ne "^(?:/(.*))$") {
                throw "Vercel prebuilt output config contains an unexpected security-header route."
            }
            $actualHeaderProperties = @($headers.Value.PSObject.Properties | ForEach-Object { [string]$_.Name })
            if (@(Compare-Object `
                    -ReferenceObject ($expectedSecurityHeaders.Keys | Sort-Object) `
                    -DifferenceObject ($actualHeaderProperties | Sort-Object)).Count -ne 0) {
                throw "Vercel prebuilt security-header route does not bind the exact header set."
            }
            foreach ($headerName in $expectedSecurityHeaders.Keys) {
                if ([string]$headers.Value.$headerName -cne [string]$expectedSecurityHeaders[$headerName]) {
                    throw "Vercel prebuilt security-header route changed $headerName."
                }
            }
            $routeKinds += "headers"
            continue
        }
        $src = $route.PSObject.Properties["src"]
        $status = $route.PSObject.Properties["status"]
        $check = $route.PSObject.Properties["check"]
        if ($null -ne $status -and [int]$status.Value -eq 404 -and
            $null -ne $src -and [string]$src.Value -eq "^/api(/.*)?$" -and
            @(Compare-Object -ReferenceObject @("src", "status") -DifferenceObject ($routeProperties | Sort-Object)).Count -eq 0) {
            $routeKinds += "api404"
            continue
        }
        if ($null -ne $status -and [int]$status.Value -eq 404 -and
            $null -ne $src -and [string]$src.Value -eq "^(?!/api).*$" -and
            $null -ne $destination -and [string]$destination.Value -eq "/404.html" -and
            @(Compare-Object -ReferenceObject @("dest", "src", "status") -DifferenceObject ($routeProperties | Sort-Object)).Count -eq 0) {
            $routeKinds += "nonApi404"
            continue
        }
        if ($null -ne $src -and [string]$src.Value -eq "^/api/(.+)(?:\.(?:py))$" -and
            $null -ne $destination -and [string]$destination.Value -eq '/api/$1' -and
            $null -ne $check -and [bool]$check.Value -eq $true -and
            @(Compare-Object -ReferenceObject @("check", "dest", "src") -DifferenceObject ($routeProperties | Sort-Object)).Count -eq 0) {
            $routeKinds += "pythonRewrite"
            continue
        }
        throw "Vercel prebuilt output config contains an unexpected route or route semantics: $($routeProperties -join ',')"
    }
    $expectedRouteKinds = @("headers", "filesystem", "api404", "error", "nonApi404", "miss", "pythonRewrite")
    if (@(Compare-Object -ReferenceObject ($expectedRouteKinds | Sort-Object) -DifferenceObject ($routeKinds | Sort-Object)).Count -ne 0) {
        throw "Vercel prebuilt output config does not contain exactly the expected transformed routes."
    }

    if (-not (Test-Path -LiteralPath $staticManifest -PathType Leaf)) {
        throw "Vercel prebuilt static source manifest is missing."
    }
    Assert-VercelManifestBytesEqual `
        -ExpectedPath (Join-Path $StageRoot "vercel-source-manifest.json") `
        -ActualPath $staticManifest `
        -Label "Vercel prebuilt static package"
    if (-not (Test-Path -LiteralPath $functions -PathType Container)) {
        throw "Vercel prebuilt function package is missing."
    }

    $expectedFunctionRouteNames = @(
        "api\health.func", "api\readiness.func",
        "api_health.func", "api_readiness.func",
        "api\health.py.func", "api\readiness.py.func",
        "api_health.py.func", "api_readiness.py.func"
    )
    $functionRouteDirs = @(
        Get-ChildItem -LiteralPath $functions -Recurse -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*.func" }
    )
    if ($functionRouteDirs.Count -ne 2) {
        throw "Vercel prebuilt function package must contain exactly two function routes."
    }
    $referencedFiles = @()
    $derivedRouteNames = @()
    $functionsPrefix = [System.IO.Path]::GetFullPath($functions).TrimEnd("\") + "\"
    foreach ($routeDir in $functionRouteDirs) {
        $routeFullPath = [System.IO.Path]::GetFullPath($routeDir.FullName)
        if (-not $routeFullPath.StartsWith(
                $functionsPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Vercel prebuilt function route escaped the functions root."
        }
        # Path.GetRelativePath is unavailable in Windows PowerShell 5.1.
        $relativeRoute = $routeFullPath.Substring($functionsPrefix.Length)
        if ($relativeRoute -notin $expectedFunctionRouteNames) {
            throw "Vercel prebuilt function package contains an unexpected function route: $relativeRoute"
        }
        $routeApiName = if ($relativeRoute -match "health(?:\.py)?\.func$") {
            "health.py"
        }
        elseif ($relativeRoute -match "readiness(?:\.py)?\.func$") {
            "readiness.py"
        }
        else { $null }
        if (-not $routeApiName) {
            throw "Vercel prebuilt function route has an unrecognized API binding."
        }
        $derivedRouteNames += $routeApiName
        $vcConfigs = @(
            Get-ChildItem -LiteralPath $routeDir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq ".vc-config.json" }
        )
        if ($vcConfigs.Count -ne 1) {
            throw "Vercel prebuilt function route must contain exactly one .vc-config.json binding."
        }
        try {
            $vcConfigRaw = Get-Content -Raw -LiteralPath $vcConfigs[0].FullName
            Assert-VercelJsonObjectKeysUnique -RawJson $vcConfigRaw
            $vcConfig = $vcConfigRaw | ConvertFrom-Json
        }
        catch {
            throw "Vercel prebuilt function route has an unreadable .vc-config.json binding."
        }
        $expectedVcConfigProperties = @(
            "handler", "runtime", "architecture", "maxDuration", "environment",
            "supportsResponseStreaming", "filePathMap"
        )
        $actualVcConfigProperties = @($vcConfig.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if (@(Compare-Object -ReferenceObject ($expectedVcConfigProperties | Sort-Object) -DifferenceObject ($actualVcConfigProperties | Sort-Object)).Count -ne 0) {
            throw "Vercel prebuilt function route .vc-config.json contains an unexpected schema."
        }
        $handler = $vcConfig.PSObject.Properties["handler"]
        if ($null -eq $handler -or [string]$handler.Value -ne "vc__handler__python.vc_handler") {
            throw "Vercel prebuilt function route .vc-config.json does not bind the Vercel Python wrapper."
        }
        if ([string]$vcConfig.runtime -notmatch '^python3\.[0-9]+$' -or
            [string]$vcConfig.architecture -notin @("x86_64", "arm64") -or
            [int]$vcConfig.maxDuration -le 0 -or
            [bool]$vcConfig.supportsResponseStreaming -ne $true) {
            throw "Vercel prebuilt function route .vc-config.json contains invalid runtime settings."
        }
        $environmentProperties = @($vcConfig.environment.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if (@(Compare-Object -ReferenceObject @("PYTHONPATH", "PYTHONDONTWRITEBYTECODE") -DifferenceObject ($environmentProperties | Sort-Object)).Count -ne 0 -or
            [string]$vcConfig.environment.PYTHONPATH -ne "_vendor" -or
            [string]$vcConfig.environment.PYTHONDONTWRITEBYTECODE -ne "1") {
            throw "Vercel prebuilt function route .vc-config.json contains invalid environment settings."
        }
        $filePathMapProperty = $vcConfig.PSObject.Properties["filePathMap"]
        if ($null -eq $filePathMapProperty -or $null -eq $filePathMapProperty.Value) {
            throw "Vercel prebuilt function route .vc-config.json is missing filePathMap."
        }
        $expectedApiSource = "api/$routeApiName"
        $requiredMapSources = @(
            ".python-version", "api/health.py", "api/public_state.py", "api/readiness.py",
            "pyproject.toml", "uv.lock", "vercel.json"
        )
        $expectedPublicMapSources = @(
            Get-ChildItem -LiteralPath (Join-Path $StageRoot "function_public") -Recurse -File -Force -ErrorAction Stop |
                ForEach-Object {
                    "function_public/" + (Get-VercelRelativePath -Root (Join-Path $StageRoot "function_public") -Path $_.FullName).Replace("\", "/")
                }
        )
        $requiredMapSources += $expectedPublicMapSources
        $mapProperties = @($filePathMapProperty.Value.PSObject.Properties)
        $normalizedMapKeys = @{}
        foreach ($mapProperty in $mapProperties) {
            $mapSource = [string]$mapProperty.Name
            $mapTarget = [string]$mapProperty.Value
            if (-not $mapSource -or -not $mapTarget -or
                $mapSource -ne $mapSource.Replace("\", "/") -or
                $mapTarget -ne $mapTarget.Replace("\", "/") -or
                $mapSource.StartsWith("/") -or $mapTarget.StartsWith("/") -or
                $mapSource -match '(^|/)\.\.(/|$)' -or $mapTarget -match '(^|/)\.\.(/|$)' -or
                $mapSource -match '(^|/)\.(/|$)' -or $mapTarget -match '(^|/)\.(/|$)' -or
                $mapSource -notmatch '^[A-Za-z0-9._/-]+$' -or $mapTarget -notmatch '^[A-Za-z0-9._/-]+$') {
                throw "Vercel prebuilt function route filePathMap contains an unsafe path."
            }
            $mapKeyFolded = $mapSource.ToLowerInvariant()
            if ($normalizedMapKeys.ContainsKey($mapKeyFolded)) {
                throw "Vercel prebuilt function route filePathMap contains duplicate paths."
            }
            $normalizedMapKeys[$mapKeyFolded] = $true
            if ($mapSource -notin $requiredMapSources -and $mapSource -notlike "_vendor/*") {
                throw "Vercel prebuilt function route filePathMap contains an unexpected source."
            }
            if ($mapSource -like "_vendor/*" -and $mapTarget -notlike ".vercel/python/.venv/*") {
                throw "Vercel prebuilt function route filePathMap retargets the generated vendor package."
            }
            if ($mapSource -like "_vendor/*" -and
                $mapSource -notmatch '^_vendor/(?:pip|pip-26\.2\.1\.dist-info|vercel_runtime|vercel_runtime-0\.22\.1\.dist-info)/') {
                throw "Vercel prebuilt function route filePathMap contains an unexpected generated vendor root."
            }
            if ($mapSource -notlike "_vendor/*" -and $mapTarget -ne $mapSource) {
                throw "Vercel prebuilt function route filePathMap retargets a governed source."
            }
            $targetPath = Join-Path $StageRoot ($mapTarget -replace "/", "\")
            if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
                throw "Vercel prebuilt function route filePathMap target is missing: $mapTarget"
            }
            $targetRelative = (Get-VercelRelativePath -Root $StageRoot -Path $targetPath).Replace("\", "/")
            if ($targetRelative -ne $mapTarget) {
                throw "Vercel prebuilt function route filePathMap target escaped the stage root."
            }
            $referencedFiles += [pscustomobject]@{
                route = $relativeRoute.Replace("\", "/")
                source = $mapSource
                target = $mapTarget
                sha256 = Get-VercelFileSha256 -Path $targetPath
                size = [int64](Get-Item -LiteralPath $targetPath -Force).Length
            }
        }
        foreach ($requiredSource in $requiredMapSources) {
            $mapProperty = $filePathMapProperty.Value.PSObject.Properties[$requiredSource]
            if ($null -eq $mapProperty) {
                throw "Vercel prebuilt function route filePathMap is missing $requiredSource."
            }
        }
        $sourceMapProperty = $filePathMapProperty.Value.PSObject.Properties[$expectedApiSource]
        if ($null -eq $sourceMapProperty -or [string]$sourceMapProperty.Value -ne $expectedApiSource) {
            throw "Vercel prebuilt function route filePathMap does not bind its exact API source."
        }
        foreach ($apiSource in @("api/health.py", "api/readiness.py")) {
            $stageApiPath = Join-Path $StageRoot ($apiSource -replace "/", "\")
            $mapProperty = $filePathMapProperty.Value.PSObject.Properties[$apiSource]
            $mappedTargetPath = Join-Path $StageRoot ([string]$mapProperty.Value -replace "/", "\")
            if ((Get-VercelFileSha256 -Path $mappedTargetPath) -ne (Get-VercelFileSha256 -Path $stageApiPath)) {
                throw "Vercel prebuilt function filePathMap bytes do not match exact Git source for $apiSource."
            }
        }
        $wrapperFiles = @(
            Get-ChildItem -LiteralPath $routeDir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq "vc__handler__python.py" }
        )
        if ($wrapperFiles.Count -ne 1) {
            throw "Vercel prebuilt function route must contain exactly one Vercel Python wrapper."
        }
        $wrapper = Get-Content -Raw -LiteralPath $wrapperFiles[0].FullName
        $entrypoint = "api/$routeApiName"
        $entrypointMarker = '__VC_HANDLER_ENTRYPOINT": "' + $entrypoint + '"'
        $moduleMarker = '__VC_HANDLER_MODULE_NAME": "api.' +
            ($routeApiName -replace '\.py$', '') + '"'
        if (
            $wrapper -notmatch ([regex]::Escape($entrypointMarker)) -or
            $wrapper -notmatch ([regex]::Escape($moduleMarker))
        ) {
            throw "Vercel prebuilt function wrapper does not bind the expected source entrypoint."
        }
    }
    if (@(Compare-Object -ReferenceObject @("health.py", "readiness.py") -DifferenceObject ($derivedRouteNames | Sort-Object)).Count -ne 0) {
        throw "Vercel prebuilt function package must bind exactly one health and one readiness route."
    }
    $generatedRuntimeAuthoritySha256 = Assert-VercelGeneratedRuntimeAuthority `
        -StageRoot $StageRoot -ReferencedFiles $referencedFiles
    Assert-VercelPackageInventory `
        -StageRoot $StageRoot `
        -FunctionRouteDirs $functionRouteDirs `
        -ReferencedFiles $referencedFiles `
        -GeneratedRuntimeAuthoritySha256 $generatedRuntimeAuthoritySha256
    $packageManifestSha256 = Get-VercelPackageManifestSha256 -StageRoot $StageRoot
    if ($ExpectedPackageManifestSha256 -and
        $packageManifestSha256 -ne $ExpectedPackageManifestSha256.ToLowerInvariant()) {
        throw "Vercel prebuilt package inventory manifest hash changed after the build."
    }
    return $packageManifestSha256
}
