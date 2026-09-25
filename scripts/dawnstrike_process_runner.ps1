$global:PSModuleAutoLoadingPreference = 'None'
$env:PSModulePath = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
. ([IO.Path]::Combine($PSScriptRoot, 'powershell_module_boundary.ps1'))

Set-StrictMode -Version Latest
$script:DawnstrikeExpectedReleaseSha = ""
$script:DawnstrikeScheduledSourceLocks = @()
$script:DawnstrikeProtectedReleaseParent = 'C:\Program Files\Dawnstrike\releases'
$script:DawnstrikeProtectedGitRoot = 'C:\Program Files\Dawnstrike\Git-2.55.0.5'
$script:DawnstrikeProtectedGitPath = 'C:\Program Files\Dawnstrike\Git-2.55.0.5\cmd\git.exe'
$script:DawnstrikeProtectedGitSha256 = '78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f' # pragma: allowlist secret
$script:DawnstrikeProtectedGitBoundaryManifest = 'C:\Program Files\Dawnstrike\Git-2.55.0.5\.dawnstrike-git-boundary-v1.json'
$script:DawnstrikeProtectedGitArchiveUri = 'https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.5/MinGit-2.55.0.5-64-bit.zip'
$script:DawnstrikeProtectedGitArchiveSha256 = '56d7b226b7693196cfc71fef26568f536c4a021ab6c37ff2db4287bed908e96e' # pragma: allowlist secret
$script:DawnstrikeProtectedGitBoundaryContract = $null
$script:DawnstrikeProtectedVercelTreeSha256 = '3bfb7509c4bf6a8fec920566c290a385c8160b9851b2350a655f0fd8b6c9e069' # pragma: allowlist secret
$script:DawnstrikeProtectedVercelRoot = 'C:\Program Files\Dawnstrike\VercelCli-' + $script:DawnstrikeProtectedVercelTreeSha256
$script:DawnstrikeProtectedVercelManifest = Join-Path $script:DawnstrikeProtectedVercelRoot '.dawnstrike-vercel-cli-boundary-v1.json'
$script:DawnstrikeProtectedVercelEntry = Join-Path $script:DawnstrikeProtectedVercelRoot 'node_modules\vercel\dist\vc.js'
$script:DawnstrikeProtectedVercelEntrySha256 = '2dd6e7c273a24bf4317af867d9b7bacb4db35487b42ea77912e2e7c33fa0c152' # pragma: allowlist secret
$script:DawnstrikeProtectedVercelBoundaryContract = $null
$script:DawnstrikeProtectedPythonRoot = 'C:\Program Files\Dawnstrike\Python313'
$script:DawnstrikeProtectedDependencyParent = 'C:\Program Files\Dawnstrike\Dependencies'
$script:DawnstrikeProtectedPrincipalSids = @{
    'S-1-5-18' = $true
    'S-1-5-32-544' = $true
    'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' = $true
}

function Resolve-DawnstrikeProcessAclSid {
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

function Assert-DawnstrikeProcessProtectedPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path)
    $installRoot = 'C:\Program Files\Dawnstrike'
    $installPrefix = $installRoot + '\'
    if (
        -not [string]::Equals($full, $installRoot, [StringComparison]::OrdinalIgnoreCase) -and
        -not $full.StartsWith($installPrefix, [StringComparison]::OrdinalIgnoreCase)
    ) { throw 'Protected process path is outside the Dawnstrike installation.' }
    $boundaries = @('C:\Program Files', $installRoot)
    if (-not [string]::Equals($full, $installRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $cursor = $installRoot
        foreach ($component in @($full.Substring($installPrefix.Length) -split '\\')) {
            if ([string]::IsNullOrWhiteSpace($component)) {
                throw 'Protected process path contains an empty component.'
            }
            $cursor = Join-Path $cursor $component
            $boundaries += $cursor
        }
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
    foreach ($boundary in @($boundaries | Select-Object -Unique)) {
        $item = Get-Item -LiteralPath $boundary -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Protected process path contains a reparse point.'
        }
        $acl = Get-Acl -LiteralPath $boundary -ErrorAction Stop
        $ownerSid = Resolve-DawnstrikeProcessAclSid `
            -IdentityReference $acl.Owner -Label 'Protected process owner'
        if (-not $script:DawnstrikeProtectedPrincipalSids.ContainsKey($ownerSid)) {
            throw 'Protected process path is not administrator-owned.'
        }
        foreach ($rule in @($acl.Access)) {
            $ruleSid = Resolve-DawnstrikeProcessAclSid `
                -IdentityReference $rule.IdentityReference -Label 'Protected process access principal'
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                -not $script:DawnstrikeProtectedPrincipalSids.ContainsKey($ruleSid) -and
                ($rule.FileSystemRights -band $writeLikeRights) -ne 0
            ) { throw 'Protected process path is writable by a non-admin principal.' }
        }
    }
    return $full
}

function Get-DawnstrikeProtectedReleaseRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedSha
    )

    return [IO.Path]::GetFullPath(
        (Join-Path $script:DawnstrikeProtectedReleaseParent $ExpectedSha.ToLowerInvariant())
    ).TrimEnd('\')
}

# Every scheduled child is launched through the native Job Object runner.  A
# PowerShell process tree is not a sufficient ownership boundary on Windows:
# detached Python/Node descendants can outlive the wrapper after a timeout.
# Keep this import local so interactive callers get the same kill-on-close
# contract without needing to know about the implementation helper.
if (
    -not ("Dawnstrike.Native.JobProcessRunner" -as [type]) -or
    -not (Get-Command Invoke-DawnstrikeJobProcess -ErrorAction SilentlyContinue)
) {
    . (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
}
if (-not (Get-Command Get-DawnstrikeApprovedLockInterpreter -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")
}
if (-not (Get-Command Assert-DawnstrikeStateRootBoundary -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot "state_root_boundary.ps1")
}

function Get-DawnstrikeProtectedGitBoundaryContract {
    [CmdletBinding()]
    param()

    if ($null -ne $script:DawnstrikeProtectedGitBoundaryContract) {
        return $script:DawnstrikeProtectedGitBoundaryContract
    }
    $root = [IO.Path]::GetFullPath($script:DawnstrikeProtectedGitRoot).TrimEnd('\')
    $manifestPath = [IO.Path]::GetFullPath($script:DawnstrikeProtectedGitBoundaryManifest)
    $manifestName = [IO.Path]::GetFileName($manifestPath)
    $rootPrefix = $root + '\'
    $locks = @()
    try {
        foreach ($boundaryPath in @($root, $manifestPath)) {
            $null = Assert-DawnstrikeProcessProtectedPath -Path $boundaryPath
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path $boundaryPath -Label 'Protected Git boundary namespace'
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
        catch { throw 'Protected Git boundary manifest is invalid JSON.' }
        if (
            [string]$manifest.schema_version -cne 'dawnstrike.git_boundary.v1' -or
            [string]$manifest.archive_uri -cne $script:DawnstrikeProtectedGitArchiveUri -or
            [string]$manifest.archive_sha256 -cne $script:DawnstrikeProtectedGitArchiveSha256 -or
            [string]$manifest.git_sha256 -cne $script:DawnstrikeProtectedGitSha256 -or
            @($manifest.files).Count -lt 1
        ) { throw 'Protected Git boundary manifest contract is invalid.' }

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
            ) { throw 'Protected Git boundary manifest contains an invalid file identity.' }
            $full = [IO.Path]::GetFullPath((Join-Path $root ($relative.Replace('/', '\'))))
            if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Protected Git boundary manifest file escapes its root.'
            }
            $null = Assert-DawnstrikeProcessProtectedPath -Path $full
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path $full -Label 'Protected Git file namespace'
            $locks += $lease.handle
            $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Protected Git boundary contains a non-regular file.'
            }
            $stream = [IO.File]::Open(
                $full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
            )
            $locks += $stream
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
            finally { $sha.Dispose(); $stream.Position = 0 }
            if ($stream.Length -ne [long]$entry.length -or $digest -cne [string]$entry.sha256) {
                throw "Protected Git file identity differs from its sealed manifest: $relative"
            }
            $expected[$relative] = $true
        }
        $actual = @{}
        foreach ($item in @(Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Protected Git boundary contains a reparse point.'
            }
            if ($item.PSIsContainer) { continue }
            $relative = $item.FullName.Substring($rootPrefix.Length).Replace('\', '/')
            if ([string]::Equals($relative, $manifestName, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            if ($actual.ContainsKey($relative) -or -not $expected.ContainsKey($relative)) {
                throw "Protected Git boundary contains an unsealed file: $relative"
            }
            $actual[$relative] = $true
        }
        if ($actual.Count -ne $expected.Count) {
            throw 'Protected Git file set differs from its sealed manifest.'
        }
        $approvedGit = Get-DawnstrikeApprovedGit
        if (
            -not [string]::Equals([string]$approvedGit.path, $script:DawnstrikeProtectedGitPath, [StringComparison]::OrdinalIgnoreCase) -or
            [string]$approvedGit.sha256 -cne $script:DawnstrikeProtectedGitSha256
        ) { throw 'Protected Git executable identity differs from its boundary contract.' }
        $contract = [pscustomobject]@{
            git_path = $script:DawnstrikeProtectedGitPath
            git_sha256 = $script:DawnstrikeProtectedGitSha256
            git_boundary_manifest_path = $manifestPath
            git_boundary_manifest_sha256 = Get-DawnstrikeLaunchSha256Bytes $manifestBytes
        }
        $script:DawnstrikeScheduledSourceLocks += @($locks)
        $script:DawnstrikeProtectedGitBoundaryContract = $contract
        return $contract
    }
    catch {
        foreach ($lock in @($locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Get-DawnstrikeProtectedVercelBoundaryContract {
    [CmdletBinding()]
    param()

    if ($null -ne $script:DawnstrikeProtectedVercelBoundaryContract) {
        return $script:DawnstrikeProtectedVercelBoundaryContract
    }
    $root = [IO.Path]::GetFullPath($script:DawnstrikeProtectedVercelRoot).TrimEnd('\')
    $manifestPath = [IO.Path]::GetFullPath($script:DawnstrikeProtectedVercelManifest)
    $manifestName = [IO.Path]::GetFileName($manifestPath)
    $rootPrefix = $root + '\'
    $locks = @()
    try {
        foreach ($pathContract in @(
            @($root, 'Protected Vercel CLI root namespace'),
            @($manifestPath, 'Protected Vercel CLI manifest namespace'),
            @($script:DawnstrikeProtectedVercelEntry, 'Protected Vercel CLI entry namespace')
        )) {
            $null = Assert-DawnstrikeProcessProtectedPath -Path ([string]$pathContract[0])
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path ([string]$pathContract[0]) -Label ([string]$pathContract[1])
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
        catch { throw 'Protected Vercel CLI manifest is invalid JSON.' }
        if (
            [string]$manifest.schema_version -cne 'dawnstrike.vercel_cli_boundary.v1' -or
            [string]$manifest.tree_sha256 -cne $script:DawnstrikeProtectedVercelTreeSha256 -or
            [int]$manifest.file_count -ne 7131 -or
            [string]$manifest.entry_relative_path -cne 'node_modules/vercel/dist/vc.js' -or
            [string]$manifest.entry_sha256 -cne $script:DawnstrikeProtectedVercelEntrySha256 -or
            [string]$manifest.vercel_version -cne '59.11.2' -or
            @($manifest.files).Count -ne 7131 -or
            $manifest.research_only -ne $true -or
            $manifest.broker_execution_enabled -ne $false
        ) { throw 'Protected Vercel CLI manifest contract is invalid.' }
        $expected = @{}
        $treeEntries = @()
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
                [string]::Equals($relative, $manifestName, [StringComparison]::OrdinalIgnoreCase) -or
                $expected.ContainsKey($relative) -or
                [string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
                [long]$entry.length -lt 0
            ) { throw 'Protected Vercel CLI manifest contains an invalid file identity.' }
            $full = [IO.Path]::GetFullPath((Join-Path $root ($relative.Replace('/', '\'))))
            if (-not $full.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Protected Vercel CLI manifest entry escaped its root.'
            }
            $null = Assert-DawnstrikeProcessProtectedPath -Path $full
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path $full -Label 'Protected Vercel CLI file namespace'
            $locks += $lease.handle
            $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
            if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Protected Vercel CLI boundary contains a non-regular file.'
            }
            $stream = [IO.File]::Open(
                $full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
            )
            $locks += $stream
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
            finally { $sha.Dispose(); $stream.Position = 0 }
            if ($stream.Length -ne [long]$entry.length -or $digest -cne [string]$entry.sha256) {
                throw "Protected Vercel CLI file differs from its seal: $relative"
            }
            $expected[$relative] = $true
            $treeEntries += [pscustomobject]@{
                path = $relative
                length = [long]$entry.length
                sha256 = [string]$entry.sha256
            }
        }
        $actual = @{}
        foreach ($item in @(Get-ChildItem -LiteralPath $root -Recurse -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Protected Vercel CLI boundary contains a reparse point.'
            }
            if ($item.PSIsContainer) { continue }
            $relative = $item.FullName.Substring($rootPrefix.Length).Replace('\', '/')
            if ([string]::Equals($relative, $manifestName, [StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            if ($actual.ContainsKey($relative) -or -not $expected.ContainsKey($relative)) {
                throw "Protected Vercel CLI contains an unsealed file: $relative"
            }
            $actual[$relative] = $true
        }
        if ($actual.Count -ne $expected.Count) {
            throw 'Protected Vercel CLI file set differs from its sealed manifest.'
        }
        $canonical = @(
            $treeEntries |
                Sort-Object `
                    @{ Expression = { ([string]$_.path).ToLowerInvariant() } }, `
                    @{ Expression = { [string]$_.path } } |
                ForEach-Object {
                    [string]$_.path + '|' + [string][long]$_.length + '|' + [string]$_.sha256
                }
        ) -join "`n"
        $treeSha256 = Get-DawnstrikeLaunchSha256Bytes ([Text.Encoding]::UTF8.GetBytes($canonical))
        if ($treeSha256 -cne $script:DawnstrikeProtectedVercelTreeSha256) {
            throw 'Protected Vercel CLI canonical tree identity is invalid.'
        }
        $entryStream = @($locks | Where-Object {
            $_ -is [IO.FileStream] -and
            [string]::Equals($_.Name, $script:DawnstrikeProtectedVercelEntry, [StringComparison]::OrdinalIgnoreCase)
        })
        if ($entryStream.Count -ne 1) {
            throw 'Protected Vercel CLI entrypoint is not retained exactly once.'
        }
        $contract = [pscustomobject]@{
            vercel_cli_root = $root
            vercel_cli_tree_sha256 = $script:DawnstrikeProtectedVercelTreeSha256
            vercel_cli_entry_path = $script:DawnstrikeProtectedVercelEntry
            vercel_cli_entry_sha256 = $script:DawnstrikeProtectedVercelEntrySha256
            vercel_cli_boundary_manifest_path = $manifestPath
            vercel_cli_boundary_manifest_sha256 = Get-DawnstrikeLaunchSha256Bytes $manifestBytes
        }
        $script:DawnstrikeScheduledSourceLocks += @($locks)
        $script:DawnstrikeProtectedVercelBoundaryContract = $contract
        return $contract
    }
    catch {
        foreach ($lock in @($locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        throw
    }
}

function Get-DawnstrikeGitBlobSha1 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $raw = [IO.File]::ReadAllBytes($Path)
    $normalized = New-Object byte[] $raw.Length
    $count = 0
    for ($index = 0; $index -lt $raw.Length; $index++) {
        if ($raw[$index] -eq 13 -and $index + 1 -lt $raw.Length -and $raw[$index + 1] -eq 10) {
            $normalized[$count] = 10; $count++; $index++
        } else { $normalized[$count] = $raw[$index]; $count++ }
    }
    $body = New-Object byte[] $count
    [Array]::Copy($normalized, $body, $count)
    $header = [Text.Encoding]::ASCII.GetBytes("blob $count`0")
    $payload = New-Object byte[] ($header.Length + $body.Length)
    [Array]::Copy($header, 0, $payload, 0, $header.Length)
    [Array]::Copy($body, 0, $payload, $header.Length, $body.Length)
    $sha = [Security.Cryptography.SHA1]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($payload))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Assert-DawnstrikeProcessSourceBoundToHead {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseRoot,
        [string]$ExpectedSha = "",
        [string]$EntryScript = "",
        [string[]]$AdditionalSourceFiles = @()
    )

    $root = [System.IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')
    $gitDirectory = Join-Path $root ".git"
    $gitPointerPath = $null
    if (Test-Path -LiteralPath $gitDirectory -PathType Leaf) {
        $gitPointerPath = $gitDirectory
        $pointer = Get-Content -Raw -LiteralPath $gitDirectory -ErrorAction Stop
        if ($pointer -notmatch '(?s)^\s*gitdir:\s*([^\r\n]+?)\s*$') {
            throw "Scheduled Python release has an invalid Git worktree pointer."
        }
        $gitPointerValue = $Matches[1].Trim()
        $gitDirectory = if ([System.IO.Path]::IsPathRooted($gitPointerValue)) {
            [System.IO.Path]::GetFullPath($gitPointerValue)
        }
        else { [System.IO.Path]::GetFullPath((Join-Path $root $gitPointerValue)) }
    }
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container)) {
        throw "Scheduled Python release root is not a self-contained Git checkout."
    }
    # Read the repository-local configuration before invoking Git.  A
    # candidate-controlled filter, attributes file, or hook path must not be
    # allowed to influence the identity check.
    $gitCommonDirectory = $gitDirectory
    $commonDirPath = Join-Path $gitDirectory 'commondir'
    if (Test-Path -LiteralPath $commonDirPath -PathType Leaf) {
        $commonDir = Get-Content -Raw -LiteralPath $commonDirPath -ErrorAction Stop
        if ($commonDir -notmatch '(?s)^\s*([^\r\n]+?)\s*$') {
            throw "Scheduled Python release has an invalid Git common-dir pointer."
        }
        $commonDirValue = $Matches[1].Trim()
        $gitCommonDirectory = if ([System.IO.Path]::IsPathRooted($commonDirValue)) {
            [System.IO.Path]::GetFullPath($commonDirValue)
        }
        else { [System.IO.Path]::GetFullPath((Join-Path $gitDirectory $commonDirValue)) }
    }
    if (-not (Test-Path -LiteralPath $gitCommonDirectory -PathType Container)) {
        throw "Scheduled Python release Git common directory is missing."
    }
    foreach ($attributesPath in @(
        (Join-Path $gitDirectory 'info\attributes'),
        (Join-Path $gitCommonDirectory 'info\attributes')
    ) | Select-Object -Unique) {
        if (Test-Path -LiteralPath $attributesPath) {
            throw "Scheduled Python release contains an ungoverned Git attributes file."
        }
    }
    $localConfigPath = Join-Path $gitCommonDirectory "config"
    if (-not (Test-Path -LiteralPath $localConfigPath -PathType Leaf)) {
        throw "Scheduled Python release local Git configuration is missing."
    }
    $configPaths = @($localConfigPath)
    $configTexts = @(Get-Content -Raw -LiteralPath $localConfigPath -ErrorAction Stop)
    foreach ($configDirectory in @(@($gitDirectory, $gitCommonDirectory) | Select-Object -Unique)) {
        $worktreeConfigPath = Join-Path $configDirectory 'config.worktree'
        if (Test-Path -LiteralPath $worktreeConfigPath -PathType Leaf) {
            $configPaths += $worktreeConfigPath
            $configTexts += Get-Content -Raw -LiteralPath $worktreeConfigPath -ErrorAction Stop
        }
    }
    $localConfig = $configTexts -join "`n"
    if ($localConfig -match "(?im)^\s*\[\s*(?:filter|url|protocol|include|credential|http)(?:\s|\])|^\s*(?:attributesfile|hookspath|path|sshcommand|proxy|helper|command)\s*=") {
        throw "Scheduled Python release contains a Git execution/filter configuration."
    }
    $git = (Get-DawnstrikeProtectedGitBoundaryContract).git_path
    # Windows checkouts may materialize committed LF blobs as CRLF.  Keep the
    # normal Git text normalization contract while disabling all external
    # filters/hooks; otherwise a clean, ordinary checkout is falsely rejected
    # as a byte-substituted release.
    $gitArgs = @(
        '-c', 'core.autocrlf=true', '-c', 'core.fsmonitor=false',
        '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=NUL',
        '-c', 'core.attributesFile=NUL', '-C', $root
    )
    $savedGitEnvironment = @{}
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        $savedGitEnvironment[[string]$entry.Name] = [string]$entry.Value
        Remove-Item -LiteralPath ("Env:" + [string]$entry.Name) -ErrorAction SilentlyContinue
    }
    try {
        if ($null -eq $script:DawnstrikeScheduledSourceLocks) {
            $script:DawnstrikeScheduledSourceLocks = @()
        }
        $metadataFiles = @($configPaths)
        if ($null -ne $gitPointerPath) { $metadataFiles += $gitPointerPath }
        if (Test-Path -LiteralPath $commonDirPath -PathType Leaf) { $metadataFiles += $commonDirPath }
        foreach ($metadataPath in @($metadataFiles | Select-Object -Unique)) {
            Assert-DawnstrikeSharedLockNoReparse $metadataPath "Scheduled Git metadata"
            $metadataItem = Get-Item -LiteralPath $metadataPath -Force -ErrorAction Stop
            if ($metadataItem.PSIsContainer -or ($metadataItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Scheduled Git metadata contains a non-regular file."
            }
            $script:DawnstrikeScheduledSourceLocks += [IO.File]::Open(
                $metadataPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
            )
        }
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_CONFIG_SYSTEM = 'NUL'
        $env:GIT_CONFIG_GLOBAL = 'NUL'
        $env:GIT_TERMINAL_PROMPT = '0'
        $env:GIT_OPTIONAL_LOCKS = '0'
        $env:GIT_NO_REPLACE_OBJECTS = '1'
        $env:GIT_ATTR_NOSYSTEM = '1'
        $top = ((& $git @gitArgs rev-parse --show-toplevel 2>$null) -join '').Trim()
        if ($LASTEXITCODE -ne 0 -or [System.IO.Path]::GetFullPath($top).TrimEnd('\') -ine $root) {
            throw "Scheduled Python release root is not the exact Git root."
        }
        $releaseHead = ((& $git @gitArgs rev-parse HEAD 2>$null) -join '').Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $releaseHead -notmatch '^[0-9a-f]{40}$') {
            throw "Scheduled Python release HEAD is invalid."
        }
        if ($ExpectedSha -and $releaseHead -cne $ExpectedSha.ToLowerInvariant()) {
            throw "Scheduled release HEAD does not match the externally activated SHA."
        }
        $status = ((& $git @gitArgs status --porcelain=v1 --untracked-files=all 2>$null) -join '')
        if ($LASTEXITCODE -ne 0 -or $status) {
            throw "Scheduled Python release checkout is not clean."
        }
        $flags = ((& $git @gitArgs ls-files -v -z 2>$null) -join '')
        if ($LASTEXITCODE -ne 0 -or @($flags -split "`0" | Where-Object { $_ -and $_.Substring(0, 1) -cmatch '[hSs]' }).Count -gt 0) {
            throw "Scheduled Python release contains hidden Git index entries."
        }
        $ignored = ((& $git @gitArgs ls-files --others --ignored --exclude-standard -z 2>$null) -join '')
        if ($LASTEXITCODE -ne 0) { throw "Scheduled Python ignored-artifact inventory failed." }
        $forbiddenIgnored = @(
            $ignored -split "`0" | Where-Object {
                if (-not $_) { return $false }
                $name = [System.IO.Path]::GetFileName($_).ToLowerInvariant()
                $extension = [System.IO.Path]::GetExtension($_).ToLowerInvariant()
                $extension -in @(
                    '.ps1', '.psm1', '.py', '.pyc', '.pyd', '.dll', '.exe',
                    '.com', '.bat', '.cmd', '.sh', '.pth'
                ) -or $name -in @('sitecustomize.py', 'usercustomize.py')
            }
        )
        if ($forbiddenIgnored.Count -gt 0) {
            throw "Scheduled Python release contains ignored executable or startup artifacts."
        }
        $replacements = ((& $git @gitArgs replace -l 2>$null) -join '').Trim()
        if ($LASTEXITCODE -eq 0 -and $replacements) {
            throw "Scheduled Python release contains Git replace refs."
        }
        foreach ($configPattern in @('filter.*', 'core.attributesfile', 'core.hooksPath')) {
            $config = ((& $git @gitArgs config --local --get-regexp $configPattern 2>$null) -join '').Trim()
            if ($LASTEXITCODE -eq 0 -and $config) {
                throw "Scheduled Python release contains a Git execution/filter configuration."
            }
        }
        $null = & $git @gitArgs diff-index --quiet HEAD -- 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Scheduled Python release differs from exact HEAD."
        }
        $sourceFiles = @(
            ".gitattributes",
            "scripts/dawnstrike_process_runner.ps1",
            "scripts/dawnstrike_job_process.ps1",
            "scripts/runtime_activation_lock.ps1",
            "scripts/dawnstrike_python_bootstrap.py"
        )
        foreach ($additional in @($AdditionalSourceFiles)) {
            if ([string]::IsNullOrWhiteSpace($additional)) {
                throw "Scheduled Python additional source path is empty."
            }
            $normalizedAdditional = $additional.Replace('\', '/')
            if (
                [System.IO.Path]::IsPathRooted($additional) -or
                $normalizedAdditional.StartsWith('/') -or
                $normalizedAdditional -match '(^|/)\.\.(/|$)'
            ) {
                throw "Scheduled Python additional source path is unsafe."
            }
            $additionalPath = [System.IO.Path]::GetFullPath(
                (Join-Path $root ($normalizedAdditional.Replace('/', '\')))
            )
            $rootPrefix = $root.TrimEnd('\') + '\'
            if (-not $additionalPath.StartsWith(
                $rootPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
                throw "Scheduled Python additional source path escapes the exact release root."
            }
            $sourceFiles += $normalizedAdditional
        }
        foreach ($relative in @($sourceFiles | Select-Object -Unique)) {
            $relative = $relative.Replace('\', '/')
            $path = Join-Path $root ($relative.Replace('/', '\'))
            Assert-DawnstrikeSharedLockNoReparse $path "Scheduled Python helper"
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "Scheduled Python helper is missing: $relative"
            }
            $script:DawnstrikeScheduledSourceLocks += [IO.File]::Open(
                $path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
            )
            $headBlob = ((& $git @gitArgs rev-parse ("HEAD:" + $relative) 2>$null) -join '').Trim().ToLowerInvariant()
            if ($LASTEXITCODE -ne 0 -or $headBlob -notmatch '^[0-9a-f]{40}$') {
                throw "Scheduled Python helper is not tracked by exact HEAD: $relative"
            }
            $raw = ((& $git @gitArgs hash-object --no-filters -- $path 2>$null) -join '').Trim().ToLowerInvariant()
            $worktree = Get-DawnstrikeGitBlobSha1 $path
            if ($LASTEXITCODE -ne 0 -or $worktree -cne $headBlob -or $raw -notmatch '^[0-9a-f]{40}$') {
                throw "Scheduled Python helper bytes changed from exact HEAD: $relative"
            }
        }
        if ($EntryScript) {
            $entryPath = [System.IO.Path]::GetFullPath($EntryScript)
            $rootPrefix = $root.TrimEnd('\') + '\'
            if (-not $entryPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Scheduled entry script is outside the exact release root."
            }
            Assert-DawnstrikeSharedLockNoReparse $entryPath "Scheduled entry script"
            $entryRelative = $entryPath.Substring($rootPrefix.Length).Replace('\', '/')
            $entryHeadBlob = ((& $git @gitArgs rev-parse ("HEAD:" + $entryRelative) 2>$null) -join '').Trim().ToLowerInvariant()
            $entryRaw = ((& $git @gitArgs hash-object --no-filters -- $entryPath 2>$null) -join '').Trim().ToLowerInvariant()
            $entryWorktree = Get-DawnstrikeGitBlobSha1 $entryPath
            if ($LASTEXITCODE -ne 0 -or $entryHeadBlob -notmatch '^[0-9a-f]{40}$' -or $entryWorktree -cne $entryHeadBlob -or $entryRaw -notmatch '^[0-9a-f]{40}$') {
                throw "Scheduled entry script bytes changed from exact HEAD."
            }
        }
        return [pscustomobject]@{ root = $root; head = $releaseHead }
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ("Env:" + [string]$entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $savedGitEnvironment.Keys) { Set-Item -LiteralPath ("Env:" + $name) -Value $savedGitEnvironment[$name] }
    }
}

function Get-DawnstrikeLunaCoreSourceFiles {
    [CmdletBinding()]
    param(
        [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$ExpectedSha = ''
    )

    $releaseRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
    $packageRoot = Join-Path $releaseRoot 'intraday_scanner'
    Assert-DawnstrikeSharedLockNoReparse $packageRoot 'Luna core Python source root'
    $releasePrefix = $releaseRoot + '\'
    $diskSources = @()
    foreach ($item in @(
        Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Filter '*.py' -Force |
            Sort-Object -Property FullName
    )) {
        Assert-DawnstrikeSharedLockNoReparse $item.FullName 'Luna core Python source'
        $full = [IO.Path]::GetFullPath($item.FullName)
        if (-not $full.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Luna core Python source escaped the exact release root.'
        }
        $relative = $full.Substring($releasePrefix.Length).Replace('\', '/')
        if ($relative -notmatch '^intraday_scanner/[A-Za-z0-9._/-]+\.py$') {
            throw 'Luna core Python source path is unsafe.'
        }
        $diskSources += $relative
    }

    # The committed tree, not mutable filesystem enumeration, is authoritative.
    # Returning every HEAD-listed Python path means a file hidden during the
    # directory walk is still required and opened by the caller's admission
    # pass before Python can import anything.
    $git = [string](Get-DawnstrikeProtectedGitBoundaryContract).git_path
    $treeish = if ($ExpectedSha) { $ExpectedSha.ToLowerInvariant() } else { 'HEAD' }
    $gitArgs = @(
        '-c', 'core.autocrlf=true', '-c', 'core.fsmonitor=false',
        '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=NUL',
        '-c', 'core.attributesFile=NUL', '-c', 'protocol.ext.allow=never',
        '-C', $releaseRoot
    )
    $savedGitEnvironment = @{}
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        $savedGitEnvironment[[string]$entry.Name] = [string]$entry.Value
        Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction SilentlyContinue
    }
    try {
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_CONFIG_SYSTEM = 'NUL'
        $env:GIT_CONFIG_GLOBAL = 'NUL'
        $env:GIT_TERMINAL_PROMPT = '0'
        $env:GIT_OPTIONAL_LOCKS = '0'
        $env:GIT_NO_REPLACE_OBJECTS = '1'
        $env:GIT_ATTR_NOSYSTEM = '1'
        $rawTree = ((& $git @gitArgs ls-tree -r --name-only -z $treeish -- intraday_scanner 2>$null) -join '')
        if ($LASTEXITCODE -ne 0) {
            throw 'Luna core Python source tree could not be derived from exact Git.'
        }
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $savedGitEnvironment.Keys) {
            Set-Item -LiteralPath ('Env:' + $name) -Value $savedGitEnvironment[$name]
        }
    }
    $headSources = @(
        $rawTree -split "`0" | Where-Object { $_ } | ForEach-Object {
            $relative = [string]$_
            if ($relative -match '\.py$') {
                if ($relative -notmatch '^intraday_scanner/[A-Za-z0-9._/-]+\.py$') {
                    throw 'Exact Git contains an unsafe Luna core Python source path.'
                }
                $relative
            }
        }
    )
    if ($headSources.Count -eq 0) {
        throw 'Exact Git contains no Luna core Python source files.'
    }
    $diskIdentity = @($diskSources | Sort-Object) -join "`n"
    $headIdentity = @($headSources | Sort-Object) -join "`n"
    if ($diskIdentity -cne $headIdentity) {
        throw 'Luna core Python filesystem inventory differs from exact Git.'
    }
    return @('scripts/refresh_luna_core_universe.py') + @($headSources)
}

function Get-DawnstrikeScheduledLaunchFiles {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet(
            "run_alphaops_morning.ps1",
            "run_alphaops_monitor.ps1",
            "run_alphaops_eod.ps1",
            "run_alphaops_weekly_training.ps1",
            "run_daily_finalize.ps1"
        )]
        [string]$TaskScript,
        [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$ExpectedSha = ''
    )

    $common = @(
        "requirements.lock",
        "scripts/$TaskScript",
        "scripts/import_dawnstrike_environment.ps1",
        "scripts/dawnstrike_process_runner.ps1",
        "scripts/dawnstrike_job_process.ps1",
        "scripts/runtime_activation_lock.ps1",
        "scripts/runtime_activation_lock_contract.py",
        "scripts/runtime_operation_journal.py",
        "scripts/runtime_activation_contract.py",
        "scripts/dawnstrike_python_bootstrap.py",
        "scripts/state_disaster_recovery.py",
        "scripts/powershell_module_boundary.ps1",
        "scripts/state_root_boundary.ps1",
        "scripts/invoke_dawnstrike_stage.ps1"
    )
    if ($TaskScript -in @("run_alphaops_morning.ps1", "run_alphaops_monitor.ps1")) {
        $common += "scripts/alpha_cycle_artifact.ps1"
    }
    if ($TaskScript -eq "run_alphaops_morning.ps1") {
        $common += Get-DawnstrikeLunaCoreSourceFiles -ExpectedSha $ExpectedSha
    }
    if ($TaskScript -eq "run_alphaops_monitor.ps1") {
        $common += "scripts/monitor_schedule_helper.ps1"
    }
    if ($TaskScript -eq "run_daily_finalize.ps1") {
        $common += @(
            "scripts/publish_vercel_public.ps1",
            "scripts/vercel_source_contract.ps1",
            "scripts/vercel_toolchain_contract.py",
            "scripts/vercel_publication_journal.py",
            "scripts/publication_boundary.py",
            "scripts/verify_daily_prepublication.py",
            "scripts/build_vercel_public_stage.ps1",
            "scripts/verify_vercel_candidate.ps1",
            "scripts/verify_public_artifact.py"
        )
    }
    return @($common | Select-Object -Unique)
}

function Get-DawnstrikeLaunchSha256Bytes {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function New-DawnstrikeScheduledLaunchManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)]
        [ValidateSet(
            "run_alphaops_morning.ps1",
            "run_alphaops_monitor.ps1",
            "run_alphaops_eod.ps1",
            "run_alphaops_weekly_training.ps1",
            "run_daily_finalize.ps1"
        )]
        [string]$TaskScript
    )

    $runtime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $release = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSha
    $state = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $gitContract = Get-DawnstrikeProtectedGitBoundaryContract
    $pythonContract = Get-DawnstrikeProtectedPythonDependencyContract `
        -ReleaseRoot $release -ExpectedSha $ExpectedSha
    $vercelContract = if ($TaskScript -eq 'run_daily_finalize.ps1') {
        Get-DawnstrikeProtectedVercelBoundaryContract
    }
    else { $null }
    $stateBoundary = $null
    if ([string]::Equals($state, $script:DawnstrikeStateBoundaryFixedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $stateBoundary = Assert-DawnstrikeStateRootBoundary -StateRoot $state
    }
    try {
    $safeTask = $TaskScript -replace '[^A-Za-z0-9._-]', '_'
    $root = Join-Path $state 'receipts\scheduler-launch'
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $path = Join-Path $root ($ExpectedSha.ToLowerInvariant() + '-' + $safeTask + '.json')
    $entries = @()
    $releasePrefix = $release.TrimEnd('\') + '\'
    foreach ($relative in @(
        Get-DawnstrikeScheduledLaunchFiles -TaskScript $TaskScript -ExpectedSha $ExpectedSha
    )) {
        $full = [IO.Path]::GetFullPath((Join-Path $release ($relative.Replace('/', '\'))))
        if (-not $full.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Scheduled launch manifest entry escaped the protected release root: $relative"
        }
        Assert-DawnstrikeSharedLockNoReparse $full "Scheduled launch manifest entry"
        $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Scheduled launch manifest entry is not a regular file: $relative"
        }
        $bytes = [IO.File]::ReadAllBytes($full)
        $entries += [ordered]@{
            path = $relative
            sha256 = Get-DawnstrikeLaunchSha256Bytes $bytes
            byte_count = $bytes.Length
        }
    }
    $payload = [ordered]@{
        schema_version = 'dawnstrike.scheduled_launch_manifest.v1'
        release_sha = $ExpectedSha.ToLowerInvariant()
        task_script = $TaskScript
        protected_release_root = $release
        runtime_root = $runtime
        git_path = [string]$gitContract.git_path
        git_sha256 = [string]$gitContract.git_sha256
        git_boundary_manifest_path = [string]$gitContract.git_boundary_manifest_path
        git_boundary_manifest_sha256 = [string]$gitContract.git_boundary_manifest_sha256
        python_path = [string]$pythonContract.python_path
        python_sha256 = [string]$pythonContract.python_sha256
        python_boundary_manifest_path = [string]$pythonContract.python_boundary_manifest_path
        python_boundary_manifest_sha256 = [string]$pythonContract.python_boundary_manifest_sha256
        requirements_lock_sha256 = [string]$pythonContract.requirements_lock_sha256
        requirements_lock_blob = [string]$pythonContract.requirements_lock_blob
        dependency_root = [string]$pythonContract.dependency_root
        dependency_manifest_path = [string]$pythonContract.dependency_manifest_path
        dependency_manifest_sha256 = [string]$pythonContract.dependency_manifest_sha256
        files = @($entries)
        research_only = $true
        broker_execution_enabled = $false
    }
    if ($null -ne $vercelContract) {
        foreach ($name in @(
            'vercel_cli_root',
            'vercel_cli_tree_sha256',
            'vercel_cli_entry_path',
            'vercel_cli_entry_sha256',
            'vercel_cli_boundary_manifest_path',
            'vercel_cli_boundary_manifest_sha256'
        )) { $payload[$name] = [string]$vercelContract.$name }
    }
    $json = $payload | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))
    return [pscustomobject]@{
        path = $path
        sha256 = Get-DawnstrikeLaunchSha256Bytes ([IO.File]::ReadAllBytes($path))
        files = @($entries)
    }
    }
    finally {
        if ($null -ne $stateBoundary -and $null -ne $stateBoundary.locks) {
            foreach ($lock in @($stateBoundary.locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
    }
}

function Get-DawnstrikeProtectedPythonDependencyContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseRoot,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{40}$')]
        [string]$ExpectedSha,
        [switch]$RetainLocks
    )

    $release = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')
    $expectedRelease = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSha
    if (-not [string]::Equals($release, $expectedRelease, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Python dependency contract release root is not the protected exact-SHA root.'
    }
    $pythonRoot = [IO.Path]::GetFullPath($script:DawnstrikeProtectedPythonRoot).TrimEnd('\')
    $pythonPath = Join-Path $pythonRoot 'python.exe'
    $pythonBoundaryManifest = Join-Path $pythonRoot '.dawnstrike-python-boundary-v1.json'
    $dependencyParent = [IO.Path]::GetFullPath(
        $script:DawnstrikeProtectedDependencyParent
    ).TrimEnd('\')
    $requirementsPath = Join-Path $release 'requirements.lock'
    $locks = @()
    $returnLocks = $false
    try {
        foreach ($pathContract in @(
            @($requirementsPath, 'Exact requirements.lock namespace'),
            @($pythonPath, 'Protected Python executable namespace'),
            @($pythonBoundaryManifest, 'Protected Python manifest namespace')
        )) {
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path ([string]$pathContract[0]) -Label ([string]$pathContract[1])
            $locks += $lease.handle
        }
        $requirementsStream = [IO.File]::Open(
            $requirementsPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $requirementsStream
        $requirementsBuffer = [IO.MemoryStream]::new()
        $requirementsStream.CopyTo($requirementsBuffer)
        $requirementsBytes = $requirementsBuffer.ToArray()
        $requirementsSha256 = Get-DawnstrikeLaunchSha256Bytes $requirementsBytes
        $dependencyRoot = Join-Path $dependencyParent $requirementsSha256
        $dependencyManifest = Join-Path $dependencyRoot '.dawnstrike-dependency-boundary-v1.json'
        foreach ($pathContract in @(
            @($dependencyRoot, 'Protected dependency root namespace'),
            @($dependencyManifest, 'Protected dependency manifest namespace')
        )) {
            $lease = Open-DawnstrikeStateBoundaryPath `
                -Path ([string]$pathContract[0]) -Label ([string]$pathContract[1])
            $locks += $lease.handle
        }
        $pythonManifestStream = [IO.File]::Open(
            $pythonBoundaryManifest, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $pythonManifestStream
        $pythonManifestBuffer = [IO.MemoryStream]::new()
        $pythonManifestStream.CopyTo($pythonManifestBuffer)
        $pythonManifestBytes = $pythonManifestBuffer.ToArray()
        $dependencyManifestStream = [IO.File]::Open(
            $dependencyManifest, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $locks += $dependencyManifestStream
        $dependencyManifestBuffer = [IO.MemoryStream]::new()
        $dependencyManifestStream.CopyTo($dependencyManifestBuffer)
        $dependencyManifestBytes = $dependencyManifestBuffer.ToArray()
        try {
            $pythonManifestPayload = [Text.Encoding]::UTF8.GetString(
                $pythonManifestBytes
            ) | ConvertFrom-Json
            $dependencyManifestPayload = [Text.Encoding]::UTF8.GetString(
                $dependencyManifestBytes
            ) | ConvertFrom-Json
        }
        catch { throw 'Protected Python or dependency boundary manifest is invalid JSON.' }
        $approvedPython = Get-DawnstrikeApprovedLockInterpreter
        if (
            [string]$pythonManifestPayload.schema_version -cne 'dawnstrike.python_boundary.v1' -or
            [string]$pythonManifestPayload.python_sha256 -cne [string]$approvedPython.sha256 -or
            @($pythonManifestPayload.files).Count -lt 1 -or
            $pythonManifestPayload.research_only -ne $true -or
            $pythonManifestPayload.broker_execution_enabled -ne $false
        ) { throw 'Protected Python boundary manifest contract is invalid.' }
        $requirementsBlob = Get-DawnstrikeGitBlobSha1 $requirementsPath
        if (
            [string]$dependencyManifestPayload.schema_version -cne 'dawnstrike.dependency_boundary.v1' -or
            [string]$dependencyManifestPayload.requirements_lock_blob -cne $requirementsBlob -or
            [string]$dependencyManifestPayload.requirements_lock_sha256 -cne $requirementsSha256 -or
            @($dependencyManifestPayload.files).Count -lt 1 -or
            $dependencyManifestPayload.research_only -ne $true -or
            $dependencyManifestPayload.broker_execution_enabled -ne $false
        ) { throw 'Protected dependency boundary manifest contract is invalid.' }
        Assert-DawnstrikePythonDependencyAclBoundary `
            -InterpreterPath $pythonPath `
            -AdditionalPaths @(
                $pythonBoundaryManifest,
                $dependencyParent,
                $dependencyRoot,
                $dependencyManifest
            )
        $contract = [pscustomobject]@{
            python_path = [string]$approvedPython.path
            python_sha256 = [string]$approvedPython.sha256
            python_boundary_manifest_path = $pythonBoundaryManifest
            python_boundary_manifest_sha256 = Get-DawnstrikeLaunchSha256Bytes $pythonManifestBytes
            requirements_lock_sha256 = $requirementsSha256
            requirements_lock_blob = $requirementsBlob
            dependency_root = [IO.Path]::GetFullPath($dependencyRoot).TrimEnd('\')
            dependency_manifest_path = [IO.Path]::GetFullPath($dependencyManifest)
            dependency_manifest_sha256 = Get-DawnstrikeLaunchSha256Bytes $dependencyManifestBytes
            locks = if ($RetainLocks) { @($locks) } else { @() }
        }
        if ($RetainLocks) { $returnLocks = $true }
        return $contract
    }
    finally {
        if (-not $returnLocks) {
            foreach ($lock in @($locks)) { if ($null -ne $lock) { $lock.Dispose() } }
        }
    }
}

function Assert-DawnstrikeScheduledLaunchManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
        [Parameter(Mandatory = $true)]
        [ValidateSet(
            "run_alphaops_morning.ps1",
            "run_alphaops_monitor.ps1",
            "run_alphaops_eod.ps1",
            "run_alphaops_weekly_training.ps1",
            "run_daily_finalize.ps1"
        )]
        [string]$TaskScript,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ManifestSha256,
        [string]$EntryScript = ''
    )

    $manifest = [System.IO.Path]::GetFullPath($ManifestPath)
    $runtime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $release = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSha
    $state = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    if (-not $manifest.StartsWith(([System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Scheduled launch manifest is outside the approved state root.'
    }
    Assert-DawnstrikeSharedLockNoReparse $manifest "Scheduled launch manifest"
    $stateBoundary = $null
    if ([string]::Equals($state, $script:DawnstrikeStateBoundaryFixedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        $stateBoundary = Assert-DawnstrikeStateRootBoundary -StateRoot $state
    }
    $locks = @()
    if ($null -ne $stateBoundary) { $locks += @($stateBoundary.locks) }
    try {
        $gitContract = Get-DawnstrikeProtectedGitBoundaryContract
        $pythonContract = Get-DawnstrikeProtectedPythonDependencyContract `
            -ReleaseRoot $release -ExpectedSha $ExpectedSha -RetainLocks
        $locks += @($pythonContract.locks)
        $vercelContract = if ($TaskScript -eq 'run_daily_finalize.ps1') {
            Get-DawnstrikeProtectedVercelBoundaryContract
        }
        else { $null }
        $manifestStream = [IO.File]::Open($manifest, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $locks += $manifestStream
        $manifestBytes = [IO.MemoryStream]::new()
        $manifestStream.CopyTo($manifestBytes)
        $manifestRaw = $manifestBytes.ToArray()
        if ((Get-DawnstrikeLaunchSha256Bytes $manifestRaw) -cne $ManifestSha256.ToLowerInvariant()) {
            throw 'Scheduled launch manifest hash does not match the task action binding.'
        }
        $payload = [Text.Encoding]::UTF8.GetString($manifestRaw) | ConvertFrom-Json
        if (
            [string]$payload.schema_version -cne 'dawnstrike.scheduled_launch_manifest.v1' -or
            [string]$payload.release_sha -cne $ExpectedSha.ToLowerInvariant() -or
            [string]$payload.task_script -cne $TaskScript -or
            -not [string]::Equals(
                [string]$payload.protected_release_root,
                $release,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            -not [string]::Equals(
                [string]$payload.runtime_root,
                $runtime,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            -not [string]::Equals([string]$payload.git_path, [string]$gitContract.git_path, [StringComparison]::OrdinalIgnoreCase) -or
            [string]$payload.git_sha256 -cne [string]$gitContract.git_sha256 -or
            -not [string]::Equals([string]$payload.git_boundary_manifest_path, [string]$gitContract.git_boundary_manifest_path, [StringComparison]::OrdinalIgnoreCase) -or
            [string]$payload.git_boundary_manifest_sha256 -cne [string]$gitContract.git_boundary_manifest_sha256 -or
            -not [string]::Equals([string]$payload.python_path, [string]$pythonContract.python_path, [StringComparison]::OrdinalIgnoreCase) -or
            [string]$payload.python_sha256 -cne [string]$pythonContract.python_sha256 -or
            -not [string]::Equals([string]$payload.python_boundary_manifest_path, [string]$pythonContract.python_boundary_manifest_path, [StringComparison]::OrdinalIgnoreCase) -or
            [string]$payload.python_boundary_manifest_sha256 -cne [string]$pythonContract.python_boundary_manifest_sha256 -or
            [string]$payload.requirements_lock_sha256 -cne [string]$pythonContract.requirements_lock_sha256 -or
            [string]$payload.requirements_lock_blob -cne [string]$pythonContract.requirements_lock_blob -or
            -not [string]::Equals([string]$payload.dependency_root, [string]$pythonContract.dependency_root, [StringComparison]::OrdinalIgnoreCase) -or
            -not [string]::Equals([string]$payload.dependency_manifest_path, [string]$pythonContract.dependency_manifest_path, [StringComparison]::OrdinalIgnoreCase) -or
            [string]$payload.dependency_manifest_sha256 -cne [string]$pythonContract.dependency_manifest_sha256 -or
            $payload.research_only -ne $true -or
            $payload.broker_execution_enabled -ne $false
        ) { throw 'Scheduled launch manifest safety identity is invalid.' }
        $vercelFields = @(
            'vercel_cli_root',
            'vercel_cli_tree_sha256',
            'vercel_cli_entry_path',
            'vercel_cli_entry_sha256',
            'vercel_cli_boundary_manifest_path',
            'vercel_cli_boundary_manifest_sha256'
        )
        if ($null -ne $vercelContract) {
            foreach ($name in $vercelFields) {
                $expectedValue = [string]$vercelContract.$name
                $actualValue = [string]$payload.$name
                $matches = if ($name -in @(
                    'vercel_cli_root',
                    'vercel_cli_entry_path',
                    'vercel_cli_boundary_manifest_path'
                )) {
                    [string]::Equals($actualValue, $expectedValue, [StringComparison]::OrdinalIgnoreCase)
                }
                else { $actualValue -ceq $expectedValue }
                if (-not $matches) {
                    throw "Scheduled finalizer Vercel CLI boundary differs from its launch manifest: $name"
                }
            }
        }
        elseif (@($vercelFields | Where-Object { $payload.PSObject.Properties.Name -ccontains $_ }).Count -ne 0) {
            throw 'Non-publication launch manifest contains an unexpected Vercel CLI boundary.'
        }
        $expected = @{}
        foreach ($entry in @($payload.files)) {
            $relative = [string]$entry.path
            if (
                $relative -notmatch '^(?:requirements\.lock|(?:scripts|intraday_scanner)/[A-Za-z0-9._/-]+)$' -or
                $expected.ContainsKey($relative)
            ) {
                throw 'Scheduled launch manifest contains an invalid or duplicate path.'
            }
            $full = [IO.Path]::GetFullPath((Join-Path $release ($relative.Replace('/', '\'))))
            $releasePrefix = $release.TrimEnd('\') + '\'
            if (-not $full.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Scheduled launch manifest entry escaped the protected release root.'
            }
            Assert-DawnstrikeSharedLockNoReparse $full "Scheduled launch manifest entry"
            $stream = [IO.File]::Open($full, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            $locks += $stream
            $stream.Position = 0
            $buffer = [IO.MemoryStream]::new()
            $stream.CopyTo($buffer)
            $hash = Get-DawnstrikeLaunchSha256Bytes $buffer.ToArray()
            if ($hash -cne ([string]$entry.sha256).ToLowerInvariant()) {
                throw "Scheduled launch entry bytes do not match the manifest: $relative"
            }
            $expected[$relative] = $true
        }
        $required = @(
            Get-DawnstrikeScheduledLaunchFiles -TaskScript $TaskScript -ExpectedSha $ExpectedSha
        )
        if ((@($expected.Keys | Sort-Object) -join "`n") -cne (@($required | Sort-Object) -join "`n")) {
            throw 'Scheduled launch manifest does not cover the complete trusted helper set.'
        }
        if ($EntryScript) {
            $entryPath = [IO.Path]::GetFullPath($EntryScript)
            $rootPrefix = $release.TrimEnd('\') + '\'
            if (-not $entryPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Scheduled entry script is outside the protected release root.'
            }
            $entryRelative = $entryPath.Substring($rootPrefix.Length).Replace('\', '/')
            if (-not $expected.ContainsKey($entryRelative)) { throw 'Scheduled entry script is absent from the launch manifest.' }
        }
        return [pscustomobject]@{ manifest = $payload; locks = $locks }
    }
    catch {
        foreach ($lock in $locks) { $lock.Dispose() }
        throw
    }
}

function Assert-DawnstrikePythonDependencyAclBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$InterpreterPath,
        [string[]]$AdditionalPaths = @()
    )

    $interpreter = [IO.Path]::GetFullPath($InterpreterPath)
    $expectedInterpreter = Join-Path $script:DawnstrikeProtectedPythonRoot 'python.exe'
    if (-not [string]::Equals(
            $interpreter,
            $expectedInterpreter,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Python dependency boundary is outside the administrator-owned prefix."
    }
    $prefix = [IO.Directory]::GetParent($interpreter).FullName
    $targets = @(@(
        'C:\Program Files',
        'C:\Program Files\Dawnstrike',
        $prefix,
        $interpreter,
        (Join-Path $prefix 'python3.dll'),
        (Join-Path $prefix 'python313.dll'),
        (Join-Path $prefix 'vcruntime140.dll'),
        (Join-Path $prefix 'DLLs'),
        (Join-Path $prefix 'DLLs\_hashlib.pyd'),
        (Join-Path $prefix 'Lib'),
        (Join-Path $prefix 'Lib\hashlib.py'),
        (Join-Path $prefix 'Lib\site-packages'),
        (Join-Path $prefix 'Scripts'),
        (Join-Path $prefix 'Scripts\uv.exe')
    ) + @($AdditionalPaths)) | Select-Object -Unique
    $writeLikeRights = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership -bor
        [Security.AccessControl.FileSystemRights]::FullControl
    )
    foreach ($target in $targets) {
        $item = Get-Item -LiteralPath $target -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Python dependency boundary contains a reparse point: $target"
        }
        $acl = Get-Acl -LiteralPath $target -ErrorAction Stop
        $ownerSid = Resolve-DawnstrikeProcessAclSid `
            -IdentityReference $acl.Owner -Label 'Python dependency owner'
        if (-not $script:DawnstrikeProtectedPrincipalSids.ContainsKey($ownerSid)) {
            throw "Python dependency boundary is not owned by an administrator principal: $target"
        }
        foreach ($rule in @($acl.Access)) {
            $ruleSid = Resolve-DawnstrikeProcessAclSid `
                -IdentityReference $rule.IdentityReference -Label 'Python dependency access principal'
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                -not $script:DawnstrikeProtectedPrincipalSids.ContainsKey($ruleSid) -and
                ($rule.FileSystemRights -band $writeLikeRights) -ne 0
            ) {
                throw "Python dependency boundary is writable by a non-admin principal: $target"
            }
        }
    }
}

function Get-DawnstrikeScheduledLaunchCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Runner,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [Parameter(Mandatory = $true)][string]$ManifestSha256,
        [string]$PublicationMode = '',
        [string]$VercelProjectId = ''
    )

    function Quote-Launch([string]$Value) { return "'" + $Value.Replace("'", "''") + "'" }
    $releaseRoot = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSha
    $gitContract = Get-DawnstrikeProtectedGitBoundaryContract
    $pythonContract = Get-DawnstrikeProtectedPythonDependencyContract `
        -ReleaseRoot $releaseRoot -ExpectedSha $ExpectedSha
    $runnerLeaf = [IO.Path]::GetFileName($Runner)
    $vercelContract = if ($runnerLeaf -eq 'run_daily_finalize.ps1') {
        Get-DawnstrikeProtectedVercelBoundaryContract
    }
    else { $null }
    $expectedRunner = Join-Path $releaseRoot ('scripts\' + [IO.Path]::GetFileName($Runner))
    if (-not [string]::Equals(
        [IO.Path]::GetFullPath($Runner),
        [IO.Path]::GetFullPath($expectedRunner),
        [StringComparison]::OrdinalIgnoreCase
    )) {
        throw 'Scheduled runner is outside the protected exact-SHA release root.'
    }
    $entry = Quote-Launch $Runner
    $runtime = Quote-Launch $RuntimeRoot
    $state = Quote-Launch $StateRoot
    $manifest = Quote-Launch $ManifestPath
    $runnerName = Quote-Launch $runnerLeaf
    $moduleBoundaryHelper = Join-Path $releaseRoot 'scripts\powershell_module_boundary.ps1'
    $moduleBoundaryHelperSha256 = Get-DawnstrikeLaunchSha256Bytes `
        ([IO.File]::ReadAllBytes($moduleBoundaryHelper))
    $stateBoundaryHelper = Join-Path $releaseRoot 'scripts\state_root_boundary.ps1'
    $stateBoundaryHelperSha256 = Get-DawnstrikeLaunchSha256Bytes ([IO.File]::ReadAllBytes($stateBoundaryHelper))
    $namespacePaths = @(
        $releaseRoot,
        $Runner,
        $ManifestPath,
        $moduleBoundaryHelper,
        $stateBoundaryHelper,
        [string]$gitContract.git_path,
        [string]$gitContract.git_boundary_manifest_path,
        [string]$pythonContract.python_path,
        [string]$pythonContract.python_boundary_manifest_path,
        [string]$pythonContract.dependency_root,
        [string]$pythonContract.dependency_manifest_path,
        (Join-Path $releaseRoot 'requirements.lock')
    ) | Select-Object -Unique
    if ($null -ne $vercelContract) {
        $namespacePaths = @($namespacePaths) + @(
            [string]$vercelContract.vercel_cli_root,
            [string]$vercelContract.vercel_cli_entry_path,
            [string]$vercelContract.vercel_cli_boundary_manifest_path
        ) | Select-Object -Unique
    }
    $namespacePathLiteral = '@(' + (($namespacePaths | ForEach-Object {
        Quote-Launch ([string]$_)
    }) -join ',') + ')'
    $commandPrefix = "`$global:PSModuleAutoLoadingPreference='None'; `$env:PSModulePath='C:\Windows\System32\WindowsPowerShell\v1.0\Modules'; `$bp=$(Quote-Launch $moduleBoundaryHelper); `$bs=[IO.File]::Open(`$bp,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read); `$boundaryLocks=@(`$bs); try { `$bb=[IO.MemoryStream]::new(); `$bs.CopyTo(`$bb); `$bbytes=`$bb.ToArray(); `$bx=[Security.Cryptography.SHA256]::Create(); try { `$bh=([BitConverter]::ToString(`$bx.ComputeHash(`$bbytes))).Replace('-','').ToLowerInvariant() } finally { `$bx.Dispose(); `$bs.Position=0 }; if (`$bh -cne $(Quote-Launch $moduleBoundaryHelperSha256)) { throw 'Scheduled PowerShell module boundary hash mismatch.' }; . ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString(`$bbytes))); `$ErrorActionPreference='Stop'; `$hp=$(Quote-Launch $stateBoundaryHelper); `$hi=Get-Item -LiteralPath `$hp -Force -ErrorAction Stop; if ((`$hi.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Scheduled StateRoot helper is a reparse point.' }; `$hs=[IO.File]::Open(`$hp,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read); `$boundaryLocks+=`$hs; `$hb=[IO.MemoryStream]::new(); `$hs.CopyTo(`$hb); `$hbytes=`$hb.ToArray(); `$hx=[Security.Cryptography.SHA256]::Create(); try { `$hh=([BitConverter]::ToString(`$hx.ComputeHash(`$hbytes))).Replace('-','').ToLowerInvariant() } finally { `$hx.Dispose(); `$hs.Position=0 }; if (`$hh -cne $(Quote-Launch $stateBoundaryHelperSha256)) { throw 'Scheduled StateRoot helper hash mismatch.' }; . ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString(`$hbytes))); foreach(`$np in $namespacePathLiteral){ `$nl=Open-DawnstrikeStateBoundaryPath -Path `$np -Label 'Scheduled protected namespace'; `$boundaryLocks += `$nl.handle }; `$sb=Assert-DawnstrikeStateRootBoundary -StateRoot $(Quote-Launch $StateRoot); `$boundaryLocks += @(`$sb.locks); "
    $vercelIdentityClause = ''
    if ($null -ne $vercelContract) {
        $vercelIdentityClause = " -or -not [string]::Equals([string]`$j.vercel_cli_root,$(Quote-Launch ([string]$vercelContract.vercel_cli_root)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.vercel_cli_tree_sha256 -cne $(Quote-Launch ([string]$vercelContract.vercel_cli_tree_sha256)) -or -not [string]::Equals([string]`$j.vercel_cli_entry_path,$(Quote-Launch ([string]$vercelContract.vercel_cli_entry_path)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.vercel_cli_entry_sha256 -cne $(Quote-Launch ([string]$vercelContract.vercel_cli_entry_sha256)) -or -not [string]::Equals([string]`$j.vercel_cli_boundary_manifest_path,$(Quote-Launch ([string]$vercelContract.vercel_cli_boundary_manifest_path)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.vercel_cli_boundary_manifest_sha256 -cne $(Quote-Launch ([string]$vercelContract.vercel_cli_boundary_manifest_sha256))"
    }
    $command = "`$ErrorActionPreference='Stop'; `$m=$(Quote-Launch $ManifestPath); `$expected=$(Quote-Launch $ManifestSha256.ToLowerInvariant()); `$s=[IO.File]::Open(`$m,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read); try { `$b=[IO.MemoryStream]::new(); `$s.CopyTo(`$b); `$x=[Security.Cryptography.SHA256]::Create(); try { `$actual=([BitConverter]::ToString(`$x.ComputeHash(`$b.ToArray()))).Replace('-','').ToLowerInvariant() } finally { `$x.Dispose() }; if (`$actual -cne `$expected) { throw 'Scheduled launch manifest hash mismatch.' }; `$j=[Text.Encoding]::UTF8.GetString(`$b.ToArray()) | ConvertFrom-Json; if ([string]`$j.schema_version -cne 'dawnstrike.scheduled_launch_manifest.v1' -or [string]`$j.release_sha -cne $(Quote-Launch $ExpectedSha.ToLowerInvariant()) -or -not [string]::Equals([string]`$j.protected_release_root,$(Quote-Launch $releaseRoot),[StringComparison]::OrdinalIgnoreCase) -or -not [string]::Equals([string]`$j.runtime_root,$(Quote-Launch $RuntimeRoot),[StringComparison]::OrdinalIgnoreCase) -or -not [string]::Equals([string]`$j.git_path,$(Quote-Launch ([string]$gitContract.git_path)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.git_sha256 -cne $(Quote-Launch ([string]$gitContract.git_sha256)) -or -not [string]::Equals([string]`$j.git_boundary_manifest_path,$(Quote-Launch ([string]$gitContract.git_boundary_manifest_path)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.git_boundary_manifest_sha256 -cne $(Quote-Launch ([string]$gitContract.git_boundary_manifest_sha256)) -or -not [string]::Equals([string]`$j.python_path,$(Quote-Launch ([string]$pythonContract.python_path)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.python_sha256 -cne $(Quote-Launch ([string]$pythonContract.python_sha256)) -or -not [string]::Equals([string]`$j.python_boundary_manifest_path,$(Quote-Launch ([string]$pythonContract.python_boundary_manifest_path)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.python_boundary_manifest_sha256 -cne $(Quote-Launch ([string]$pythonContract.python_boundary_manifest_sha256)) -or [string]`$j.requirements_lock_sha256 -cne $(Quote-Launch ([string]$pythonContract.requirements_lock_sha256)) -or [string]`$j.requirements_lock_blob -cne $(Quote-Launch ([string]$pythonContract.requirements_lock_blob)) -or -not [string]::Equals([string]`$j.dependency_root,$(Quote-Launch ([string]$pythonContract.dependency_root)),[StringComparison]::OrdinalIgnoreCase) -or -not [string]::Equals([string]`$j.dependency_manifest_path,$(Quote-Launch ([string]$pythonContract.dependency_manifest_path)),[StringComparison]::OrdinalIgnoreCase) -or [string]`$j.dependency_manifest_sha256 -cne $(Quote-Launch ([string]$pythonContract.dependency_manifest_sha256))$vercelIdentityClause -or [string]`$j.task_script -cne `$runnerName -or `$j.research_only -ne `$true -or `$j.broker_execution_enabled -ne `$false) { throw 'Scheduled launch manifest identity is invalid.' }; `$locks=@(`$s); foreach(`$f in @(`$j.files)) { `$p=Join-Path $(Quote-Launch $releaseRoot) ([string]`$f.path -replace '/', '\\'); `$pl=Open-DawnstrikeStateBoundaryPath -Path `$p -Label 'Scheduled launch file namespace'; `$boundaryLocks += `$pl.handle; `$h=[IO.File]::Open(`$p,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read); `$locks += `$h; `$v=[IO.MemoryStream]::new(); `$h.CopyTo(`$v); `$y=[Security.Cryptography.SHA256]::Create(); try { `$fh=([BitConverter]::ToString(`$y.ComputeHash(`$v.ToArray()))).Replace('-','').ToLowerInvariant() } finally { `$y.Dispose() }; if (`$fh -cne ([string]`$f.sha256).ToLowerInvariant()) { throw ('Scheduled launch bytes mismatch: ' + [string]`$f.path) } }; & $(Quote-Launch $Runner) -RuntimeRoot $(Quote-Launch $RuntimeRoot) -StateRoot $(Quote-Launch $StateRoot) -ExpectedSha $(Quote-Launch $ExpectedSha.ToLowerInvariant()) -LaunchManifestPath `$m -LaunchManifestSha256 `$expected"
    $command = $commandPrefix + $command
    if ($PublicationMode) { $command += " -PublicationMode $(Quote-Launch $PublicationMode)" }
    if ($VercelProjectId) { $command += " -VercelProjectId $(Quote-Launch $VercelProjectId)" }
    $command += ' } finally { foreach($h in $locks){$h.Dispose()} }'
    $command += ' } finally { foreach($h in $boundaryLocks){if($null -ne $h){$h.Dispose()}} }'
    return $command
}

function Get-DawnstrikeProcessBootstrapPreloader {
    [CmdletBinding()]
    param()
    return "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw(RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
}

function ConvertTo-DawnstrikeIsolatedPythonArguments {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$ReleaseRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSha
    )

    $source = @($ArgumentList)
    if ($source.Count -gt 0 -and [string]$source[0] -eq '-3.13') {
        $source = @($source | Select-Object -Skip 1)
    }
    $interpreterOptions = @()
    while ($source.Count -gt 0) {
        $token = [string]$source[0]
        if ($token -in @('-I', '-B', '-S')) {
            $source = @($source | Select-Object -Skip 1)
            continue
        }
        if ($token -eq '-u') {
            $interpreterOptions += $token
            $source = @($source | Select-Object -Skip 1)
            continue
        }
        if ($token -eq '-X') {
            if ($source.Count -lt 2) { throw "Scheduled Python -X option is incomplete." }
            $interpreterOptions += @($token, [string]$source[1])
            $source = @($source | Select-Object -Skip 2)
            continue
        }
        if ($token.StartsWith('-X', [System.StringComparison]::Ordinal)) {
            $interpreterOptions += $token
            $source = @($source | Select-Object -Skip 1)
            continue
        }
        break
    }
    $bootstrap = Join-Path $ReleaseRoot "scripts\dawnstrike_python_bootstrap.py"
    if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
        throw "Scheduled Python release bootstrap is missing."
    }
    $bootstrapSha256 = Get-DawnstrikeRuntimeLockHash $bootstrap
    $bootstrapLaunch = @(
        '-c', (Get-DawnstrikeProcessBootstrapPreloader), $bootstrap, $bootstrapSha256,
        '--release-root', $ReleaseRoot, '--expected-sha', $ExpectedSha
    )
    if ($source.Count -gt 0 -and [string]$source[0] -eq '-m') {
        if ($source.Count -lt 2 -or [string]::IsNullOrWhiteSpace([string]$source[1])) {
            throw "Scheduled Python module target is incomplete."
        }
        $module = [string]$source[1]
        $tail = if ($source.Count -gt 2) { @($source | Select-Object -Skip 2) } else { @() }
        return @('-I', '-B', '-S') + $interpreterOptions + $bootstrapLaunch + @(
            '--module', $module, '--'
        ) + $tail
    }
    if ($source.Count -gt 0 -and [string]$source[0] -notin @('-c', '-')) {
        $script = [string]$source[0]
        if ($script.ToLowerInvariant().EndsWith('.py')) {
            $scriptPath = if ([System.IO.Path]::IsPathRooted($script)) {
                [System.IO.Path]::GetFullPath($script)
            }
            else {
                [System.IO.Path]::GetFullPath((Join-Path $ReleaseRoot $script))
            }
            $rootPrefix = $ReleaseRoot.TrimEnd('\') + '\'
            if (-not $scriptPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Scheduled Python script is outside the exact release root."
            }
            $tail = if ($source.Count -gt 1) { @($source | Select-Object -Skip 1) } else { @() }
            return @('-I', '-B', '-S') + $interpreterOptions + $bootstrapLaunch + @(
                '--script', $scriptPath, '--'
            ) + $tail
        }
    }
    # Inline snippets are not used by governed scheduled stages, but retain
    # their semantics while still forcing -S and clearing startup mappings.
    return @('-I', '-B', '-S') + $interpreterOptions + $source
}

function Invoke-DawnstrikeNativeProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$LogRoot,
        [Parameter(Mandatory = $true)][string]$LogName,
        [Parameter()][ValidateRange(0, 86400)][int]$TimeoutSeconds = 0,
        [Parameter()][ValidateRange(1, 60)][int]$OutputDrainTimeoutSeconds = 5,
        [Parameter()][string]$WorkingDirectory = (Get-Location).Path,
        [Parameter()][hashtable]$EnvironmentOverrides = @{},
        [Parameter()][switch]$NoSite,
        [Parameter()][switch]$SuppressConsoleReplay
    )

    $startedAt = (Get-Date).ToUniversalTime()
    if ($TimeoutSeconds -eq 0) {
        # Match the child deadline to the scheduled stage while retaining one
        # native tree-kill contract for every invocation.
        $TimeoutSeconds = switch -Regex ($LogName) {
            "(?i)monitor|trade_watch|scenario" { 180; break }
            "(?i)weekly|training" { 10800; break }
            "(?i)finalize" { 10800; break }
            "(?i)eod|paperops" { 7200; break }
            "(?i)morning|universe" { 3600; break }
            default { 900 }
        }
    }
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    $safeName = $LogName -replace "[^A-Za-z0-9._-]", "_"
    $stdoutPath = Join-Path $LogRoot "$safeName.stdout.log"
    $stderrPath = Join-Path $LogRoot "$safeName.stderr.log"
    $receiptPath = Join-Path $LogRoot "$safeName.receipt.json"
    $exitCode = 127
    $startError = $null
    $timedOut = $false
    $activeJobMembersAfterCleanup = $null
    $previousErrorActionPreference = $ErrorActionPreference
    $effectiveArguments = @($ArgumentList)
    $effectiveEnvironmentOverrides = @{}
    $resolved = $null
    $resolvedExecutableSha256 = $null
    $pythonIsolated = $false
    $pythonBootstrapPath = $null
    $pythonBootstrapSha256 = $null
    $pythonDependencyContract = $null
    $pythonDependencyLocks = @()
    $gitBoundaryContract = $null

    try {
        # Windows PowerShell promotes native stderr records to PowerShell error
        # records.  With the scheduled runners' ErrorActionPreference=Stop,
        # ordinary Python logging on stderr otherwise jumps into this catch and
        # is falsely recorded as a process-start failure with exit code 127.
        # Resolve the executable while errors still terminate, then allow the
        # native process to complete and trust its real exit code.
        $requestedLeaf = [System.IO.Path]::GetFileName($FilePath).ToLowerInvariant()
        if ($requestedLeaf -in @("py.exe", "python.exe")) {
            $gitBoundaryContract = Get-DawnstrikeProtectedGitBoundaryContract
            $approved = Get-DawnstrikeApprovedLockInterpreter
            $resolved = [string]$approved.path
            $resolvedExecutableSha256 = [string]$approved.sha256
            Assert-DawnstrikePythonDependencyAclBoundary -InterpreterPath $resolved
            $sourceIdentity = Assert-DawnstrikeProcessSourceBoundToHead `
                (Join-Path $PSScriptRoot "..") `
                -ExpectedSha ([string]$script:DawnstrikeExpectedReleaseSha)
            $releaseRoot = [string]$sourceIdentity.root
            $pythonDependencyContract = Get-DawnstrikeProtectedPythonDependencyContract `
                -ReleaseRoot $releaseRoot -ExpectedSha ([string]$sourceIdentity.head) -RetainLocks
            $pythonDependencyLocks = @($pythonDependencyContract.locks)
            $effectiveArguments = ConvertTo-DawnstrikeIsolatedPythonArguments `
                -ArgumentList $effectiveArguments -ReleaseRoot $releaseRoot `
                -ExpectedSha ([string]$sourceIdentity.head)
            $pythonBootstrapPath = Join-Path $releaseRoot "scripts\dawnstrike_python_bootstrap.py"
            $pythonBootstrapSha256 = Get-DawnstrikeRuntimeLockHash $pythonBootstrapPath
            $effectiveEnvironmentOverrides = @{
                PYTHONHOME = $null
                PYTHONPATH = $null
                PYTHONSTARTUP = $null
                PYTHONDONTWRITEBYTECODE = "1"
                PYTHONNOUSERSITE = "1"
                PYTHONSAFEPATH = "1"
            }
            $pythonIsolated = $true
        }
        elseif ($requestedLeaf -in @("git", "git.exe")) {
            $gitBoundaryContract = Get-DawnstrikeProtectedGitBoundaryContract
            $resolved = [string]$gitBoundaryContract.git_path
            $resolvedExecutableSha256 = [string]$gitBoundaryContract.git_sha256
        }
        else {
            $resolved = (Get-Command $FilePath -ErrorAction Stop).Path
            Assert-DawnstrikeSharedLockNoReparse $resolved "Governed native executable"
            $resolvedExecutableSha256 = Get-DawnstrikeRuntimeLockHash $resolved
        }
        foreach ($key in $EnvironmentOverrides.Keys) {
            $effectiveEnvironmentOverrides[[string]$key] = $EnvironmentOverrides[$key]
        }
        $ErrorActionPreference = "Continue"
        # Native runner owns the complete process tree and enforces the
        # deadline.  Do not use PowerShell redirection/pipelines here: those
        # wrappers can outlive the child and obscure its real exit status.
        # The retired wrapper used ``$exitCode = if ($null -eq $LASTEXITCODE)``;
        # the Job Object result now supplies the authoritative native code.
        $result = Invoke-DawnstrikeJobProcess `
            -FilePath $resolved `
            -ArgumentList $effectiveArguments `
            -WorkingDirectory $WorkingDirectory `
            -Label $LogName `
            -TimeoutSeconds $TimeoutSeconds `
            -OutputDrainTimeoutSeconds $OutputDrainTimeoutSeconds `
            -EnvironmentOverrides $effectiveEnvironmentOverrides
        $exitCode = [int]$result.ExitCode
        $activeJobMembersAfterCleanup = [int]$result.ActiveJobMembersAfterCleanup
        [System.IO.File]::WriteAllText($stdoutPath, [string]$result.Stdout, [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::WriteAllText($stderrPath, [string]$result.Stderr, [System.Text.UTF8Encoding]::new($false))
    }
    catch {
        $startError = $_.Exception.Message
        $timedOut = $startError -match "(?i)timed out after"
        if ($timedOut) {
            # 124 is reserved for deadline termination and cannot be confused
            # with a child application's non-zero exit code.
            $exitCode = 124
            $cleanupMatch = [regex]::Match(
                $startError,
                "(?i)active_job_members_after_cleanup=(\d+)"
            )
            if ($cleanupMatch.Success) {
                $activeJobMembersAfterCleanup = [int]$cleanupMatch.Groups[1].Value
            }
        }
        [System.IO.File]::WriteAllText($stderrPath, $startError, [System.Text.UTF8Encoding]::new($false))
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        foreach ($pythonDependencyLock in @($pythonDependencyLocks)) {
            if ($null -ne $pythonDependencyLock) { $pythonDependencyLock.Dispose() }
        }
    }

    $completedAt = (Get-Date).ToUniversalTime()
    $stdoutHash = if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
        Get-DawnstrikeRuntimeLockHash $stdoutPath
    } else { $null }
    $stderrHash = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
        Get-DawnstrikeRuntimeLockHash $stderrPath
    } else { $null }
    $receipt = [ordered]@{
        schema_version = "dawnstrike.native_process_receipt.v1"
        process_name = [IO.Path]::GetFileName($FilePath)
        argument_count = @($effectiveArguments).Count
        resolved_executable_path = $resolved
        resolved_executable_sha256 = $resolvedExecutableSha256
        git_boundary_manifest_path = if ($null -ne $gitBoundaryContract) { [string]$gitBoundaryContract.git_boundary_manifest_path } else { $null }
        git_boundary_manifest_sha256 = if ($null -ne $gitBoundaryContract) { [string]$gitBoundaryContract.git_boundary_manifest_sha256 } else { $null }
        python_isolated = $pythonIsolated
        python_bootstrap_path = $pythonBootstrapPath
        python_bootstrap_sha256 = $pythonBootstrapSha256
        python_boundary_manifest_path = if ($null -ne $pythonDependencyContract) { [string]$pythonDependencyContract.python_boundary_manifest_path } else { $null }
        python_boundary_manifest_sha256 = if ($null -ne $pythonDependencyContract) { [string]$pythonDependencyContract.python_boundary_manifest_sha256 } else { $null }
        requirements_lock_sha256 = if ($null -ne $pythonDependencyContract) { [string]$pythonDependencyContract.requirements_lock_sha256 } else { $null }
        requirements_lock_blob = if ($null -ne $pythonDependencyContract) { [string]$pythonDependencyContract.requirements_lock_blob } else { $null }
        dependency_root = if ($null -ne $pythonDependencyContract) { [string]$pythonDependencyContract.dependency_root } else { $null }
        dependency_manifest_path = if ($null -ne $pythonDependencyContract) { [string]$pythonDependencyContract.dependency_manifest_path } else { $null }
        dependency_manifest_sha256 = if ($null -ne $pythonDependencyContract) { [string]$pythonDependencyContract.dependency_manifest_sha256 } else { $null }
        started_at = $startedAt.ToString("o")
        completed_at = $completedAt.ToString("o")
        duration_ms = [math]::Round(($completedAt - $startedAt).TotalMilliseconds)
        exit_code = $exitCode
        timeout_seconds = $TimeoutSeconds
        timed_out = $timedOut
        active_job_members_after_cleanup = $activeJobMembersAfterCleanup
        timeout_cleanup_confirmed = ($timedOut -and $activeJobMembersAfterCleanup -eq 0)
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
        stdout_sha256 = $stdoutHash
        stderr_sha256 = $stderrHash
        start_error = $startError
        research_only = $true
        broker_execution_enabled = $false
    }
    $receipt | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $receiptPath -Encoding UTF8

    if (-not $SuppressConsoleReplay) {
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                Get-Content -LiteralPath $path | ForEach-Object { [Console]::Out.WriteLine($_) }
            }
        }
    }
    $receipt["receipt_path"] = $receiptPath
    return [pscustomobject]$receipt
}

function Resolve-DawnstrikeReleaseSha {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$LogRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha
    )

    $runtimePath = [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $RuntimeRoot).Path
    ).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    # This function is hosted by the immutable exact-SHA code tree while the
    # RuntimeRoot remains the mutable deployed data/worktree target.  Prove the
    # executing helper against the protected tree; the Git commands below
    # independently prove the runtime checkout identity and cleanliness.
    $codeRoot = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSha
    $null = Assert-DawnstrikeProcessSourceBoundToHead `
        -ReleaseRoot $codeRoot -ExpectedSha $ExpectedSha
    $rootReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @("-C", $runtimePath, "rev-parse", "--show-toplevel") `
        -LogRoot $LogRoot `
        -LogName "resolve_release_root"
    if ($rootReceipt.exit_code -ne 0) {
        throw "Could not resolve the deployed runtime Git root."
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath(
        (Get-Content -LiteralPath $rootReceipt.stdout_path -Raw).Trim()
    ).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (-not [string]::Equals(
        $runtimePath,
        $resolvedRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Runtime root must be the exact deployed Git worktree root."
    }

    $beforeReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @("-C", $runtimePath, "rev-parse", "HEAD") `
        -LogRoot $LogRoot `
        -LogName "resolve_release_sha_before_cleanliness"
    if ($beforeReceipt.exit_code -ne 0) {
        throw "Could not resolve the deployed runtime release SHA."
    }
    $shaBefore = (Get-Content -LiteralPath $beforeReceipt.stdout_path -Raw).Trim()
    if ($shaBefore -notmatch "^[0-9a-fA-F]{40}$") {
        throw "Runtime release SHA was not a full Git commit SHA."
    }
    if ($shaBefore -cne $ExpectedSha) {
        throw "Runtime release SHA does not match the externally activated task SHA."
    }

    # A commit identity is truthful only when every executable byte in the
    # deployed worktree is represented by that commit.  Include index,
    # worktree, submodule, and non-ignored untracked paths; ignored runtime
    # state remains outside the release identity by repository policy.
    $statusReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @(
            "-C", $runtimePath,
            "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
        ) `
        -LogRoot $LogRoot `
        -LogName "resolve_release_cleanliness"
    if ($statusReceipt.exit_code -ne 0) {
        throw "Could not verify deployed runtime worktree cleanliness."
    }
    $status = Get-Content -LiteralPath $statusReceipt.stdout_path -Raw
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw "Runtime release SHA is untrustworthy because the deployed worktree is dirty."
    }

    $afterReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @("-C", $runtimePath, "rev-parse", "HEAD") `
        -LogRoot $LogRoot `
        -LogName "resolve_release_sha_after_cleanliness"
    if ($afterReceipt.exit_code -ne 0) {
        throw "Could not confirm the deployed runtime release SHA."
    }
    $shaAfter = (Get-Content -LiteralPath $afterReceipt.stdout_path -Raw).Trim()
    if (
        $shaAfter -notmatch "^[0-9a-fA-F]{40}$" -or
        $shaBefore -ne $shaAfter
    ) {
        throw "Runtime release SHA changed while verifying deployed bytes."
    }
    $script:DawnstrikeExpectedReleaseSha = $ExpectedSha.ToLowerInvariant()
    return $script:DawnstrikeExpectedReleaseSha
}
