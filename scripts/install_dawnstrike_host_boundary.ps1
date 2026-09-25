[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [Parameter(Mandatory = $true)][string]$CandidateRoot,
    [string]$DependencySourcePython = 'C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe',
    [string]$InstallRoot = 'C:\Program Files\Dawnstrike',
    [string]$ReceiptRoot = 'C:\ProgramData\Dawnstrike',
    [string]$StateRoot = 'C:\r\dawnstrike-state',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$BoundaryPredecessorSha = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$BoundaryPredecessorTree = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$RuntimePredecessorSha = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$RuntimePredecessorTree = ''
)

$global:PSModuleAutoLoadingPreference = 'None'
$env:PSModulePath = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
if (
    [string]$global:DawnstrikePowerShellModuleBoundary.schema_version -cne
    'dawnstrike.powershell_module_boundary.v1' -or
    $null -eq $global:DawnstrikePowerShellTempGuard -or
    -not $global:DawnstrikePowerShellTempGuard.CanWrite
) {
    throw 'Host-boundary installer requires the retained bootstrap PowerShell boundary.'
}
Initialize-DawnstrikePowerShellModuleBoundary

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Dawnstrike host-boundary installation requires an elevated administrator process.'
}
if (
    $InstallRoot -cne 'C:\Program Files\Dawnstrike' -or
    $ReceiptRoot -cne 'C:\ProgramData\Dawnstrike' -or
    $StateRoot -cne 'C:\r\dawnstrike-state'
) {
    throw 'Dawnstrike host-boundary installation paths are fixed host trust anchors.'
}
$migrationInputs = @(
    $BoundaryPredecessorSha,
    $BoundaryPredecessorTree,
    $RuntimePredecessorSha,
    $RuntimePredecessorTree
)
$migrationInputCount = @(
    $migrationInputs | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) }
).Count
if ($migrationInputCount -notin @(0, 4)) {
    throw 'Host-boundary candidate migration requires all four predecessor SHA/tree identities.'
}

$installMutex = [Threading.Mutex]::new($false, 'Global\Dawnstrike.HostBoundary.Install.v1')
$installMutexAcquired = $false
try {
try {
    $installMutexAcquired = $installMutex.WaitOne(0, $false)
}
catch [Threading.AbandonedMutexException] {
    # The abandoned owner is gone and this thread now owns the mutex. Active
    # Git, Python, dependency, and release identities are published only as
    # self-sealed directories, so retry can validate and reuse exact roots.
    $installMutexAcquired = $true
}
if (-not $installMutexAcquired) {
    throw 'Another Dawnstrike host-boundary installation is already active.'
}

$requestedCandidate = [IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
$canonicalOrigin = 'https://github.com/mattfren/DawnStrike.git'
$expectedGitHash = '78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f' # pragma: allowlist secret
$gitArchiveUri = 'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/MinGit-2.55.0.5-64-bit.zip'
$expectedGitArchiveHash = '56d7b226b7693196cfc71fef26568f536c4a021ab6c37ff2db4287bed908e96e' # pragma: allowlist secret
$expectedGitArchiveLength = 38989688L
$expectedGitSubject = 'CN=Johannes Schindelin, O=Johannes Schindelin, L=Bruehl, C=DE'
$expectedGitThumbprint = '2A1E97CBF0DFCDA15B0DA0AC9745014F989D4AD0' # pragma: allowlist secret
$expectedPythonHash = '85b71d8c6ec1905935f74be0c9869aae198d00e98f39df699ec66f9c5a84cecd' # pragma: allowlist secret
$expectedPythonSubject = 'CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US'
$expectedPythonThumbprint = '847785B686B2D3879731FA9AA3F1F5D48E85D99E' # pragma: allowlist secret
$pythonCoreHashes = [ordered]@{
    'python.exe' = '85b71d8c6ec1905935f74be0c9869aae198d00e98f39df699ec66f9c5a84cecd' # pragma: allowlist secret
    'python3.dll' = '76e7fd69a01081726308317d6e502721c2ba6792b48880dc1ef649c755788db7' # pragma: allowlist secret
    'python313.dll' = 'e820bf024efd2b56bb2b82791e6b6ddc7303f070f8e72cba7637482a8a906238' # pragma: allowlist secret
    'vcruntime140.dll' = 'd1f4225df2cd877dbf130d5668a021dce3f94118455ff5ec952061c30afc9ce7' # pragma: allowlist secret
    'vcruntime140_1.dll' = 'a7146c08f89fe5b04541ab507cdb59ff7b44534d4ba3c668a426c6450a03434e' # pragma: allowlist secret
    'DLLs\_hashlib.pyd' = '79a26289aef0d92545a082507c7021f10e219621d137cb726c4d0fbbfd4b46ae' # pragma: allowlist secret
    'DLLs\libcrypto-3.dll' = '09499e186cf0e434ffa17ad153aa9a66d6048c54a2c03b823e2a06c49d9affe4' # pragma: allowlist secret
    'DLLs\_ssl.pyd' = '8d2f51fa1dc929073a2ad8d1c7c0c911e8f695870dcf4dda2846f4b75a7f77fb' # pragma: allowlist secret
    'DLLs\libssl-3.dll' = '28c395290279fa4f4f54a57b85cd66d853895f925fefb8f6fdebc0d8b1cdd07c' # pragma: allowlist secret
    'DLLs\_socket.pyd' = 'ccbd0f83df3784da08147b8e8765e28c978de28b5c0eddcfba5567b59d43fef9' # pragma: allowlist secret
    'DLLs\select.pyd' = '024f955349fe46cf1bb2c22c6b57fcf38cf5755d46166fbf63e7e5b92433b111' # pragma: allowlist secret
    'Lib\hashlib.py' = '406f36c3db109c14f737a8d99e6897d3ab8620be454abdbb3a4ce09c95713388' # pragma: allowlist secret
}
$pythonInstallerUri = 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe'
$expectedPythonInstallerHash = 'edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403' # pragma: allowlist secret
$expectedPythonInstallerLength = 29452944L
$expectedUvHash = 'b5d230c79ffa3629422f48bfce0766e9827769608a79bbb4e4e540081f59d97c' # pragma: allowlist secret
$nodeArchiveUri = 'https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip'
$expectedNodeArchiveHash = '6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba' # pragma: allowlist secret
$expectedNodeArchiveLength = 37539751L
$expectedNodeHash = '5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5' # pragma: allowlist secret
$expectedNodeLength = 93381448L
$expectedNodeSubject = 'CN=OpenJS Foundation, O=OpenJS Foundation, L=San Francisco, S=California, C=US'
$expectedNodeThumbprint = 'D1DC7755FAB17F01224CCCEA1C2FAEDC6F963E12' # pragma: allowlist secret
$nodeRoot = Join-Path $InstallRoot 'Node-24.20.0'
$nodeExecutable = Join-Path $nodeRoot 'node.exe'
$nodeBoundaryManifestName = '.dawnstrike-node-boundary-v1.json'
$nodeBoundaryManifest = Join-Path $nodeRoot $nodeBoundaryManifestName
$nodeBoundaryDisposition = 'REUSED_EXACT_SEALED_NODE'
$vercelSourceRoot = 'C:\Users\MattFields\AppData\Local\npm-cache\_npx\80bcb0c7f142fce6'
$vercelTreeSha256 = '3bfb7509c4bf6a8fec920566c290a385c8160b9851b2350a655f0fd8b6c9e069' # pragma: allowlist secret
$vercelTreeFileCount = 7131
$vercelVersion = '59.11.2'
$vercelEntryRelative = 'node_modules/vercel/dist/vc.js'
$vercelEntrySha256 = '2dd6e7c273a24bf4317af867d9b7bacb4db35487b42ea77912e2e7c33fa0c152' # pragma: allowlist secret
$vercelRoot = Join-Path $InstallRoot ('VercelCli-' + $vercelTreeSha256)
$vercelBoundaryManifestName = '.dawnstrike-vercel-cli-boundary-v1.json'
$vercelBoundaryManifest = Join-Path $vercelRoot $vercelBoundaryManifestName
$vercelEntry = Join-Path $vercelRoot ($vercelEntryRelative.Replace('/', '\'))
$vercelBoundaryDisposition = 'REUSED_EXACT_SEALED_VERCEL_CLI'
$installerRelative = 'scripts/install_dawnstrike_host_boundary.ps1'
$launcherRelative = 'scripts/dawnstrike_release_launcher.ps1'
$stateBoundaryRelative = 'scripts/state_root_boundary.ps1'
$installerRoot = Join-Path $InstallRoot ('installers\' + $ExpectedSha)
$installerDestination = Join-Path $installerRoot 'install_dawnstrike_host_boundary.ps1'
if ([IO.Path]::GetFullPath($PSCommandPath) -cne $installerDestination) {
    throw 'Dawnstrike host-boundary installation must run from the protected installed bootstrap path.'
}
$pythonSource = [IO.Path]::GetFullPath($DependencySourcePython)
$pythonSourceRoot = Split-Path -Parent $pythonSource
$pythonDestination = Join-Path $InstallRoot 'Python313'
$pythonBoundaryManifestName = '.dawnstrike-python-boundary-v1.json'
$pythonBoundaryManifest = Join-Path $pythonDestination $pythonBoundaryManifestName
$gitRoot = Join-Path $InstallRoot 'Git-2.55.0.5'
$git = Join-Path $gitRoot 'cmd\git.exe'
$gitBoundaryManifestName = '.dawnstrike-git-boundary-v1.json'
$gitBoundaryManifest = Join-Path $gitRoot $gitBoundaryManifestName
$releaseParent = Join-Path $InstallRoot 'releases'
$protectedReleaseRoot = Join-Path $releaseParent $ExpectedSha
$launcherDestination = Join-Path $protectedReleaseRoot $launcherRelative
$stateBoundaryDestination = Join-Path $protectedReleaseRoot $stateBoundaryRelative
$releaseAdmissionManifestName = 'dawnstrike-host-admission-v1.json'
$protectedImportRoot = ''
$emptyTemplateRoot = ''
$candidate = ''
$safeGit = @()
$candidateTree = ''
$requirementsLockBlob = ''
$requirementsLockSha256 = ''
$releaseDisposition = 'FRESH_CURRENT_MAIN_ATOMIC_IMPORT'
$launcherBlob = ''
$stateBoundaryBlob = ''

function Get-DawnstrikeInstallSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($Path)))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Save-DawnstrikePinnedDownload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][uri]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][long]$ExpectedLength
    )

    $destinationFull = [IO.Path]::GetFullPath($Destination)
    $installPrefix = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\'
    if (-not $destinationFull.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Pinned download destination escaped the protected installation root.'
    }
    Assert-DawnstrikeInstallNoReparse -Path (Split-Path -Parent $destinationFull) `
        -Label 'Pinned download destination parent'
    $destinationHandle = [IO.File]::Open(
        $destinationFull,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    try {
        $request = [Net.HttpWebRequest]::Create($Uri)
        $request.AllowAutoRedirect = $true
        $request.MaximumAutomaticRedirections = 5
        $request.UserAgent = 'Dawnstrike-Host-Boundary/1.0'
        $request.Timeout = 120000
        $request.ReadWriteTimeout = 120000
        $response = $request.GetResponse()
        try {
            $responseStream = $response.GetResponseStream()
            try { $responseStream.CopyTo($destinationHandle) }
            finally { $responseStream.Dispose() }
        }
        finally { $response.Dispose() }
        $destinationHandle.Flush($true)
        if ($destinationHandle.Length -ne $ExpectedLength) {
            throw 'Pinned download length is invalid.'
        }
        $destinationHandle.Position = 0
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $actual = ([BitConverter]::ToString($sha.ComputeHash($destinationHandle))).Replace('-', '').ToLowerInvariant()
        }
        finally { $sha.Dispose() }
        if ($actual -cne $ExpectedSha256) { throw 'Pinned download digest is invalid.' }
    }
    finally { $destinationHandle.Dispose() }
    Set-DawnstrikeProtectedReadableFileAcl -Path $destinationFull
}

function Copy-DawnstrikePinnedFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256
    )

    $sourceHandle = [IO.File]::Open(
        $Source,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $actual = ([BitConverter]::ToString($sha.ComputeHash($sourceHandle))).Replace('-', '').ToLowerInvariant()
        }
        finally { $sha.Dispose() }
        if ($actual -cne $ExpectedSha256) { throw 'Pinned file source digest is invalid.' }
        $sourceHandle.Position = 0
        $destinationHandle = [IO.File]::Open(
            $Destination,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $sourceHandle.CopyTo($destinationHandle)
            $destinationHandle.Flush($true)
        }
        finally { $destinationHandle.Dispose() }
    }
    finally { $sourceHandle.Dispose() }
}

function New-DawnstrikeProtectedDirectorySecurity {
    [CmdletBinding()]
    param()

    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $none = [Security.AccessControl.PropagationFlags]::None
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($administrators, 'FullControl', $inheritance, $none, 'Allow'))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($system, 'FullControl', $inheritance, $none, 'Allow'))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($users, 'ReadAndExecute', $inheritance, $none, 'Allow'))
    return $acl
}

function Set-DawnstrikeProtectedDirectoryAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $acl = New-DawnstrikeProtectedDirectorySecurity
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Set-DawnstrikeProtectedReadableFileAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
    $none = [Security.AccessControl.InheritanceFlags]::None
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $administrators, 'FullControl', $none, $propagation, 'Allow'
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $system, 'FullControl', $none, $propagation, 'Allow'
    ))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $users, 'ReadAndExecute', $none, $propagation, 'Allow'
    ))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Assert-DawnstrikeInstallNoReparse {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [IO.Path]::GetFullPath($Path)
    $current = [IO.Path]::GetPathRoot($full)
    foreach ($segment in $full.Substring($current.Length).Split([IO.Path]::DirectorySeparatorChar, [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $segment
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse point."
            }
        }
    }
}

function Assert-DawnstrikeInstalledBoundaryAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    $writeLikeRights = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership -bor
        [Security.AccessControl.FileSystemRights]::FullControl
    )
    $trustedSids = @{
        'S-1-5-18' = $true
        'S-1-5-32-544' = $true
        'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' = $true
    }
    foreach ($path in $Paths) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Installed Dawnstrike host boundary contains a reparse point.'
        }
        $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
        try { $ownerSid = [string]$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value }
        catch { throw 'Installed Dawnstrike host boundary owner SID cannot be resolved.' }
        if (-not $trustedSids.ContainsKey($ownerSid)) {
            throw 'Installed Dawnstrike host boundary is not administrator-owned.'
        }
        foreach ($rule in @($acl.Access)) {
            try {
                $ruleSid = [string]$rule.IdentityReference.Translate(
                    [Security.Principal.SecurityIdentifier]
                ).Value
            }
            catch { throw 'Installed Dawnstrike host boundary ACE SID cannot be resolved.' }
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                -not $trustedSids.ContainsKey($ruleSid) -and
                ($rule.FileSystemRights -band $writeLikeRights) -ne 0
            ) {
                throw 'Installed Dawnstrike host boundary grants non-admin write access.'
            }
        }
    }
}

function Get-DawnstrikeProtectedTreeEntries {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string[]]$ExcludeRelative = @()
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $rootPrefix = $rootFull + '\'
    $excluded = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($relative in $ExcludeRelative) {
        $normalized = ([string]$relative).Replace('\', '/')
        if (
            -not $normalized -or
            [IO.Path]::IsPathRooted($normalized) -or
            $normalized -match '(^|/)\.\.(/|$)' -or
            $normalized -match '(^|/)\.(/|$)'
        ) {
            throw 'Protected manifest exclusion is not a safe relative path.'
        }
        $null = $excluded.Add($normalized)
    }
    foreach ($file in @(Get-ChildItem -LiteralPath $rootFull -File -Recurse -Force -ErrorAction Stop | Sort-Object FullName)) {
        $full = [IO.Path]::GetFullPath([string]$file.FullName)
        if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Protected Python manifest enumeration escaped its root.'
        }
        $relative = $full.Substring($rootPrefix.Length).Replace('\', '/')
        if ($excluded.Contains($relative)) { continue }
        [ordered]@{
            path = $relative
            length = [long]$file.Length
            sha256 = Get-DawnstrikeInstallSha256 $full
        }
    }
}

function Get-DawnstrikeVercelTreeIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object[]]$Entries)

    $canonical = @(
        $Entries |
            Sort-Object `
                @{ Expression = { ([string]$_.path).ToLowerInvariant() } }, `
                @{ Expression = { [string]$_.path } } |
            ForEach-Object {
                [string]$_.path + '|' + [string][long]$_.length + '|' + [string]$_.sha256
            }
    ) -join "`n"
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString(
            $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))
        )).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Assert-DawnstrikePinnedVercelPayload {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $rootFull -PathType Container)) {
        throw 'Pinned Vercel CLI payload root is absent.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $rootFull -Label 'Pinned Vercel CLI payload root'
    $items = @(Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction Stop)
    $reparse = $items | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $reparse) { throw 'Pinned Vercel CLI payload contains a reparse point.' }
    Assert-DawnstrikeInstalledBoundaryAcl -Paths (
        @($rootFull) + @($items | ForEach-Object { [string]$_.FullName })
    )
    $entries = @(
        Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
            -ExcludeRelative @($vercelBoundaryManifestName)
    )
    $treeSha256 = Get-DawnstrikeVercelTreeIdentity -Entries $entries
    if ($entries.Count -ne $vercelTreeFileCount -or $treeSha256 -cne $vercelTreeSha256) {
        throw 'Pinned Vercel CLI payload identity is invalid.'
    }
    $entryPath = Join-Path $rootFull ($vercelEntryRelative.Replace('/', '\'))
    if (
        -not (Test-Path -LiteralPath $entryPath -PathType Leaf) -or
        (Get-DawnstrikeInstallSha256 $entryPath) -cne $vercelEntrySha256
    ) { throw 'Pinned Vercel CLI entrypoint identity is invalid.' }
    $packagePath = Join-Path $rootFull 'node_modules\vercel\package.json'
    try { $package = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($packagePath)) | ConvertFrom-Json }
    catch { throw 'Pinned Vercel CLI package metadata is invalid.' }
    if ([string]$package.name -cne 'vercel' -or [string]$package.version -cne $vercelVersion) {
        throw 'Pinned Vercel CLI package version is invalid.'
    }
    return @($entries)
}

function Write-DawnstrikeProtectedVercelManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestPath = Join-Path $rootFull $vercelBoundaryManifestName
    if (Test-Path -LiteralPath $manifestPath) {
        throw 'Protected Vercel CLI boundary manifest already exists.'
    }
    $entries = @(Assert-DawnstrikePinnedVercelPayload -Root $rootFull)
    $payload = [ordered]@{
        schema_version = 'dawnstrike.vercel_cli_boundary.v1'
        tree_sha256 = $vercelTreeSha256
        file_count = $vercelTreeFileCount
        entry_relative_path = $vercelEntryRelative
        entry_sha256 = $vercelEntrySha256
        vercel_version = $vercelVersion
        files = @($entries)
        research_only = $true
        broker_execution_enabled = $false
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($payload | ConvertTo-Json -Depth 6))
    $temporary = Join-Path $rootFull ('.vercel-boundary-write-' + [Guid]::NewGuid().ToString('N'))
    try {
        $stream = [IO.File]::Open(
            $temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Set-DawnstrikeProtectedReadableFileAcl -Path $temporary
        [IO.File]::Move($temporary, $manifestPath)
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Assert-DawnstrikeProtectedVercelTree {
    [CmdletBinding()]
    param(
        [string]$Root = $vercelRoot,
        [string]$ManifestPath = $vercelBoundaryManifest
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestFull = [IO.Path]::GetFullPath($ManifestPath)
    if ($manifestFull -cne (Join-Path $rootFull $vercelBoundaryManifestName)) {
        throw 'Protected Vercel CLI manifest is outside its atomic root.'
    }
    if (-not (Test-Path -LiteralPath $manifestFull -PathType Leaf)) {
        throw 'Protected Vercel CLI boundary manifest is absent.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $manifestFull -Label 'Protected Vercel CLI manifest'
    Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestFull)
    try {
        $manifest = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($manifestFull)) | ConvertFrom-Json
    }
    catch { throw 'Protected Vercel CLI boundary manifest is invalid JSON.' }
    if (
        [string]$manifest.schema_version -cne 'dawnstrike.vercel_cli_boundary.v1' -or
        [string]$manifest.tree_sha256 -cne $vercelTreeSha256 -or
        [int]$manifest.file_count -ne $vercelTreeFileCount -or
        [string]$manifest.entry_relative_path -cne $vercelEntryRelative -or
        [string]$manifest.entry_sha256 -cne $vercelEntrySha256 -or
        [string]$manifest.vercel_version -cne $vercelVersion -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false
    ) { throw 'Protected Vercel CLI boundary manifest contract is invalid.' }
    $expected = @{}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            $relative.Contains('\') -or
            $relative.Contains(':') -or
            [IO.Path]::IsPathRooted($relative) -or
            $relative -match '(^|/)(?:\.|\.\.)(/|$)' -or
            [string]::Equals($relative, $vercelBoundaryManifestName, [StringComparison]::OrdinalIgnoreCase) -or
            $expected.ContainsKey($relative) -or
            [string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
            [long]$entry.length -lt 0
        ) { throw 'Protected Vercel CLI manifest contains an invalid file identity.' }
        $expected[$relative] = $entry
    }
    $actual = @(Assert-DawnstrikePinnedVercelPayload -Root $rootFull)
    if ($actual.Count -ne $expected.Count) {
        throw 'Protected Vercel CLI file set differs from its sealed manifest.'
    }
    foreach ($entry in $actual) {
        $relative = [string]$entry.path
        if (-not $expected.ContainsKey($relative)) {
            throw "Protected Vercel CLI contains an unsealed file: $relative"
        }
        $sealed = $expected[$relative]
        if (
            [long]$entry.length -ne [long]$sealed.length -or
            [string]$entry.sha256 -cne [string]$sealed.sha256
        ) { throw "Protected Vercel CLI file identity differs from its seal: $relative" }
    }
    return $manifest
}

function Copy-DawnstrikePinnedVercelTree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $sourceFull = [IO.Path]::GetFullPath($Source).TrimEnd('\')
    $sourcePrefix = $sourceFull + '\'
    $destinationFull = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
    $destinationPrefix = $destinationFull + '\'
    if (-not [string]::Equals($sourceFull, $vercelSourceRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Pinned Vercel CLI source root is not the governed cache identity.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $sourceFull -Label 'Pinned Vercel CLI source root'
    foreach ($item in @(Get-ChildItem -LiteralPath $sourceFull -Recurse -Force -ErrorAction Stop)) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Pinned Vercel CLI source contains a reparse point.'
        }
    }
    $sourceFiles = @(
        Get-ChildItem -LiteralPath $sourceFull -File -Recurse -Force -ErrorAction Stop |
            Sort-Object @{ Expression = { ([string]$_.FullName).ToLowerInvariant() } }
    )
    if ($sourceFiles.Count -ne $vercelTreeFileCount) {
        throw 'Pinned Vercel CLI source file count is invalid.'
    }
    foreach ($sourceFile in $sourceFiles) {
        $sourcePath = [IO.Path]::GetFullPath([string]$sourceFile.FullName)
        if (-not $sourcePath.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Pinned Vercel CLI source enumeration escaped its root.'
        }
        $relative = $sourcePath.Substring($sourcePrefix.Length).Replace('\', '/')
        if (
            -not $relative -or
            $relative.Contains(':') -or
            $relative -match '(^|/)(?:\.|\.\.)(/|$)'
        ) { throw 'Pinned Vercel CLI source contains an unsafe relative path.' }
        $target = [IO.Path]::GetFullPath((Join-Path $destinationFull $relative.Replace('/', '\')))
        if (-not $target.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Pinned Vercel CLI copy escaped its protected stage.'
        }
        $null = [IO.Directory]::CreateDirectory((Split-Path -Parent $target))
        $sourceStream = [IO.File]::Open(
            $sourcePath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        try {
            $targetStream = [IO.File]::Open(
                $target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None
            )
            try {
                $sourceStream.CopyTo($targetStream)
                $targetStream.Flush($true)
            }
            finally { $targetStream.Dispose() }
        }
        finally { $sourceStream.Dispose() }
    }
}

function Expand-DawnstrikePinnedZip {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destinationFull = [IO.Path]::GetFullPath($Destination).TrimEnd('\')
    $destinationPrefix = $destinationFull + '\'
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        foreach ($entry in $archive.Entries) {
            $relative = ([string]$entry.FullName).Replace('/', '\')
            if (
                -not $relative -or
                [IO.Path]::IsPathRooted($relative) -or
                $relative -match '(^|\\)\.\.(\\|$)' -or
                $relative.Contains(':')
            ) {
                throw 'Pinned Git archive contains an unsafe path.'
            }
            $target = [IO.Path]::GetFullPath((Join-Path $destinationFull $relative))
            if (-not $target.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Pinned Git archive path escaped the protected staging root.'
            }
            if (-not $seen.Add($target)) { throw 'Pinned Git archive contains a duplicate path.' }
            if (-not [string]$entry.Name) {
                $null = [IO.Directory]::CreateDirectory($target)
                continue
            }
            $parent = Split-Path -Parent $target
            $null = [IO.Directory]::CreateDirectory($parent)
            $source = $entry.Open()
            try {
                $destinationStream = [IO.File]::Open(
                    $target,
                    [IO.FileMode]::CreateNew,
                    [IO.FileAccess]::Write,
                    [IO.FileShare]::None
                )
                try {
                    $source.CopyTo($destinationStream)
                    $destinationStream.Flush($true)
                }
                finally { $destinationStream.Dispose() }
            }
            finally { $source.Dispose() }
        }
    }
    finally { $archive.Dispose() }
}

function Assert-DawnstrikeProtectedGitTree {
    [CmdletBinding()]
    param([string]$Root = $gitRoot)

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestPath = Join-Path $rootFull $gitBoundaryManifestName
    $gitPath = Join-Path $rootFull 'cmd\git.exe'
    if (-not (Test-Path -LiteralPath $rootFull -PathType Container)) {
        throw 'Protected Git root is absent.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $rootFull -Label 'Protected Git root'
    $items = @(Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction Stop)
    $reparse = $items | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $reparse) { throw 'Protected Git tree contains a reparse point.' }
    Assert-DawnstrikeInstalledBoundaryAcl -Paths (@($rootFull) + @($items | ForEach-Object { [string]$_.FullName }))
    foreach ($relative in @(
        'cmd\git.exe',
        'mingw64\bin\git.exe',
        'mingw64\bin\git-remote-https.exe',
        'mingw64\bin\libcurl-4.dll',
        'mingw64\bin\libssl-3-x64.dll',
        'mingw64\bin\libcrypto-3-x64.dll',
        'mingw64\etc\ssl\certs\ca-bundle.crt'
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $rootFull $relative) -PathType Leaf)) {
            throw "Protected Git execution closure is incomplete: $relative"
        }
    }
    if ((Get-DawnstrikeInstallSha256 $gitPath) -cne $expectedGitHash) {
        throw 'Protected Git executable hash is invalid.'
    }
    $gitSignature = Get-AuthenticodeSignature -LiteralPath $gitPath -ErrorAction Stop
    if (
        [string]$gitSignature.Status -cne 'Valid' -or
        $null -eq $gitSignature.SignerCertificate -or
        [string]$gitSignature.SignerCertificate.Subject -cne $expectedGitSubject -or
        [string]$gitSignature.SignerCertificate.Thumbprint -cne $expectedGitThumbprint
    ) {
        throw 'Protected Git executable signer is invalid.'
    }
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Protected Git boundary manifest is absent.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $manifestPath -Label 'Protected Git boundary manifest'
    Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    try {
        $manifest = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($manifestPath)) | ConvertFrom-Json
    }
    catch { throw 'Protected Git boundary manifest is invalid JSON.' }
    if (
        [string]$manifest.schema_version -cne 'dawnstrike.git_boundary.v1' -or
        [string]$manifest.archive_uri -cne $gitArchiveUri -or
        [string]$manifest.archive_sha256 -cne $expectedGitArchiveHash -or
        [string]$manifest.git_sha256 -cne $expectedGitHash
    ) {
        throw 'Protected Git boundary manifest contract is invalid.'
    }
    $expectedFiles = @{}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            [IO.Path]::IsPathRooted($relative) -or
            $relative -match '(^|/)\.\.(/|$)' -or
            $relative -match '(^|/)\.(/|$)' -or
            $expectedFiles.ContainsKey($relative) -or
            [string]$entry.sha256 -notmatch '^[0-9a-f]{64}$' -or
            [long]$entry.length -lt 0
        ) {
            throw 'Protected Git boundary manifest contains an invalid file identity.'
        }
        $expectedFiles[$relative] = $entry
    }
    $actualFiles = @(
        Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
            -ExcludeRelative @($gitBoundaryManifestName)
    )
    if ($actualFiles.Count -ne $expectedFiles.Count) {
        throw 'Protected Git file set differs from its sealed manifest.'
    }
    foreach ($actual in $actualFiles) {
        $relative = [string]$actual.path
        if (-not $expectedFiles.ContainsKey($relative)) {
            throw "Protected Git contains an unsealed file: $relative"
        }
        $expected = $expectedFiles[$relative]
        if (
            [long]$actual.length -ne [long]$expected.length -or
            [string]$actual.sha256 -cne [string]$expected.sha256
        ) {
            throw "Protected Git file identity differs from its sealed manifest: $relative"
        }
    }
}

function Write-DawnstrikeProtectedGitManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestPath = Join-Path $rootFull $gitBoundaryManifestName
    if (Test-Path -LiteralPath $manifestPath) {
        throw 'Protected Git boundary manifest already exists.'
    }
    $payload = [ordered]@{
        schema_version = 'dawnstrike.git_boundary.v1'
        archive_uri = $gitArchiveUri
        archive_sha256 = $expectedGitArchiveHash
        git_sha256 = $expectedGitHash
        files = @(
            Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
                -ExcludeRelative @($gitBoundaryManifestName)
        )
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($payload | ConvertTo-Json -Depth 6))
    $temporary = Join-Path $rootFull ('.git-boundary-write-' + [Guid]::NewGuid().ToString('N'))
    try {
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Set-DawnstrikeProtectedReadableFileAcl -Path $temporary
        [IO.File]::Move($temporary, $manifestPath)
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Expand-DawnstrikePinnedNodeExecutable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ArchivePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $expectedEntry = 'node-v24.20.0-win-x64/node.exe'
    $matches = @()
    $archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $archive.Entries) {
            $relative = ([string]$entry.FullName).Replace('\', '/')
            if (
                -not $relative -or
                [IO.Path]::IsPathRooted($relative) -or
                $relative.Contains(':') -or
                $relative -match '(^|/)\.\.(/|$)' -or
                -not $seen.Add($relative)
            ) { throw 'Pinned Node archive contains an unsafe or duplicate path.' }
            if ($relative -ceq $expectedEntry) { $matches += $entry }
        }
        if ($matches.Count -ne 1 -or [long]$matches[0].Length -ne $expectedNodeLength) {
            throw 'Pinned Node archive does not contain the exact executable entry.'
        }
        $source = $matches[0].Open()
        try {
            $target = Join-Path $Destination 'node.exe'
            $output = [IO.File]::Open(
                $target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None
            )
            try {
                $source.CopyTo($output)
                $output.Flush($true)
            }
            finally { $output.Dispose() }
        }
        finally { $source.Dispose() }
    }
    finally { $archive.Dispose() }
}

function Write-DawnstrikeProtectedNodeManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestPath = Join-Path $rootFull $nodeBoundaryManifestName
    if (Test-Path -LiteralPath $manifestPath) {
        throw 'Protected Node boundary manifest already exists.'
    }
    $payload = [ordered]@{
        schema_version = 'dawnstrike.node_boundary.v1'
        node_version = '24.20.0'
        archive_uri = $nodeArchiveUri
        archive_sha256 = $expectedNodeArchiveHash
        archive_length = $expectedNodeArchiveLength
        node_sha256 = $expectedNodeHash
        node_length = $expectedNodeLength
        signer_subject = $expectedNodeSubject
        signer_thumbprint = $expectedNodeThumbprint
        files = @(
            Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
                -ExcludeRelative @($nodeBoundaryManifestName)
        )
        research_only = $true
        broker_execution_enabled = $false
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($payload | ConvertTo-Json -Depth 6))
    $temporary = Join-Path $rootFull ('.node-boundary-write-' + [Guid]::NewGuid().ToString('N'))
    try {
        $stream = [IO.File]::Open(
            $temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Set-DawnstrikeProtectedReadableFileAcl -Path $temporary
        [IO.File]::Move($temporary, $manifestPath)
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Assert-DawnstrikeProtectedNodeBoundary {
    [CmdletBinding()]
    param(
        [string]$Root = $nodeRoot,
        [string]$ManifestPath = $nodeBoundaryManifest
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestFull = [IO.Path]::GetFullPath($ManifestPath)
    $nodePath = Join-Path $rootFull 'node.exe'
    if ($manifestFull -cne (Join-Path $rootFull $nodeBoundaryManifestName)) {
        throw 'Protected Node manifest is outside its atomic root.'
    }
    if (-not (Test-Path -LiteralPath $rootFull -PathType Container) -or
        -not (Test-Path -LiteralPath $manifestFull -PathType Leaf) -or
        -not (Test-Path -LiteralPath $nodePath -PathType Leaf)) {
        throw 'Protected Node boundary is incomplete.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $rootFull -Label 'Protected Node root'
    $items = @(Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction Stop)
    if ($null -ne ($items | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1)) { throw 'Protected Node boundary contains a reparse point.' }
    Assert-DawnstrikeInstalledBoundaryAcl -Paths (
        @($rootFull) + @($items | ForEach-Object { [string]$_.FullName })
    )
    $entries = @(
        Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
            -ExcludeRelative @($nodeBoundaryManifestName)
    )
    if ($entries.Count -ne 1 -or [string]$entries[0].path -cne 'node.exe' -or
        [long]$entries[0].length -ne $expectedNodeLength -or
        [string]$entries[0].sha256 -cne $expectedNodeHash) {
        throw 'Protected Node file set or executable identity is invalid.'
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $nodePath -ErrorAction Stop
    if ([string]$signature.Status -cne 'Valid' -or $null -eq $signature.SignerCertificate -or
        [string]$signature.SignerCertificate.Subject -cne $expectedNodeSubject -or
        [string]$signature.SignerCertificate.Thumbprint -cne $expectedNodeThumbprint) {
        throw 'Protected Node executable signer is invalid.'
    }
    try {
        $manifest = [Text.Encoding]::UTF8.GetString(
            [IO.File]::ReadAllBytes($manifestFull)
        ) | ConvertFrom-Json
    }
    catch { throw 'Protected Node boundary manifest is invalid JSON.' }
    $expectedKeys = @(
        'archive_length', 'archive_sha256', 'archive_uri', 'broker_execution_enabled',
        'files', 'node_length', 'node_sha256', 'node_version', 'research_only',
        'schema_version', 'signer_subject', 'signer_thumbprint'
    ) | Sort-Object
    $actualKeys = @($manifest.PSObject.Properties.Name | Sort-Object)
    if ((ConvertTo-Json $actualKeys -Compress) -cne (ConvertTo-Json $expectedKeys -Compress) -or
        [string]$manifest.schema_version -cne 'dawnstrike.node_boundary.v1' -or
        [string]$manifest.node_version -cne '24.20.0' -or
        [string]$manifest.archive_uri -cne $nodeArchiveUri -or
        [string]$manifest.archive_sha256 -cne $expectedNodeArchiveHash -or
        [long]$manifest.archive_length -ne $expectedNodeArchiveLength -or
        [string]$manifest.node_sha256 -cne $expectedNodeHash -or
        [long]$manifest.node_length -ne $expectedNodeLength -or
        [string]$manifest.signer_subject -cne $expectedNodeSubject -or
        [string]$manifest.signer_thumbprint -cne $expectedNodeThumbprint -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false -or
        @($manifest.files).Count -ne 1 -or
        [string]$manifest.files[0].path -cne 'node.exe' -or
        [long]$manifest.files[0].length -ne $expectedNodeLength -or
        [string]$manifest.files[0].sha256 -cne $expectedNodeHash) {
        throw 'Protected Node boundary manifest contract is invalid.'
    }
    return $manifest
}

function Assert-DawnstrikeProtectedPythonTree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$ManifestPath = ''
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not (Test-Path -LiteralPath $rootFull -PathType Container)) {
        throw 'Protected Python root is absent.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $rootFull -Label 'Protected Python root'
    $items = @(Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction Stop)
    $reparse = $items | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $reparse) { throw 'Protected Python tree contains a reparse point.' }
    Assert-DawnstrikeInstalledBoundaryAcl -Paths (@($rootFull) + @($items | ForEach-Object { [string]$_.FullName }))

    foreach ($relative in $pythonCoreHashes.Keys) {
        $path = Join-Path $rootFull $relative
        if (
            -not (Test-Path -LiteralPath $path -PathType Leaf) -or
            (Get-DawnstrikeInstallSha256 $path) -cne [string]$pythonCoreHashes[$relative]
        ) {
            throw "Protected Python loader-closure hash is invalid: $relative"
        }
    }
    foreach ($relative in @(
        'python.exe',
        'python3.dll',
        'python313.dll',
        'DLLs\_hashlib.pyd',
        'DLLs\_ssl.pyd',
        'DLLs\_socket.pyd',
        'DLLs\select.pyd'
    )) {
        $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $rootFull $relative) -ErrorAction Stop
        if (
            [string]$signature.Status -cne 'Valid' -or
            $null -eq $signature.SignerCertificate -or
            [string]$signature.SignerCertificate.Subject -cne $expectedPythonSubject -or
            [string]$signature.SignerCertificate.Thumbprint -cne $expectedPythonThumbprint
        ) {
            throw "Protected Python loader-closure signer is invalid: $relative"
        }
    }

    if (-not $ManifestPath) { return $null }
    $manifestFull = [IO.Path]::GetFullPath($ManifestPath)
    $expectedManifestPath = Join-Path $rootFull $pythonBoundaryManifestName
    if ($manifestFull -cne $expectedManifestPath) {
        throw 'Protected Python boundary manifest is outside its atomic root.'
    }
    if (-not (Test-Path -LiteralPath $manifestFull -PathType Leaf)) {
        throw 'Protected Python reuse manifest is absent.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $manifestFull -Label 'Protected Python reuse manifest'
    Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestFull)
    try {
        $manifest = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($manifestFull)) | ConvertFrom-Json
    }
    catch { throw 'Protected Python reuse manifest is invalid JSON.' }
    if (
        [string]$manifest.schema_version -cne 'dawnstrike.python_boundary.v1' -or
        [string]$manifest.python_installer_sha256 -cne $expectedPythonInstallerHash -or
        [string]$manifest.python_sha256 -cne $expectedPythonHash -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false
    ) {
        throw 'Protected Python reuse manifest contract is invalid.'
    }
    $expectedFiles = @{}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            [IO.Path]::IsPathRooted($relative) -or
            $relative -match '(^|/)\.\.(/|$)' -or
            $relative -match '(^|/)\.(/|$)' -or
            $expectedFiles.ContainsKey($relative)
        ) {
            throw 'Protected Python reuse manifest contains an unsafe or duplicate path.'
        }
        if ([string]$entry.sha256 -notmatch '^[0-9a-f]{64}$' -or [long]$entry.length -lt 0) {
            throw 'Protected Python reuse manifest contains an invalid file identity.'
        }
        $expectedFiles[$relative] = $entry
    }
    $actualFiles = @(
        Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
            -ExcludeRelative @($pythonBoundaryManifestName)
    )
    if ($actualFiles.Count -ne $expectedFiles.Count) {
        throw 'Protected Python reuse file set differs from its sealed manifest.'
    }
    foreach ($actual in $actualFiles) {
        $relative = [string]$actual.path
        if (-not $expectedFiles.ContainsKey($relative)) {
            throw "Protected Python reuse contains an unsealed file: $relative"
        }
        $expected = $expectedFiles[$relative]
        if (
            [long]$actual.length -ne [long]$expected.length -or
            [string]$actual.sha256 -cne [string]$expected.sha256
        ) {
            throw "Protected Python reuse file identity differs from its sealed manifest: $relative"
        }
    }
    return $manifest
}

function Write-DawnstrikeProtectedPythonManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestPath = Join-Path $rootFull $pythonBoundaryManifestName
    if (Test-Path -LiteralPath $manifestPath) {
        throw 'Protected Python boundary manifest already exists.'
    }
    $payload = [ordered]@{
        schema_version = 'dawnstrike.python_boundary.v1'
        python_installer_sha256 = $expectedPythonInstallerHash
        python_sha256 = $expectedPythonHash
        files = @(
            Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
                -ExcludeRelative @($pythonBoundaryManifestName)
        )
        research_only = $true
        broker_execution_enabled = $false
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($payload | ConvertTo-Json -Depth 6))
    $temporary = Join-Path $rootFull ('.python-boundary-write-' + [Guid]::NewGuid().ToString('N'))
    try {
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Set-DawnstrikeProtectedReadableFileAcl -Path $temporary
        [IO.File]::Move($temporary, $manifestPath)
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Assert-DawnstrikeProtectedDependencyTree {
    [CmdletBinding()]
    param(
        [string]$Root = $dependencyDestination,
        [string]$ManifestPath = $dependencyManifest
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestFull = [IO.Path]::GetFullPath($ManifestPath)
    if ($manifestFull -cne (Join-Path $rootFull $dependencyManifestName)) {
        throw 'Protected dependency manifest is outside its atomic root.'
    }
    if (
        -not (Test-Path -LiteralPath $rootFull -PathType Container) -or
        -not (Test-Path -LiteralPath $manifestFull -PathType Leaf)
    ) {
        throw 'Protected dependency boundary is partial.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $rootFull -Label 'Protected dependency root'
    $items = @(Get-ChildItem -LiteralPath $rootFull -Recurse -Force -ErrorAction Stop)
    $reparse = $items | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $reparse) { throw 'Protected dependency tree contains a reparse point.' }
    Assert-DawnstrikeInstalledBoundaryAcl -Paths (
        @($rootFull) + @($items | ForEach-Object { [string]$_.FullName })
    )
    Assert-DawnstrikeInstallNoReparse -Path $manifestFull -Label 'Protected dependency manifest'
    Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestFull)
    try {
        $manifest = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($manifestFull)) | ConvertFrom-Json
    }
    catch { throw 'Protected dependency manifest is invalid JSON.' }
    if (
        [string]$manifest.schema_version -cne 'dawnstrike.dependency_boundary.v1' -or
        [string]$manifest.requirements_lock_blob -cne $requirementsLockBlob -or
        [string]$manifest.requirements_lock_sha256 -cne $requirementsLockSha256 -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false
    ) {
        throw 'Protected dependency manifest contract is invalid.'
    }
    $expectedFiles = @{}
    foreach ($entry in @($manifest.files)) {
        $relative = [string]$entry.path
        if (
            -not $relative -or
            [IO.Path]::IsPathRooted($relative) -or
            $relative -match '(^|/)\.\.(/|$)' -or
            $relative -match '(^|/)\.(/|$)' -or
            $expectedFiles.ContainsKey($relative) -or
            [string]$entry.sha256 -notmatch '^[0-9a-f]{64}$' -or
            [long]$entry.length -lt 0
        ) {
            throw 'Protected dependency manifest contains an invalid file identity.'
        }
        $expectedFiles[$relative] = $entry
    }
    $actualFiles = @(
        Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
            -ExcludeRelative @($dependencyManifestName)
    )
    if ($actualFiles.Count -ne $expectedFiles.Count) {
        throw 'Protected dependency file set differs from its sealed manifest.'
    }
    foreach ($actual in $actualFiles) {
        $relative = [string]$actual.path
        if (-not $expectedFiles.ContainsKey($relative)) {
            throw "Protected dependency boundary contains an unsealed file: $relative"
        }
        $expected = $expectedFiles[$relative]
        if (
            [long]$actual.length -ne [long]$expected.length -or
            [string]$actual.sha256 -cne [string]$expected.sha256
        ) {
            throw "Protected dependency file identity differs from its sealed manifest: $relative"
        }
    }
    $sitePackages = Join-Path $rootFull 'Lib\site-packages'
    if (-not (Test-Path -LiteralPath $sitePackages -PathType Container)) {
        throw 'Protected dependency boundary has no site-packages root.'
    }
    foreach ($forbidden in @('sitecustomize.py', 'usercustomize.py')) {
        if (Test-Path -LiteralPath (Join-Path $sitePackages $forbidden)) {
            throw 'Protected dependency boundary contains an unsafe startup file.'
        }
    }
    if (Get-ChildItem -LiteralPath $sitePackages -Recurse -File -Force | Where-Object {
        $_.Extension -ieq '.pth'
    } | Select-Object -First 1) {
        throw 'Protected dependency boundary contains an unapproved path startup file.'
    }
}

function Write-DawnstrikeProtectedDependencyManifest {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $manifestPath = Join-Path $rootFull $dependencyManifestName
    if (Test-Path -LiteralPath $manifestPath) {
        throw 'Protected dependency manifest already exists.'
    }
    $payload = [ordered]@{
        schema_version = 'dawnstrike.dependency_boundary.v1'
        requirements_lock_blob = $requirementsLockBlob
        requirements_lock_sha256 = $requirementsLockSha256
        files = @(
            Get-DawnstrikeProtectedTreeEntries -Root $rootFull `
                -ExcludeRelative @($dependencyManifestName)
        )
        research_only = $true
        broker_execution_enabled = $false
    }
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($payload | ConvertTo-Json -Depth 6))
    $temporary = Join-Path $rootFull ('.dependency-manifest-write-' + [Guid]::NewGuid().ToString('N'))
    try {
        $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Set-DawnstrikeProtectedReadableFileAcl -Path $temporary
        [IO.File]::Move($temporary, $manifestPath)
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Copy-DawnstrikeExactGitFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $copyGitEnvironment = @{}
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        $copyGitEnvironment[[string]$entry.Name] = [string]$entry.Value
        Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction Stop
    }
    try {
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_CONFIG_SYSTEM = 'NUL'
        $env:GIT_CONFIG_GLOBAL = 'NUL'
        $env:GIT_ATTR_NOSYSTEM = '1'
        $env:GIT_NO_REPLACE_OBJECTS = '1'
    $gitRelative = $RelativePath.Replace('\', '/')
    if (
        [IO.Path]::IsPathRooted($RelativePath) -or
        $gitRelative -match '(^|/)\.\.(/|$)' -or
        $gitRelative -match '(^|/)\.(/|$)' -or
        $gitRelative.StartsWith('/')
    ) {
        throw 'Exact Git export path is not a safe repository-relative path.'
    }
    $destinationFull = [IO.Path]::GetFullPath($Destination)
    $installPrefix = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\'
    if (-not $destinationFull.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Exact Git export destination escaped the protected installation root.'
    }
    $destinationParent = Split-Path -Parent $destinationFull
    Assert-DawnstrikeInstallNoReparse -Path $destinationParent -Label 'Exact Git export destination parent'

    $expectedBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $gitRelative)).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $expectedBlob -notmatch '^[0-9a-f]{40}$') {
        throw "Exact protected release file is absent: $gitRelative"
    }
    $expectedSizeText = (& $git @safeGit cat-file -s $expectedBlob).Trim()
    $expectedSize = 0L
    if ($LASTEXITCODE -ne 0 -or -not [long]::TryParse($expectedSizeText, [ref]$expectedSize) -or $expectedSize -lt 0) {
        throw "Exact protected release blob size is invalid: $gitRelative"
    }

    $temporary = Join-Path $destinationParent ('.exact-git-export-' + [Guid]::NewGuid().ToString('N'))
    try {
        $start = [Diagnostics.ProcessStartInfo]::new()
        $start.FileName = $git
        $start.WorkingDirectory = $candidate
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $start.Arguments = (
            '-c core.autocrlf=false ' +
            '-c core.fsmonitor=false ' +
            '-c core.untrackedCache=false ' +
            '-c core.hooksPath=NUL ' +
            '-c core.attributesFile=NUL ' +
            '-c protocol.ext.allow=never ' +
            '-c protocol.file.allow=never ' +
            'cat-file blob ' + $expectedBlob
        )
        foreach ($name in @($start.EnvironmentVariables.Keys)) {
            if ([string]$name -like 'GIT_*') { $start.EnvironmentVariables.Remove([string]$name) }
        }
        $start.EnvironmentVariables['GIT_CONFIG_NOSYSTEM'] = '1'
        $start.EnvironmentVariables['GIT_CONFIG_SYSTEM'] = 'NUL'
        $start.EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'
        $start.EnvironmentVariables['GIT_ATTR_NOSYSTEM'] = '1'
        $start.EnvironmentVariables['GIT_NO_REPLACE_OBJECTS'] = '1'
        $start.EnvironmentVariables['GIT_DIR'] = '.git'
        $start.EnvironmentVariables['GIT_WORK_TREE'] = '.'

        $process = [Diagnostics.Process]::new()
        $process.StartInfo = $start
        try {
            if (-not $process.Start()) { throw 'Exact Git blob export process did not start.' }
            $stderrTask = $process.StandardError.ReadToEndAsync()
            $destinationHandle = [IO.File]::Open(
                $temporary,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            try {
                $process.StandardOutput.BaseStream.CopyTo($destinationHandle)
                $destinationHandle.Flush($true)
            }
            finally { $destinationHandle.Dispose() }
            if (-not $process.WaitForExit(60000)) {
                try { $process.Kill() } catch { }
                throw 'Exact Git blob export timed out.'
            }
            $stderr = [string]$stderrTask.Result
            if ($process.ExitCode -ne 0) {
                throw ('Exact Git blob export failed: ' + $stderr.Trim())
            }
        }
        finally { $process.Dispose() }

        $temporaryItem = Get-Item -LiteralPath $temporary -Force -ErrorAction Stop
        if ([long]$temporaryItem.Length -ne $expectedSize) {
            throw "Exact Git blob export length is invalid: $gitRelative"
        }
        $temporaryBlob = (& $git @safeGit hash-object ('--path=' + $gitRelative) $temporary).Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $temporaryBlob -cne $expectedBlob) {
            throw "Exact Git blob export bytes are invalid: $gitRelative"
        }
        Set-DawnstrikeProtectedReadableFileAcl -Path $temporary

        if (Test-Path -LiteralPath $destinationFull) {
            $destinationItem = Get-Item -LiteralPath $destinationFull -Force -ErrorAction Stop
            if (($destinationItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Exact Git export destination is a reparse point.'
            }
            [IO.File]::Replace($temporary, $destinationFull, $null, $true)
        }
        else {
            [IO.File]::Move($temporary, $destinationFull)
        }
        $installedBlob = (& $git @safeGit hash-object ('--path=' + $gitRelative) $destinationFull).Trim().ToLowerInvariant()
        $installedItem = Get-Item -LiteralPath $destinationFull -Force -ErrorAction Stop
        if (
            $LASTEXITCODE -ne 0 -or
            $installedBlob -cne $expectedBlob -or
            [long]$installedItem.Length -ne $expectedSize
        ) {
            throw "Exact protected file promotion read-back failed: $gitRelative"
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $copyGitEnvironment.Keys) {
            Set-Item -LiteralPath ('Env:' + $name) -Value $copyGitEnvironment[$name]
        }
    }
}

function Assert-DawnstrikeProtectedReleaseRepository {
    [CmdletBinding()]
    param()

    Assert-DawnstrikeInstallNoReparse -Path $candidate -Label 'Protected exact-SHA release root'
    $reparse = Get-ChildItem -LiteralPath $candidate -Recurse -Force -ErrorAction Stop | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $reparse) { throw 'Protected exact-SHA release contains a reparse point.' }

    $head = (& $git @safeGit rev-parse HEAD).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $head -cne $ExpectedSha) {
        throw 'Protected exact-SHA release HEAD identity is invalid.'
    }
    $remoteMain = (& $git @safeGit rev-parse refs/remotes/origin/main).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $remoteMain -cne $ExpectedSha) {
        throw 'Protected exact-SHA release origin/main identity is invalid.'
    }
    $remoteUrls = @(& $git @safeGit remote get-url --all origin)
    if (
        $LASTEXITCODE -ne 0 -or
        $remoteUrls.Count -ne 1 -or
        [string]$remoteUrls[0] -cne $canonicalOrigin
    ) {
        throw 'Protected exact-SHA release canonical origin is invalid.'
    }
    $objectFormat = (& $git @safeGit rev-parse --show-object-format).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $objectFormat -cne 'sha1') {
        throw 'Protected exact-SHA release object format is invalid.'
    }
    $shallow = (& $git @safeGit rev-parse --is-shallow-repository).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $shallow -cne 'false') {
        throw 'Protected exact-SHA release must be a complete non-shallow repository.'
    }
    foreach ($forbiddenGitMetadata in @(
        (Join-Path $candidate '.git\objects\info\alternates'),
        (Join-Path $candidate '.git\info\grafts'),
        (Join-Path $candidate '.git\shallow')
    )) {
        if (Test-Path -LiteralPath $forbiddenGitMetadata) {
            throw 'Protected exact-SHA release contains forbidden object indirection metadata.'
        }
    }
    $replaceRefs = @(& $git @safeGit for-each-ref --format='%(refname)' refs/replace/)
    if ($LASTEXITCODE -ne 0 -or $replaceRefs.Count -ne 0) {
        throw 'Protected exact-SHA release contains replace references.'
    }
    $promisorConfiguration = @(& $git @safeGit config --get-regexp '^(extensions\.partialClone|remote\..*\.promisor|remote\..*\.partialCloneFilter|core\.alternateRefsCommand)$' 2>$null)
    if ($LASTEXITCODE -notin @(0, 1) -or $promisorConfiguration.Count -ne 0) {
        throw 'Protected exact-SHA release contains partial-clone or alternate-object configuration.'
    }
    $treeModes = @(& $git @safeGit ls-tree -r $ExpectedSha | ForEach-Object { ([string]$_ -split ' ', 2)[0] } | Sort-Object -Unique)
    if ($LASTEXITCODE -ne 0 -or $treeModes.Count -ne 1 -or [string]$treeModes[0] -cne '100644') {
        throw 'Protected exact-SHA release contains a symlink, submodule, or unsupported file mode.'
    }
    $script:candidateTree = (& $git @safeGit rev-parse ($ExpectedSha + '^{tree}')).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $script:candidateTree -notmatch '^[0-9a-f]{40}$') {
        throw 'Protected exact-SHA release tree is invalid.'
    }
    $status = ((& $git @safeGit status --porcelain=v1 --untracked-files=all --ignore-submodules=none) -join "`n").Trim()
    if ($LASTEXITCODE -ne 0 -or $status) {
        throw 'Protected exact-SHA release working tree is not immutable and clean.'
    }
    $fsck = @(& $git @safeGit fsck --full --strict --no-reflogs --no-dangling $ExpectedSha 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw ('Protected exact-SHA release object verification failed: ' + (($fsck | ForEach-Object { [string]$_ }) -join "`n").Trim())
    }

    $protectedItems = @($releaseParent, $candidate) + @(
        Get-ChildItem -LiteralPath $candidate -Recurse -Force -ErrorAction Stop |
            ForEach-Object { [string]$_.FullName }
    )
    Assert-DawnstrikeInstalledBoundaryAcl -Paths $protectedItems
}

function Get-DawnstrikeReleaseAdmissionIdentity {
    [CmdletBinding()]
    param()

    $installerBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $installerRelative)).Trim().ToLowerInvariant()
    $launcherBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $launcherRelative)).Trim().ToLowerInvariant()
    $lockBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':requirements.lock')).Trim().ToLowerInvariant()
    if (
        $LASTEXITCODE -ne 0 -or
        $installerBlob -notmatch '^[0-9a-f]{40}$' -or
        $launcherBlob -notmatch '^[0-9a-f]{40}$' -or
        $lockBlob -notmatch '^[0-9a-f]{40}$'
    ) {
        throw 'Protected release admission blob identity is invalid.'
    }
    return [ordered]@{
        schema_version = 'dawnstrike.release_admission.v1'
        candidate_sha = $ExpectedSha
        candidate_tree = $candidateTree
        canonical_origin = $canonicalOrigin
        canonical_main_sha_at_admission = $ExpectedSha
        installer_blob = $installerBlob
        launcher_blob = $launcherBlob
        requirements_lock_blob = $lockBlob
        git_archive_sha256 = $expectedGitArchiveHash
        research_only = $true
        broker_execution_enabled = $false
    }
}

function Write-DawnstrikeProtectedReleaseAdmission {
    [CmdletBinding()]
    param()

    $gitMetadata = Join-Path $candidate '.git'
    $manifestPath = Join-Path $gitMetadata $releaseAdmissionManifestName
    Assert-DawnstrikeInstallNoReparse -Path $gitMetadata -Label 'Protected release Git metadata'
    Assert-DawnstrikeInstalledBoundaryAcl -Paths @($gitMetadata)
    if (Test-Path -LiteralPath $manifestPath) {
        throw 'Protected release admission manifest already exists.'
    }
    $identity = Get-DawnstrikeReleaseAdmissionIdentity
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($identity | ConvertTo-Json -Depth 4))
    $temporary = Join-Path $gitMetadata ('.admission-write-' + [Guid]::NewGuid().ToString('N'))
    try {
        $stream = [IO.File]::Open(
            $temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally { $stream.Dispose() }
        Set-DawnstrikeProtectedReadableFileAcl -Path $temporary
        [IO.File]::Move($temporary, $manifestPath)
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Assert-DawnstrikeProtectedReleaseAdmission {
    [CmdletBinding()]
    param()

    $manifestPath = Join-Path (Join-Path $candidate '.git') $releaseAdmissionManifestName
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw 'Protected release admission manifest is absent.'
    }
    Assert-DawnstrikeInstallNoReparse -Path $manifestPath -Label 'Protected release admission manifest'
    Assert-DawnstrikeInstalledBoundaryAcl -Paths @($manifestPath)
    try {
        $actual = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($manifestPath)) | ConvertFrom-Json
    }
    catch { throw 'Protected release admission manifest is invalid JSON.' }
    $expected = Get-DawnstrikeReleaseAdmissionIdentity
    foreach ($name in $expected.Keys) {
        if ([string]$actual.$name -cne [string]$expected[$name]) {
            throw "Protected release admission identity is invalid: $name"
        }
    }
}

if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
    throw 'Protected installation root is absent; complete the operator-controlled bootstrap ceremony first.'
}
if (-not (Test-Path -LiteralPath $installerRoot -PathType Container)) {
    throw 'Protected content-addressed installer root is absent; complete the operator-controlled bootstrap ceremony first.'
}
Assert-DawnstrikeInstallNoReparse -Path $InstallRoot -Label 'Protected installation root'
Assert-DawnstrikeInstallNoReparse -Path $installerRoot -Label 'Protected content-addressed installer root'
Assert-DawnstrikeInstalledBoundaryAcl -Paths @($InstallRoot, $installerRoot, $installerDestination)

if ((Test-Path -LiteralPath $gitRoot) -or (Test-Path -LiteralPath $gitBoundaryManifest)) {
    if (
        -not (Test-Path -LiteralPath $gitRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $gitBoundaryManifest -PathType Leaf)
    ) {
        throw 'Protected Git boundary is partial; refuse implicit repair or reuse.'
    }
    Assert-DawnstrikeProtectedGitTree
}
else {
    $gitDownloadRoot = Join-Path $InstallRoot ('.git-download-' + [Guid]::NewGuid().ToString('N'))
    $gitStage = Join-Path $InstallRoot ('.git-stage-' + [Guid]::NewGuid().ToString('N'))
    $gitArchive = Join-Path $gitDownloadRoot 'MinGit-2.55.0.5-64-bit.zip'
    $null = [IO.Directory]::CreateDirectory($gitDownloadRoot)
    $null = [IO.Directory]::CreateDirectory($gitStage)
    Set-DawnstrikeProtectedDirectoryAcl -Path $gitDownloadRoot
    Set-DawnstrikeProtectedDirectoryAcl -Path $gitStage
    try {
        Save-DawnstrikePinnedDownload -Uri $gitArchiveUri -Destination $gitArchive `
            -ExpectedSha256 $expectedGitArchiveHash -ExpectedLength $expectedGitArchiveLength
        Expand-DawnstrikePinnedZip -ArchivePath $gitArchive -Destination $gitStage
        & C:\Windows\System32\icacls.exe $gitStage /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Staged protected Git ownership hardening failed.' }
        $gitStageItems = @(Get-ChildItem -LiteralPath $gitStage -Recurse -Force -ErrorAction Stop)
        $gitStageReparse = $gitStageItems | Where-Object {
            ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        } | Select-Object -First 1
        if ($null -ne $gitStageReparse) { throw 'Staged protected Git contains a reparse point.' }
        Assert-DawnstrikeInstalledBoundaryAcl -Paths (@($gitStage) + @($gitStageItems | ForEach-Object { [string]$_.FullName }))
        $stagedGit = Join-Path $gitStage 'cmd\git.exe'
        if ((Get-DawnstrikeInstallSha256 $stagedGit) -cne $expectedGitHash) {
            throw 'Staged protected Git executable hash is invalid.'
        }
        $stagedGitSignature = Get-AuthenticodeSignature -LiteralPath $stagedGit -ErrorAction Stop
        if (
            [string]$stagedGitSignature.Status -cne 'Valid' -or
            $null -eq $stagedGitSignature.SignerCertificate -or
            [string]$stagedGitSignature.SignerCertificate.Subject -cne $expectedGitSubject -or
            [string]$stagedGitSignature.SignerCertificate.Thumbprint -cne $expectedGitThumbprint
        ) {
            throw 'Staged protected Git executable signer is invalid.'
        }
        Write-DawnstrikeProtectedGitManifest -Root $gitStage
        Assert-DawnstrikeProtectedGitTree -Root $gitStage
        if (Test-Path -LiteralPath $gitRoot) { throw 'Protected Git destination appeared during staging.' }
        [IO.Directory]::Move($gitStage, $gitRoot)
        $gitStage = ''
        Assert-DawnstrikeProtectedGitTree
    }
    finally {
        if (Test-Path -LiteralPath $gitDownloadRoot) {
            Remove-Item -LiteralPath $gitDownloadRoot -Recurse -Force
        }
        if ($gitStage -and (Test-Path -LiteralPath $gitStage)) {
            Remove-Item -LiteralPath $gitStage -Recurse -Force
        }
    }
}

if ((Test-Path -LiteralPath $nodeRoot) -or (Test-Path -LiteralPath $nodeBoundaryManifest)) {
    if (
        -not (Test-Path -LiteralPath $nodeRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $nodeBoundaryManifest -PathType Leaf)
    ) {
        throw 'Protected Node boundary is partial; refuse implicit repair or reuse.'
    }
    $null = Assert-DawnstrikeProtectedNodeBoundary
}
else {
    $nodeBoundaryDisposition = 'FRESH_EXACT_ATOMIC_NODE'
    $nodeDownloadRoot = Join-Path $InstallRoot ('.node-download-' + [Guid]::NewGuid().ToString('N'))
    $nodeStage = Join-Path $InstallRoot ('.node-stage-' + [Guid]::NewGuid().ToString('N'))
    $nodeArchive = Join-Path $nodeDownloadRoot 'node-v24.20.0-win-x64.zip'
    $null = [IO.Directory]::CreateDirectory($nodeDownloadRoot)
    $null = [IO.Directory]::CreateDirectory($nodeStage)
    Set-DawnstrikeProtectedDirectoryAcl -Path $nodeDownloadRoot
    Set-DawnstrikeProtectedDirectoryAcl -Path $nodeStage
    try {
        Save-DawnstrikePinnedDownload -Uri $nodeArchiveUri -Destination $nodeArchive `
            -ExpectedSha256 $expectedNodeArchiveHash -ExpectedLength $expectedNodeArchiveLength
        Expand-DawnstrikePinnedNodeExecutable -ArchivePath $nodeArchive -Destination $nodeStage
        & C:\Windows\System32\icacls.exe $nodeStage /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Staged protected Node ownership hardening failed.' }
        Write-DawnstrikeProtectedNodeManifest -Root $nodeStage
        $null = Assert-DawnstrikeProtectedNodeBoundary `
            -Root $nodeStage `
            -ManifestPath (Join-Path $nodeStage $nodeBoundaryManifestName)
        if (Test-Path -LiteralPath $nodeRoot) {
            throw 'Protected Node destination appeared during staging.'
        }
        [IO.Directory]::Move($nodeStage, $nodeRoot)
        $nodeStage = ''
        $null = Assert-DawnstrikeProtectedNodeBoundary
    }
    finally {
        if (Test-Path -LiteralPath $nodeDownloadRoot) {
            Remove-Item -LiteralPath $nodeDownloadRoot -Recurse -Force
        }
        if ($nodeStage -and (Test-Path -LiteralPath $nodeStage)) {
            Remove-Item -LiteralPath $nodeStage -Recurse -Force
        }
    }
}

if ((Test-Path -LiteralPath $vercelRoot) -or (Test-Path -LiteralPath $vercelBoundaryManifest)) {
    if (
        -not (Test-Path -LiteralPath $vercelRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $vercelBoundaryManifest -PathType Leaf)
    ) {
        throw 'Protected Vercel CLI boundary is partial; refuse implicit repair or reuse.'
    }
    $null = Assert-DawnstrikeProtectedVercelTree
}
else {
    $vercelBoundaryDisposition = 'FRESH_EXACT_ATOMIC_VERCEL_CLI'
    $vercelStage = Join-Path $InstallRoot ('.vercel-cli-stage-' + [Guid]::NewGuid().ToString('N'))
    $null = [IO.Directory]::CreateDirectory($vercelStage)
    Set-DawnstrikeProtectedDirectoryAcl -Path $vercelStage
    try {
        Copy-DawnstrikePinnedVercelTree -Source $vercelSourceRoot -Destination $vercelStage
        & C:\Windows\System32\icacls.exe $vercelStage /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Staged protected Vercel CLI ownership hardening failed.' }
        $null = Assert-DawnstrikePinnedVercelPayload -Root $vercelStage
        Write-DawnstrikeProtectedVercelManifest -Root $vercelStage
        $null = Assert-DawnstrikeProtectedVercelTree `
            -Root $vercelStage `
            -ManifestPath (Join-Path $vercelStage $vercelBoundaryManifestName)
        if (Test-Path -LiteralPath $vercelRoot) {
            throw 'Protected Vercel CLI destination appeared during staging.'
        }
        [IO.Directory]::Move($vercelStage, $vercelRoot)
        $vercelStage = ''
        $null = Assert-DawnstrikeProtectedVercelTree
    }
    finally {
        if ($vercelStage -and (Test-Path -LiteralPath $vercelStage)) {
            Remove-Item -LiteralPath $vercelStage -Recurse -Force
        }
    }
}

if (-not (Test-Path -LiteralPath $releaseParent)) {
    $null = [IO.Directory]::CreateDirectory($releaseParent)
    Set-DawnstrikeProtectedDirectoryAcl -Path $releaseParent
}
elseif (-not (Test-Path -LiteralPath $releaseParent -PathType Container)) {
    throw 'Protected exact-SHA release parent exists but is not a directory.'
}
Assert-DawnstrikeInstallNoReparse -Path $releaseParent -Label 'Protected exact-SHA release parent'
Assert-DawnstrikeInstalledBoundaryAcl -Paths @($releaseParent)

$savedGitEnvironment = @{}
foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
    $savedGitEnvironment[[string]$entry.Name] = [string]$entry.Value
    Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction Stop
}
try {
    $env:GIT_CONFIG_NOSYSTEM = '1'
    $env:GIT_CONFIG_SYSTEM = 'NUL'
    $env:GIT_CONFIG_GLOBAL = 'NUL'
    $env:GIT_ATTR_NOSYSTEM = '1'
    $env:GIT_NO_REPLACE_OBJECTS = '1'
    $env:GIT_TERMINAL_PROMPT = '0'
    $gitBase = @(
        '-c', 'core.autocrlf=false',
        '-c', 'core.fsmonitor=false',
        '-c', 'core.untrackedCache=false',
        '-c', 'core.hooksPath=NUL',
        '-c', 'core.attributesFile=NUL',
        '-c', 'credential.helper=',
        '-c', 'protocol.ext.allow=never',
        '-c', 'protocol.file.allow=never',
        '-c', 'protocol.https.allow=always',
        '-c', 'fetch.fsckObjects=true',
        '-c', 'transfer.fsckObjects=true'
    )
    if (Test-Path -LiteralPath $protectedReleaseRoot) {
        if (-not (Test-Path -LiteralPath $protectedReleaseRoot -PathType Container)) {
            throw 'Protected exact-SHA release destination is not a directory.'
        }
        $releaseDisposition = 'REUSED_EXACT_ATOMIC_IMPORT'
        $candidate = $protectedReleaseRoot
        $safeGit = @($gitBase) + @('-C', $candidate)
        Assert-DawnstrikeProtectedReleaseRepository
        Assert-DawnstrikeProtectedReleaseAdmission
    }
    else {
        $canonicalHead = @(& $git @gitBase ls-remote --exit-code $canonicalOrigin refs/heads/main)
        if (
            $LASTEXITCODE -ne 0 -or
            $canonicalHead.Count -ne 1 -or
            [string]$canonicalHead[0] -cnotmatch ('^' + [regex]::Escape($ExpectedSha) + "\s+refs/heads/main$")
        ) {
            throw 'Host-boundary installer requires ExpectedSha to be the live canonical main head.'
        }

        $protectedImportRoot = Join-Path $releaseParent ('.import-' + [Guid]::NewGuid().ToString('N'))
        $emptyTemplateRoot = Join-Path $releaseParent ('.empty-template-' + [Guid]::NewGuid().ToString('N'))
        $null = [IO.Directory]::CreateDirectory($protectedImportRoot)
        $null = [IO.Directory]::CreateDirectory($emptyTemplateRoot)
        Set-DawnstrikeProtectedDirectoryAcl -Path $protectedImportRoot
        Set-DawnstrikeProtectedDirectoryAcl -Path $emptyTemplateRoot
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($protectedImportRoot, $emptyTemplateRoot)

        & $git @gitBase init --quiet ('--template=' + $emptyTemplateRoot) $protectedImportRoot
        if ($LASTEXITCODE -ne 0) { throw 'Protected exact-SHA repository initialization failed.' }
        $candidate = $protectedImportRoot
        $safeGit = @($gitBase) + @('-C', $candidate)
        & $git @safeGit remote add origin $canonicalOrigin
        if ($LASTEXITCODE -ne 0) { throw 'Protected exact-SHA canonical origin registration failed.' }
        & $git @safeGit fetch --no-tags --no-recurse-submodules --force origin '+refs/heads/main:refs/remotes/origin/main'
        if ($LASTEXITCODE -ne 0) { throw 'Protected exact-SHA canonical main fetch failed.' }
        & $git @safeGit checkout --detach --force $ExpectedSha
        if ($LASTEXITCODE -ne 0) { throw 'Protected exact-SHA working-tree materialization failed.' }
        Assert-DawnstrikeProtectedReleaseRepository
        $canonicalHeadBeforePublish = @(
            & $git @gitBase ls-remote --exit-code $canonicalOrigin refs/heads/main
        )
        if (
            $LASTEXITCODE -ne 0 -or
            $canonicalHeadBeforePublish.Count -ne 1 -or
            [string]$canonicalHeadBeforePublish[0] -cnotmatch (
                '^' + [regex]::Escape($ExpectedSha) + "\s+refs/heads/main$"
            )
        ) {
            throw 'Canonical main changed before protected release admission.'
        }
        Write-DawnstrikeProtectedReleaseAdmission
        Assert-DawnstrikeProtectedReleaseAdmission
        Assert-DawnstrikeProtectedReleaseRepository
        if (Test-Path -LiteralPath $protectedReleaseRoot) {
            throw 'Protected exact-SHA release destination appeared during import.'
        }
        [IO.Directory]::Move($protectedImportRoot, $protectedReleaseRoot)
        $protectedImportRoot = ''

        $candidate = $protectedReleaseRoot
        $safeGit = @($gitBase) + @('-C', $candidate)
        Assert-DawnstrikeProtectedReleaseRepository
        Assert-DawnstrikeProtectedReleaseAdmission
    }
    $launcherBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $launcherRelative)).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $launcherBlob -notmatch '^[0-9a-f]{40}$') {
        throw 'Host-boundary launcher is absent from the protected exact-SHA release.'
    }
    $script:requirementsLockBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':requirements.lock')).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $script:requirementsLockBlob -notmatch '^[0-9a-f]{40}$') {
        throw 'Protected exact-SHA release requirements lock is absent.'
    }
    $script:requirementsLockSha256 = Get-DawnstrikeInstallSha256 (Join-Path $candidate 'requirements.lock')
    $expectedInstallerBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $installerRelative)).Trim().ToLowerInvariant()
    $installedInstallerBlob = (& $git @safeGit hash-object ('--path=' + $installerRelative) $installerDestination).Trim().ToLowerInvariant()
    if (
        $LASTEXITCODE -ne 0 -or
        $expectedInstallerBlob -notmatch '^[0-9a-f]{40}$' -or
        $installedInstallerBlob -cne $expectedInstallerBlob
    ) {
        throw 'Protected host-boundary installer bytes do not match the protected exact-SHA release.'
    }
    $script:stateBoundaryBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $stateBoundaryRelative)).Trim().ToLowerInvariant()
    $materializedLauncherBlob = (& $git @safeGit hash-object ('--path=' + $launcherRelative) $launcherDestination).Trim().ToLowerInvariant()
    $materializedStateBoundaryBlob = (& $git @safeGit hash-object ('--path=' + $stateBoundaryRelative) $stateBoundaryDestination).Trim().ToLowerInvariant()
    if (
        $LASTEXITCODE -ne 0 -or
        $script:stateBoundaryBlob -notmatch '^[0-9a-f]{40}$' -or
        $materializedLauncherBlob -cne $launcherBlob -or
        $materializedStateBoundaryBlob -cne $script:stateBoundaryBlob
    ) {
        throw 'Protected release control-plane bytes differ from the exact Git objects.'
    }
}
finally {
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction SilentlyContinue
    }
    foreach ($name in $savedGitEnvironment.Keys) { Set-Item -LiteralPath ('Env:' + $name) -Value $savedGitEnvironment[$name] }
    if ($protectedImportRoot -and (Test-Path -LiteralPath $protectedImportRoot)) {
        Remove-Item -LiteralPath $protectedImportRoot -Recurse -Force
    }
    if ($emptyTemplateRoot -and (Test-Path -LiteralPath $emptyTemplateRoot)) {
        Remove-Item -LiteralPath $emptyTemplateRoot -Recurse -Force
    }
}

if ((Get-DawnstrikeInstallSha256 $pythonSource) -cne $expectedPythonHash) {
    throw 'Host-boundary installer rejected the dependency-source Python hash.'
}
$pythonSignature = Get-AuthenticodeSignature -LiteralPath $pythonSource -ErrorAction Stop
if (
    [string]$pythonSignature.Status -cne 'Valid' -or
    $null -eq $pythonSignature.SignerCertificate -or
    [string]$pythonSignature.SignerCertificate.Subject -cne $expectedPythonSubject -or
    [string]$pythonSignature.SignerCertificate.Thumbprint -cne $expectedPythonThumbprint
) {
    throw 'Host-boundary installer rejected the dependency-source Python signer.'
}
$dependencySourcePaths = @(
    (Join-Path $pythonSourceRoot 'Lib\site-packages')
)
foreach ($dependencySourcePath in $dependencySourcePaths) {
    $sourceReparse = Get-ChildItem -LiteralPath $dependencySourcePath -Recurse -Force -ErrorAction Stop | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $sourceReparse) {
        throw 'Host-boundary installer refuses a reparse point in the dependency-source Python paths.'
    }
}
$uvSource = Join-Path $pythonSourceRoot 'Scripts\uv.exe'
$uvSourceItem = Get-Item -LiteralPath $uvSource -Force -ErrorAction Stop
if (($uvSourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Host-boundary installer refuses a reparse point for the dependency-source uv executable.'
}
$scriptsSource = Join-Path $pythonSourceRoot 'Scripts'

Set-DawnstrikeProtectedDirectoryAcl -Path $InstallRoot
Set-DawnstrikeProtectedDirectoryAcl -Path $installerRoot

$pythonFresh = $false
$pythonBoundaryDisposition = 'REUSED_EXACT_SEALED_CORE'
$pythonStage = ''
if (Test-Path -LiteralPath $pythonDestination) {
    $null = Assert-DawnstrikeProtectedPythonTree `
        -Root $pythonDestination -ManifestPath $pythonBoundaryManifest
}
else {
    $pythonFresh = $true
    $pythonBoundaryDisposition = 'FRESH_OFFICIAL_CORE_INSTALL'
    $downloadRoot = Join-Path $InstallRoot ('.host-boundary-download-' + [Guid]::NewGuid().ToString('N'))
    $pythonStage = Join-Path $InstallRoot ('.python-stage-' + [Guid]::NewGuid().ToString('N'))
    $pythonInstaller = Join-Path $downloadRoot 'python-3.13.15-amd64.exe'
    $null = New-Item -ItemType Directory -Path $downloadRoot
    $null = New-Item -ItemType Directory -Path $pythonStage
    Set-DawnstrikeProtectedDirectoryAcl -Path $downloadRoot
    Set-DawnstrikeProtectedDirectoryAcl -Path $pythonStage
    try {
        Save-DawnstrikePinnedDownload -Uri $pythonInstallerUri -Destination $pythonInstaller `
            -ExpectedSha256 $expectedPythonInstallerHash -ExpectedLength $expectedPythonInstallerLength
        & C:\Windows\System32\icacls.exe $pythonInstaller /setowner '*S-1-5-32-544' /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Python installer ownership hardening failed.' }
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($downloadRoot, $pythonInstaller)
        $installerSignature = Get-AuthenticodeSignature -LiteralPath $pythonInstaller -ErrorAction Stop
        if (
            [string]$installerSignature.Status -cne 'Valid' -or
            $null -eq $installerSignature.SignerCertificate -or
            [string]$installerSignature.SignerCertificate.Subject -cne $expectedPythonSubject -or
            [string]$installerSignature.SignerCertificate.Thumbprint -cne $expectedPythonThumbprint
        ) {
            throw 'Downloaded Python installer does not match the approved signer.'
        }
        # Windows PowerShell 5.1 flattens ArgumentList arrays into one native
        # command line. Quote the fixed protected target explicitly so the
        # space in "Program Files" cannot split TargetDir into extra tokens.
        $pythonInstallArguments = @(
            '/quiet',
            'InstallAllUsers=1',
            ('TargetDir="' + $pythonStage + '"'),
            'Include_launcher=0',
            'Include_test=0',
            'Include_doc=0',
            'Include_tcltk=0',
            'Include_pip=0',
            'PrependPath=0',
            'Shortcuts=0'
        ) -join ' '
        $pythonInstall = Start-Process -FilePath $pythonInstaller `
            -ArgumentList $pythonInstallArguments -Wait -PassThru -WindowStyle Hidden
        if ($pythonInstall.ExitCode -ne 0) {
            throw "Official Python installation failed with exit $($pythonInstall.ExitCode)."
        }
        $stagedScripts = Join-Path $pythonStage 'Scripts'
        if (-not (Test-Path -LiteralPath $stagedScripts)) {
            $null = [IO.Directory]::CreateDirectory($stagedScripts)
        }
        Copy-DawnstrikePinnedFile -Source (Join-Path $scriptsSource 'uv.exe') `
            -Destination (Join-Path $stagedScripts 'uv.exe') -ExpectedSha256 $expectedUvHash
        & C:\Windows\System32\icacls.exe $pythonStage /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Staged Python ownership hardening failed.' }
        Assert-DawnstrikeProtectedPythonTree -Root $pythonStage
        Write-DawnstrikeProtectedPythonManifest -Root $pythonStage
        Assert-DawnstrikeProtectedPythonTree `
            -Root $pythonStage `
            -ManifestPath (Join-Path $pythonStage $pythonBoundaryManifestName) | Out-Null
        if (Test-Path -LiteralPath $pythonDestination) {
            throw 'Protected Python destination appeared during staged installation.'
        }
        [IO.Directory]::Move($pythonStage, $pythonDestination)
        $pythonStage = ''
        $null = Assert-DawnstrikeProtectedPythonTree `
            -Root $pythonDestination -ManifestPath $pythonBoundaryManifest
    }
    finally {
        if (Test-Path -LiteralPath $downloadRoot) {
            Remove-Item -LiteralPath $downloadRoot -Recurse -Force
        }
        if ($pythonStage -and (Test-Path -LiteralPath $pythonStage)) {
            Remove-Item -LiteralPath $pythonStage -Recurse -Force
        }
    }
}

$dependencyParent = Join-Path $InstallRoot 'Dependencies'
$dependencyDestination = Join-Path $dependencyParent $requirementsLockSha256
$dependencyManifestName = '.dawnstrike-dependency-boundary-v1.json'
$dependencyManifest = Join-Path $dependencyDestination $dependencyManifestName
$dependencyDisposition = 'REUSED_EXACT_SEALED_DEPENDENCIES'
if (-not (Test-Path -LiteralPath $dependencyParent)) {
    $null = [IO.Directory]::CreateDirectory($dependencyParent)
    Set-DawnstrikeProtectedDirectoryAcl -Path $dependencyParent
}
elseif (-not (Test-Path -LiteralPath $dependencyParent -PathType Container)) {
    throw 'Protected dependency parent exists but is not a directory.'
}
Assert-DawnstrikeInstallNoReparse -Path $dependencyParent -Label 'Protected dependency parent'
Assert-DawnstrikeInstalledBoundaryAcl -Paths @($dependencyParent)

if ((Test-Path -LiteralPath $dependencyDestination) -or (Test-Path -LiteralPath $dependencyManifest)) {
    Assert-DawnstrikeProtectedDependencyTree
}
else {
    $dependencyDisposition = 'FRESH_EXACT_ATOMIC_DEPENDENCIES'
    $dependencyStage = Join-Path $dependencyParent ('.import-' + [Guid]::NewGuid().ToString('N'))
    $materializationRoot = Join-Path $InstallRoot ('.dependency-tool-' + [Guid]::NewGuid().ToString('N'))
    $null = [IO.Directory]::CreateDirectory($dependencyStage)
    $null = [IO.Directory]::CreateDirectory($materializationRoot)
    Set-DawnstrikeProtectedDirectoryAcl -Path $dependencyStage
    Set-DawnstrikeProtectedDirectoryAcl -Path $materializationRoot
    $materializer = Join-Path $materializationRoot 'materialize_dawnstrike_dependencies.py'
    $materializationLock = Join-Path $materializationRoot 'requirements.lock'
    try {
        Copy-DawnstrikeExactGitFile -RelativePath 'scripts/materialize_dawnstrike_dependencies.py' -Destination $materializer
        Copy-DawnstrikeExactGitFile -RelativePath 'requirements.lock' -Destination $materializationLock
        if ((Get-DawnstrikeInstallSha256 $materializationLock) -cne $requirementsLockSha256) {
            throw 'Protected dependency materialization lock differs from its content address.'
        }
        $materializationOutput = @(
            & (Join-Path $pythonDestination 'python.exe') -I -B -S $materializer `
                --source-prefix $pythonSourceRoot `
                --stage-prefix $dependencyStage `
                --requirements-lock $materializationLock 2>&1
        )
        if ($LASTEXITCODE -ne 0) { throw 'Protected dependency materialization failed.' }
        try { $materialization = (($materializationOutput | ForEach-Object { [string]$_ }) -join "`n").Trim() | ConvertFrom-Json }
        catch { throw 'Protected dependency materialization did not return valid JSON.' }
        if (
            [string]$materialization.schema_version -cne 'dawnstrike.dependency_materialization.v1' -or
            [string]$materialization.status -cne 'PASS' -or
            [int]$materialization.distribution_count -lt 1 -or
            [int]$materialization.payload_count -lt 1 -or
            [int]$materialization.record_count -ne [int]$materialization.distribution_count -or
            [string]$materialization.record_set_sha256 -notmatch '^[0-9a-f]{64}$' -or
            $materialization.research_only -ne $true -or
            $materialization.broker_execution_enabled -ne $false
        ) {
            throw 'Protected dependency materialization returned an invalid safety contract.'
        }
        $stagedSitePackages = Join-Path $dependencyStage 'Lib\site-packages'
        if (-not (Test-Path -LiteralPath $stagedSitePackages -PathType Container)) {
            throw 'Protected dependency materialization produced no site-packages directory.'
        }
        & C:\Windows\System32\icacls.exe $dependencyStage /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Staged protected dependency ownership hardening failed.' }
        $dependencyStageItems = @(Get-ChildItem -LiteralPath $dependencyStage -Recurse -Force -ErrorAction Stop)
        $dependencyStageReparse = $dependencyStageItems | Where-Object {
            ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
        } | Select-Object -First 1
        if ($null -ne $dependencyStageReparse) { throw 'Staged protected dependency tree contains a reparse point.' }
        Assert-DawnstrikeInstalledBoundaryAcl -Paths (
            @($dependencyStage) + @($dependencyStageItems | ForEach-Object { [string]$_.FullName })
        )
        Write-DawnstrikeProtectedDependencyManifest -Root $dependencyStage
        Assert-DawnstrikeProtectedDependencyTree `
            -Root $dependencyStage `
            -ManifestPath (Join-Path $dependencyStage $dependencyManifestName)
        if (Test-Path -LiteralPath $dependencyDestination) {
            throw 'Protected dependency destination appeared during staging.'
        }
        [IO.Directory]::Move($dependencyStage, $dependencyDestination)
        $dependencyStage = ''
        Assert-DawnstrikeProtectedDependencyTree
    }
    finally {
        if ($dependencyStage -and (Test-Path -LiteralPath $dependencyStage)) {
            Remove-Item -LiteralPath $dependencyStage -Recurse -Force
        }
        if (Test-Path -LiteralPath $materializationRoot) {
            Remove-Item -LiteralPath $materializationRoot -Recurse -Force
        }
    }
}

$stateBoundarySha256 = Get-DawnstrikeInstallSha256 $stateBoundaryDestination
. $stateBoundaryDestination

$criticalPaths = @(
    $InstallRoot,
    $installerRoot,
    $installerDestination,
    $launcherDestination,
    $stateBoundaryDestination,
    $gitRoot,
    $git,
    $gitBoundaryManifest,
    $nodeRoot,
    $nodeExecutable,
    $nodeBoundaryManifest,
    $vercelRoot,
    $vercelBoundaryManifest,
    $vercelEntry,
    $pythonDestination,
    $pythonBoundaryManifest,
    (Join-Path $pythonDestination 'python.exe'),
    (Join-Path $pythonDestination 'python3.dll'),
    (Join-Path $pythonDestination 'python313.dll'),
    (Join-Path $pythonDestination 'DLLs'),
    (Join-Path $pythonDestination 'DLLs\_hashlib.pyd'),
    (Join-Path $pythonDestination 'Lib'),
    (Join-Path $pythonDestination 'Lib\hashlib.py'),
    (Join-Path $pythonDestination 'Lib\site-packages'),
    (Join-Path $pythonDestination 'Scripts'),
    (Join-Path $pythonDestination 'Scripts\uv.exe'),
    $dependencyParent,
    $dependencyDestination,
    $dependencyManifest,
    (Join-Path $dependencyDestination 'Lib\site-packages')
)
Assert-DawnstrikeInstalledBoundaryAcl -Paths $criticalPaths
$null = Assert-DawnstrikeProtectedNodeBoundary
if ((Get-DawnstrikeInstallSha256 (Join-Path $pythonDestination 'python.exe')) -cne $expectedPythonHash) {
    throw 'Protected Python bytes differ from the approved interpreter.'
}
$bootstrapRoot = Join-Path $InstallRoot ('.host-boundary-verification-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $bootstrapRoot
Set-DawnstrikeProtectedDirectoryAcl -Path $bootstrapRoot
$bootstrapReference = Join-Path $bootstrapRoot 'dawnstrike_python_bootstrap.py'
$bootstrap = Join-Path $candidate 'scripts\dawnstrike_python_bootstrap.py'
$verificationTarget = Join-Path $candidate 'scripts\verify_dawnstrike_python_environment.py'
Copy-DawnstrikeExactGitFile `
    -RelativePath 'scripts/dawnstrike_python_bootstrap.py' `
    -Destination $bootstrapReference
$bootstrapHash = Get-DawnstrikeInstallSha256 $bootstrapReference
$preloader = "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw(RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
try {
    $verificationOutput = @(
        & (Join-Path $pythonDestination 'python.exe') `
            -I -B -S -c $preloader $bootstrap $bootstrapHash `
            --release-root $candidate --expected-sha $ExpectedSha `
            --script $verificationTarget -- 2>&1
    )
    if ($LASTEXITCODE -ne 0) { throw 'Protected Python dependency verification failed.' }
    try { $verification = (($verificationOutput | ForEach-Object { [string]$_ }) -join "`n").Trim() | ConvertFrom-Json }
    catch { throw 'Protected Python dependency verification did not return valid JSON.' }
    if (
        [string]$verification.schema_version -cne 'dawnstrike.protected_python_verification.v1' -or
        [string]$verification.status -cne 'PASS' -or
        $verification.research_only -ne $true -or
        $verification.broker_execution_enabled -ne $false
    ) {
        throw 'Protected Python dependency verification returned an invalid safety contract.'
    }
}
finally {
    if (Test-Path -LiteralPath $bootstrapRoot) {
        Remove-Item -LiteralPath $bootstrapRoot -Recurse -Force
    }
}

$receiptParent = Split-Path -Parent $ReceiptRoot
$null = Assert-DawnstrikeInstallNoReparse `
    -Path $receiptParent -Label 'Protected host receipt parent'
$receiptParentLease = Open-DawnstrikeStateBoundaryPath `
    -Path $receiptParent -Label 'Protected host receipt parent'
$receiptRootLease = $null
try {
    $receiptRootExisted = Test-Path -LiteralPath $ReceiptRoot
    if ($receiptRootExisted) {
        # This check deliberately precedes every ACL mutation.  Set-Acl must
        # never follow an attacker-planted ProgramData junction.
        $null = Assert-DawnstrikeInstallNoReparse `
            -Path $ReceiptRoot -Label 'Preexisting protected host receipt root'
        if (-not (Test-Path -LiteralPath $ReceiptRoot -PathType Container)) {
            throw 'Protected host receipt root exists but is not a directory.'
        }
    }
    else {
        # Directory.CreateDirectory with a protected DirectorySecurity applies
        # the DACL as part of first creation.  A competing precreation is still
        # opened no-follow and identity-bound below before any Set-Acl/use.
        $receiptRootCreateAcl = New-DawnstrikeProtectedDirectorySecurity
        $null = [IO.Directory]::CreateDirectory($ReceiptRoot, $receiptRootCreateAcl)
        $null = Assert-DawnstrikeInstallNoReparse `
            -Path $ReceiptRoot -Label 'New protected host receipt root'
    }
    $receiptRootLease = Open-DawnstrikeStateBoundaryPath `
        -Path $ReceiptRoot -Label 'Protected host receipt root before ACL migration'
    try {
        Set-DawnstrikeProtectedDirectoryAcl -Path $ReceiptRoot
        $receiptRootAfter = Open-DawnstrikeStateBoundaryPath `
            -Path $ReceiptRoot -Label 'Protected host receipt root after ACL migration'
        try {
            if ([string]$receiptRootAfter.identity -cne [string]$receiptRootLease.identity) {
                throw 'Protected host receipt root identity changed during ACL migration.'
            }
        }
        finally { $receiptRootAfter.handle.Dispose() }
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($ReceiptRoot)
        $receiptRootSddl = [string](Get-Acl -LiteralPath $ReceiptRoot -ErrorAction Stop).Sddl
        $receiptRootSddlSha256 = Get-DawnstrikeStateBoundarySha256Text $receiptRootSddl

        $receiptPath = Join-Path $ReceiptRoot ('host-boundary-' + $ExpectedSha + '.json')
        if (Test-Path -LiteralPath $receiptPath) {
            $existingHostReceiptRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $receiptPath
            $existingStateBoundary = $null
            $existingStateReceiptRead = $null
            $existingRollbackRead = $null
            try {
                $existingHostReceipt = $existingHostReceiptRead.payload
                $expectedHostValues = [ordered]@{
                    schema_version = 'dawnstrike.host_boundary_installation.v2'
                    candidate_sha = $ExpectedSha
                    candidate_tree = $candidateTree
                    canonical_origin = $canonicalOrigin
                    protected_release_root = $protectedReleaseRoot
                    install_root = $InstallRoot
                    installer_path = $installerDestination
                    installer_sha256 = Get-DawnstrikeInstallSha256 $installerDestination
                    git_path = $git
                    git_sha256 = $expectedGitHash
                    git_archive_uri = $gitArchiveUri
                    git_archive_sha256 = $expectedGitArchiveHash
                    git_boundary_manifest_path = $gitBoundaryManifest
                    git_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $gitBoundaryManifest
                    node_path = $nodeExecutable
                    node_sha256 = $expectedNodeHash
                    node_archive_uri = $nodeArchiveUri
                    node_archive_sha256 = $expectedNodeArchiveHash
                    node_boundary_manifest_path = $nodeBoundaryManifest
                    node_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $nodeBoundaryManifest
                    vercel_cli_root = $vercelRoot
                    vercel_cli_tree_sha256 = $vercelTreeSha256
                    vercel_cli_entry_path = $vercelEntry
                    vercel_cli_entry_sha256 = $vercelEntrySha256
                    vercel_cli_boundary_manifest_path = $vercelBoundaryManifest
                    vercel_cli_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $vercelBoundaryManifest
                    python_path = (Join-Path $pythonDestination 'python.exe')
                    python_sha256 = $expectedPythonHash
                    python_installer_uri = $pythonInstallerUri
                    python_installer_sha256 = $expectedPythonInstallerHash
                    python_boundary_manifest_path = $pythonBoundaryManifest
                    python_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $pythonBoundaryManifest
                    uv_sha256 = $expectedUvHash
                    requirements_lock_blob = $requirementsLockBlob
                    requirements_lock_sha256 = $requirementsLockSha256
                    dependency_root = $dependencyDestination
                    dependency_manifest_path = $dependencyManifest
                    dependency_manifest_sha256 = Get-DawnstrikeInstallSha256 $dependencyManifest
                    launcher_path = $launcherDestination
                    launcher_sha256 = Get-DawnstrikeInstallSha256 $launcherDestination
                    state_boundary_helper_path = $stateBoundaryDestination
                    state_boundary_helper_sha256 = $stateBoundarySha256
                    receipt_root = $ReceiptRoot
                    receipt_root_identity = [string]$receiptRootLease.identity
                    receipt_root_sddl = $receiptRootSddl
                    receipt_root_sddl_sha256 = $receiptRootSddlSha256
                    state_root = $StateRoot
                    canonical_task_disposition = 'DISABLED_PENDING_GOVERNED_ACTIVATE_RESEAL'
                    auxiliary_capture_disposition = 'DISABLED_PENDING_GOVERNED_HARDEN_CAPTURE_REBIND'
                    dependency_verification = 'PASS'
                }
                foreach ($name in $expectedHostValues.Keys) {
                    if ([string]$existingHostReceipt.$name -cne [string]$expectedHostValues[$name]) {
                        throw "Preexisting protected host receipt identity is invalid: $name"
                    }
                }
                if (
                    $existingHostReceipt.research_only -ne $true -or
                    $existingHostReceipt.broker_execution_enabled -ne $false -or
                    [string]$existingHostReceipt.release_disposition -notin @(
                        'FRESH_CURRENT_MAIN_ATOMIC_IMPORT', 'REUSED_EXACT_ATOMIC_IMPORT'
                    ) -or
                    [string]$existingHostReceipt.python_boundary_disposition -notin @(
                        'FRESH_OFFICIAL_CORE_INSTALL', 'REUSED_EXACT_SEALED_CORE'
                    ) -or
                    [string]$existingHostReceipt.dependency_disposition -notin @(
                        'FRESH_EXACT_ATOMIC_DEPENDENCIES', 'REUSED_EXACT_SEALED_DEPENDENCIES'
                    ) -or
                    [string]$existingHostReceipt.node_boundary_disposition -notin @(
                        'FRESH_EXACT_ATOMIC_NODE', 'REUSED_EXACT_SEALED_NODE'
                    ) -or
                    [string]$existingHostReceipt.vercel_cli_boundary_disposition -notin @(
                        'FRESH_EXACT_ATOMIC_VERCEL_CLI', 'REUSED_EXACT_SEALED_VERCEL_CLI'
                    )
                ) {
                    throw 'Preexisting protected host receipt safety contract is invalid.'
                }

                $expectedStateReceiptPath = Join-Path $ReceiptRoot (
                    'state-boundary-' + $ExpectedSha + '.json'
                )
                if ([string]$existingHostReceipt.state_boundary_receipt_path -cne $expectedStateReceiptPath) {
                    throw 'Preexisting host receipt StateRoot receipt path is invalid.'
                }
                $existingStateReceiptRead = Read-DawnstrikeStateBoundaryProtectedJson `
                    -Path $expectedStateReceiptPath
                if (
                    [string]$existingStateReceiptRead.sha256 -cne `
                        [string]$existingHostReceipt.state_boundary_receipt_sha256
                ) {
                    throw 'Preexisting host receipt StateRoot receipt hash is invalid.'
                }
                $existingStateBoundary = Assert-DawnstrikeStateRootBoundary `
                    -StateRoot $StateRoot -EvidenceRoot $ReceiptRoot -AllowTaskDefinitionDrift
                if (
                    [string]$existingStateBoundary.candidate_sha -cne $ExpectedSha -or
                    [string]$existingStateBoundary.candidate_tree -cne $candidateTree -or
                    [string]$existingStateBoundary.receipt_sha256 -cne `
                        [string]$existingHostReceipt.state_boundary_receipt_sha256
                ) {
                    throw 'Live StateRoot boundary differs from the preexisting host receipt.'
                }

                $rollbackPath = [IO.Path]::GetFullPath(
                    [string]$existingHostReceipt.state_boundary_rollback_manifest_path
                )
                $receiptPrefix = [IO.Path]::GetFullPath($ReceiptRoot).TrimEnd('\') + '\'
                if (-not $rollbackPath.StartsWith($receiptPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                    throw 'Preexisting host receipt rollback manifest escaped its evidence root.'
                }
                $existingRollbackRead = Read-DawnstrikeStateBoundaryProtectedJson -Path $rollbackPath
                if (
                    [string]$existingRollbackRead.sha256 -cne `
                        [string]$existingHostReceipt.state_boundary_rollback_manifest_sha256
                ) {
                    throw 'Preexisting host receipt rollback manifest hash is invalid.'
                }

                [pscustomobject][ordered]@{
                    receipt = $existingHostReceipt
                    receipt_path = $receiptPath
                    receipt_sha256 = [string]$existingHostReceiptRead.sha256
                    reused_exact = $true
                } | ConvertTo-Json -Depth 8
                return
            }
            finally {
                if ($null -ne $existingRollbackRead) { $existingRollbackRead.stream.Dispose() }
                if ($null -ne $existingStateReceiptRead) { $existingStateReceiptRead.stream.Dispose() }
                if ($null -ne $existingStateBoundary) {
                    foreach ($handle in @($existingStateBoundary.locks)) {
                        if ($null -ne $handle) { $handle.Dispose() }
                    }
                }
                $existingHostReceiptRead.stream.Dispose()
            }
        }

        $currentStateBoundaryPath = Join-Path $ReceiptRoot 'state-boundary-current.json'
        if (Test-Path -LiteralPath $currentStateBoundaryPath) {
            $resumedStateBoundary = Assert-DawnstrikeStateRootBoundary `
                -StateRoot $StateRoot -EvidenceRoot $ReceiptRoot -AllowTaskDefinitionDrift
            $resumedCandidateReceiptRead = $null
            try {
                $targetBoundaryCurrent = (
                    [string]$resumedStateBoundary.candidate_sha -ceq $ExpectedSha -and
                    [string]$resumedStateBoundary.candidate_tree -ceq $candidateTree
                )
                if (-not $targetBoundaryCurrent) {
                    if ($migrationInputCount -ne 4) {
                        throw 'A different StateRoot boundary requires explicit protected candidate migration.'
                    }
                    if (
                        [string]$resumedStateBoundary.candidate_sha -cne $BoundaryPredecessorSha -or
                        [string]$resumedStateBoundary.candidate_tree -cne $BoundaryPredecessorTree
                    ) {
                        throw 'StateRoot migration predecessor differs from the exact current boundary.'
                    }
                    foreach ($handle in @($resumedStateBoundary.locks)) {
                        if ($null -ne $handle) { $handle.Dispose() }
                    }
                    $resumedStateBoundary.locks = @()
                    $migrationOutput = @(& $launcherDestination `
                        -Mode MigrateBoundary `
                        -ExpectedSha $ExpectedSha `
                        -CandidateRoot $protectedReleaseRoot `
                        -StateRoot $StateRoot `
                        -BoundaryPredecessorSha $BoundaryPredecessorSha `
                        -BoundaryPredecessorTree $BoundaryPredecessorTree `
                        -RuntimePredecessorSha $RuntimePredecessorSha `
                        -RuntimePredecessorTree $RuntimePredecessorTree)
                    if ($migrationOutput.Count -eq 0) {
                        throw 'Protected StateRoot candidate migration returned no terminal receipt.'
                    }
                    try {
                        $migrationTerminal = ($migrationOutput -join "`n") | ConvertFrom-Json
                    }
                    catch { throw 'Protected StateRoot candidate migration returned invalid JSON.' }
                    if (
                        [string]$migrationTerminal.status -cne 'COMPLETE' -or
                        [string]$migrationTerminal.operation_id -cnotmatch '^[0-9a-f]{32}$' -or
                        [string]$migrationTerminal.receipt.candidate_sha -cne $ExpectedSha -or
                        [string]$migrationTerminal.receipt.candidate_tree -cne $candidateTree -or
                        [string]$migrationTerminal.receipt.candidate_migration_from_sha -cne
                            $BoundaryPredecessorSha -or
                        [string]$migrationTerminal.receipt.candidate_migration_from_tree -cne
                            $BoundaryPredecessorTree -or
                        [string]$migrationTerminal.receipt.candidate_migration_runtime_sha -cne
                            $RuntimePredecessorSha -or
                        [string]$migrationTerminal.receipt.candidate_migration_runtime_tree -cne
                            $RuntimePredecessorTree -or
                        $migrationTerminal.research_only -ne $true -or
                        $migrationTerminal.broker_execution_enabled -ne $false
                    ) { throw 'Protected StateRoot candidate migration terminal identity is invalid.' }
                    $resumedStateBoundary = Assert-DawnstrikeStateRootBoundary `
                        -StateRoot $StateRoot -EvidenceRoot $ReceiptRoot -AllowTaskDefinitionDrift
                    if (
                        [string]$resumedStateBoundary.candidate_sha -cne $ExpectedSha -or
                        [string]$resumedStateBoundary.candidate_tree -cne $candidateTree
                    ) { throw 'Protected StateRoot candidate migration did not converge.' }
                }
                elseif ($migrationInputCount -eq 4 -and (
                    [string]$resumedStateBoundary.receipt.candidate_migration_from_sha -cne
                        $BoundaryPredecessorSha -or
                    [string]$resumedStateBoundary.receipt.candidate_migration_from_tree -cne
                        $BoundaryPredecessorTree -or
                    [string]$resumedStateBoundary.receipt.candidate_migration_runtime_sha -cne
                        $RuntimePredecessorSha -or
                    [string]$resumedStateBoundary.receipt.candidate_migration_runtime_tree -cne
                        $RuntimePredecessorTree
                )) {
                    throw 'Resumed StateRoot candidate migration belongs to another predecessor.'
                }
                if (
                    [string]$resumedStateBoundary.candidate_sha -cne $ExpectedSha -or
                    [string]$resumedStateBoundary.candidate_tree -cne $candidateTree
                ) {
                    throw 'A different exact-SHA StateRoot boundary is already current; historical install resume is denied.'
                }
                $resumedCandidateReceiptPath = Join-Path $ReceiptRoot (
                    'state-boundary-' + $ExpectedSha + '.json'
                )
                $resumedCandidateReceiptRead = Read-DawnstrikeStateBoundaryProtectedJson `
                    -Path $resumedCandidateReceiptPath
                if (
                    [string]$resumedCandidateReceiptRead.sha256 -cne `
                        [string]$resumedStateBoundary.receipt_sha256
                ) {
                    throw 'Resumed StateRoot candidate/current receipts differ.'
                }
                $stateBoundary = [pscustomobject]@{
                    receipt = $resumedStateBoundary.receipt
                    receipt_path = $resumedCandidateReceiptPath
                    receipt_sha256 = [string]$resumedCandidateReceiptRead.sha256
                    current_receipt_path = $currentStateBoundaryPath
                    rollback_manifest_path = [string]$resumedStateBoundary.receipt.rollback_manifest_path
                    rollback_manifest_sha256 = [string]$resumedStateBoundary.receipt.rollback_manifest_sha256
                    resumed_exact = $true
                }
            }
            finally {
                if ($null -ne $resumedCandidateReceiptRead) {
                    $resumedCandidateReceiptRead.stream.Dispose()
                }
                foreach ($handle in @($resumedStateBoundary.locks)) {
                    if ($null -ne $handle) { $handle.Dispose() }
                }
            }
        }
        else {
            $stateBoundary = Install-DawnstrikeStateRootBoundary `
                -StateRoot $StateRoot `
                -EvidenceRoot $ReceiptRoot `
                -CandidateSha $ExpectedSha `
                -CandidateTree $candidateTree `
                -InstalledHelperPath $stateBoundaryDestination `
                -InstalledHelperSha256 $stateBoundarySha256
        }
        $receipt = [ordered]@{
        schema_version = 'dawnstrike.host_boundary_installation.v2'
        candidate_sha = $ExpectedSha
        candidate_tree = $candidateTree
        canonical_origin = $canonicalOrigin
        requested_candidate_root = $requestedCandidate
        protected_release_root = $protectedReleaseRoot
        release_disposition = $releaseDisposition
        installed_at_utc = [DateTime]::UtcNow.ToString('o')
        installer_principal = [string]$identity.Name
        install_root = $InstallRoot
        installer_path = $installerDestination
        installer_sha256 = Get-DawnstrikeInstallSha256 $installerDestination
        git_path = $git
        git_sha256 = $expectedGitHash
        git_archive_uri = $gitArchiveUri
        git_archive_sha256 = $expectedGitArchiveHash
        git_boundary_manifest_path = $gitBoundaryManifest
        git_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $gitBoundaryManifest
        node_boundary_disposition = $nodeBoundaryDisposition
        node_path = $nodeExecutable
        node_sha256 = $expectedNodeHash
        node_archive_uri = $nodeArchiveUri
        node_archive_sha256 = $expectedNodeArchiveHash
        node_boundary_manifest_path = $nodeBoundaryManifest
        node_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $nodeBoundaryManifest
        vercel_cli_boundary_disposition = $vercelBoundaryDisposition
        vercel_cli_root = $vercelRoot
        vercel_cli_tree_sha256 = $vercelTreeSha256
        vercel_cli_entry_path = $vercelEntry
        vercel_cli_entry_sha256 = $vercelEntrySha256
        vercel_cli_boundary_manifest_path = $vercelBoundaryManifest
        vercel_cli_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $vercelBoundaryManifest
        python_path = (Join-Path $pythonDestination 'python.exe')
        python_sha256 = $expectedPythonHash
        python_installer_uri = $pythonInstallerUri
        python_installer_sha256 = $expectedPythonInstallerHash
        python_boundary_disposition = $pythonBoundaryDisposition
        python_boundary_manifest_path = $pythonBoundaryManifest
        python_boundary_manifest_sha256 = Get-DawnstrikeInstallSha256 $pythonBoundaryManifest
        uv_sha256 = $expectedUvHash
        requirements_lock_blob = $requirementsLockBlob
        requirements_lock_sha256 = $requirementsLockSha256
        dependency_disposition = $dependencyDisposition
        dependency_root = $dependencyDestination
        dependency_manifest_path = $dependencyManifest
        dependency_manifest_sha256 = Get-DawnstrikeInstallSha256 $dependencyManifest
        launcher_path = $launcherDestination
        launcher_sha256 = Get-DawnstrikeInstallSha256 $launcherDestination
        state_boundary_helper_path = $stateBoundaryDestination
        state_boundary_helper_sha256 = $stateBoundarySha256
        receipt_root = $ReceiptRoot
        receipt_root_preexisted = [bool]$receiptRootExisted
        receipt_root_identity = [string]$receiptRootLease.identity
        receipt_root_sddl = $receiptRootSddl
        receipt_root_sddl_sha256 = $receiptRootSddlSha256
        state_root = $StateRoot
        state_boundary_receipt_path = [string]$stateBoundary.receipt_path
        state_boundary_receipt_sha256 = [string]$stateBoundary.receipt_sha256
        state_boundary_rollback_manifest_path = [string]$stateBoundary.rollback_manifest_path
        state_boundary_rollback_manifest_sha256 = [string]$stateBoundary.rollback_manifest_sha256
        state_boundary = $stateBoundary.receipt
        canonical_task_disposition = 'DISABLED_PENDING_GOVERNED_ACTIVATE_RESEAL'
        auxiliary_capture_disposition = 'DISABLED_PENDING_GOVERNED_HARDEN_CAPTURE_REBIND'
        dependency_verification = 'PASS'
        research_only = $true
        broker_execution_enabled = $false
        }
        $hostReceiptWrite = Write-DawnstrikeStateBoundaryProtectedJson `
            -Payload $receipt -Path $receiptPath -NoReplace
        if ((Get-DawnstrikeInstallSha256 $receiptPath) -cne [string]$hostReceiptWrite.sha256) {
            throw 'Protected host-boundary receipt atomic replacement did not read back exactly.'
        }
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($receiptPath)
        [pscustomobject][ordered]@{
            receipt = $receipt
            receipt_path = $receiptPath
            receipt_sha256 = [string]$hostReceiptWrite.sha256
        } | ConvertTo-Json -Depth 6
    }
    finally {
        if ($null -ne $receiptRootLease) { $receiptRootLease.handle.Dispose() }
    }
}
finally {
    $receiptParentLease.handle.Dispose()
}
}
finally {
    if ($installMutexAcquired) { $installMutex.ReleaseMutex() }
    $installMutex.Dispose()
}
