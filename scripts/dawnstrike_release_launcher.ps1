[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet(
        'Prepare',
        'HardenCapture',
        'Activate',
        'RebindCapture',
        'Rollback',
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
    [pscredential]$RunAsCredential,
    [switch]$PreflightOnly,
    [switch]$EnableCapture
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (
    [string]$PSVersionTable.PSEdition -cne 'Desktop' -or
    [int]$PSVersionTable.PSVersion.Major -ne 5 -or
    [int]$PSVersionTable.PSVersion.Minor -ne 1
) {
    throw 'The Dawnstrike release launcher requires Windows PowerShell 5.1 Desktop.'
}

$script:DawnstrikeReleaseLauncherPath = 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1'
$script:DawnstrikeReleaseStateBoundaryPath = 'C:\Program Files\Dawnstrike\bin\state_root_boundary.ps1'
$script:DawnstrikeReleaseGitPath = 'C:\Program Files\Git\cmd\git.exe'
$script:DawnstrikeReleaseGitSha256 = '37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9' # pragma: allowlist secret
$script:DawnstrikeReleaseGitSubject = 'CN=Johannes Schindelin, O=Johannes Schindelin, S=Nordrhein-Westfalen, C=DE'
$script:DawnstrikeReleaseGitThumbprint = '3EB14A3AEF84B7153E139397F0A49E2FAC662B0E' # pragma: allowlist secret

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
    foreach ($boundary in @(
        'C:\Program Files',
        'C:\Program Files\Dawnstrike',
        'C:\Program Files\Dawnstrike\bin',
        $full
    ) | Select-Object -Unique) {
        $acl = Get-Acl -LiteralPath $boundary -ErrorAction Stop
        if ([string]$acl.Owner -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$') {
            throw 'Trusted release launcher is not administrator-owned.'
        }
        foreach ($rule in @($acl.Access)) {
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                [string]$rule.IdentityReference -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$' -and
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
        $item = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
        if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Trusted release launcher Git metadata is unsafe.'
        }
        foreach ($name in @('config', 'config.worktree')) {
            $config = Join-Path $directory $name
            if (Test-Path -LiteralPath $config -PathType Leaf) {
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
    if ($origin -notmatch '^(?:https://github\.com/mattfren/DawnStrike(?:\.git)?|git@github\.com:mattfren/DawnStrike\.git|ssh://git@github\.com/mattfren/DawnStrike\.git)$') {
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

    $path = Assert-DawnstrikeLauncherEntryPath -Root $Root -RelativePath $RelativePath
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Trusted release entry is not a regular file.'
    }
    $stream = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $buffer = [IO.MemoryStream]::new()
        try { $stream.CopyTo($buffer) }
        finally { $stream.Position = 0 }
        $actualBlob = Get-DawnstrikeNormalizedBlobSha1 -Bytes $buffer.ToArray()
        $expectedBlob = (Invoke-DawnstrikeLauncherGit -Root $Root -Arguments @('rev-parse', ($Sha + ':' + $RelativePath.Replace('\', '/')))).ToLowerInvariant()
        if ($expectedBlob -notmatch '^[0-9a-f]{40}$' -or $actualBlob -cne $expectedBlob) {
            throw 'Trusted release entry bytes do not match the exact candidate commit.'
        }
        return [pscustomobject]@{ path = $path; stream = $stream }
    }
    catch {
        $stream.Dispose()
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
. $script:DawnstrikeReleaseStateBoundaryPath
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

$candidate = [IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
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
            'scripts\vercel_publication_journal.py'
        )
    }
}
$relativeEntries = @($modeEntries | Select-Object -Unique)
$entryLocks = @()
$taskMutationMode = $Mode -in @('HardenCapture', 'RebindCapture', 'Rollback') -or
    ($Mode -eq 'Activate' -and -not $PreflightOnly)
$stateBoundary = $null
$requestAdmission = $null
$requestInputLocks = @()
$taskMutationAlreadyCompleted = $false
$taskMutationTerminalReceipt = ''
$taskMutationTerminalJournal = ''
$modeOutput = @()
$taskMutationRequestContractSha256 = ''
try {
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
            $effectiveContractRoot = if ([string]::IsNullOrWhiteSpace($ContractRoot)) {
                $candidate
            }
            else { [IO.Path]::GetFullPath($ContractRoot).TrimEnd('\') }
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
$stateBoundary = if ($taskMutationMode) {
    Enter-DawnstrikeStateBoundaryTaskMutation `
        -StateRoot $StateRoot -Mode $Mode `
        -ExpectedSha $ExpectedSha -ExpectedTree $candidateTree `
        -RequestContractSha256 $taskMutationRequestContractSha256
}
else { Assert-DawnstrikeStateRootBoundary -StateRoot $StateRoot }
if ($null -ne $requestAdmission) {
    foreach ($requestAdmissionLock in @($requestAdmission.locks)) {
        if ($null -ne $requestAdmissionLock) { $requestAdmissionLock.Dispose() }
    }
    $requestAdmission = $null
}
if ($taskMutationMode) {
    $taskMutationAlreadyCompleted = [bool]$stateBoundary.already_completed
}
    if ($null -ne $RunAsCredential) {
        if ($requestedWriterSid -notin @($stateBoundary.writer_sids)) {
            throw 'RunAsCredential is not an exact ACL-admitted StateRoot writer SID.'
        }
    }
    foreach ($relative in $relativeEntries) {
        $entryLocks += Open-DawnstrikeLauncherEntry -Root $candidate -Sha $ExpectedSha -RelativePath $relative
    }
    if ($taskMutationAlreadyCompleted) {
        $stateBoundary | Select-Object status, operation_id, mode, receipt_sha256,
            research_only, broker_execution_enabled | ConvertTo-Json -Depth 4
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
            -RunAsCredential $RunAsCredential)
    }
    elseif ($Mode -eq 'Activate') {
        if ([string]::IsNullOrWhiteSpace($MarketDate) -or [string]::IsNullOrWhiteSpace($CiEvidencePath) -or [string]::IsNullOrWhiteSpace($SolEvidencePath)) {
            throw 'Activate mode requires MarketDate, CiEvidencePath, and SolEvidencePath.'
        }
        if (-not $PreflightOnly) {
            $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
            $principal = [Security.Principal.WindowsPrincipal]::new($identity)
            if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
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
            -PreflightOnly:$PreflightOnly `
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
            -Enable:$EnableCapture `
            -ProcessTimeoutSeconds $ProcessTimeoutSeconds)
    }
    elseif ($Mode -eq 'Rollback') {
        if ([string]::IsNullOrWhiteSpace($ActivationReceipt)) {
            throw 'Rollback mode requires ActivationReceipt.'
        }
        if ([string]::IsNullOrWhiteSpace($ContractRoot)) { $ContractRoot = $candidate }
        $modeOutput = @(& $entryLocks[0].path `
            -ActivationReceipt $ActivationReceipt `
            -ContractRoot $ContractRoot `
            -RuntimeRoot $RuntimeRoot `
            -StateRoot $StateRoot `
            -BackupRoot $BackupRoot `
            -ProcessTimeoutSeconds $ProcessTimeoutSeconds `
            -RunAsCredential $RunAsCredential)
    }
    elseif ($Mode -eq 'BootstrapUniverse') {
        if ([string]::IsNullOrWhiteSpace($MarketDate)) {
            throw 'BootstrapUniverse mode requires MarketDate.'
        }
        $mountedRuntime = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $RuntimeRoot).Path).TrimEnd('\')
        if (-not [string]::Equals($mountedRuntime, $candidate, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'BootstrapUniverse mode requires CandidateRoot to be the exact mounted runtime.'
        }
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
        if (-not [string]::Equals($mountedRuntime, $candidate, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'RecoverPublication mode requires CandidateRoot to be the exact mounted runtime.'
        }
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
        $expectedTerminal = switch ($Mode) {
            'HardenCapture' { @('dawnstrike.capture_task_hardening_receipt.v2', 'COMPLETE') }
            'RebindCapture' { @('dawnstrike.capture_task_rebind_receipt.v2', 'COMPLETE') }
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
            $receiptFolder = if ($Mode -eq 'Activate') { 'runtime-activation' } else { 'runtime-rollback' }
            $receiptStem = if ($Mode -eq 'Activate') { 'runtime-activation-' } else { 'runtime-rollback-' }
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
            -TerminalReceiptPath $taskMutationTerminalReceipt `
            -TerminalJournalPath $taskMutationTerminalJournal
        $modeOutput | Write-Output
        $taskBindingCompletion | ConvertTo-Json -Depth 5
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
    }
    if ($null -ne $stateBoundary -and $null -ne $stateBoundary.locks) {
        foreach ($stateLock in @($stateBoundary.locks)) {
            if ($null -ne $stateLock) { $stateLock.Dispose() }
        }
    }
    foreach ($requestInputLock in @($requestInputLocks)) {
        if ($null -ne $requestInputLock) { $requestInputLock.Dispose() }
    }
    if ($null -ne $requestAdmission -and $null -ne $requestAdmission.locks) {
        foreach ($requestAdmissionLock in @($requestAdmission.locks)) {
            if ($null -ne $requestAdmissionLock) { $requestAdmissionLock.Dispose() }
        }
    }
    if ($null -ne $script:DawnstrikeReleaseStateBoundarySourceLock) {
        $script:DawnstrikeReleaseStateBoundarySourceLock.Dispose()
    }
}
