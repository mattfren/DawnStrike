[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet(
        'Prepare',
        'HardenCapture',
        'BootstrapBaseline',
        'Activate',
        'RebindCapture',
        'Rollback',
        'MigrateBoundary',
        'BootstrapUniverse',
        'RecoverPublication'
    )][string]$Mode,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [Parameter(Mandatory = $true)][string]$CandidateRoot,
    [string]$MarketDate = '',
    [string]$CiEvidencePath = '',
    [string]$SolEvidencePath = '',
    [string]$ActivationReceipt = '',
    [string]$ContractRoot = '',
    [string]$SymbolsManifest = '',
    [string]$SymbolsManifestSha256 = '',
    [string]$EntitlementReceipt = '',
    [string]$EntitlementReceiptSha256 = '',
    [string]$SourceConfig = '',
    [string]$SourceConfigSha256 = '',
    [string]$RuntimeRoot = 'C:\r\dawnstrike-runtime',
    [string]$StateRoot = 'C:\r\dawnstrike-state',
    [string]$BackupRoot = 'C:\r\dawnstrike-state-backups',
    [ValidateRange(1, 30)][int]$BackupRetention = 3,
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300,
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$BoundaryPredecessorSha = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$BoundaryPredecessorTree = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$RuntimePredecessorSha = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$RuntimePredecessorTree = '',
    [pscredential]$RunAsCredential,
    [switch]$PromptForRunAsCredential,
    [switch]$PreflightOnly,
    [switch]$EnableCapture
)

$global:PSModuleAutoLoadingPreference = 'None'
$env:PSModulePath = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
. ([IO.Path]::Combine($PSScriptRoot, 'powershell_module_boundary.ps1'))

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (
    [string]$PSVersionTable.PSEdition -cne 'Desktop' -or
    [int]$PSVersionTable.PSVersion.Major -ne 5 -or
    [int]$PSVersionTable.PSVersion.Minor -ne 1
) {
    throw 'The Dawnstrike release launcher requires Windows PowerShell 5.1 Desktop.'
}
if ($Mode -eq 'BootstrapBaseline' -and $PreflightOnly) {
    throw 'BootstrapBaseline is a governed mutation and does not support PreflightOnly.'
}

$script:DawnstrikeReleaseRootParent = 'C:\Program Files\Dawnstrike\releases'
$script:DawnstrikeReleaseExactRoot = [IO.Path]::GetFullPath(
    (Join-Path $script:DawnstrikeReleaseRootParent $ExpectedSha)
).TrimEnd('\')
$script:DawnstrikeReleaseLauncherPath = Join-Path $script:DawnstrikeReleaseExactRoot 'scripts\dawnstrike_release_launcher.ps1'
$script:DawnstrikeReleaseStateBoundaryPath = Join-Path $script:DawnstrikeReleaseExactRoot 'scripts\state_root_boundary.ps1'
$script:DawnstrikeReleaseGitRoot = 'C:\Program Files\Dawnstrike\Git-2.55.0.5'
$script:DawnstrikeReleaseGitPath = 'C:\Program Files\Dawnstrike\Git-2.55.0.5\cmd\git.exe'
$script:DawnstrikeReleaseGitBoundaryManifest = 'C:\Program Files\Dawnstrike\Git-2.55.0.5\.dawnstrike-git-boundary-v1.json'
$script:DawnstrikeReleaseGitSha256 = '78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f' # pragma: allowlist secret
$script:DawnstrikeReleaseGitArchiveUri = 'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/MinGit-2.55.0.5-64-bit.zip'
$script:DawnstrikeReleaseGitArchiveSha256 = '56d7b226b7693196cfc71fef26568f536c4a021ab6c37ff2db4287bed908e96e' # pragma: allowlist secret
$script:DawnstrikeReleasePythonPath = 'C:\Program Files\Dawnstrike\Python313\python.exe'
$script:DawnstrikeReleasePythonSha256 = '85b71d8c6ec1905935f74be0c9869aae198d00e98f39df699ec66f9c5a84cecd' # pragma: allowlist secret
$script:DawnstrikeReleasePythonBoundaryManifest = 'C:\Program Files\Dawnstrike\Python313\.dawnstrike-python-boundary-v1.json'
$script:DawnstrikeReleaseDependencyParent = 'C:\Program Files\Dawnstrike\Dependencies'
$script:DawnstrikeReleaseGitSubject = 'CN=Johannes Schindelin, O=Johannes Schindelin, L=Bruehl, C=DE'
$script:DawnstrikeReleaseGitThumbprint = '2A1E97CBF0DFCDA15B0DA0AC9745014F989D4AD0' # pragma: allowlist secret
$script:DawnstrikeReleaseProtectedPrincipalSids = @{
    'S-1-5-18' = $true
    'S-1-5-32-544' = $true
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' = $true
}

function Resolve-DawnstrikeLauncherAclSid {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$IdentityReference,
        [Parameter(Mandatory = $true)][string]$Label
    )

    try {
        if ($IdentityReference -is [Security.Principal.SecurityIdentifier]) {
            return [string]$IdentityReference.Value
        }
        if ($IdentityReference -is [Security.Principal.IdentityReference]) {
            return [string]$IdentityReference.Translate(
                [Security.Principal.SecurityIdentifier]
            ).Value
        }
        return [string]([Security.Principal.NTAccount]::new(
            [string]$IdentityReference
        )).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch { throw "$Label cannot be translated to an exact SID." }
}

function Get-DawnstrikeLauncherSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($Path)))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Assert-DawnstrikeLauncherProtectedPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path)
    $dawnstrikeRoot = 'C:\Program Files\Dawnstrike'
    $dawnstrikePrefix = $dawnstrikeRoot + '\'
    if (
        -not [string]::Equals($full, $dawnstrikeRoot, [StringComparison]::OrdinalIgnoreCase) -and
        -not $full.StartsWith($dawnstrikePrefix, [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw 'Trusted release path is outside the protected Dawnstrike installation.'
    }
    $cursor = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    while ($null -ne $cursor) {
        if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Trusted release launcher path contains a reparse point."
        }
        $cursor = $cursor.Parent
    }
    $writeLikeRights = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership -bor
        [Security.AccessControl.FileSystemRights]::FullControl
    )
    $boundaries = @('C:\Program Files', $dawnstrikeRoot)
    if (-not [string]::Equals($full, $dawnstrikeRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $boundaryCursor = $dawnstrikeRoot
        foreach ($component in @($full.Substring($dawnstrikePrefix.Length) -split '\\')) {
            if ([string]::IsNullOrWhiteSpace($component)) {
                throw 'Trusted release path contains an empty protected component.'
            }
            $boundaryCursor = Join-Path $boundaryCursor $component
            $boundaries += $boundaryCursor
        }
    }
    foreach ($boundary in @($boundaries | Select-Object -Unique)) {
        $acl = Get-Acl -LiteralPath $boundary -ErrorAction Stop
        $ownerSid = Resolve-DawnstrikeLauncherAclSid `
            -IdentityReference $acl.Owner -Label 'Trusted release launcher owner'
        if (-not $script:DawnstrikeReleaseProtectedPrincipalSids.ContainsKey($ownerSid)) {
            throw 'Trusted release launcher is not administrator-owned.'
        }
        foreach ($rule in @($acl.Access)) {
            $ruleSid = Resolve-DawnstrikeLauncherAclSid `
                -IdentityReference $rule.IdentityReference `
                -Label 'Trusted release launcher access principal'
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                -not $script:DawnstrikeReleaseProtectedPrincipalSids.ContainsKey($ruleSid) -and
                ($rule.FileSystemRights -band $writeLikeRights) -ne 0
            ) {
                throw 'Trusted release launcher is writable by a non-admin principal.'
            }
        }
    }
}

function Get-DawnstrikeNormalizedBlobSha1 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $normalized = New-Object byte[] $Bytes.Length
    $count = 0
    for ($index = 0; $index -lt $Bytes.Length; $index++) {
        if ($Bytes[$index] -eq 13 -and $index + 1 -lt $Bytes.Length -and $Bytes[$index + 1] -eq 10) {
            $normalized[$count] = 10
            $count++
            $index++
        }
        else {
            $normalized[$count] = $Bytes[$index]
            $count++
        }
    }
    $body = New-Object byte[] $count
    [Array]::Copy($normalized, $body, $count)
    $header = [Text.Encoding]::ASCII.GetBytes("blob $count`0")
    $payload = New-Object byte[] ($header.Length + $body.Length)
    [Array]::Copy($header, 0, $payload, 0, $header.Length)
    [Array]::Copy($body, 0, $payload, $header.Length, $body.Length)
    $sha = [Security.Cryptography.SHA1]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($payload))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Invoke-DawnstrikeLauncherGit {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $saved = @{}
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        $saved[[string]$entry.Name] = [string]$entry.Value
        Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction Stop
    }
    try {
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_CONFIG_SYSTEM = 'NUL'
        $env:GIT_CONFIG_GLOBAL = 'NUL'
        $env:GIT_ATTR_NOSYSTEM = '1'
        $env:GIT_NO_REPLACE_OBJECTS = '1'
        $output = & $script:DawnstrikeReleaseGitPath `
            -c core.autocrlf=true `
            -c core.fsmonitor=false `
            -c core.untrackedCache=false `
            -c core.hooksPath=NUL `
            -c core.attributesFile=NUL `
            -c protocol.ext.allow=never `
            -C $Root @Arguments 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Trusted release launcher Git identity check failed.' }
        return ((@($output) | ForEach-Object { [string]$_ }) -join "`n").Trim()
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $saved.Keys) { Set-Item -LiteralPath ('Env:' + $name) -Value $saved[$name] }
    }
}

function Get-DawnstrikeLauncherGitDirectory {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $dotGit = Join-Path $Root '.git'
    if (Test-Path -LiteralPath $dotGit -PathType Container) { return $dotGit }
    if (-not (Test-Path -LiteralPath $dotGit -PathType Leaf)) {
        throw 'Trusted release launcher cannot resolve candidate Git metadata.'
    }
    $pointer = [IO.File]::ReadAllText($dotGit)
    if ($pointer -notmatch '(?s)^\s*gitdir:\s*([^\r\n]+?)\s*$') {
        throw 'Trusted release launcher found an invalid Git worktree pointer.'
    }
    $value = $Matches[1].Trim()
    if ([IO.Path]::IsPathRooted($value)) { return [IO.Path]::GetFullPath($value) }
    return [IO.Path]::GetFullPath((Join-Path $Root $value))
}

function Get-DawnstrikeLauncherSha256Bytes {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-DawnstrikeLauncherRequestFileContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Path)) { throw "$Label is required." }
    $full = [IO.Path]::GetFullPath($Path)
    $lease = Open-DawnstrikeStateBoundaryPath -Path $full -Label $Label
    $stream = $null
    try {
        if ($lease.is_directory) { throw "$Label is not a regular file." }
        $stream = [IO.File]::Open(
            $full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $digest = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
        finally { $sha.Dispose() }
        return [pscustomobject]@{
            path = $full
            sha256 = $digest
            stream = $stream
            lease = $lease.handle
        }
    }
    catch {
        if ($null -ne $stream) { $stream.Dispose() }
        $lease.handle.Dispose()
        throw
    }
}

function Assert-DawnstrikeLauncherEntryPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $normalized = $RelativePath.Replace('/', '\')
    if (
        [IO.Path]::IsPathRooted($RelativePath) -or
        $normalized -match '(^|\\)\.\.?($|\\)'
    ) {
        throw 'Trusted release entry path is unsafe.'
    }
    $path = [IO.Path]::GetFullPath((Join-Path $rootFull $normalized))
    $prefix = $rootFull + '\'
    if (-not $path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Trusted release entry escapes the candidate root.'
    }
    $cursor = $rootFull
    $segments = @($path.Substring($prefix.Length) -split '\\')
    for ($index = 0; $index -lt $segments.Count; $index++) {
        $segment = [string]$segments[$index]
        if ([string]::IsNullOrWhiteSpace($segment)) {
            throw 'Trusted release entry path contains an empty component.'
        }
        $cursor = Join-Path $cursor $segment
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Trusted release entry path contains a reparse point.'
        }
        if ($index -lt ($segments.Count - 1) -and -not $item.PSIsContainer) {
            throw 'Trusted release entry parent is not a regular directory.'
        }
    }
    return $path
}

function Assert-DawnstrikeLauncherCandidate {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Sha
    )

    Assert-DawnstrikeLauncherProtectedPath -Path $Root
    $rootItem = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Trusted release launcher candidate root is not a regular directory.'
    }
    $gitDirectory = Get-DawnstrikeLauncherGitDirectory -Root $Root
    $commonDirectory = $gitDirectory
    $commonPointer = Join-Path $gitDirectory 'commondir'
    if (Test-Path -LiteralPath $commonPointer -PathType Leaf) {
        $value = ([IO.File]::ReadAllText($commonPointer)).Trim()
        $commonDirectory = if ([IO.Path]::IsPathRooted($value)) {
            [IO.Path]::GetFullPath($value)
        }
        else { [IO.Path]::GetFullPath((Join-Path $gitDirectory $value)) }
    }
    foreach ($directory in @($gitDirectory, $commonDirectory) | Select-Object -Unique) {
        Assert-DawnstrikeLauncherProtectedPath -Path $directory
        $item = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Trusted release launcher Git metadata is unsafe.'
        }
        foreach ($name in @('config', 'config.worktree')) {
            $config = Join-Path $directory $name
            if (Test-Path -LiteralPath $config -PathType Leaf) {
                Assert-DawnstrikeLauncherProtectedPath -Path $config
                $text = [IO.File]::ReadAllText($config)
                if ($text -match '(?im)^\s*\[\s*(?:filter|url|protocol|include|credential|http)(?:\s|\])|^\s*(?:attributesfile|hookspath|path|sshcommand|proxy|helper|command)\s*=') {
                    throw 'Trusted release launcher rejected executable Git configuration.'
                }
            }
        }
        if (Test-Path -LiteralPath (Join-Path $directory 'info\attributes')) {
            throw 'Trusted release launcher rejected ungoverned Git attributes.'
        }
    }
    $top = [IO.Path]::GetFullPath((Invoke-DawnstrikeLauncherGit -Root $Root -Arguments @('rev-parse', '--show-toplevel'))).TrimEnd('\')
    if (-not [string]::Equals($top, $Root.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Trusted release launcher candidate root is not the exact Git worktree root.'
    }
    $head = (Invoke-DawnstrikeLauncherGit -Root $Root -Arguments @('rev-parse', 'HEAD')).ToLowerInvariant()
    $remoteMain = (Invoke-DawnstrikeLauncherGit -Root $Root -Arguments @('rev-parse', 'refs/remotes/origin/main')).ToLowerInvariant()
    if ($head -cne $Sha -or $remoteMain -cne $Sha) {
        throw 'Trusted release launcher requires exact local and origin/main SHA identity.'
    }
    $origin = Invoke-DawnstrikeLauncherGit -Root $Root -Arguments @('config', '--local', '--get', 'remote.origin.url')
    if ([string]$origin -cne 'https://github.com/mattfren/DawnStrike.git') {
        throw 'Trusted release launcher candidate origin is not governed.'
    }
    $status = Invoke-DawnstrikeLauncherGit -Root $Root -Arguments @('status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw 'Trusted release launcher requires a clean candidate worktree.'
    }
}

function Open-DawnstrikeLauncherEntry {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Sha,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    $namespaceLease = $null
    $stream = $null
    try {
        $path = Assert-DawnstrikeLauncherEntryPath -Root $Root -RelativePath $RelativePath
        Assert-DawnstrikeLauncherProtectedPath -Path $path
        $namespaceLease = Open-DawnstrikeStateBoundaryPath `
            -Path $path -Label 'Trusted release entry namespace'
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Trusted release entry is not a regular file.'
        }
        $stream = [IO.File]::Open(
            $path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $buffer = [IO.MemoryStream]::new()
        try { $stream.CopyTo($buffer) }
        finally { $stream.Position = 0 }
        $actualBlob = Get-DawnstrikeNormalizedBlobSha1 -Bytes $buffer.ToArray()
        $expectedBlob = (Invoke-DawnstrikeLauncherGit -Root $Root -Arguments @('rev-parse', ($Sha + ':' + $RelativePath.Replace('\', '/')))).ToLowerInvariant()
        if ($expectedBlob -notmatch '^[0-9a-f]{40}$' -or $actualBlob -cne $expectedBlob) {
            throw 'Trusted release entry bytes do not match the exact candidate commit.'
        }
        return [pscustomobject]@{
            path = $path
            stream = $stream
            namespace_lease = $namespaceLease.handle
        }
    }
    catch {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $namespaceLease -and $null -ne $namespaceLease.handle) {
            $namespaceLease.handle.Dispose()
        }
        throw
    }
}

$actualLauncher = [IO.Path]::GetFullPath($PSCommandPath)
if (-not [string]::Equals($actualLauncher, $script:DawnstrikeReleaseLauncherPath, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The Dawnstrike release launcher must run from its administrator-installed path.'
}
Assert-DawnstrikeLauncherProtectedPath -Path $actualLauncher
Assert-DawnstrikeLauncherProtectedPath -Path $script:DawnstrikeReleaseStateBoundaryPath
$script:DawnstrikeReleaseStateBoundarySourceLock = [IO.File]::Open(
    $script:DawnstrikeReleaseStateBoundaryPath,
    [IO.FileMode]::Open,
    [IO.FileAccess]::Read,
    [IO.FileShare]::Read
)
$stateBoundarySourceBuffer = [IO.MemoryStream]::new()
$script:DawnstrikeReleaseStateBoundarySourceLock.CopyTo($stateBoundarySourceBuffer)
$script:DawnstrikeReleaseStateBoundarySourceLock.Position = 0
. ([ScriptBlock]::Create(
    [Text.Encoding]::UTF8.GetString($stateBoundarySourceBuffer.ToArray())
))
$script:DawnstrikeReleaseNamespaceLocks = @()
foreach ($protectedBootstrapPath in @(
    $actualLauncher,
    $script:DawnstrikeReleaseStateBoundaryPath,
    $script:DawnstrikeReleaseGitPath
)) {
    Assert-DawnstrikeLauncherProtectedPath -Path $protectedBootstrapPath
    $protectedBootstrapLease = Open-DawnstrikeStateBoundaryPath `
        -Path $protectedBootstrapPath -Label 'Trusted release bootstrap namespace'
    $script:DawnstrikeReleaseNamespaceLocks += $protectedBootstrapLease.handle
}

function Open-DawnstrikeLauncherGitBoundary {
    [CmdletBinding()]
    param()

    $root = [IO.Path]::GetFullPath($script:DawnstrikeReleaseGitRoot).TrimEnd('\')
    $manifestPath = [IO.Path]::GetFullPath($script:DawnstrikeReleaseGitBoundaryManifest)
    $manifestName = [IO.Path]::GetFileName($manifestPath)
    $rootPrefix = $root + '\'
    $locks = @()
    try {
        foreach ($boundaryPath in @($root, $manifestPath)) {
            Assert-DawnstrikeLauncherProtectedPath -Path $boundaryPath
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path $boundaryPath -Label 'Trusted Git boundary namespace'
            $locks += $lease.handle
        }
        $manifestStream = [IO.File]::Open(
            $manifestPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $manifestStream
        $manifestBuffer = [IO.MemoryStream]::new()
        $manifestStream.CopyTo($manifestBuffer)
        $manifestBytes = $manifestBuffer.ToArray()
        try { $manifest = [Text.Encoding]::UTF8.GetString($manifestBytes) | ConvertFrom-Json }
        catch { throw 'Trusted Git boundary manifest is invalid JSON.' }
        if (
            [string]$manifest.schema_version -cne 'dawnstrike.git_boundary.v1' -or
            [string]$manifest.archive_uri -cne $script:DawnstrikeReleaseGitArchiveUri -or
            [string]$manifest.archive_sha256 -cne $script:DawnstrikeReleaseGitArchiveSha256 -or
            [string]$manifest.git_sha256 -cne $script:DawnstrikeReleaseGitSha256 -or
            @($manifest.files).Count -lt 1
        ) { throw 'Trusted Git boundary manifest contract is invalid.' }

        $expected = @{}
        foreach ($entry in @($manifest.files)) {
            $relative = [string]$entry.path
            $segments = @($relative -split '/')
            if (
                [string]::IsNullOrWhiteSpace($relative) -or
                $relative.Contains('\') -or
                $relative.Contains(':') -or
                [IO.Path]::IsPathRooted($relative) -or
                $segments.Count -lt 1 -or
                @($segments | Where-Object { $_ -in @('', '.', '..') }).Count -gt 0 -or
                $expected.ContainsKey($relative) -or
                [string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [long]$entry.length -lt 0
            ) { throw 'Trusted Git boundary manifest contains an invalid file identity.' }
            $full = [IO.Path]::GetFullPath((Join-Path $root ($relative.Replace('/', '\'))))
            if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Trusted Git boundary manifest file escapes its root.'
            }
            Assert-DawnstrikeLauncherProtectedPath -Path $full
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path $full -Label 'Trusted Git file namespace'
            $locks += $lease.handle
            $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Trusted Git boundary contains a non-regular file.'
            }
            $stream = [IO.File]::Open(
                $full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
            )
            $locks += $stream
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
            finally { $sha.Dispose(); $stream.Position = 0 }
            if ($stream.Length -ne [long]$entry.length -or $digest -cne [string]$entry.sha256) {
                throw "Trusted Git file identity differs from its sealed manifest: $relative"
            }
            $expected[$relative] = $true
        }

        $actual = @{}
        foreach ($item in @(Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Trusted Git boundary contains a reparse point.'
            }
            if ($item.PSIsContainer) { continue }
            $relative = $item.FullName.Substring($rootPrefix.Length).Replace('\', '/')
            if ([string]::Equals($relative, $manifestName, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            if ($actual.ContainsKey($relative) -or -not $expected.ContainsKey($relative)) {
                throw "Trusted Git boundary contains an unsealed file: $relative"
            }
            $actual[$relative] = $true
        }
        if ($actual.Count -ne $expected.Count) {
            throw 'Trusted Git file set differs from its sealed manifest.'
        }
        return [pscustomobject]@{
            git_path = $script:DawnstrikeReleaseGitPath
            git_sha256 = $script:DawnstrikeReleaseGitSha256
            git_boundary_manifest_path = $manifestPath
            git_boundary_manifest_sha256 = Get-DawnstrikeLauncherSha256Bytes $manifestBytes
            locks = @($locks)
        }
    }
    catch {
        foreach ($lock in @($locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Open-DawnstrikeLauncherPythonBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Sha
    )

    $requirementsPath = Join-Path $Root 'requirements.lock'
    $locks = @()
    try {
        foreach ($path in @(
            $requirementsPath,
            $script:DawnstrikeReleasePythonPath,
            $script:DawnstrikeReleasePythonBoundaryManifest,
            $script:DawnstrikeReleaseDependencyParent
        )) {
            Assert-DawnstrikeLauncherProtectedPath -Path $path
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path $path -Label 'Trusted Python boundary namespace'
            $locks += $lease.handle
        }
        $requirementsStream = [IO.File]::Open(
            $requirementsPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $requirementsStream
        $requirementsBuffer = [IO.MemoryStream]::new()
        $requirementsStream.CopyTo($requirementsBuffer)
        $requirementsStream.Position = 0
        $requirementsBytes = $requirementsBuffer.ToArray()
        $requirementsSha256 = Get-DawnstrikeLauncherSha256Bytes $requirementsBytes
        $requirementsBlob = Get-DawnstrikeNormalizedBlobSha1 -Bytes $requirementsBytes
        $expectedRequirementsBlob = (Invoke-DawnstrikeLauncherGit `
            -Root $Root -Arguments @('rev-parse', ($Sha + ':requirements.lock'))
        ).ToLowerInvariant()
        if ($requirementsBlob -cne $expectedRequirementsBlob) {
            throw 'Trusted Python boundary requirements.lock differs from exact release Git.'
        }
        $dependencyRoot = Join-Path $script:DawnstrikeReleaseDependencyParent $requirementsSha256
        $dependencyManifest = Join-Path $dependencyRoot '.dawnstrike-dependency-boundary-v1.json'
        foreach ($path in @($dependencyRoot, $dependencyManifest)) {
            Assert-DawnstrikeLauncherProtectedPath -Path $path
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path $path -Label 'Trusted dependency boundary namespace'
            $locks += $lease.handle
        }
        $pythonStream = [IO.File]::Open(
            $script:DawnstrikeReleasePythonPath,
            [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $pythonStream
        $pythonBuffer = [IO.MemoryStream]::new()
        $pythonStream.CopyTo($pythonBuffer)
        $pythonStream.Position = 0
        if ((Get-DawnstrikeLauncherSha256Bytes $pythonBuffer.ToArray()) -cne $script:DawnstrikeReleasePythonSha256) {
            throw 'Trusted Python boundary executable hash changed.'
        }
        $pythonManifestStream = [IO.File]::Open(
            $script:DawnstrikeReleasePythonBoundaryManifest,
            [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $pythonManifestStream
        $pythonManifestBuffer = [IO.MemoryStream]::new()
        $pythonManifestStream.CopyTo($pythonManifestBuffer)
        $pythonManifestStream.Position = 0
        $dependencyManifestStream = [IO.File]::Open(
            $dependencyManifest, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $dependencyManifestStream
        $dependencyManifestBuffer = [IO.MemoryStream]::new()
        $dependencyManifestStream.CopyTo($dependencyManifestBuffer)
        $dependencyManifestStream.Position = 0
        try {
            $pythonManifestPayload = [Text.Encoding]::UTF8.GetString(
                $pythonManifestBuffer.ToArray()
            ) | ConvertFrom-Json
            $dependencyManifestPayload = [Text.Encoding]::UTF8.GetString(
                $dependencyManifestBuffer.ToArray()
            ) | ConvertFrom-Json
        }
        catch { throw 'Trusted Python boundary manifest is invalid JSON.' }
        if (
            [string]$pythonManifestPayload.schema_version -cne 'dawnstrike.python_boundary.v1' -or
            [string]$pythonManifestPayload.python_sha256 -cne $script:DawnstrikeReleasePythonSha256 -or
            @($pythonManifestPayload.files).Count -lt 1 -or
            $pythonManifestPayload.research_only -ne $true -or
            $pythonManifestPayload.broker_execution_enabled -ne $false
        ) { throw 'Trusted Python boundary manifest contract is invalid.' }
        if (
            [string]$dependencyManifestPayload.schema_version -cne 'dawnstrike.dependency_boundary.v1' -or
            [string]$dependencyManifestPayload.requirements_lock_blob -cne $requirementsBlob -or
            [string]$dependencyManifestPayload.requirements_lock_sha256 -cne $requirementsSha256 -or
            @($dependencyManifestPayload.files).Count -lt 1 -or
            $dependencyManifestPayload.research_only -ne $true -or
            $dependencyManifestPayload.broker_execution_enabled -ne $false
        ) { throw 'Trusted dependency boundary manifest contract is invalid.' }
        return [pscustomobject]@{
            python_path = $script:DawnstrikeReleasePythonPath
            python_sha256 = $script:DawnstrikeReleasePythonSha256
            python_boundary_manifest_path = $script:DawnstrikeReleasePythonBoundaryManifest
            python_boundary_manifest_sha256 = Get-DawnstrikeLauncherSha256Bytes $pythonManifestBuffer.ToArray()
            requirements_lock_sha256 = $requirementsSha256
            requirements_lock_blob = $requirementsBlob
            dependency_root = [IO.Path]::GetFullPath($dependencyRoot).TrimEnd('\')
            dependency_manifest_path = [IO.Path]::GetFullPath($dependencyManifest)
            dependency_manifest_sha256 = Get-DawnstrikeLauncherSha256Bytes $dependencyManifestBuffer.ToArray()
            locks = @($locks)
        }
    }
    catch {
        foreach ($lock in @($locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}
$gitBoundary = Open-DawnstrikeLauncherGitBoundary
$script:DawnstrikeReleaseNamespaceLocks += @($gitBoundary.locks)
if ((Get-DawnstrikeLauncherSha256 $script:DawnstrikeReleaseGitPath) -cne $script:DawnstrikeReleaseGitSha256) {
    throw 'Trusted release launcher rejected the Git executable hash.'
}
$gitSignature = Get-AuthenticodeSignature -LiteralPath $script:DawnstrikeReleaseGitPath -ErrorAction Stop
if (
    [string]$gitSignature.Status -cne 'Valid' -or
    $null -eq $gitSignature.SignerCertificate -or
    [string]$gitSignature.SignerCertificate.Subject -cne $script:DawnstrikeReleaseGitSubject -or
    [string]$gitSignature.SignerCertificate.Thumbprint -cne $script:DawnstrikeReleaseGitThumbprint
) {
    throw 'Trusted release launcher rejected the Git executable signer.'
}

$candidate = [IO.Path]::GetFullPath(
    (Join-Path $script:DawnstrikeReleaseRootParent $ExpectedSha)
).TrimEnd('\')
Assert-DawnstrikeLauncherProtectedPath -Path $candidate
$protectedCandidateLease = Open-DawnstrikeStateBoundaryPath `
    -Path $candidate -Label 'Protected exact-SHA release root'
$script:DawnstrikeReleaseNamespaceLocks += $protectedCandidateLease.handle
Assert-DawnstrikeLauncherCandidate -Root $candidate -Sha $ExpectedSha
$candidateTree = (Invoke-DawnstrikeLauncherGit -Root $candidate -Arguments @('rev-parse', ($ExpectedSha + '^{tree}'))).ToLowerInvariant()
if ($candidateTree -notmatch '^[0-9a-f]{40}$') {
    throw 'Trusted release launcher could not resolve the exact candidate tree.'
}
$lunaCoreEntries = @()
if ($Mode -eq 'BootstrapUniverse') {
    $treeText = Invoke-DawnstrikeLauncherGit `
        -Root $candidate `
        -Arguments @('ls-tree', '-r', '--name-only', $ExpectedSha, '--', 'intraday_scanner')
    $lunaCoreEntries = @(
        $treeText -split "`n" | Where-Object { $_ -match '\.py$' } | ForEach-Object {
            $relative = ([string]$_).Trim().Replace('/', '\')
            if ($relative -notmatch '^intraday_scanner\\[A-Za-z0-9._\\/-]+\.py$') {
                throw 'Trusted release launcher found an unsafe Luna core source path.'
            }
            $relative
        }
    )
    if ($lunaCoreEntries.Count -eq 0) {
        throw 'Trusted release launcher found no committed Luna core Python sources.'
    }
}
$modeEntries = switch ($Mode) {
    'Prepare' { @('scripts\prepare_dawnstrike_state.ps1', 'scripts\activate_dawnstrike_runtime.ps1') }
    'HardenCapture' {
        @(
            'scripts\harden_intraday_capture_task.ps1',
            'scripts\capture_task_safety.ps1',
            'scripts\runtime_activation_lock.ps1',
            'scripts\capture_task_hardening_recovery.ps1',
            'scripts\resolve_dawnstrike_task_principal.ps1'
        )
    }
    'BootstrapBaseline' { @('scripts\activate_dawnstrike_runtime.ps1') }
    'Activate' { @('scripts\activate_dawnstrike_runtime.ps1') }
    'RebindCapture' {
        @(
            'scripts\rebind_intraday_capture_task.ps1',
            'scripts\resolve_dawnstrike_task_principal.ps1',
            'scripts\activate_dawnstrike_runtime.ps1',
            'scripts\dawnstrike_job_process.ps1',
            'scripts\invoke_dawnstrike_stage.ps1'
        )
    }
    'Rollback' { @('scripts\rollback_dawnstrike_runtime.ps1', 'scripts\activate_dawnstrike_runtime.ps1') }
    'MigrateBoundary' { @() }
    'BootstrapUniverse' {
        @(
            'scripts\bootstrap_luna_core_universe.ps1',
            'scripts\protected_operation_contract.ps1',
            'scripts\dawnstrike_process_runner.ps1',
            'scripts\dawnstrike_job_process.ps1',
            'scripts\runtime_activation_lock.ps1',
            'scripts\invoke_dawnstrike_stage.ps1',
            'scripts\dawnstrike_python_bootstrap.py',
            'scripts\refresh_luna_core_universe.py'
        ) + $lunaCoreEntries
    }
    'RecoverPublication' {
        @(
            'scripts\recover_vercel_publication.ps1',
            'scripts\protected_operation_contract.ps1',
            'scripts\runtime_activation_lock.ps1',
            'scripts\import_dawnstrike_environment.ps1',
            'scripts\publish_vercel_public.ps1',
            'scripts\dawnstrike_job_process.ps1',
            'scripts\dawnstrike_process_runner.ps1',
            'scripts\dawnstrike_python_bootstrap.py',
            'scripts\vercel_source_contract.ps1',
            'scripts\vercel_toolchain_contract.py',
            'scripts\vercel_publication_journal.py',
            'scripts\publication_boundary.py',
            'scripts\verify_daily_prepublication.py',
            'scripts\build_vercel_public_stage.ps1',
            'scripts\verify_vercel_candidate.ps1',
            'scripts\verify_public_artifact.py'
        )
    }
}
$relativeEntries = @(
    @($modeEntries) + @('requirements.lock', 'scripts\powershell_module_boundary.ps1') |
        Select-Object -Unique
)
$entryLocks = @()
$taskMutationMode = $Mode -in @('HardenCapture', 'BootstrapBaseline', 'RebindCapture', 'Rollback') -or
    ($Mode -eq 'Activate' -and -not $PreflightOnly)
$stateBoundary = $null
$migrationBoundary = $null
$requestAdmission = $null
$requestInputLocks = @()
$pythonBoundary = $null
$taskMutationAlreadyCompleted = $false
$stateBoundaryTerminalReconciliationRequired = $false
$taskMutationTerminalReceipt = ''
$taskMutationTerminalJournal = ''
$modeOutput = @()
$taskMutationRequestContractSha256 = ''
try {
if ($PromptForRunAsCredential) {
    if ($null -ne $RunAsCredential) {
        throw 'Specify either RunAsCredential or PromptForRunAsCredential, not both.'
    }
    if ($Mode -notin @('HardenCapture', 'BootstrapBaseline', 'Activate', 'RebindCapture', 'Rollback')) {
        throw 'This launcher mode does not accept a scheduled-task credential.'
    }
    $RunAsCredential = Microsoft.PowerShell.Security\Get-Credential `
        -Message 'Enter the exact local scheduled-task principal credential.'
    if ($null -eq $RunAsCredential) { throw 'Scheduled-task credential prompt was cancelled.' }
}
if ($taskMutationMode) {
    # No task-mutation request file, including a StateRoot receipt, is read
    # before the protected boundary admits this exact mode/SHA/tree. These
    # admission and per-file leases remain held through request hashing and are
    # replaced by the mutation admission leases before dispatch.
    $requestAdmission = Get-DawnstrikeStateBoundaryTaskMutationReadAdmission `
        -StateRoot $StateRoot -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $candidateTree
}
$requestedWriterSid = if ($null -ne $RunAsCredential) {
    Resolve-DawnstrikeStateBoundaryPrincipalSid -Principal ([string]$RunAsCredential.UserName)
}
else { 'NONE' }
if ($taskMutationMode) {
    $requestComponents = @(
        'dawnstrike.state_boundary_task_mutation_request.v1',
        ('mode=' + $Mode),
        ('candidate_sha=' + $ExpectedSha),
        ('candidate_tree=' + $candidateTree),
        ('runtime_root=' + [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')),
        ('state_root=' + [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')),
        ('run_as_sid=' + $requestedWriterSid)
    )
    switch ($Mode) {
        'HardenCapture' {
            if ($null -eq $RunAsCredential) {
                throw 'HardenCapture mode requires a locally prompted RunAsCredential.'
            }
        }
        'BootstrapBaseline' {
            if ([string]::IsNullOrWhiteSpace($MarketDate)) {
                throw 'BootstrapBaseline mode requires MarketDate.'
            }
            $ciRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $CiEvidencePath -Label 'BootstrapBaseline CI evidence'
            $requestInputLocks += @($ciRequest.stream, $ciRequest.lease)
            $solRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $SolEvidencePath -Label 'BootstrapBaseline Sol evidence'
            $requestInputLocks += @($solRequest.stream, $solRequest.lease)
            $requestComponents += @(
                ('market_date=' + $MarketDate),
                ('ci_evidence_path=' + $ciRequest.path),
                ('ci_evidence_sha256=' + $ciRequest.sha256),
                ('sol_evidence_path=' + $solRequest.path),
                ('sol_evidence_sha256=' + $solRequest.sha256),
                ('backup_root=' + [IO.Path]::GetFullPath($BackupRoot).TrimEnd('\')),
                ('backup_retention=' + $BackupRetention),
                ('process_timeout_seconds=' + $ProcessTimeoutSeconds)
            )
        }
        'Activate' {
            if ([string]::IsNullOrWhiteSpace($MarketDate)) { throw 'Activate mode requires MarketDate.' }
            $ciRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $CiEvidencePath -Label 'Activate CI evidence'
            $requestInputLocks += @($ciRequest.stream, $ciRequest.lease)
            $solRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $SolEvidencePath -Label 'Activate Sol evidence'
            $requestInputLocks += @($solRequest.stream, $solRequest.lease)
            $requestComponents += @(
                ('market_date=' + $MarketDate),
                ('ci_evidence_path=' + $ciRequest.path),
                ('ci_evidence_sha256=' + $ciRequest.sha256),
                ('sol_evidence_path=' + $solRequest.path),
                ('sol_evidence_sha256=' + $solRequest.sha256),
                ('backup_root=' + [IO.Path]::GetFullPath($BackupRoot).TrimEnd('\')),
                ('backup_retention=' + $BackupRetention),
                ('process_timeout_seconds=' + $ProcessTimeoutSeconds)
            )
        }
        'RebindCapture' {
            if (-not $EnableCapture) { throw 'RebindCapture mode requires explicit EnableCapture.' }
            if (
                $SymbolsManifestSha256 -notmatch '^[0-9a-f]{64}$' -or
                $EntitlementReceiptSha256 -notmatch '^[0-9a-f]{64}$' -or
                $SourceConfigSha256 -notmatch '^[0-9a-f]{64}$'
            ) { throw 'RebindCapture mode requires exact lowercase SHA-256 input bindings.' }
            $activationRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $ActivationReceipt -Label 'Rebind activation receipt'
            $requestInputLocks += @($activationRequest.stream, $activationRequest.lease)
            $symbolsRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $SymbolsManifest -Label 'Rebind symbols manifest'
            $requestInputLocks += @($symbolsRequest.stream, $symbolsRequest.lease)
            $entitlementRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $EntitlementReceipt -Label 'Rebind entitlement receipt'
            $requestInputLocks += @($entitlementRequest.stream, $entitlementRequest.lease)
            $sourceRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $SourceConfig -Label 'Rebind source config'
            $requestInputLocks += @($sourceRequest.stream, $sourceRequest.lease)
            $requestComponents += @(
                ('activation_receipt_path=' + $activationRequest.path),
                ('activation_receipt_sha256=' + $activationRequest.sha256),
                ('symbols_manifest_path=' + $symbolsRequest.path),
                ('symbols_manifest_actual_sha256=' + $symbolsRequest.sha256),
                ('symbols_manifest_claimed_sha256=' + $SymbolsManifestSha256),
                ('entitlement_receipt_path=' + $entitlementRequest.path),
                ('entitlement_receipt_actual_sha256=' + $entitlementRequest.sha256),
                ('entitlement_receipt_claimed_sha256=' + $EntitlementReceiptSha256),
                ('source_config_path=' + $sourceRequest.path),
                ('source_config_actual_sha256=' + $sourceRequest.sha256),
                ('source_config_claimed_sha256=' + $SourceConfigSha256),
                'enable_capture=true',
                ('process_timeout_seconds=' + $ProcessTimeoutSeconds)
            )
        }
        'Rollback' {
            $activationRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $ActivationReceipt -Label 'Rollback activation receipt'
            $requestInputLocks += @($activationRequest.stream, $activationRequest.lease)
            $lineageFields = @(
                'last_activation_id',
                'last_activation_terminal_receipt_relative_path',
                'last_activation_terminal_receipt_sha256',
                'last_activation_terminal_journal_relative_path',
                'last_activation_terminal_journal_sha256'
            )
            foreach ($lineageField in $lineageFields) {
                if ($requestAdmission.receipt.PSObject.Properties.Name -cnotcontains $lineageField) {
                    throw 'Rollback is not authorized by protected activation lineage.'
                }
            }
            $lastActivationId = [string]$requestAdmission.receipt.last_activation_id
            if ($lastActivationId -cnotmatch '^[0-9a-f]{24}$') {
                throw 'Rollback protected activation lineage has an invalid activation identity.'
            }
            $expectedActivationRelative = "receipts/runtime-activation/runtime-activation-$lastActivationId.json"
            $expectedJournalRelative = "receipts/runtime-operation/runtime-activation-$lastActivationId.json"
            $stateFull = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
            $expectedActivationPath = [IO.Path]::GetFullPath((Join-Path `
                $stateFull ($expectedActivationRelative.Replace('/', '\'))
            ))
            $expectedJournalPath = [IO.Path]::GetFullPath((Join-Path `
                $stateFull ($expectedJournalRelative.Replace('/', '\'))
            ))
            if (
                [string]$requestAdmission.receipt.last_activation_terminal_receipt_relative_path -cne $expectedActivationRelative -or
                [string]$requestAdmission.receipt.last_activation_terminal_journal_relative_path -cne $expectedJournalRelative -or
                -not [string]::Equals($activationRequest.path, $expectedActivationPath, [StringComparison]::OrdinalIgnoreCase) -or
                [string]$requestAdmission.receipt.last_activation_terminal_receipt_sha256 -cne $activationRequest.sha256
            ) { throw 'Rollback activation receipt is not the protected current activation lineage.' }
            $activationJournalRequest = Get-DawnstrikeLauncherRequestFileContract `
                -Path $expectedJournalPath -Label 'Rollback activation terminal journal'
            $requestInputLocks += @(
                $activationJournalRequest.stream,
                $activationJournalRequest.lease
            )
            if (
                [string]$requestAdmission.receipt.last_activation_terminal_journal_sha256 -cne
                    $activationJournalRequest.sha256
            ) { throw 'Rollback activation journal is not the protected current activation lineage.' }
            $effectiveContractRoot = $candidate
            $requestComponents += @(
                ('activation_receipt_path=' + $activationRequest.path),
                ('activation_receipt_sha256=' + $activationRequest.sha256),
                ('contract_root=' + $effectiveContractRoot),
                ('backup_root=' + [IO.Path]::GetFullPath($BackupRoot).TrimEnd('\')),
                ('process_timeout_seconds=' + $ProcessTimeoutSeconds)
            )
        }
    }
    $taskMutationRequestContractSha256 = Get-DawnstrikeStateBoundarySha256Text (
        ($requestComponents -join "`0") + "`n"
    )

    # Request hashing is complete under the admission leases. Release only the
    # current-receipt stream before Enter: pending completion adoption may need
    # to atomically replace that receipt. Retain both StateRoot namespace leases
    # until Enter has revalidated the receipt and returned mutation leases.
    if ($null -eq $requestAdmission -or @($requestAdmission.locks).Count -lt 3 -or
        $null -eq $requestAdmission.locks[0]) {
        throw 'StateRoot task-mutation request admission did not retain its exact receipt and namespace leases.'
    }
    $requestAdmission.locks[0].Dispose()
    $requestAdmission.locks = @($requestAdmission.locks | Select-Object -Skip 1)
}
if ($Mode -eq 'MigrateBoundary') {
    if (@(
        $BoundaryPredecessorSha,
        $BoundaryPredecessorTree,
        $RuntimePredecessorSha,
        $RuntimePredecessorTree
    ) | Where-Object { [string]::IsNullOrWhiteSpace([string]$_) }) {
        throw 'MigrateBoundary requires exact boundary and runtime predecessor SHA/tree identities.'
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'MigrateBoundary mode requires an elevated administrator process.'
    }
    $candidateAdmissionPath = Join-Path $candidate '.git\dawnstrike-host-admission-v1.json'
    Assert-DawnstrikeLauncherProtectedPath -Path $candidateAdmissionPath
    $candidateAdmissionRequest = Get-DawnstrikeLauncherRequestFileContract `
        -Path $candidateAdmissionPath -Label 'MigrateBoundary candidate admission'
    $requestInputLocks += @($candidateAdmissionRequest.stream, $candidateAdmissionRequest.lease)
    $candidateAdmissionBuffer = [IO.MemoryStream]::new()
    $candidateAdmissionRequest.stream.Position = 0
    $candidateAdmissionRequest.stream.CopyTo($candidateAdmissionBuffer)
    $candidateAdmissionRequest.stream.Position = 0
    try {
        $candidateAdmission = [Text.Encoding]::UTF8.GetString(
            $candidateAdmissionBuffer.ToArray()
        ) | ConvertFrom-Json
    }
    catch { throw 'MigrateBoundary candidate admission is invalid JSON.' }
    if (
        [string]$candidateAdmission.schema_version -cne 'dawnstrike.release_admission.v1' -or
        [string]$candidateAdmission.candidate_sha -cne $ExpectedSha -or
        [string]$candidateAdmission.candidate_tree -cne $candidateTree -or
        $candidateAdmission.research_only -ne $true -or
        $candidateAdmission.broker_execution_enabled -ne $false
    ) { throw 'MigrateBoundary candidate admission has the wrong safety identity.' }
    $installedHelperSha256 = Get-DawnstrikeLauncherSha256Bytes `
        $stateBoundarySourceBuffer.ToArray()
    $migrationRequestComponents = @(
        'dawnstrike.state_boundary_candidate_migration_request.v1',
        ('expected_boundary_sha=' + $BoundaryPredecessorSha),
        ('expected_boundary_tree=' + $BoundaryPredecessorTree),
        ('expected_runtime_sha=' + $RuntimePredecessorSha),
        ('expected_runtime_tree=' + $RuntimePredecessorTree),
        ('candidate_sha=' + $ExpectedSha),
        ('candidate_tree=' + $candidateTree),
        ('state_root=' + [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')),
        ('installed_helper_path=' + $script:DawnstrikeReleaseStateBoundaryPath),
        ('installed_helper_sha256=' + $installedHelperSha256),
        ('candidate_admission_path=' + $candidateAdmissionRequest.path),
        ('candidate_admission_sha256=' + $candidateAdmissionRequest.sha256)
    )
    $migrationRequestSha256 = Get-DawnstrikeStateBoundarySha256Text (
        ($migrationRequestComponents -join "`0") + "`n"
    )
    $migrationBoundary = Migrate-DawnstrikeStateRootBoundaryCandidate `
        -StateRoot $StateRoot `
        -ExpectedBoundarySha $BoundaryPredecessorSha `
        -ExpectedBoundaryTree $BoundaryPredecessorTree `
        -ExpectedRuntimeSha $RuntimePredecessorSha `
        -ExpectedRuntimeTree $RuntimePredecessorTree `
        -CandidateSha $ExpectedSha -CandidateTree $candidateTree `
        -RequestContractSha256 $migrationRequestSha256 `
        -InstalledHelperPath $script:DawnstrikeReleaseStateBoundaryPath `
        -InstalledHelperSha256 $installedHelperSha256 `
        -CandidateAdmissionPath $candidateAdmissionRequest.path `
        -CandidateAdmissionSha256 $candidateAdmissionRequest.sha256
    $stateBoundary = Assert-DawnstrikeStateRootBoundary -StateRoot $StateRoot
}
elseif ($taskMutationMode) {
    $stateBoundary = Enter-DawnstrikeStateBoundaryTaskMutation `
        -StateRoot $StateRoot -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $candidateTree `
        -RequestContractSha256 $taskMutationRequestContractSha256
}
else { $stateBoundary = Assert-DawnstrikeStateRootBoundary -StateRoot $StateRoot }
if ($null -ne $requestAdmission) {
    foreach ($requestAdmissionLock in @($requestAdmission.locks)) {
        if ($null -ne $requestAdmissionLock) { $requestAdmissionLock.Dispose() }
    }
    $requestAdmission = $null
}
if ($taskMutationMode) {
    $taskMutationAlreadyCompleted = [bool]$stateBoundary.already_completed
    $stateBoundaryTerminalReconciliationRequired = (
        $stateBoundary.PSObject.Properties.Name -contains 'terminal_reconciliation_required' -and
        [bool]$stateBoundary.terminal_reconciliation_required
    )
}
    if ($null -ne $RunAsCredential) {
        if ($requestedWriterSid -notin @($stateBoundary.writer_sids)) {
            throw 'RunAsCredential is not an exact ACL-admitted StateRoot writer SID.'
        }
    }
    foreach ($relative in $relativeEntries) {
        $entryLocks += Open-DawnstrikeLauncherEntry -Root $candidate -Sha $ExpectedSha -RelativePath $relative
    }
    $pythonBoundary = Open-DawnstrikeLauncherPythonBoundary `
        -Root $candidate -Sha $ExpectedSha
    if ($taskMutationAlreadyCompleted) {
        $stateBoundary | Select-Object status, operation_id, mode, receipt_sha256,
            research_only, broker_execution_enabled | ConvertTo-Json -Depth 4
    }
    elseif ($Mode -eq 'MigrateBoundary') {
        $migrationBoundary | ConvertTo-Json -Depth 8
    }
    elseif ($Mode -eq 'Prepare') {
        & $entryLocks[0].path `
            -CandidateRoot $candidate `
            -RuntimeRoot $RuntimeRoot `
            -StateRoot $StateRoot `
            -BackupRoot $BackupRoot `
            -CandidateSha $ExpectedSha `
            -Retention $BackupRetention `
            -ProcessTimeoutSeconds $ProcessTimeoutSeconds
    }
    elseif ($Mode -eq 'HardenCapture') {
        if ($null -eq $RunAsCredential) {
            throw 'HardenCapture mode requires a locally prompted RunAsCredential.'
        }
        $modeOutput = @(& $entryLocks[0].path `
            -RuntimeRoot $RuntimeRoot `
            -StateRoot $StateRoot `
            -CandidateSha $ExpectedSha `
            -CandidateTree $candidateTree `
            -StateBoundaryTaskMutationOperationId ([string]$stateBoundary.operation_id) `
            -RunAsCredential $RunAsCredential)
    }
    elseif ($Mode -in @('BootstrapBaseline', 'Activate')) {
        if ([string]::IsNullOrWhiteSpace($MarketDate) -or [string]::IsNullOrWhiteSpace($CiEvidencePath) -or [string]::IsNullOrWhiteSpace($SolEvidencePath)) {
            throw "$Mode mode requires MarketDate, CiEvidencePath, and SolEvidencePath."
        }
        if ($Mode -eq 'BootstrapBaseline' -or -not $PreflightOnly) {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $principal = [Security.Principal.WindowsPrincipal]::new($identity)
            if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
                if ($Mode -eq 'BootstrapBaseline') {
                    throw 'BootstrapBaseline mode requires an elevated administrator process.'
                }
                throw 'Activate mode requires an elevated administrator process.'
            }
        }
        $modeOutput = @(& $entryLocks[0].path `
            -ExpectedSha $ExpectedSha `
            -MarketDate $MarketDate `
            -CiEvidencePath $CiEvidencePath `
            -SolEvidencePath $SolEvidencePath `
            -CandidateRoot $candidate `
            -RuntimeRoot $RuntimeRoot `
            -StateRoot $StateRoot `
            -BackupRoot $BackupRoot `
            -BackupRetention $BackupRetention `
            -ProcessTimeoutSeconds $ProcessTimeoutSeconds `
            -RunAsCredential $RunAsCredential `
            -StateBoundaryTaskMutationOperationId ([string]$stateBoundary.operation_id) `
            -StateBoundaryTerminalReconciliationRequired:$stateBoundaryTerminalReconciliationRequired `
            -PreflightOnly:$PreflightOnly `
            -BootstrapBaseline:($Mode -eq 'BootstrapBaseline') `
            -AllowLegacyCanonicalExecute)
    }
    elseif ($Mode -eq 'RebindCapture') {
        if (
            $null -eq $RunAsCredential -or
            $SymbolsManifestSha256 -notmatch '^[0-9a-f]{64}$' -or
            $EntitlementReceiptSha256 -notmatch '^[0-9a-f]{64}$' -or
            $SourceConfigSha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]::IsNullOrWhiteSpace($SymbolsManifest) -or
            [string]::IsNullOrWhiteSpace($EntitlementReceipt) -or
            [string]::IsNullOrWhiteSpace($SourceConfig)
        ) {
            throw 'RebindCapture mode requires a credential and exact input files with SHA-256 bindings.'
        }
        $modeOutput = @(& $entryLocks[0].path `
            -RuntimeRoot $RuntimeRoot `
            -StateRoot $StateRoot `
            -CandidateSha $ExpectedSha `
            -SymbolsManifest $SymbolsManifest `
            -SymbolsManifestSha256 $SymbolsManifestSha256 `
            -EntitlementReceipt $EntitlementReceipt `
            -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
            -SourceConfig $SourceConfig `
            -SourceConfigSha256 $SourceConfigSha256 `
            -RunAsCredential $RunAsCredential `
            -StateBoundaryTaskMutationOperationId ([string]$stateBoundary.operation_id) `
            -StateBoundaryTerminalReconciliationRequired:$stateBoundaryTerminalReconciliationRequired `
            -Enable:$EnableCapture `
            -ProcessTimeoutSeconds $ProcessTimeoutSeconds)
    }
    elseif ($Mode -eq 'Rollback') {
        if ([string]::IsNullOrWhiteSpace($ActivationReceipt)) {
            throw 'Rollback mode requires ActivationReceipt.'
        }
        $ContractRoot = $candidate
        $modeOutput = @(& $entryLocks[0].path `
            -ActivationReceipt $ActivationReceipt `
            -ContractRoot $ContractRoot `
            -RuntimeRoot $RuntimeRoot `
            -StateRoot $StateRoot `
            -BackupRoot $BackupRoot `
            -ProcessTimeoutSeconds $ProcessTimeoutSeconds `
            -RunAsCredential $RunAsCredential `
            -StateBoundaryTaskMutationOperationId ([string]$stateBoundary.operation_id) `
            -StateBoundaryTerminalReconciliationRequired:$stateBoundaryTerminalReconciliationRequired)
    }
    elseif ($Mode -eq 'BootstrapUniverse') {
        if ([string]::IsNullOrWhiteSpace($MarketDate)) {
            throw 'BootstrapUniverse mode requires MarketDate.'
        }
        $mountedRuntime = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $RuntimeRoot).Path).TrimEnd('\')
        & $entryLocks[0].path `
            -ExpectedSha $ExpectedSha `
            -MarketDate $MarketDate `
            -RuntimeRoot $mountedRuntime `
            -StateRoot $StateRoot `
            -ProtectedLauncherGrant
    }
    else {
        if ([string]::IsNullOrWhiteSpace($MarketDate)) {
            throw 'RecoverPublication mode requires MarketDate.'
        }
        $mountedRuntime = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $RuntimeRoot).Path).TrimEnd('\')
        & $entryLocks[0].path `
            -ExpectedSha $ExpectedSha `
            -MarketDate $MarketDate `
            -RuntimeRoot $mountedRuntime `
            -StateRoot $StateRoot `
            -ProjectId 'prj_5pef3EZF1u5YadebEz3dFjnkWOXy' `
            -ProtectedLauncherGrant
    }
    if ($taskMutationMode -and -not $taskMutationAlreadyCompleted) {
        if ($modeOutput.Count -eq 0) { throw 'Task-mutating release mode returned no terminal receipt.' }
        try { $terminal = [string]$modeOutput[-1] | ConvertFrom-Json }
        catch { throw 'Task-mutating release mode did not return a terminal JSON receipt.' }
        $failClosedActivation = (
            $Mode -eq 'Activate' -and
            [string]$terminal.schema_version -ceq 'dawnstrike.runtime_activation_fail_closed.v1' -and
            [string]$terminal.status -ceq 'COMPENSATED_DISABLED'
        )
        if ($failClosedActivation) {
            if (
                [string]$terminal.candidate_sha -cne $ExpectedSha -or
                [string]$terminal.candidate_tree -cne $candidateTree -or
                [string]$terminal.restored_sha -cnotmatch '^[0-9a-f]{40}$' -or
                [string]$terminal.restored_tree -cnotmatch '^[0-9a-f]{40}$' -or
                [string]$terminal.activation_id -cnotmatch '^[0-9a-f]{24}$' -or
                [string]$terminal.source_terminal_receipt_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [string]$terminal.source_terminal_journal_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [string]$terminal.state_boundary_operation_id -cne [string]$stateBoundary.operation_id -or
                [string]$terminal.state_boundary_request_contract_sha256 -cne
                    $taskMutationRequestContractSha256 -or
                [string]$terminal.state_boundary_terminal_receipt_sha256 -cnotmatch
                    '^[0-9a-f]{64}$' -or
                [string]$terminal.state_boundary_terminal_journal_sha256 -cnotmatch
                    '^[0-9a-f]{64}$' -or
                $terminal.research_only -ne $true -or
                $terminal.broker_execution_enabled -ne $false
            ) { throw 'Activation fail-closed terminal has an invalid protected safety envelope.' }
            $compensationRelative = ([string]$terminal.compensation_receipt_relative_path).Replace('/', '\')
            if (
                [string]::IsNullOrWhiteSpace($compensationRelative) -or
                [IO.Path]::IsPathRooted($compensationRelative) -or
                $compensationRelative -match '(^|\\)\.\.?($|\\)' -or
                -not $compensationRelative.StartsWith(
                    'receipts\runtime-activation\', [StringComparison]::OrdinalIgnoreCase
                )
            ) { throw 'Activation fail-closed compensation receipt path is not canonical.' }
            $stateFull = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
            $taskMutationTerminalReceipt = [IO.Path]::GetFullPath(
                (Join-Path $stateFull $compensationRelative)
            )
            $taskMutationTerminalJournal = Join-Path $stateFull (
                'receipts\runtime-operation\terminal-recovery-' +
                [string]$stateBoundary.operation_id + '.json'
            )
            if ($null -eq $stateBoundary.locks -or @($stateBoundary.locks).Count -lt 3) {
                throw 'Activation fail-closed completion lost its retained StateRoot leases.'
            }
            $stateBoundary.locks[0].Dispose()
            $stateBoundary.locks = @($stateBoundary.locks | Select-Object -Skip 1)
            $taskBindingCompletion = Complete-DawnstrikeStateBoundaryTaskMutationFailClosed `
                -StateRoot $StateRoot -ExpectedSha $ExpectedSha -ExpectedTree $candidateTree `
                -OperationId ([string]$stateBoundary.operation_id) `
                -RequestContractSha256 $taskMutationRequestContractSha256 `
                -CompensationReceiptPath $taskMutationTerminalReceipt `
                -CompensationReceiptSha256 ([string]$terminal.state_boundary_terminal_receipt_sha256) `
                -CompensationJournalPath $taskMutationTerminalJournal `
                -CompensationJournalSha256 ([string]$terminal.state_boundary_terminal_journal_sha256) `
                -SourceActivationId ([string]$terminal.activation_id) `
                -SourceTerminalReceiptSha256 ([string]$terminal.source_terminal_receipt_sha256) `
                -SourceTerminalJournalSha256 ([string]$terminal.source_terminal_journal_sha256)
            $stateBoundary.locks = @($stateBoundary.locks) + @($taskBindingCompletion.locks)
            $taskMutationAlreadyCompleted = $true
            $modeOutput | Write-Output
            $taskBindingCompletion | Select-Object status, operation_id, mode,
                already_completed, receipt_sha256, research_only,
                broker_execution_enabled | ConvertTo-Json -Depth 5
        }
        if (-not $taskMutationAlreadyCompleted) {
        $expectedTerminal = switch ($Mode) {
            'HardenCapture' { @('dawnstrike.capture_task_hardening_receipt.v2', 'COMPLETE') }
            'RebindCapture' { @('dawnstrike.capture_task_rebind_receipt.v2', 'COMPLETE') }
            'BootstrapBaseline' { @('dawnstrike.runtime_activation_receipt.v2', 'COMPLETE') }
            'Activate' { @('dawnstrike.runtime_activation_receipt.v2', 'COMPLETE') }
            'Rollback' { @('dawnstrike.runtime_rollback_receipt.v1', 'ROLLED_BACK') }
        }
        if (
            [string]$terminal.schema_version -cne [string]$expectedTerminal[0] -or
            [string]$terminal.status -cne [string]$expectedTerminal[1] -or
            [string]$terminal.candidate_sha -cne $ExpectedSha -or
            [string]$terminal.candidate_tree -cne $candidateTree -or
            $terminal.research_only -ne $true -or
            $terminal.broker_execution_enabled -ne $false
        ) { throw 'Task-mutating release mode returned the wrong terminal safety identity.' }
        if (
            [string]$terminal.state_boundary_operation_id -notmatch '^[0-9a-f]{32}$' -or
            [string]$terminal.state_boundary_operation_id -cne [string]$stateBoundary.operation_id -or
            [string]$terminal.state_boundary_request_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$terminal.state_boundary_request_contract_sha256 -cne $taskMutationRequestContractSha256 -or
            [string]$terminal.state_boundary_terminal_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$terminal.state_boundary_terminal_journal_sha256 -notmatch '^[0-9a-f]{64}$'
        ) { throw 'Task-mutating release mode returned an invalid protected terminal envelope.' }
        if ($Mode -eq 'HardenCapture') {
            $taskMutationTerminalReceipt = Join-Path $StateRoot "receipts\capture-task\capture-task-hardening-$ExpectedSha.json"
            $taskMutationTerminalJournal = Join-Path $StateRoot "receipts\runtime-operation\capture-task-hardening-$ExpectedSha.json"
        }
        elseif ($Mode -eq 'RebindCapture') {
            $taskMutationTerminalReceipt = Join-Path $StateRoot "receipts\capture-task\capture-task-rebind-$ExpectedSha.json"
            $taskMutationTerminalJournal = Join-Path $StateRoot "receipts\runtime-operation\capture-task-rebind-$ExpectedSha.json"
        }
        else {
            $activationId = [string]$terminal.activation_id
            if ($activationId -notmatch '^[0-9a-f]{24}$') {
                throw 'Task-mutating release receipt has an invalid activation identity.'
            }
            $activationMode = $Mode -in @('BootstrapBaseline', 'Activate')
            $receiptFolder = if ($activationMode) { 'runtime-activation' } else { 'runtime-rollback' }
            $receiptStem = if ($activationMode) { 'runtime-activation-' } else { 'runtime-rollback-' }
            $taskMutationTerminalReceipt = Join-Path $StateRoot ("receipts\$receiptFolder\$receiptStem$activationId.json")
            $taskMutationTerminalJournal = Join-Path $StateRoot ("receipts\runtime-operation\$receiptStem$activationId.json")
        }
        # Keep every exact candidate entry and StateRoot namespace lease held
        # through terminal receipt verification and the protected ProgramData
        # reseal. Only the current-receipt stream itself must be released so
        # Complete can atomically replace that file.
        if ($null -eq $stateBoundary.locks -or @($stateBoundary.locks).Count -lt 3) {
            throw 'Task-mutation completion lost its retained StateRoot leases.'
        }
        $stateBoundary.locks[0].Dispose()
        $stateBoundary.locks = @($stateBoundary.locks | Select-Object -Skip 1)
        $taskBindingCompletion = Complete-DawnstrikeStateBoundaryTaskMutation `
            -StateRoot $StateRoot -Mode $Mode `
            -ExpectedSha $ExpectedSha -ExpectedTree $candidateTree `
            -OperationId ([string]$stateBoundary.operation_id) `
            -RequestContractSha256 $taskMutationRequestContractSha256 `
            -TerminalReceiptPath $taskMutationTerminalReceipt `
            -TerminalJournalPath $taskMutationTerminalJournal `
            -TerminalReceiptSha256 ([string]$terminal.state_boundary_terminal_receipt_sha256) `
            -TerminalJournalSha256 ([string]$terminal.state_boundary_terminal_journal_sha256)
        $modeOutput | Write-Output
        $taskBindingCompletion | ConvertTo-Json -Depth 5
        }
    }
}
catch {
    if ($taskMutationMode -and -not $taskMutationAlreadyCompleted -and $null -ne $stateBoundary) {
        try {
            $null = Cancel-DawnstrikeStateBoundaryTaskMutationIfUnchanged `
                -StateRoot $StateRoot -OperationId ([string]$stateBoundary.operation_id)
        }
        catch { }
    }
    throw
}
finally {
    foreach ($entryLock in $entryLocks) {
        if ($null -ne $entryLock -and $null -ne $entryLock.stream) { $entryLock.stream.Dispose() }
        if ($null -ne $entryLock -and $null -ne $entryLock.namespace_lease) {
            $entryLock.namespace_lease.Dispose()
        }
    }
    if ($null -ne $stateBoundary -and $null -ne $stateBoundary.locks) {
        foreach ($stateLock in @($stateBoundary.locks)) {
            if ($null -ne $stateLock) { $stateLock.Dispose() }
        }
    }
    foreach ($requestInputLock in @($requestInputLocks)) {
        if ($null -ne $requestInputLock) { $requestInputLock.Dispose() }
    }
    if ($null -ne $pythonBoundary -and $null -ne $pythonBoundary.locks) {
        foreach ($pythonBoundaryLock in @($pythonBoundary.locks)) {
            if ($null -ne $pythonBoundaryLock) { $pythonBoundaryLock.Dispose() }
        }
    }
    if ($null -ne $requestAdmission -and $null -ne $requestAdmission.locks) {
        foreach ($requestAdmissionLock in @($requestAdmission.locks)) {
            if ($null -ne $requestAdmissionLock) { $requestAdmissionLock.Dispose() }
        }
    }
    if ($null -ne $script:DawnstrikeReleaseStateBoundarySourceLock) {
        $script:DawnstrikeReleaseStateBoundarySourceLock.Dispose()
    }
    foreach ($namespaceLock in @($script:DawnstrikeReleaseNamespaceLocks)) {
        if ($null -ne $namespaceLock) { $namespaceLock.Dispose() }
    }
}
