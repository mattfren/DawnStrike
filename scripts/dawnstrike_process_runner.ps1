Set-StrictMode -Version Latest
$script:DawnstrikeExpectedReleaseSha = ""
$script:DawnstrikeScheduledSourceLocks = @()

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
    $git = (Get-DawnstrikeApprovedGit).path
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
    $git = [string](Get-DawnstrikeApprovedGit).path
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
            "scripts/verify_vercel_candidate.ps1"
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
    $state = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $safeTask = $TaskScript -replace '[^A-Za-z0-9._-]', '_'
    $root = Join-Path $state 'receipts\scheduler-launch'
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $path = Join-Path $root ($ExpectedSha.ToLowerInvariant() + '-' + $safeTask + '.json')
    $entries = @()
    $runtimePrefix = $runtime.TrimEnd('\') + '\'
    foreach ($relative in @(
        Get-DawnstrikeScheduledLaunchFiles -TaskScript $TaskScript -ExpectedSha $ExpectedSha
    )) {
        $full = [IO.Path]::GetFullPath((Join-Path $runtime ($relative.Replace('/', '\'))))
        if (-not $full.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Scheduled launch manifest entry escaped the runtime root: $relative"
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
        runtime_root = $runtime
        files = @($entries)
        research_only = $true
        broker_execution_enabled = $false
    }
    $json = $payload | ConvertTo-Json -Depth 8
    [IO.File]::WriteAllText($path, $json, [Text.UTF8Encoding]::new($false))
    return [pscustomobject]@{
        path = $path
        sha256 = Get-DawnstrikeLaunchSha256Bytes ([IO.File]::ReadAllBytes($path))
        files = @($entries)
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
    if (-not $manifest.StartsWith(([System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Scheduled launch manifest is outside the approved state root.'
    }
    Assert-DawnstrikeSharedLockNoReparse $manifest "Scheduled launch manifest"
    $locks = @()
    try {
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
            $payload.research_only -ne $true -or
            $payload.broker_execution_enabled -ne $false
        ) { throw 'Scheduled launch manifest safety identity is invalid.' }
        $expected = @{}
        foreach ($entry in @($payload.files)) {
            $relative = [string]$entry.path
            if (
                $relative -notmatch '^(?:scripts|intraday_scanner)/[A-Za-z0-9._/-]+$' -or
                $expected.ContainsKey($relative)
            ) {
                throw 'Scheduled launch manifest contains an invalid or duplicate path.'
            }
            $full = [IO.Path]::GetFullPath((Join-Path $runtime ($relative.Replace('/', '\'))))
            $runtimePrefix = $runtime.TrimEnd('\') + '\'
            if (-not $full.StartsWith($runtimePrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Scheduled launch manifest entry escaped the runtime root.'
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
            $rootPrefix = $runtime.TrimEnd('\') + '\'
            if (-not $entryPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Scheduled entry script is outside the approved runtime root.'
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
    param([Parameter(Mandatory = $true)][string]$InterpreterPath)

    $interpreter = [IO.Path]::GetFullPath($InterpreterPath)
    $expectedInterpreter = 'C:\Program Files\Dawnstrike\Python313\python.exe'
    if (-not [string]::Equals(
            $interpreter,
            $expectedInterpreter,
            [StringComparison]::OrdinalIgnoreCase
        )) {
        throw "Python dependency boundary is outside the administrator-owned prefix."
    }
    $prefix = [IO.Directory]::GetParent($interpreter).FullName
    $targets = @(
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
    ) | Select-Object -Unique
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
        if ([string]$acl.Owner -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$') {
            throw "Python dependency boundary is not owned by an administrator principal: $target"
        }
        foreach ($rule in @($acl.Access)) {
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                [string]$rule.IdentityReference -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$' -and
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
    $entry = Quote-Launch $Runner
    $runtime = Quote-Launch $RuntimeRoot
    $state = Quote-Launch $StateRoot
    $manifest = Quote-Launch $ManifestPath
    $runnerName = Quote-Launch ([IO.Path]::GetFileName($Runner))
    $command = "`$ErrorActionPreference='Stop'; `$m=$(Quote-Launch $ManifestPath); `$expected=$(Quote-Launch $ManifestSha256.ToLowerInvariant()); `$s=[IO.File]::Open(`$m,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read); try { `$b=[IO.MemoryStream]::new(); `$s.CopyTo(`$b); `$x=[Security.Cryptography.SHA256]::Create(); try { `$actual=([BitConverter]::ToString(`$x.ComputeHash(`$b.ToArray()))).Replace('-','').ToLowerInvariant() } finally { `$x.Dispose() }; if (`$actual -cne `$expected) { throw 'Scheduled launch manifest hash mismatch.' }; `$j=[Text.Encoding]::UTF8.GetString(`$b.ToArray()) | ConvertFrom-Json; if ([string]`$j.schema_version -cne 'dawnstrike.scheduled_launch_manifest.v1' -or [string]`$j.release_sha -cne $(Quote-Launch $ExpectedSha.ToLowerInvariant()) -or [string]`$j.task_script -cne `$runnerName -or `$j.research_only -ne `$true -or `$j.broker_execution_enabled -ne `$false) { throw 'Scheduled launch manifest identity is invalid.' }; `$locks=@(`$s); foreach(`$f in @(`$j.files)) { `$p=Join-Path $(Quote-Launch $RuntimeRoot) ([string]`$f.path -replace '/', '\\'); `$h=[IO.File]::Open(`$p,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read); `$locks += `$h; `$v=[IO.MemoryStream]::new(); `$h.CopyTo(`$v); `$y=[Security.Cryptography.SHA256]::Create(); try { `$fh=([BitConverter]::ToString(`$y.ComputeHash(`$v.ToArray()))).Replace('-','').ToLowerInvariant() } finally { `$y.Dispose() }; if (`$fh -cne ([string]`$f.sha256).ToLowerInvariant()) { throw ('Scheduled launch bytes mismatch: ' + [string]`$f.path) } }; & $(Quote-Launch $Runner) -RuntimeRoot $(Quote-Launch $RuntimeRoot) -StateRoot $(Quote-Launch $StateRoot) -ExpectedSha $(Quote-Launch $ExpectedSha.ToLowerInvariant()) -LaunchManifestPath `$m -LaunchManifestSha256 `$expected"
    if ($PublicationMode) { $command += " -PublicationMode $(Quote-Launch $PublicationMode)" }
    if ($VercelProjectId) { $command += " -VercelProjectId $(Quote-Launch $VercelProjectId)" }
    $command += ' } finally { foreach($h in $locks){$h.Dispose()} }'
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

    try {
        # Windows PowerShell promotes native stderr records to PowerShell error
        # records.  With the scheduled runners' ErrorActionPreference=Stop,
        # ordinary Python logging on stderr otherwise jumps into this catch and
        # is falsely recorded as a process-start failure with exit code 127.
        # Resolve the executable while errors still terminate, then allow the
        # native process to complete and trust its real exit code.
        $requestedLeaf = [System.IO.Path]::GetFileName($FilePath).ToLowerInvariant()
        if ($requestedLeaf -in @("py.exe", "python.exe")) {
            $approved = Get-DawnstrikeApprovedLockInterpreter
            $resolved = [string]$approved.path
            $resolvedExecutableSha256 = [string]$approved.sha256
            Assert-DawnstrikePythonDependencyAclBoundary -InterpreterPath $resolved
            $sourceIdentity = Assert-DawnstrikeProcessSourceBoundToHead `
                (Join-Path $PSScriptRoot "..") `
                -ExpectedSha ([string]$script:DawnstrikeExpectedReleaseSha)
            $releaseRoot = [string]$sourceIdentity.root
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
            }
            $pythonIsolated = $true
        }
        elseif ($requestedLeaf -in @("git", "git.exe")) {
            $approved = Get-DawnstrikeApprovedGit
            $resolved = [string]$approved.path
            $resolvedExecutableSha256 = [string]$approved.sha256
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
        python_isolated = $pythonIsolated
        python_bootstrap_path = $pythonBootstrapPath
        python_bootstrap_sha256 = $pythonBootstrapSha256
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
    $null = Assert-DawnstrikeProcessSourceBoundToHead -ReleaseRoot $runtimePath -ExpectedSha $ExpectedSha
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
