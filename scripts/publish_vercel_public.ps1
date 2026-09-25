[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage",
    [string]$ProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [string]$ProjectName = "dawnstrike-command-center-x3",
    [string]$ProviderScope = "mattfrens-projects",
    [string]$ProviderTeamId = "team_b6Z9qvhxLpInBs2kGeP5Nb8X",
    [string]$ProductionAlias = "https://dawnstrike-command-center-x3.vercel.app",
    [string[]]$AdditionalProductionAliases = @(
        "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
        "https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app"
    ),
    [switch]$AllowDegraded,
    [switch]$Promote,
    [switch]$RecoveryOnly,
    [switch]$SuppressNativeConsoleReplay,
    [string]$StateRoot = "",
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$ExpectedSha = "",
    [ValidatePattern('^$|^\d{4}-\d{2}-\d{2}$')][string]$ExpectedMarketDate = "",
    [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$PrepublicationAuthorizationId = "",
    [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$DailyLedgerAuthorizationId = "",
    [string]$TestNowUtc = "",
    [ValidateSet("", "after_promote", "after_aliases", "after_production_verification", "after_result_write_before_complete")]
    [string]$TestCrashPoint = "",
    [ValidateSet("", "after_promote", "after_aliases", "after_production_verification", "result_write", "after_result_write_before_complete")]
    [string]$TestFailurePoint = "",
    [ValidateRange(1, 3600)][int]$VercelBuildTimeoutSeconds = 600,
    [ValidateRange(1, 3600)][int]$VercelCommandTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
if (($TestCrashPoint -or $TestFailurePoint) -and
    $env:DAWNSTRIKE_TEST_VERCEL_PUBLICATION -ne "1") {
    throw "Vercel publication failure and crash injection are test-only."
}
if ($Promote -and $AllowDegraded) {
    throw "Production promotion requires readiness HTTP 200; -AllowDegraded cannot be combined with -Promote."
}
if ($RecoveryOnly -and ($AllowDegraded -or $PrepublicationAuthorizationId -or $DailyLedgerAuthorizationId)) {
    throw "Recovery-only Vercel convergence cannot accept fresh publication authorization or degraded mode."
}
if ($SuppressNativeConsoleReplay -and -not $RecoveryOnly) {
    throw "Native console replay suppression is restricted to recovery-only Vercel convergence."
}
if (($Promote -or $RecoveryOnly) -and [string]::IsNullOrWhiteSpace($StateRoot)) {
    throw "Production publication and recovery require an explicit durable StateRoot."
}
$governedProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy"
$governedProjectName = "dawnstrike-command-center-x3"
$governedProviderScope = "mattfrens-projects"
$governedProviderTeamId = "team_b6Z9qvhxLpInBs2kGeP5Nb8X"
$governedProductionAlias = "https://dawnstrike-command-center-x3.vercel.app"
$governedAdditionalAliases = @(
    "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
    "https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app"
)
$pinnedLegacyDeploymentId = 'dpl_H7UQb8hWkwxLVbNwSM1BAQq1t9g8' # pragma: allowlist secret
$pinnedLegacyDeploymentUrl = 'https://dawnstrike-command-center-x3-jte5klucs-mattfrens-projects.vercel.app'
$pinnedLegacySourceSha = '5190ab6beb1b81556bfc70640c43a4cff48bd1f8' # pragma: allowlist secret
$pinnedLegacySourceTree = '5ad147c8813de9f841655f27c30b3aa59ccac1d6' # pragma: allowlist secret
$pinnedLegacyMarketDate = '2026-08-28' # pragma: allowlist secret
$pinnedLegacyBuildId = '1476e74cf607c056c5f0' # pragma: allowlist secret
$pinnedLegacyBuildSha = '1476e74cf607c056c5f070eecfff0632507145c4a37f9186eb1ad9b0ea4bf8b7' # pragma: allowlist secret
$pinnedLegacyBuildManifestSha256 = '38165e0f80c5044b8cf18b664098dd90ee3f4bac83ee4ab685ab47e6e545bdbe' # pragma: allowlist secret
$pinnedLegacyReleaseManifestSha256 = '6fd7b2e4701e9ef0f4b0c4989e0088f493bfdc1a919f6fff1bb8fea1766308e1' # pragma: allowlist secret
$pinnedLegacyArtifactMapSha256 = '335bcff3e359ced371356eeed4a9373253968650d77c6d0ecb6da723fbf53f9c' # pragma: allowlist secret
$pinnedLegacyArtifactTotalBytes = 3286836
$pinnedLegacyAttestationSha256 = '6846a6dd24bc905fee86d2b1c541140d2da3420cc5101c57c0793959b9efaa30' # pragma: allowlist secret
$pinnedLegacyReadinessReason = 'public_integrity_check_failed'
$pinnedLegacyFailedChecks = @('calendar_freshness_stale_by_clock', 'market_date_stale')
if ($ProjectId -cne $governedProjectId -or
    $ProjectName -cne $governedProjectName -or
    $ProviderScope -cne $governedProviderScope -or
    $ProviderTeamId -cne $governedProviderTeamId) {
    throw "Vercel publication target differs from the governed project/team tuple."
}
if ($Promote -or $RecoveryOnly) {
    if (
        $ProductionAlias -cne $governedProductionAlias -or
        (ConvertTo-Json @($AdditionalProductionAliases) -Compress) -cne
            (ConvertTo-Json @($governedAdditionalAliases) -Compress)) {
        throw "Production publication target differs from the governed Vercel target tuple."
    }
}

function Resolve-VercelCanonicalMarketDate {
    param([AllowEmptyString()][string]$Value)
    $candidate = $Value.Trim()
    if ([string]::IsNullOrWhiteSpace($candidate)) { return '' }
    try {
        $parsed = [DateTime]::ParseExact(
            $candidate,
            'yyyy-MM-dd',
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::None
        )
    }
    catch { throw 'ExpectedMarketDate is not a real canonical calendar date.' }
    if ($parsed.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) -cne
        $candidate) {
        throw 'ExpectedMarketDate is not a real canonical calendar date.'
    }
    return $candidate
}

$canonicalExpectedMarketDate = Resolve-VercelCanonicalMarketDate $ExpectedMarketDate

# Recovery is allowed to mutate provider aliases before a fresh daily build
# exists, so its implementation must first prove that the currently mounted
# runtime is the exact clean origin/main tree.  Keep this bootstrap independent
# of every repository helper: those helpers are trusted only after this check.
function Assert-VercelRecoveryBootstrapSource {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$AllowedStageRoot
    )
    $bootstrapGit = 'C:\Program Files\Dawnstrike\Git-2.55.0.5\cmd\git.exe'
    $bootstrapGitSha256 = '78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f'
    $bootstrapGitSubject = 'CN=Johannes Schindelin, O=Johannes Schindelin, L=Bruehl, C=DE'
    $bootstrapGitThumbprint = '2A1E97CBF0DFCDA15B0DA0AC9745014F989D4AD0'
    $cursor = [System.IO.Path]::GetFullPath($bootstrapGit)
    while ($true) {
        $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Vercel recovery bootstrap Git path contains a reparse point."
        }
        $parent = Split-Path -Parent $cursor
        if (-not $parent -or $parent -eq $cursor) { break }
        $cursor = $parent
    }
    if (-not (Test-Path -LiteralPath $bootstrapGit -PathType Leaf) -or
        (Get-FileHash -LiteralPath $bootstrapGit -Algorithm SHA256).Hash.ToLowerInvariant() -cne $bootstrapGitSha256) {
        throw "Vercel recovery bootstrap Git identity changed."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $bootstrapGit -ErrorAction Stop
    if ([string]$signature.Status -cne 'Valid' -or $null -eq $signature.SignerCertificate -or
        [string]$signature.SignerCertificate.Subject -cne $bootstrapGitSubject -or
        [string]$signature.SignerCertificate.Thumbprint -cne $bootstrapGitThumbprint) {
        throw "Vercel recovery bootstrap Git signer changed."
    }
    function Invoke-BootstrapGit {
        param([Parameter(Mandatory = $true)][string[]]$Arguments)
        $saved = @{}
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            $saved[$entry.Name] = $entry.Value
            Remove-Item -LiteralPath ("Env:" + $entry.Name) -ErrorAction Stop
        }
        try {
            $env:GIT_CONFIG_NOSYSTEM = '1'
            $env:GIT_CONFIG_GLOBAL = 'NUL'
            $safeConfiguration = @(
                '-c', 'core.fsmonitor=false',
                '-c', 'core.hooksPath=NUL',
                '-c', 'protocol.ext.allow=never',
                '-c', 'submodule.recurse=false'
            )
            $output = & $bootstrapGit @safeConfiguration -C $Root @Arguments 2>&1
            $exitCode = $LASTEXITCODE
        }
        finally {
            Remove-Item Env:GIT_CONFIG_NOSYSTEM -ErrorAction SilentlyContinue
            Remove-Item Env:GIT_CONFIG_GLOBAL -ErrorAction SilentlyContinue
            foreach ($entry in $saved.GetEnumerator()) {
                Set-Item -LiteralPath ("Env:" + $entry.Key) -Value $entry.Value
            }
        }
        if ($exitCode -ne 0) { throw "Vercel recovery bootstrap Git command failed." }
        return ((@($output) | ForEach-Object { [string]$_ }) -join "`n").Trim()
    }
    $expectedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $top = [System.IO.Path]::GetFullPath(
        (Invoke-BootstrapGit -Arguments @('rev-parse', '--show-toplevel'))
    ).TrimEnd('\')
    if (-not [string]::Equals($expectedRoot, $top, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Vercel recovery bootstrap root is not the exact Git worktree root."
    }
    $originUrl = Invoke-BootstrapGit -Arguments @('config', '--local', '--get', 'remote.origin.url')
    if ([string]$originUrl -cne 'https://github.com/mattfren/DawnStrike.git') {
        throw "Vercel recovery bootstrap origin URL is not the governed repository."
    }
    $localConfigRaw = Invoke-BootstrapGit -Arguments @(
        'config', '--local', '--no-includes', '--get-regexp', '^.*$'
    )
    $seenConfig = @{}
    $fixedConfig = @{
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
    $nonExecutingConfig = @('user.email', 'user.name')
    foreach ($record in ([string]$localConfigRaw).Split(
        @("`r`n", "`n"), [System.StringSplitOptions]::RemoveEmptyEntries
    )) {
        $parts = $record -split ' ', 2
        if ($parts.Count -ne 2) { throw "Vercel recovery local Git config is malformed." }
        $key = ([string]$parts[0]).ToLowerInvariant()
        $value = [string]$parts[1]
        if ($seenConfig.ContainsKey($key)) {
            throw "Vercel recovery local Git config contains a duplicate key."
        }
        $seenConfig[$key] = $true
        if ([string]$value -match "[`r`n]") {
            throw "Vercel recovery local Git config contains a duplicate or multiline value."
        }
        if ($fixedConfig.ContainsKey($key)) {
            if ([string]$fixedConfig[$key] -cne $value) {
                throw "Vercel recovery local Git config value is not governed: $key"
            }
            continue
        }
        if ($key -in $nonExecutingConfig -and $value.Length -ge 1 -and $value.Length -le 512) {
            continue
        }
        if ($key -match '^branch\.([a-z0-9._/-]+)\.(remote|merge)$') {
            $branchName = $Matches[1]
            $field = $Matches[2]
            $expectedValue = if ($field -eq 'remote') { 'origin' } else { "refs/heads/$branchName" }
            if ($value -cne $expectedValue) {
                throw "Vercel recovery branch Git config value is not governed: $key"
            }
            continue
        }
        throw "Vercel recovery local Git config key is not governed: $key"
    }
    $headBefore = (Invoke-BootstrapGit -Arguments @('rev-parse', 'HEAD')).ToLowerInvariant()
    $treeBefore = (Invoke-BootstrapGit -Arguments @('rev-parse', 'HEAD^{tree}')).ToLowerInvariant()
    $originBefore = (Invoke-BootstrapGit -Arguments @('rev-parse', 'refs/remotes/origin/main')).ToLowerInvariant()
    if ($headBefore -cnotmatch '^[0-9a-f]{40}$' -or $treeBefore -cnotmatch '^[0-9a-f]{40}$' -or
        $originBefore -cnotmatch '^[0-9a-f]{40}$' -or $headBefore -cne $originBefore) {
        throw "Vercel recovery bootstrap requires exact origin/main HEAD and tree identity."
    }
    $status = Invoke-BootstrapGit -Arguments @(
        'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none'
    )
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw "Vercel recovery bootstrap requires a clean runtime worktree."
    }
    $allowedPrefix = [System.IO.Path]::GetFullPath($AllowedStageRoot).TrimEnd('\') + '\'
    $ignored = Invoke-BootstrapGit -Arguments @(
        'ls-files', '--others', '--ignored', '--exclude-standard', '-z'
    )
    $forbidden = @(
        ([string]$ignored).Split([char]0, [System.StringSplitOptions]::RemoveEmptyEntries) |
            Where-Object {
                $relative = [string]$_
                $full = [System.IO.Path]::GetFullPath((Join-Path $Root $relative))
                if ($full.StartsWith($allowedPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    return $false
                }
                $name = [System.IO.Path]::GetFileName($relative).ToLowerInvariant()
                $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
                $extension -in @(
                    '.ps1', '.psm1', '.py', '.pyc', '.pyd', '.dll', '.exe',
                    '.com', '.bat', '.cmd', '.sh', '.pth'
                ) -or $name -in @('sitecustomize.py', 'usercustomize.py')
            }
    )
    if ($forbidden.Count -gt 0) {
        throw "Vercel recovery bootstrap found ignored executable/startup artifacts."
    }
    $headAfter = (Invoke-BootstrapGit -Arguments @('rev-parse', 'HEAD')).ToLowerInvariant()
    $treeAfter = (Invoke-BootstrapGit -Arguments @('rev-parse', 'HEAD^{tree}')).ToLowerInvariant()
    $originAfter = (Invoke-BootstrapGit -Arguments @('rev-parse', 'refs/remotes/origin/main')).ToLowerInvariant()
    if ($headAfter -cne $headBefore -or $treeAfter -cne $treeBefore -or $originAfter -cne $originBefore) {
        throw "Vercel recovery bootstrap source changed during verification."
    }
    return [pscustomobject]@{ head = $headAfter; tree = $treeAfter; origin = $originAfter }
}

$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$codeRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$bootstrapStage = Join-Path $resolvedRoot $StageRoot
$bootstrapSource = Assert-VercelRecoveryBootstrapSource `
    -Root $resolvedRoot -AllowedStageRoot $bootstrapStage
. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_process_runner.ps1")
$expectedCodeRoot = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSha
if (-not [string]::Equals($codeRoot, $expectedCodeRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Vercel publisher must execute from the protected exact-SHA release root."
}
$null = Assert-DawnstrikeProcessSourceBoundToHead -ReleaseRoot $codeRoot -ExpectedSha $ExpectedSha -EntryScript $PSCommandPath
. (Join-Path $PSScriptRoot "vercel_source_contract.ps1")
$approvedPython = Get-DawnstrikeApprovedLockInterpreter
$approvedGit = Get-DawnstrikeApprovedGit
$toolchainHelper = Join-Path $codeRoot "scripts\vercel_toolchain_contract.py"
$expectedNodeRoot = 'C:\Program Files\Dawnstrike\Node-24.20.0'
$expectedNodePath = Join-Path $expectedNodeRoot 'node.exe'
$expectedNodeBoundaryManifest = Join-Path $expectedNodeRoot '.dawnstrike-node-boundary-v1.json'
$expectedNodeSha256 = '5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5'
$expectedNodeLength = 93381448L
$expectedNodeSubject = 'CN=OpenJS Foundation, O=OpenJS Foundation, L=San Francisco, S=California, C=US'
$expectedNodeThumbprint = 'D1DC7755FAB17F01224CCCEA1C2FAEDC6F963E12'
$expectedVercelTreeSha256 = '3bfb7509c4bf6a8fec920566c290a385c8160b9851b2350a655f0fd8b6c9e069'
$expectedVercelRoot = 'C:\Program Files\Dawnstrike\VercelCli-' + $expectedVercelTreeSha256
$expectedVercelEntry = Join-Path $expectedVercelRoot 'node_modules\vercel\dist\vc.js'
$expectedVercelEntrySha256 = '2dd6e7c273a24bf4317af867d9b7bacb4db35487b42ea77912e2e7c33fa0c152'
$expectedVercelBoundaryManifest = Join-Path $expectedVercelRoot '.dawnstrike-vercel-cli-boundary-v1.json'

function Assert-VercelNodeIdentity {
    $null = Assert-DawnstrikeProcessProtectedPath -Path $expectedNodeRoot
    $null = Assert-DawnstrikeProcessProtectedPath -Path $expectedNodeBoundaryManifest
    $null = Assert-DawnstrikeProcessProtectedPath -Path $expectedNodePath
    Assert-DawnstrikeSharedLockNoReparse $expectedNodeRoot "Approved Vercel Node root"
    Assert-DawnstrikeSharedLockNoReparse $expectedNodeBoundaryManifest `
        "Approved Vercel Node boundary manifest"
    Assert-DawnstrikeSharedLockNoReparse $expectedNodePath "Approved Vercel Node executable"
    if (-not (Test-Path -LiteralPath $expectedNodePath -PathType Leaf) -or
        (Get-Item -LiteralPath $expectedNodePath -Force -ErrorAction Stop).Length -ne
            $expectedNodeLength -or
        (Get-DawnstrikeRuntimeLockHash $expectedNodePath) -cne $expectedNodeSha256) {
        throw "Approved Vercel Node executable identity changed."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $expectedNodePath -ErrorAction Stop
    if ([string]$signature.Status -cne 'Valid' -or $null -eq $signature.SignerCertificate -or
        [string]$signature.SignerCertificate.Subject -cne $expectedNodeSubject -or
        [string]$signature.SignerCertificate.Thumbprint -cne $expectedNodeThumbprint) {
        throw "Approved Vercel Node executable signer changed."
    }
}

function Get-VercelPublicationToolchain {
    $null = Get-DawnstrikeApprovedLockInterpreter
    $null = Get-DawnstrikeApprovedGit
    Assert-VercelNodeIdentity
    Assert-DawnstrikeSharedLockNoReparse $toolchainHelper "Vercel toolchain contract"
    $null = Assert-DawnstrikeProcessProtectedPath -Path $expectedVercelRoot
    $null = Assert-DawnstrikeProcessProtectedPath -Path $expectedVercelBoundaryManifest
    $null = Assert-DawnstrikeProcessProtectedPath -Path $expectedVercelEntry
    $environment = @{
        PYTHONHOME = ""
        PYTHONPATH = ""
        PYTHONSTARTUP = ""
        PYTHONDONTWRITEBYTECODE = "1"
    }
    $verified = Invoke-DawnstrikeJobProcess `
        -FilePath ([string]$approvedPython.path) `
        -ArgumentList @("-I", "-B", "-S", $toolchainHelper, "verify") `
        -WorkingDirectory $resolvedRoot `
        -Label "Vercel exact toolchain verification" `
        -TimeoutSeconds 120 `
        -OutputDrainTimeoutSeconds 5 `
        -EnvironmentOverrides $environment
    if ($verified.ExitCode -ne 0) { throw "Vercel exact toolchain verification failed." }
    try { $payload = ([string]$verified.Stdout) | ConvertFrom-Json }
    catch { throw "Vercel exact toolchain verification returned invalid JSON." }
    if ([string]$payload.schema_version -cne 'dawnstrike.vercel_toolchain.v1' -or
        [string]$payload.python.path -cne [string]$approvedPython.path -or
        [string]$payload.python.sha256 -cne [string]$approvedPython.sha256 -or
        [string]$payload.git.path -cne [string]$approvedGit.path -or
        [string]$payload.git.sha256 -cne [string]$approvedGit.sha256 -or
        [string]$payload.node.path -cne $expectedNodePath -or
        [string]$payload.node.sha256 -cne $expectedNodeSha256 -or
        [long]$payload.node.byte_count -ne $expectedNodeLength -or
        -not [string]::Equals(
            [string]$payload.node.root,
            $expectedNodeRoot,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            [string]$payload.node.boundary_manifest_path,
            $expectedNodeBoundaryManifest,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$payload.node.boundary_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$payload.node.version -cne '24.20.0' -or
        -not [string]::Equals(
            [string]$payload.vercel_cli.root,
            $expectedVercelRoot,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not [string]::Equals(
            [string]$payload.vercel_cli.entry_path,
            $expectedVercelEntry,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$payload.vercel_cli.entry_sha256 -cne $expectedVercelEntrySha256 -or
        [string]$payload.vercel_cli.tree_sha256 -cne $expectedVercelTreeSha256 -or
        [string]$payload.vercel_cli.version -cne '59.11.2' -or
        [int]$payload.vercel_cli.file_count -ne 7131 -or
        -not [string]::Equals(
            [string]$payload.vercel_cli.boundary_manifest_path,
            $expectedVercelBoundaryManifest,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        [string]$payload.vercel_cli.boundary_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$payload.provider_execution.mode -cne 'javascript' -or
        [string]$payload.provider_execution.global_config_policy -cne 'fresh_isolated_directory_per_provider_call' -or
        [string]$payload.provider_execution.network_trust_policy -cne 'direct_node_bundled_ca_no_proxy' -or
        $payload.provider_execution.native_binary_allowed -ne $false -or
        [string]$payload.toolchain_identity_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $payload.research_only -ne $true -or $payload.broker_execution_enabled -ne $false) {
        throw "Vercel exact toolchain contract identity is invalid."
    }
    return $payload
}

$vercelToolchain = Get-VercelPublicationToolchain
$toolchainIdentitySha256 = [string]$vercelToolchain.toolchain_identity_sha256
$nodePath = [string]$vercelToolchain.node.path
$uvPath = [string]$vercelToolchain.uv.path
$vercelEntryPath = [string]$vercelToolchain.vercel_cli.entry_path
$gitPath = [string]$approvedGit.path

function Assert-VercelPublicationToolchainStable {
    $fresh = Get-VercelPublicationToolchain
    if ([string]$fresh.toolchain_identity_sha256 -cne $toolchainIdentitySha256) {
        throw "Vercel publication toolchain changed during the operation."
    }
}

$helperBootstrap = Assert-VercelRecoveryBootstrapSource `
    -Root $resolvedRoot -AllowedStageRoot $bootstrapStage
if ($helperBootstrap.head -cne $bootstrapSource.head -or
    $helperBootstrap.tree -cne $bootstrapSource.tree -or
    $helperBootstrap.origin -cne $bootstrapSource.origin) {
    throw "Vercel publication source changed while loading governed helpers."
}
$expectedSourceSha = [string]$helperBootstrap.head
$expectedSourceTree = [string]$helperBootstrap.tree
$stage = Join-Path $resolvedRoot $StageRoot
$publicArtifactRoot = Join-Path $resolvedRoot "build\public"

function Assert-VercelPublicationSourceStable {
    $current = Assert-VercelRecoveryBootstrapSource `
        -Root $resolvedRoot -AllowedStageRoot $stage
    if ([string]$current.head -cne $expectedSourceSha -or
        [string]$current.tree -cne $expectedSourceTree -or
        [string]$current.origin -cne $expectedSourceSha) {
        throw "Vercel publication source/origin identity changed during the operation."
    }
}

Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $publicArtifactRoot -Label "Public artifact root"
$runtimeResultPath = Join-Path $resolvedRoot $(
    if ($Promote -or $RecoveryOnly) {
        "build\daily-deployment-result.json"
    }
    else {
        "build\daily-preview-deployment-result.json"
    }
)
$rollbackResultPath = Join-Path $resolvedRoot "build\daily-deployment-rollback-result.json"
$resolvedStateRoot = if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    $resolvedRoot
}
else {
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    (Resolve-Path $StateRoot).Path
}
$resolvedExpectedMarketDate = $canonicalExpectedMarketDate
if (($Promote -or $RecoveryOnly) -and [string]::IsNullOrWhiteSpace($resolvedExpectedMarketDate)) {
    throw "Direct -Promote is blocked: ExpectedMarketDate requires governed finalization authorization."
}
if ($Promote -and -not $RecoveryOnly -and [string]::IsNullOrWhiteSpace($PrepublicationAuthorizationId)) {
    throw "Direct -Promote is blocked: immutable prepublication authorization is required."
}
if ($Promote -and -not $RecoveryOnly -and [string]::IsNullOrWhiteSpace($DailyLedgerAuthorizationId)) {
    throw "Direct -Promote is blocked: daily-ledger authorization is required."
}
if ($PrepublicationAuthorizationId -and $DailyLedgerAuthorizationId -and
    $PrepublicationAuthorizationId -cne $DailyLedgerAuthorizationId) {
    throw "Prepublication and daily-ledger authorization identities must be identical."
}
$journalHistoryRoot = Join-Path $resolvedStateRoot "outputs\daily_finalize\vercel-publication"
$providerConfigRoot = Join-Path $journalHistoryRoot "provider-config"
$journalMarketKey = if ($resolvedExpectedMarketDate) { $resolvedExpectedMarketDate } else { "preview" }
$journalRoot = Join-Path $journalHistoryRoot $journalMarketKey
$journalPath = Join-Path $journalRoot "vercel-publication-operation.json"
$publicationLockPath = Join-Path $journalHistoryRoot "vercel-publication-operation.lock"
$publicationLockOwner = [guid]::NewGuid().ToString("N")
$publicationLockAcquired = $false
$journalHelper = Join-Path $codeRoot "scripts\vercel_publication_journal.py"
$resultNamespace = if ($Promote -or $RecoveryOnly) {
    "vercel-publication"
}
else {
    "vercel-preview"
}
$resultRelativePath = "outputs/daily_finalize/$resultNamespace/$journalMarketKey/daily-deployment-result.json"
$resultPath = Join-Path $resolvedStateRoot ($resultRelativePath -replace '/', '\')
$promoted = $false
$priorProduction = $null
$priorProductionAliases = @{}
$promotedDeployment = $null
$packageManifestSha256 = $null
$packageMapSha256 = $null
$remoteFileAttestationSha256 = $null
$promotedRemoteFileAttestationSha256 = $null
$deploymentCasOperationId = $null
$deploymentCasRequestSha256 = $null
$pinnedLegacyRollbackConsumed = $false
$allProductionAliases = @($ProductionAlias) + @($AdditionalProductionAliases) |
    Select-Object -Unique | Sort-Object

if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
    throw "The exact governed uv executable is unavailable."
}

function Convert-VercelJson {
    param(
        [object[]]$Output,
        [string]$Label
    )
    $text = (($Output | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if (-not $text) {
        throw "$Label returned no JSON output."
    }
    try {
        $start = $text.IndexOf("{")
        $end = $text.LastIndexOf("}")
        if ($start -lt 0 -or $end -lt $start) {
            throw "No JSON object found in CLI output."
        }
        return $text.Substring($start, $end - $start + 1) | ConvertFrom-Json
    }
    catch {
        throw "$Label returned invalid JSON: $text"
    }
}

function Invoke-VercelBoundedPublicGet {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [ValidateRange(1, 3600)][int]$TimeoutSeconds = 180,
        [ValidateRange(1, 67108864)][int]$MaximumResponseBytes = 4194304
    )

    try { $uri = [Uri]::new($Url, [UriKind]::Absolute) }
    catch { throw 'Vercel public-proof URI is invalid.' }
    $hostName = $uri.DnsSafeHost.ToLowerInvariant()
    $governedAliasHosts = @($allProductionAliases | ForEach-Object {
        ([Uri]::new([string]$_, [UriKind]::Absolute)).DnsSafeHost.ToLowerInvariant()
    })
    $deploymentHostPattern = '^' + [Regex]::Escape($governedProjectName) +
        '(?:-[a-z0-9-]+)?-' + [Regex]::Escape($governedProviderScope) +
        '\.vercel\.app$'
    if ($uri.Scheme -cne 'https' -or -not $uri.IsDefaultPort -or
        $uri.UserInfo -or $uri.Fragment -or
        ($hostName -notin $governedAliasHosts -and
            $hostName -cnotmatch $deploymentHostPattern)) {
        throw 'Vercel public-proof request escaped the governed HTTPS origins.'
    }
    $bypass = [string]$env:VERCEL_AUTOMATION_BYPASS_SECRET
    if ($bypass -and ($bypass.Length -gt 4096 -or $bypass -match '[\x00-\x20\x7f]')) {
        throw 'Vercel deployment-protection bypass is malformed.'
    }

    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    $handler = [Net.Http.HttpClientHandler]::new()
    $client = $null
    $request = $null
    $response = $null
    $responseStream = $null
    $memory = $null
    try {
        $handler.AllowAutoRedirect = $false
        $handler.UseProxy = $false
        $handler.UseCookies = $false
        $handler.UseDefaultCredentials = $false
        $handler.CheckCertificateRevocationList = $true
        if ($null -ne $handler.PSObject.Properties['SslProtocols']) {
            $handler.SslProtocols = [Security.Authentication.SslProtocols]::Tls12
        }
        $handler.MaxResponseHeadersLength = 64
        $client = [Net.Http.HttpClient]::new($handler, $false)
        $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
        $client.DefaultRequestHeaders.ExpectContinue = $false
        $client.DefaultRequestHeaders.Accept.Clear()
        $client.DefaultRequestHeaders.Accept.Add(
            [Net.Http.Headers.MediaTypeWithQualityHeaderValue]::new('application/json')
        )
        $client.DefaultRequestHeaders.UserAgent.ParseAdd('dawnstrike-vercel-proof/1')
        $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Get, $uri)
        $request.Version = [Version]::new(1, 1)
        if ($bypass -and
            -not $request.Headers.TryAddWithoutValidation(
                'x-vercel-protection-bypass', $bypass
            )) {
            throw 'Vercel deployment-protection header could not be applied.'
        }
        try {
            $response = $client.SendAsync(
                $request,
                [Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
        }
        catch { throw 'Vercel public-proof request failed before a complete response.' }
        $declaredLength = $response.Content.Headers.ContentLength
        if ($null -ne $declaredLength -and [long]$declaredLength -gt $MaximumResponseBytes) {
            throw 'Vercel public-proof response exceeds the bounded response limit.'
        }
        $responseStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $memory = [IO.MemoryStream]::new()
        [byte[]]$buffer = New-Object byte[] 65536
        while (($read = $responseStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($memory.Length + $read -gt $MaximumResponseBytes) {
                throw 'Vercel public-proof response exceeds the bounded response limit.'
            }
            $memory.Write($buffer, 0, $read)
        }
        return [pscustomobject]@{
            status_code = [int]$response.StatusCode
            body_bytes = [byte[]]$memory.ToArray()
        }
    }
    finally {
        if ($null -ne $memory) { $memory.Dispose() }
        if ($null -ne $responseStream) { $responseStream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $request) { $request.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
        if ($null -ne $handler) { $handler.Dispose() }
    }
}

function Invoke-VercelJson {
    param(
        [string[]]$Arguments,
        [string]$Label
    )
    if ($Arguments.Count -ne 2 -or $Arguments[0] -cne 'curl') {
        throw "$Label attempted a retired Vercel CLI operation."
    }
    $response = Invoke-VercelBoundedPublicGet `
        -Url ([string]$Arguments[1]) `
        -TimeoutSeconds $VercelCommandTimeoutSeconds
    if ([int]$response.status_code -lt 200 -or [int]$response.status_code -ge 300) {
        throw "$Label returned HTTP status $([int]$response.status_code)."
    }
    return ConvertFrom-VercelApiJsonBytes -Bytes $response.body_bytes -Label $Label
}

function Invoke-VercelBoundedApiRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidateSet('GET', 'POST')][string]$Method,
        [Parameter(Mandatory = $true)][string]$RelativeUri,
        [byte[]]$Body = $null,
        [string]$ContentType = '',
        [string]$DigestSha1 = '',
        [ValidateRange(1, 3600)][int]$TimeoutSeconds = 180,
        [ValidateRange(1, 134217728)][int]$MaximumResponseBytes = 4194304
    )

    $escapedTeam = [Regex]::Escape($governedProviderTeamId)
    $escapedProject = [Regex]::Escape($governedProjectId)
    $allowedApiPath = '^(?:' +
        '/v2/files\?teamId=' + $escapedTeam + '|' +
        '/v13/deployments\?teamId=' + $escapedTeam + '&prebuilt=1|' +
        '/v13/deployments\?teamId=' + $escapedTeam + '|' +
        '/v13/deployments/dpl_[A-Za-z0-9]+\?teamId=' + $escapedTeam + '|' +
        '/v7/deployments\?projectId=' + $escapedProject +
            '&target=production&limit=20&teamId=' + $escapedTeam + '|' +
        '/v4/aliases/[a-z0-9.-]+\?projectId=' + $escapedProject +
            '&teamId=' + $escapedTeam + '|' +
        '/v2/deployments/dpl_[A-Za-z0-9]+/aliases\?teamId=' + $escapedTeam + '|' +
        '/v10/projects/' + $escapedProject + '/promote/dpl_[A-Za-z0-9]+' +
            '\?teamId=' + $escapedTeam + '|' +
        '/v1/projects/' + $escapedProject + '/rollback/dpl_[A-Za-z0-9]+' +
            '\?teamId=' + $escapedTeam + '|' +
        '/v6/deployments/dpl_[A-Za-z0-9]+/files\?teamId=' + $escapedTeam + '|' +
        '/v8/deployments/dpl_[A-Za-z0-9]+/files/[A-Za-z0-9_-]+\?teamId=' +
            $escapedTeam + ')$'
    if ($RelativeUri -cnotmatch $allowedApiPath) {
        throw 'Vercel API request escaped the governed endpoint/team boundary.'
    }
    $token = [string]$env:VERCEL_TOKEN
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Length -gt 4096 -or
        $token -match '[\x00-\x20\x7f]') {
        throw 'Vercel API authentication is unavailable or malformed.'
    }
    if ($Method -eq 'GET' -and ($null -ne $Body -or $ContentType -or $DigestSha1)) {
        throw 'Vercel API GET requests cannot carry upload material.'
    }
    if ($Method -eq 'POST' -and $null -eq $Body) {
        throw 'Vercel API POST request body is missing.'
    }
    if ($DigestSha1 -and $DigestSha1 -cnotmatch '^[0-9a-f]{40}$') {
        throw 'Vercel API upload digest is invalid.'
    }
    if ($DigestSha1 -and $ContentType -cne 'application/octet-stream') {
        throw 'Vercel API upload content type is invalid.'
    }

    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    $handler = [Net.Http.HttpClientHandler]::new()
    $client = $null
    $request = $null
    $response = $null
    $responseStream = $null
    $memory = $null
    try {
        $handler.AllowAutoRedirect = $false
        $handler.UseProxy = $false
        $handler.UseCookies = $false
        $handler.UseDefaultCredentials = $false
        $handler.CheckCertificateRevocationList = $true
        if ($null -ne $handler.PSObject.Properties['SslProtocols']) {
            $handler.SslProtocols = [Security.Authentication.SslProtocols]::Tls12
        }
        $handler.MaxResponseHeadersLength = 64
        $client = [Net.Http.HttpClient]::new($handler, $false)
        $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
        $client.DefaultRequestHeaders.ExpectContinue = $false
        $client.DefaultRequestHeaders.Authorization =
            [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $token)
        $client.DefaultRequestHeaders.Accept.Clear()
        $client.DefaultRequestHeaders.Accept.Add(
            [Net.Http.Headers.MediaTypeWithQualityHeaderValue]::new('application/json')
        )
        $client.DefaultRequestHeaders.UserAgent.ParseAdd('dawnstrike-vercel-cas/1')

        $uri = [Uri]::new('https://api.vercel.com' + $RelativeUri, [UriKind]::Absolute)
        if ($uri.Scheme -cne 'https' -or $uri.Host -cne 'api.vercel.com' -or
            -not $uri.IsDefaultPort -or $uri.UserInfo -or $uri.Fragment) {
            throw 'Vercel API URI is not the exact governed HTTPS origin.'
        }
        $request = [Net.Http.HttpRequestMessage]::new(
            [Net.Http.HttpMethod]::new($Method),
            $uri
        )
        $request.Version = [Version]::new(1, 1)
        if ($Method -eq 'POST') {
            $request.Content = [Net.Http.ByteArrayContent]::new($Body)
            $request.Content.Headers.ContentType = [Net.Http.Headers.MediaTypeHeaderValue]::new(
                $ContentType
            )
            $request.Content.Headers.ContentLength = [long]$Body.LongLength
            if ($DigestSha1) {
                if (-not $request.Headers.TryAddWithoutValidation('x-vercel-digest', $DigestSha1)) {
                    throw 'Vercel API upload digest header could not be applied.'
                }
            }
        }
        try {
            $response = $client.SendAsync(
                $request,
                [Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
        }
        catch {
            throw 'Vercel API request failed before a complete response was received.'
        }
        $declaredLength = $response.Content.Headers.ContentLength
        if ($null -ne $declaredLength -and [long]$declaredLength -gt $MaximumResponseBytes) {
            throw 'Vercel API response exceeds the bounded response limit.'
        }
        $responseStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $memory = [IO.MemoryStream]::new()
        [byte[]]$buffer = New-Object byte[] 65536
        while (($read = $responseStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            if ($memory.Length + $read -gt $MaximumResponseBytes) {
                throw 'Vercel API response exceeds the bounded response limit.'
            }
            $memory.Write($buffer, 0, $read)
        }
        return [pscustomobject]@{
            status_code = [int]$response.StatusCode
            body_bytes = [byte[]]$memory.ToArray()
        }
    }
    finally {
        if ($null -ne $memory) { $memory.Dispose() }
        if ($null -ne $responseStream) { $responseStream.Dispose() }
        if ($null -ne $response) { $response.Dispose() }
        if ($null -ne $request) { $request.Dispose() }
        if ($null -ne $client) { $client.Dispose() }
        if ($null -ne $handler) { $handler.Dispose() }
    }
}

function ConvertFrom-VercelApiJsonBytes {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][byte[]]$Bytes,
        [Parameter(Mandatory = $true)][string]$Label
    )

    try {
        $strictUtf8 = [Text.UTF8Encoding]::new($false, $true)
        $raw = $strictUtf8.GetString($Bytes)
    }
    catch { throw "$Label returned non-UTF-8 JSON." }
    Assert-VercelJsonObjectKeysUnique -RawJson $raw
    try { return ($raw | ConvertFrom-Json) }
    catch { throw "$Label returned invalid JSON." }
}

function Invoke-VercelApiReadJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RelativeUri,
        [Parameter(Mandatory = $true)][string]$Label,
        [ValidateRange(1, 134217728)][int]$MaximumResponseBytes = 4194304
    )

    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $response = Invoke-VercelBoundedApiRequest `
                -Method GET -RelativeUri $RelativeUri `
                -TimeoutSeconds $VercelCommandTimeoutSeconds `
                -MaximumResponseBytes $MaximumResponseBytes
        }
        catch {
            if ($attempt -eq 3) { throw }
            Start-Sleep -Seconds $attempt
            continue
        }
        if ([int]$response.status_code -eq 200) {
            return ConvertFrom-VercelApiJsonBytes -Bytes $response.body_bytes -Label $Label
        }
        if ([int]$response.status_code -ne 429 -and
            ([int]$response.status_code -lt 500 -or [int]$response.status_code -gt 599)) {
            throw "$Label failed with HTTP status $([int]$response.status_code)."
        }
        if ($attempt -eq 3) {
            throw "$Label did not converge after bounded transient responses."
        }
        Start-Sleep -Seconds $attempt
    }
    throw "$Label did not return a response."
}

function Request-VercelProductionPromotion {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$PreviewDeploymentId)

    if ($PreviewDeploymentId -cnotmatch '^dpl_[A-Za-z0-9]{20,64}$') {
        throw 'Vercel promotion preview deployment ID is invalid.'
    }
    $body = [Text.Encoding]::UTF8.GetBytes((ConvertTo-VercelCanonicalJson ([ordered]@{
        deploymentId = $PreviewDeploymentId
        name = $governedProjectName
        target = 'production'
        meta = [ordered]@{ action = 'promote' }
    })))
    # Production creation is attempted exactly once. Recovery identifies at
    # most one server-side clone by action + originalDeploymentId before any
    # further provider mutation.
    $response = Invoke-VercelBoundedApiRequest `
        -Method POST -RelativeUri "/v13/deployments?teamId=$governedProviderTeamId" `
        -Body $body -ContentType 'application/json' `
        -TimeoutSeconds $VercelCommandTimeoutSeconds -MaximumResponseBytes 8388608
    if ([int]$response.status_code -notin @(200, 201, 202)) {
        throw "Vercel promotion request failed with HTTP status $([int]$response.status_code)."
    }
    if ($response.body_bytes.Length -eq 0) { return $null }
    return ConvertFrom-VercelApiJsonBytes `
        -Bytes $response.body_bytes -Label 'Vercel production promotion'
}

function Request-VercelProductionRollback {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$DeploymentId)

    if ($DeploymentId -cnotmatch '^dpl_[A-Za-z0-9]{20,64}$') {
        throw 'Vercel rollback deployment ID is invalid.'
    }
    $response = Invoke-VercelBoundedApiRequest `
        -Method POST `
        -RelativeUri ("/v1/projects/$governedProjectId/rollback/$DeploymentId" +
            "?teamId=$governedProviderTeamId") `
        -Body ([byte[]]@()) -ContentType 'application/json' `
        -TimeoutSeconds $VercelCommandTimeoutSeconds
    if ([int]$response.status_code -notin @(200, 201, 202, 409)) {
        throw "Vercel rollback request failed with HTTP status $([int]$response.status_code)."
    }
}

function Publish-VercelFrozenDeploymentFiles {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Boundary)

    foreach ($file in @($Boundary.unique_uploads)) {
        $uploaded = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            $null = Assert-VercelDeploymentPackageBoundaryStable -Boundary $Boundary
            $null = Assert-VercelFrozenDeploymentFileStable -File $file
            try {
                $response = Invoke-VercelBoundedApiRequest `
                    -Method POST `
                    -RelativeUri "/v2/files?teamId=$governedProviderTeamId" `
                    -Body ([byte[]]$file.data) `
                    -ContentType 'application/octet-stream' `
                    -DigestSha1 ([string]$file.sha1) `
                    -TimeoutSeconds $VercelCommandTimeoutSeconds `
                    -MaximumResponseBytes 1048576
            }
            catch {
                if ($attempt -eq 3) { throw }
                Start-Sleep -Seconds $attempt
                continue
            }
            $null = Assert-VercelFrozenDeploymentFileStable -File $file
            if ([int]$response.status_code -eq 200) {
                $uploaded = $true
                break
            }
            if ([int]$response.status_code -ne 429 -and
                ([int]$response.status_code -lt 500 -or [int]$response.status_code -gt 599)) {
                throw "Vercel CAS upload failed with HTTP status $([int]$response.status_code)."
            }
            if ($attempt -lt 3) { Start-Sleep -Seconds $attempt }
        }
        if (-not $uploaded) { throw 'Vercel CAS upload did not converge.' }
    }
    $null = Assert-VercelDeploymentPackageBoundaryStable -Boundary $Boundary
}

function New-VercelFrozenDeploymentRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Boundary,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceTree
    )

    $configPath = Join-Path $codeRoot 'vercel.json'
    Assert-DawnstrikeSharedLockNoReparse $configPath 'Protected Vercel request configuration'
    $rawConfig = Get-Content -Raw -LiteralPath $configPath -ErrorAction Stop
    Assert-VercelJsonObjectKeysUnique -RawJson $rawConfig
    try { $config = $rawConfig | ConvertFrom-Json }
    catch { throw 'Protected Vercel request configuration is invalid.' }
    $configKeys = @($config.PSObject.Properties | ForEach-Object { [string]$_.Name } | Sort-Object)
    $expectedConfigKeys = @('$schema', 'functions', 'git', 'headers', 'outputDirectory', 'version')
    if (@(Compare-Object $expectedConfigKeys $configKeys).Count -ne 0 -or
        [int]$config.version -ne 2 -or
        [string]$config.outputDirectory -cne 'build/public' -or
        $config.git.deploymentEnabled -ne $false) {
        throw 'Protected Vercel request configuration differs from the governed prebuilt contract.'
    }
    $request = [ordered]@{}
    foreach ($property in @($config.PSObject.Properties)) {
        if ([string]$property.Name -notin @('images', 'scope', 'github', 'public')) {
            $request[[string]$property.Name] = $property.Value
        }
    }
    $request['env'] = [ordered]@{}
    $request['build'] = [ordered]@{ env = [ordered]@{} }
    $request['name'] = $governedProjectName
    $request['project'] = $governedProjectId
    $request['meta'] = [ordered]@{
        dawnstrike_operation_id = $OperationId
        dawnstrike_source_sha = $SourceSha
        dawnstrike_source_tree = $SourceTree
        dawnstrike_package_map_sha256 = [string]$Boundary.map_sha256
    }
    $request['projectSettings'] = [ordered]@{
        sourceFilesOutsideRootDirectory = $true
        outputDirectory = 'build/public'
    }
    $request['source'] = 'cli'
    $request['autoAssignCustomDomains'] = $false
    $request['version'] = 2
    $request['files'] = @($Boundary.api_files)
    return $request
}

function Assert-VercelFrozenDeploymentIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Deployment,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$MapSha256,
        [switch]$RequireReady
    )

    $deploymentId = [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'id')
    $readyState = [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'readyState')
    $meta = Get-OptionalJsonProperty -InputObject $Deployment -Name 'meta'
    $team = Get-OptionalJsonProperty -InputObject $Deployment -Name 'team'
    $project = Get-OptionalJsonProperty -InputObject $Deployment -Name 'project'
    if ($deploymentId -cnotmatch '^dpl_[A-Za-z0-9]{20,64}$' -or
        $readyState -notin @('QUEUED', 'INITIALIZING', 'BUILDING', 'READY') -or
        ($RequireReady -and $readyState -cne 'READY') -or
        [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'ownerId') -cne
            $governedProviderTeamId -or
        [string](Get-OptionalJsonProperty -InputObject $team -Name 'id') -cne
            $governedProviderTeamId -or
        [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'projectId') -cne
            $governedProjectId -or
        [string](Get-OptionalJsonProperty -InputObject $project -Name 'id') -cne
            $governedProjectId -or
        [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'name') -cne
            $governedProjectName -or
        [string](Get-OptionalJsonProperty -InputObject $project -Name 'name') -cne
            $governedProjectName -or
        [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'source') -cne 'cli' -or
        [int](Get-OptionalJsonProperty -InputObject $Deployment -Name 'version') -ne 2 -or
        (Get-OptionalJsonProperty -InputObject $Deployment -Name 'prebuilt') -ne $true -or
        (Get-OptionalJsonProperty -InputObject $Deployment -Name 'autoAssignCustomDomains') -ne $false -or
        $null -ne (Get-OptionalJsonProperty -InputObject $Deployment -Name 'target') -or
        (Get-OptionalJsonProperty -InputObject $Deployment -Name 'aliasAssigned') -ne $false -or
        [string](Get-OptionalJsonProperty -InputObject $meta -Name 'dawnstrike_operation_id') -cne
            $OperationId -or
        [string](Get-OptionalJsonProperty -InputObject $meta -Name 'dawnstrike_source_sha') -cne
            $SourceSha -or
        [string](Get-OptionalJsonProperty -InputObject $meta -Name 'dawnstrike_source_tree') -cne
            $SourceTree -or
        [string](Get-OptionalJsonProperty -InputObject $meta -Name 'dawnstrike_package_map_sha256') -cne
            $MapSha256) {
        throw 'Vercel preview deployment identity differs from its frozen request.'
    }
    foreach ($field in @('alias', 'automaticAliases', 'userAliases')) {
        $aliases = Get-OptionalJsonProperty -InputObject $Deployment -Name $field
        if ($null -ne $aliases -and @($aliases).Count -ne 0) {
            throw 'Vercel preview unexpectedly received an alias.'
        }
    }
    foreach ($field in @('aliasError', 'errorCode', 'errorMessage')) {
        if ($null -ne (Get-OptionalJsonProperty -InputObject $Deployment -Name $field)) {
            throw 'Vercel preview deployment carries a provider error.'
        }
    }
    $url = [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'url')
    if ($url -cnotmatch '^dawnstrike-command-center-x3-[a-z0-9]+-mattfrens-projects\.vercel\.app$') {
        throw 'Vercel preview deployment URL is outside the governed project/team namespace.'
    }
    return $deploymentId
}

function Get-VercelFrozenDeployment {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][ValidatePattern('^dpl_[A-Za-z0-9]{20,64}$')][string]$DeploymentId)

    return Invoke-VercelApiReadJson `
        -RelativeUri "/v13/deployments/$DeploymentId`?teamId=$governedProviderTeamId" `
        -Label 'Vercel frozen preview inspection' -MaximumResponseBytes 8388608
}

function Wait-VercelFrozenDeploymentReady {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][ValidatePattern('^dpl_[A-Za-z0-9]{20,64}$')][string]$DeploymentId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceTree,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$MapSha256
    )

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($VercelCommandTimeoutSeconds)
    do {
        $deployment = Get-VercelFrozenDeployment -DeploymentId $DeploymentId
        $state = [string](Get-OptionalJsonProperty -InputObject $deployment -Name 'readyState')
        if ($state -in @('ERROR', 'CANCELED')) {
            throw 'Vercel frozen preview entered a terminal failure state.'
        }
        $null = Assert-VercelFrozenDeploymentIdentity `
            -Deployment $deployment -OperationId $OperationId `
            -SourceSha $SourceSha -SourceTree $SourceTree -MapSha256 $MapSha256 `
            -RequireReady:($state -eq 'READY')
        if ($state -eq 'READY') { return $deployment }
        if ([DateTimeOffset]::UtcNow -ge $deadline) { break }
        Start-Sleep -Seconds 2
    } while ($true)
    throw 'Vercel frozen preview did not reach READY within the bounded timeout.'
}

function Add-VercelRemoteDeploymentTreeEntries {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object[]]$Nodes,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Prefix,
        [Parameter(Mandatory = $true)]$Directories,
        [Parameter(Mandatory = $true)]$Files,
        [Parameter(Mandatory = $true)]$CaseFoldedPaths,
        [ValidateRange(0, 64)][int]$Depth = 0
    )

    if ($Depth -ge 64) { throw 'Vercel deployment file tree exceeds the depth limit.' }
    foreach ($node in @($Nodes)) {
        $properties = @($node.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if (-not $properties.Count -or
            @($properties | Where-Object {
                $_ -notin @('name', 'type', 'uid', 'children', 'contentType', 'mode')
            }).Count -ne 0) {
            throw 'Vercel deployment file tree contains an unexpected field.'
        }
        $name = [string](Get-OptionalJsonProperty -InputObject $node -Name 'name')
        $type = [string](Get-OptionalJsonProperty -InputObject $node -Name 'type')
        $modeValue = Get-OptionalJsonProperty -InputObject $node -Name 'mode'
        if (-not $name -or $name.Length -gt 255 -or $name -in @('.', '..') -or
            $name -match '[\\/\x00-\x1f]' -or $null -eq $modeValue -or
            [double]$modeValue -ne [Math]::Truncate([double]$modeValue) -or
            [int64]$modeValue -lt 0 -or [int64]$modeValue -gt [int]::MaxValue) {
            throw 'Vercel deployment file tree contains an invalid entry.'
        }
        $path = if ($Prefix) { "$Prefix/$name" } else { $name }
        if ($path.Length -gt 1024 -or -not $CaseFoldedPaths.Add($path)) {
            throw 'Vercel deployment file tree contains a duplicate or case-colliding path.'
        }
        if ($type -ceq 'directory') {
            $children = Get-OptionalJsonProperty -InputObject $node -Name 'children'
            if ($null -eq $children -or
                $null -ne (Get-OptionalJsonProperty -InputObject $node -Name 'uid')) {
                throw 'Vercel deployment directory entry is invalid.'
            }
            $Directories.Add([pscustomobject]@{
                relative_path = $path
                mode = [int]$modeValue
            }) | Out-Null
            Add-VercelRemoteDeploymentTreeEntries `
                -Nodes @($children) -Prefix $path `
                -Directories $Directories -Files $Files `
                -CaseFoldedPaths $CaseFoldedPaths -Depth ($Depth + 1)
            continue
        }
        if ($type -cne 'file') {
            throw 'Vercel deployment file tree contains a non-file primitive.'
        }
        $uid = [string](Get-OptionalJsonProperty -InputObject $node -Name 'uid')
        $children = Get-OptionalJsonProperty -InputObject $node -Name 'children'
        if ($uid -cnotmatch '^[A-Za-z0-9_-]{1,128}$' -or
            ($null -ne $children -and @($children).Count -ne 0)) {
            throw 'Vercel deployment file entry has invalid retrieval identity.'
        }
        $Files.Add([pscustomobject]@{
            relative_path = $path
            mode = [int]$modeValue
            uid = $uid
        }) | Out-Null
        if ($Files.Count -gt 10000) {
            throw 'Vercel deployment file tree exceeds the file-count limit.'
        }
    }
}

function Get-VercelRemoteDeploymentFileAttestation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Deployment,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedMapSha256,
        [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$ExpectedAttestationSha256 = '',
        [AllowNull()]$Boundary = $null
    )

    $deploymentId = [string](Get-OptionalJsonProperty -InputObject $Deployment -Name 'id')
    if ($deploymentId -cnotmatch '^dpl_[A-Za-z0-9]{20,64}$') {
        throw 'Vercel deployment file attestation received an invalid deployment ID.'
    }
    $tree = Invoke-VercelApiReadJson `
        -RelativeUri "/v6/deployments/$deploymentId/files`?teamId=$governedProviderTeamId" `
        -Label 'Vercel deployment file tree' -MaximumResponseBytes 16777216
    if ($null -eq $tree) { throw 'Vercel deployment file tree is empty.' }

    $directories = [Collections.Generic.List[object]]::new()
    $files = [Collections.Generic.List[object]]::new()
    $caseFolded = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    Add-VercelRemoteDeploymentTreeEntries `
        -Nodes @($tree) -Prefix '' -Directories $directories -Files $files `
        -CaseFoldedPaths $caseFolded
    if ($files.Count -lt 1) { throw 'Vercel deployment file tree contains no files.' }

    $expectedByPath = $null
    if ($null -ne $Boundary) {
        $expectedByPath = [Collections.Generic.Dictionary[string, object]]::new(
            [StringComparer]::Ordinal
        )
        foreach ($file in @($Boundary.files)) {
            $expectedByPath.Add([string]$file.relative_path, $file)
        }
        $expectedDirectories = @(
            $Boundary.provider_directories | ForEach-Object { [string]$_ }
        )
        $remoteDirectories = @($directories | ForEach-Object { [string]$_.relative_path })
        if (@(Compare-Object `
                -ReferenceObject ($expectedDirectories | Sort-Object -CaseSensitive) `
                -DifferenceObject ($remoteDirectories | Sort-Object -CaseSensitive) `
                -CaseSensitive).Count -ne 0 -or $files.Count -ne $expectedByPath.Count) {
            throw 'Vercel deployment file tree differs from the frozen package namespace.'
        }
    }

    $contentByUid = [Collections.Generic.Dictionary[string, object]]::new(
        [StringComparer]::Ordinal
    )
    $attestedFiles = [Collections.Generic.List[object]]::new()
    [long]$totalBytes = 0
    foreach ($remoteFile in @($files | Sort-Object relative_path -CaseSensitive)) {
        $expected = $null
        if ($null -ne $expectedByPath) {
            if (-not $expectedByPath.TryGetValue([string]$remoteFile.relative_path, [ref]$expected)) {
                throw 'Vercel deployment file path differs from the frozen package map.'
            }
            if ([int]$remoteFile.mode -ne [int]$expected.mode) {
                throw 'Vercel deployment file mode differs from the frozen package map.'
            }
        }
        $content = $null
        if (-not $contentByUid.TryGetValue([string]$remoteFile.uid, [ref]$content)) {
            $payload = Invoke-VercelApiReadJson `
                -RelativeUri ("/v8/deployments/$deploymentId/files/" +
                    [string]$remoteFile.uid + "?teamId=$governedProviderTeamId") `
                -Label 'Vercel deployment file contents' `
                -MaximumResponseBytes 100663296
            $payloadKeys = @($payload.PSObject.Properties | ForEach-Object { [string]$_.Name })
            $encoded = [string](Get-OptionalJsonProperty -InputObject $payload -Name 'data')
            if ($payloadKeys.Count -ne 1 -or $payloadKeys[0] -cne 'data' -or
                -not $encoded -or $encoded.Length -gt 89478488 -or
                $encoded -cnotmatch '^[A-Za-z0-9+/]*={0,2}$' -or $encoded.Length % 4 -ne 0) {
                throw 'Vercel deployment file contents are not canonical bounded base64.'
            }
            try { [byte[]]$contentBytes = [Convert]::FromBase64String($encoded) }
            catch { throw 'Vercel deployment file contents are not valid base64.' }
            if ([Convert]::ToBase64String($contentBytes) -cne $encoded -or
                $contentBytes.LongLength -gt 67108864) {
                throw 'Vercel deployment file contents exceed or violate the canonical byte boundary.'
            }
            $content = [pscustomobject]@{
                bytes = $contentBytes
                length = [long]$contentBytes.LongLength
                sha1 = Get-VercelDeploymentBytesHash -Bytes $contentBytes -Algorithm SHA1
                sha256 = Get-VercelDeploymentBytesHash -Bytes $contentBytes -Algorithm SHA256
            }
            $contentByUid.Add([string]$remoteFile.uid, $content)
        }
        if ($null -ne $expected -and (
            [long]$content.length -ne [long]$expected.length -or
            [string]$content.sha1 -cne [string]$expected.sha1 -or
            [string]$content.sha256 -cne [string]$expected.sha256
        )) {
            throw 'Vercel deployment file bytes differ from the frozen package map.'
        }
        $totalBytes += [long]$content.length
        if ($totalBytes -gt 536870912) {
            throw 'Vercel deployment file attestation exceeds the aggregate byte limit.'
        }
        $attestedFiles.Add([pscustomobject]@{
            relative_path = [string]$remoteFile.relative_path
            mode = [int]$remoteFile.mode
            uid = [string]$remoteFile.uid
            length = [long]$content.length
            sha1 = [string]$content.sha1
            sha256 = [string]$content.sha256
        }) | Out-Null
    }
    $mapHash = Get-VercelDeploymentFrozenMapHash -Files @($attestedFiles)
    if ($mapHash -cne $ExpectedMapSha256) {
        throw 'Vercel deployment source-byte map differs from the frozen package map.'
    }
    $attestationLines = @(
        $directories | Sort-Object relative_path -CaseSensitive | ForEach-Object {
            'D|' + [string]$_.relative_path + '|' + [string][int]$_.mode
        }
        $attestedFiles | Sort-Object relative_path -CaseSensitive | ForEach-Object {
            'F|' + [string]$_.relative_path + '|' + [string][int]$_.mode + '|' +
                [string]$_.uid + '|' + [string][long]$_.length + '|' +
                [string]$_.sha1 + '|' + [string]$_.sha256
        }
    )
    $attestationHash = Get-Sha256Hex ($attestationLines -join "`n")
    if ($ExpectedAttestationSha256 -and $attestationHash -cne $ExpectedAttestationSha256) {
        throw 'Vercel deployment source-file attestation changed after publication.'
    }
    return [pscustomobject]@{
        deployment_id = $deploymentId
        package_map_sha256 = $mapHash
        remote_file_attestation_sha256 = $attestationHash
        file_count = $attestedFiles.Count
        total_bytes = $totalBytes
    }
}

function New-VercelFrozenPreviewDeployment {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Boundary,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceSha,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceTree
    )

    Publish-VercelFrozenDeploymentFiles -Boundary $Boundary
    $operationId = [guid]::NewGuid().ToString('N')
    $requestPayload = New-VercelFrozenDeploymentRequest `
        -Boundary $Boundary -OperationId $operationId `
        -SourceSha $SourceSha -SourceTree $SourceTree
    $requestJson = $requestPayload | ConvertTo-Json -Depth 60 -Compress
    [byte[]]$requestBytes = [Text.UTF8Encoding]::new($false).GetBytes($requestJson)
    if ($requestBytes.LongLength -gt 8388608) {
        throw 'Vercel frozen deployment request exceeds the bounded request limit.'
    }
    $null = Assert-VercelDeploymentPackageBoundaryStable -Boundary $Boundary
    # Creation is intentionally attempted exactly once. If the response is
    # lost, the unaliased preview is an orphan and this invocation fails closed.
    $createResponse = Invoke-VercelBoundedApiRequest `
        -Method POST `
        -RelativeUri "/v13/deployments?teamId=$governedProviderTeamId&prebuilt=1" `
        -Body $requestBytes -ContentType 'application/json' `
        -TimeoutSeconds $VercelCommandTimeoutSeconds `
        -MaximumResponseBytes 8388608
    if ([int]$createResponse.status_code -ne 200) {
        throw "Vercel frozen preview creation failed with HTTP status $([int]$createResponse.status_code)."
    }
    $created = ConvertFrom-VercelApiJsonBytes `
        -Bytes $createResponse.body_bytes -Label 'Vercel frozen preview creation'
    $deploymentId = [string](Get-OptionalJsonProperty -InputObject $created -Name 'id')
    if ($deploymentId -cnotmatch '^dpl_[A-Za-z0-9]{20,64}$' -or
        $null -ne (Get-OptionalJsonProperty -InputObject $created -Name 'target') -or
        (Get-OptionalJsonProperty -InputObject $created -Name 'aliasAssigned') -ne $false) {
        throw 'Vercel frozen preview creation returned an invalid or aliased identity.'
    }
    $deployment = Wait-VercelFrozenDeploymentReady `
        -DeploymentId $deploymentId -OperationId $operationId `
        -SourceSha $SourceSha -SourceTree $SourceTree `
        -MapSha256 ([string]$Boundary.map_sha256)
    $attestation = Get-VercelRemoteDeploymentFileAttestation `
        -Deployment $deployment -ExpectedMapSha256 ([string]$Boundary.map_sha256) `
        -Boundary $Boundary
    $finalDeployment = Get-VercelFrozenDeployment -DeploymentId $deploymentId
    $null = Assert-VercelFrozenDeploymentIdentity `
        -Deployment $finalDeployment -OperationId $operationId `
        -SourceSha $SourceSha -SourceTree $SourceTree `
        -MapSha256 ([string]$Boundary.map_sha256) -RequireReady
    $null = Assert-VercelDeploymentPackageBoundaryStable -Boundary $Boundary
    return [pscustomobject]@{
        deployment = $finalDeployment
        operation_id = $operationId
        request_sha256 = Get-Sha256Hex $requestJson
        package_map_sha256 = [string]$Boundary.map_sha256
        remote_file_attestation_sha256 = [string]$attestation.remote_file_attestation_sha256
        file_count = [int]$attestation.file_count
        total_bytes = [long]$attestation.total_bytes
    }
}

function Assert-VercelJournalFrozenPreviewEvidence {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Journal)

    if ([string]$Journal.schema_version -notin @(
        'dawnstrike.vercel_publication_journal.v7',
        'dawnstrike.vercel_publication_journal.v8'
    )) {
        throw 'Recoverable Vercel publication journal lacks frozen CAS evidence.'
    }
    $deploymentId = [string]$Journal.candidate_preview_deployment_id
    $deployment = Get-VercelFrozenDeployment -DeploymentId $deploymentId
    if ([string](Get-OptionalJsonProperty -InputObject $deployment -Name 'id') -cne $deploymentId -or
        (Normalize-VercelDeploymentUrl (
            Get-OptionalJsonProperty -InputObject $deployment -Name 'url'
        )) -cne (Normalize-VercelDeploymentUrl $Journal.candidate_preview_url)) {
        throw 'Recovered Vercel preview identity differs from the sealed journal.'
    }
    $null = Assert-VercelFrozenDeploymentIdentity `
        -Deployment $deployment `
        -OperationId ([string]$Journal.candidate_deployment_operation_id) `
        -SourceSha ([string]$Journal.candidate_source_sha) `
        -SourceTree ([string]$Journal.candidate_source_tree) `
        -MapSha256 ([string]$Journal.candidate_package_map_sha256) `
        -RequireReady
    $null = Get-VercelRemoteDeploymentFileAttestation `
        -Deployment $deployment `
        -ExpectedMapSha256 ([string]$Journal.candidate_package_map_sha256) `
        -ExpectedAttestationSha256 `
            ([string]$Journal.candidate_remote_file_attestation_sha256)
    return $deployment
}

function Assert-VercelPromotedDeploymentFileEvidence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^dpl_[A-Za-z0-9]{20,64}$')][string]$DeploymentId,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^dpl_[A-Za-z0-9]{20,64}$')][string]$PreviewDeploymentId,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[0-9a-f]{64}$')][string]$PackageMapSha256,
        [ValidatePattern('^$|^[0-9a-f]{64}$')]
        [string]$ExpectedAttestationSha256 = ''
    )

    $deployment = Get-VercelFrozenDeployment -DeploymentId $DeploymentId
    $metadata = Get-OptionalJsonProperty -InputObject $deployment -Name 'meta'
    if ([string](Get-OptionalJsonProperty -InputObject $deployment -Name 'id') -cne
            $DeploymentId -or
        [string](Get-OptionalJsonProperty -InputObject $deployment -Name 'ownerId') -cne
            $governedProviderTeamId -or
        [string](Get-OptionalJsonProperty -InputObject $deployment -Name 'projectId') -cne
            $governedProjectId -or
        [string](Get-OptionalJsonProperty -InputObject $deployment -Name 'target') -cne
            'production' -or
        [string](Get-OptionalJsonProperty -InputObject $deployment -Name 'readyState') -cne
            'READY' -or
        [string](Get-OptionalJsonProperty -InputObject $metadata -Name 'action') -cne
            'promote' -or
        [string](Get-OptionalJsonProperty -InputObject $metadata -Name 'originalDeploymentId') -cne
            $PreviewDeploymentId) {
        throw 'Promoted Vercel deployment identity is not the exact governed preview clone.'
    }
    foreach ($field in @('aliasError', 'errorCode', 'errorMessage')) {
        if ($null -ne (Get-OptionalJsonProperty -InputObject $deployment -Name $field)) {
            throw 'Promoted Vercel deployment carries a provider error.'
        }
    }
    $url = Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl ([string](
        Get-OptionalJsonProperty -InputObject $deployment -Name 'url'
    ))
    $attestation = Get-VercelRemoteDeploymentFileAttestation `
        -Deployment $deployment -ExpectedMapSha256 $PackageMapSha256 `
        -ExpectedAttestationSha256 $ExpectedAttestationSha256
    return [pscustomobject]@{
        deployment = $deployment
        deployment_id = $DeploymentId
        deployment_url = $url
        remote_file_attestation_sha256 = `
            [string]$attestation.remote_file_attestation_sha256
    }
}

function Assert-RemoteVercelSourceManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $response = Invoke-VercelBoundedPublicGet `
        -Url $Url -TimeoutSeconds $VercelCommandTimeoutSeconds
    if ([int]$response.status_code -ne 200) {
        throw "$Label source manifest returned HTTP status $([int]$response.status_code)."
    }
    try {
        $remote = [Text.UTF8Encoding]::new($false, $true).GetString($response.body_bytes)
    }
    catch { throw "$Label source manifest returned non-UTF-8 bytes." }
    $expectedCanonical = Get-VercelSourceManifestCanonicalJson `
        -Path (Join-Path $stage "vercel-source-manifest.json")
    Assert-VercelSourceManifestJson `
        -RawJson $remote `
        -ExpectedCanonicalJson $expectedCanonical `
        -Label $Label
}

function Test-VercelObjectProperty {
    param(
        [AllowNull()][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $InputObject) {
        return $false
    }
    if ($InputObject -is [System.Collections.IDictionary]) {
        return $InputObject.Contains($Name)
    }
    return $null -ne $InputObject.PSObject.Properties[$Name]
}

function Get-OptionalJsonProperty {
    param(
        [AllowNull()][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $InputObject) {
        return $null
    }
    if (-not (Test-VercelObjectProperty -InputObject $InputObject -Name $Name)) {
        return $null
    }
    if ($InputObject -is [System.Collections.IDictionary]) {
        return $InputObject[$Name]
    }
    return $InputObject.PSObject.Properties[$Name].Value
}

function Set-VercelAlias {
    param(
        [Parameter(Mandatory = $true)][string]$DeploymentId,
        [Parameter(Mandatory = $true)][string]$DeploymentUrl,
        [Parameter(Mandatory = $true)][string]$AliasUrl,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($DeploymentId -cnotmatch '^dpl_[A-Za-z0-9]{20,64}$' -or
        $AliasUrl -notin $allProductionAliases) {
        throw "$Label escaped the governed deployment/alias boundary."
    }
    $expectedDeployment = Get-VercelFrozenDeployment -DeploymentId $DeploymentId
    $expectedUrl = Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl ([string](
        Get-OptionalJsonProperty -InputObject $expectedDeployment -Name 'url'
    ))
    if ($expectedUrl -cne (Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl $DeploymentUrl) -or
        [string](Get-OptionalJsonProperty -InputObject $expectedDeployment -Name 'ownerId') -cne
            $governedProviderTeamId -or
        [string](Get-OptionalJsonProperty -InputObject $expectedDeployment -Name 'projectId') -cne
            $governedProjectId -or
        [string](Get-OptionalJsonProperty -InputObject $expectedDeployment -Name 'readyState') -cne
            'READY') {
        throw "$Label target deployment is not the exact governed READY deployment."
    }
    $aliasHost = ([Uri]::new($AliasUrl, [UriKind]::Absolute)).DnsSafeHost.ToLowerInvariant()
    $bodyJson = ConvertTo-VercelCanonicalJson ([ordered]@{ alias = $aliasHost })
    $body = [Text.Encoding]::UTF8.GetBytes($bodyJson)
    $response = Invoke-VercelBoundedApiRequest `
        -Method POST `
        -RelativeUri "/v2/deployments/$DeploymentId/aliases`?teamId=$governedProviderTeamId" `
        -Body $body -ContentType 'application/json' `
        -TimeoutSeconds $VercelCommandTimeoutSeconds
    if ([int]$response.status_code -notin @(200, 409)) {
        throw "$Label failed with HTTP status $([int]$response.status_code)."
    }
    $observed = Get-VercelAliasObservation -Alias $AliasUrl
    if ([string]$observed.id -cne $DeploymentId -or
        (Normalize-VercelDeploymentUrl $observed.url) -cne
            (Normalize-VercelDeploymentUrl $expectedUrl)) {
        throw "$Label did not converge to the exact governed deployment."
    }
}

function Normalize-VercelDeploymentUrl {
    param([AllowNull()][object]$Value)
    return ([string]$Value -replace "^https?://", "").TrimEnd("/").ToLowerInvariant()
}

function Get-VercelImmutableDeploymentBaseUrl {
    param([Parameter(Mandatory = $true)][string]$DeploymentUrl)
    $deploymentHostName = Normalize-VercelDeploymentUrl $DeploymentUrl
    if (-not $deploymentHostName -or $deploymentHostName -notmatch '^[a-z0-9.-]+$') {
        throw "Vercel deployment URL is invalid."
    }
    return "https://$deploymentHostName"
}

function Assert-VercelPriorAliasSnapshotsCurrent {
    foreach ($alias in $allProductionAliases) {
        $prior = $priorProductionAliases[[string]$alias]
        if ($null -eq $prior -or -not $prior.id -or -not $prior.url) {
            throw "Prior production snapshot is incomplete for $alias."
        }
        $current = Get-VercelAliasObservation -Alias ([string]$alias)
        $currentId = [string](Get-OptionalJsonProperty -InputObject $current -Name "id")
        $currentUrl = [string](Get-OptionalJsonProperty -InputObject $current -Name "url")
        if ($currentId -cne [string]$prior.id -or
            (Normalize-VercelDeploymentUrl $currentUrl) -cne
                (Normalize-VercelDeploymentUrl ([string]$prior.url))) {
            throw "Prior production alias changed while its immutable snapshot was captured: $alias"
        }
    }
}

function Assert-VercelAliasRestored {
    param(
        [Parameter(Mandatory = $true)][string]$AliasUrl,
        [Parameter(Mandatory = $true)][object]$PriorAlias,
        [Parameter(Mandatory = $true)][int64]$CacheBuster
    )
    $restored = Get-VercelAliasObservation -Alias ([string]$AliasUrl)
    $restoredId = [string](Get-OptionalJsonProperty -InputObject $restored -Name "id")
    $restoredUrlRaw = [string](Get-OptionalJsonProperty -InputObject $restored -Name "url")
    $restoredUrl = if ($restoredUrlRaw) {
        Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl $restoredUrlRaw
    } else { '' }
    $expectedId = if (Test-VercelObjectProperty -InputObject $PriorAlias -Name 'deployment_id') {
        [string]$PriorAlias.deployment_id
    } else { [string]$PriorAlias.id }
    $expectedUrl = if (Test-VercelObjectProperty -InputObject $PriorAlias -Name 'deployment_url') {
        [string]$PriorAlias.deployment_url
    } else { [string]$PriorAlias.url }
    if (-not $restoredId -or $restoredId -ne $expectedId) {
        throw "Rollback verification for $AliasUrl resolved the wrong deployment ID."
    }
    if (
        -not $restoredUrl -or
        (Normalize-VercelDeploymentUrl $restoredUrl) -ne
        (Normalize-VercelDeploymentUrl $expectedUrl)
    ) {
        throw "Rollback verification for $AliasUrl resolved the wrong deployment URL."
    }

    $hasRollbackContract = (Test-VercelObjectProperty -InputObject $PriorAlias -Name 'rollback_contract') -and
        $null -ne $PriorAlias.rollback_contract
    $proofBaseUrl = if ($hasRollbackContract) { $expectedUrl } else { $AliasUrl }
    $proof = Get-VercelAliasEndpointProof -AliasUrl $proofBaseUrl -CacheBuster $CacheBuster
    if ($PriorAlias.health_available -and -not $proof.health_available) {
        throw "Rollback verification health is unavailable for $AliasUrl."
    }
    if ($PriorAlias.health_status -and $proof.health_status -ne $PriorAlias.health_status) {
        throw "Rollback verification health status changed for $AliasUrl."
    }
    if ($PriorAlias.readiness_available -and -not $proof.readiness_available) {
        throw "Rollback verification readiness is unavailable for $AliasUrl."
    }
    if ($PriorAlias.readiness_status -and $proof.readiness_status -ne $PriorAlias.readiness_status) {
        throw "Rollback verification readiness status changed for $AliasUrl."
    }
    if ($null -ne $PriorAlias.readiness_http_status -and
        $proof.readiness_http_status -ne $PriorAlias.readiness_http_status) {
        throw "Rollback verification readiness HTTP status changed for $AliasUrl."
    }
    if ($PriorAlias.source_manifest_available -or
        ($hasRollbackContract -and
            [string]$PriorAlias.rollback_contract.mode -ceq 'READY_SOURCE_MANIFEST')) {
        if (-not $proof.source_manifest_available) {
            throw "Rollback verification source manifest is unavailable for $AliasUrl."
        }
        if ($proof.source_manifest_sha256 -ne $PriorAlias.source_manifest_sha256) {
            throw "Rollback verification source manifest changed for $AliasUrl."
        }
    }
    $restoredBuild = Invoke-VercelJson `
        -Arguments @("curl", "$proofBaseUrl/build-manifest.json?rollback_verify=$CacheBuster") `
        -Label "Rollback build manifest proof for $AliasUrl"
    $restoredRelease = Invoke-VercelJson `
        -Arguments @("curl", "$proofBaseUrl/release-manifest.json?rollback_verify=$CacheBuster") `
        -Label "Rollback release manifest proof for $AliasUrl"
    $buildHash = Get-VercelRemoteFileSha256 `
        -BaseUrl $proofBaseUrl -RelativePath 'build-manifest.json' `
        -Label "Rollback alias $AliasUrl"
    $releaseHash = Get-VercelRemoteFileSha256 `
        -BaseUrl $proofBaseUrl -RelativePath 'release-manifest.json' `
        -Label "Rollback alias $AliasUrl"
    if ($buildHash -cne [string]$PriorAlias.build_manifest_sha256 -or
        $releaseHash -cne [string]$PriorAlias.release_manifest_sha256) {
        throw "Rollback build/release manifest bytes changed for $AliasUrl."
    }
    $isPinnedLegacy = $hasRollbackContract -and
        [string]$PriorAlias.rollback_contract.mode -ceq 'PINNED_LEGACY_CLOCK_STALE'
    $artifactProof = Get-VercelGovernedAssetProof `
        -BaseUrl $proofBaseUrl -BuildManifest $restoredBuild `
        -Label "Rollback alias $AliasUrl" -PinnedLegacyInventory:$isPinnedLegacy
    if ($isPinnedLegacy) {
        $observedRollbackContract = Assert-VercelPinnedLegacyRollbackTarget `
            -DeploymentId $restoredId -DeploymentUrl $expectedUrl `
            -EndpointProof $proof -BuildManifest $restoredBuild `
            -ReleaseManifest $restoredRelease -BuildManifestSha256 $buildHash `
            -ReleaseManifestSha256 $releaseHash -ArtifactProof $artifactProof `
            -Label "Rollback alias $AliasUrl"
    }
    else {
        Assert-PublicationState `
            -Health $proof.health `
            -Readiness $proof.readiness `
            -BuildManifest $restoredBuild `
            -ReleaseManifest $restoredRelease `
            -ExpectedSourceSha ([string]$PriorAlias.source_sha) `
            -ExpectedMarketDate ([string]$restoredBuild.market_date) `
            -Label "Rollback alias $AliasUrl"
        if ([string]$proof.source_sha -cne [string]$restoredBuild.source_sha) {
            throw "Rollback source manifest/build source diverged for $AliasUrl."
        }
        if ($hasRollbackContract) {
            $observedRollbackContract = New-VercelReadyRollbackContract `
                -EndpointProof $proof -Label "Rollback alias $AliasUrl"
        }
    }
    if ($hasRollbackContract -and
        (ConvertTo-VercelCanonicalJson $observedRollbackContract) -cne
            (ConvertTo-VercelCanonicalJson $PriorAlias.rollback_contract)) {
        throw "Rollback contract changed for $AliasUrl."
    }
    if ((ConvertTo-VercelCanonicalJson $artifactProof) -cne
        (ConvertTo-VercelCanonicalJson $PriorAlias.artifact_proof)) {
        throw "Rollback governed artifact proof changed for $AliasUrl."
    }
    $evidence = [ordered]@{
        alias = $AliasUrl
        expected_deployment_id = $expectedId
        expected_deployment_url = $expectedUrl
        observed_deployment_id = $restoredId
        observed_deployment_url = $restoredUrl
        restored = $true
        health_status = $proof.health_status
        readiness_status = $proof.readiness_status
        readiness_http_status = $proof.readiness_http_status
        source_sha = [string]$PriorAlias.source_sha
        source_tree = [string]$PriorAlias.source_tree
        source_manifest_sha256 = [string]$PriorAlias.source_manifest_sha256
        build_manifest_sha256 = $buildHash
        release_manifest_sha256 = $releaseHash
        artifact_proof = $artifactProof
    }
    if ($hasRollbackContract) {
        $evidence['rollback_contract'] = $observedRollbackContract
    }
    return $evidence
}

function Get-VercelRemoteHttpStatus {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ($RelativePath -cnotmatch '^[A-Za-z0-9._/-]+$' -or
        $RelativePath.StartsWith('/') -or $RelativePath.Contains('\')) {
        throw "$Label remote status path is unsafe."
    }
    $result = Invoke-VercelBoundedPublicGet `
        -Url "$($BaseUrl.TrimEnd('/'))/$RelativePath`?status_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())" `
        -TimeoutSeconds $VercelCommandTimeoutSeconds -MaximumResponseBytes 1048576
    return [int]$result.status_code
}

function Get-VercelAliasEndpointProof {
    param(
        [Parameter(Mandatory = $true)][string]$AliasUrl,
        [Parameter(Mandatory = $true)][int64]$CacheBuster,
        [switch]$RequireHealthReadiness
    )
    $proof = [ordered]@{
        health_available = $false
        health_status = $null
        readiness_available = $false
        readiness_status = $null
        readiness_http_status = $null
        readiness_reason = $null
        readiness_failed_checks = @()
        source_manifest_available = $false
        source_manifest_http_status = $null
        source_sha = $null
        source_tree = $null
        source_manifest_sha256 = $null
        health = $null
        readiness = $null
    }
    try {
        $health = Invoke-VercelJson `
            -Arguments @("curl", "$AliasUrl/api/health?rollback_verify=$CacheBuster") `
            -Label "Alias health proof for $AliasUrl"
        if ($null -ne $health) {
            $proof.health = $health
            $proof.health_available = $true
            $proof.health_status = Get-OptionalJsonProperty -InputObject $health -Name "status"
        }
    }
    catch { }
    try {
        $readiness = Invoke-VercelJson `
            -Arguments @("curl", "$AliasUrl/api/readiness?rollback_verify=$CacheBuster") `
            -Label "Alias readiness proof for $AliasUrl"
        if ($null -ne $readiness) {
            $proof.readiness = $readiness
            $proof.readiness_available = $true
            $proof.readiness_status = Get-OptionalJsonProperty -InputObject $readiness -Name "status"
            $proof.readiness_http_status = Get-OptionalJsonProperty -InputObject $readiness -Name "http_status"
            $proof.readiness_reason = Get-OptionalJsonProperty -InputObject $readiness -Name "reason"
            $failedChecks = Get-OptionalJsonProperty -InputObject $readiness -Name "failed_checks"
            $proof.readiness_failed_checks = if ($null -eq $failedChecks) { @() } else { @($failedChecks) }
        }
    }
    catch { }
    try {
        $manifestResponse = Invoke-VercelBoundedPublicGet `
            -Url "$AliasUrl/vercel-source-manifest.json?rollback_verify=$CacheBuster" `
            -TimeoutSeconds $VercelCommandTimeoutSeconds
        if ([int]$manifestResponse.status_code -ne 200) {
            throw 'Alias source manifest proof returned a non-success status.'
        }
        $manifestRaw = [Text.UTF8Encoding]::new($false, $true).GetString(
            $manifestResponse.body_bytes
        )
        $manifestCanonical = Convert-VercelSourceManifestToCanonicalJson `
            -RawJson $manifestRaw.Trim()
        $manifest = $manifestCanonical | ConvertFrom-Json
        $proof.source_manifest_available = $true
        $proof.source_manifest_http_status = 200
        $proof.source_sha = [string]$manifest.source_sha
        $proof.source_tree = [string]$manifest.source_tree
        $proof.source_manifest_sha256 = Get-Sha256Hex $manifestCanonical
    }
    catch {
        try {
            $proof.source_manifest_http_status = Get-VercelRemoteHttpStatus `
                -BaseUrl $AliasUrl -RelativePath 'vercel-source-manifest.json' `
                -Label "Alias source manifest proof for $AliasUrl"
        }
        catch { }
    }
    if (-not $proof.source_sha -and $proof.health_available) {
        $proof.source_sha = [string](Get-OptionalJsonProperty -InputObject $proof.health -Name 'source_sha')
    }
    if ($RequireHealthReadiness -and -not $proof.health_available) {
        throw "Prior production health proof is unavailable for $AliasUrl."
    }
    if ($RequireHealthReadiness -and $proof.health_status -ne "alive") {
        throw "Prior production health status is not alive for $AliasUrl."
    }
    if ($RequireHealthReadiness -and -not $proof.readiness_available) {
        throw "Prior production readiness proof is unavailable for $AliasUrl."
    }
    if ($RequireHealthReadiness -and $proof.readiness_status -ne "ready") {
        throw "Prior production readiness status is not ready for $AliasUrl."
    }
    if ($RequireHealthReadiness -and $proof.readiness_http_status -ne 200) {
        throw "Prior production readiness HTTP status is not 200 for $AliasUrl."
    }
    return $proof
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $hash.Dispose()
    }
}

function Assert-VercelJournalSourceManifestLive {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $response = Invoke-VercelBoundedPublicGet `
        -Url "$($BaseUrl.TrimEnd('/'))/vercel-source-manifest.json?recovery_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())" `
        -TimeoutSeconds $VercelCommandTimeoutSeconds
    if ([int]$response.status_code -ne 200) {
        throw "$Label exact source manifest returned a non-success status."
    }
    $raw = [Text.UTF8Encoding]::new($false, $true).GetString($response.body_bytes)
    $canonical = Convert-VercelSourceManifestToCanonicalJson -RawJson $raw.Trim()
    $payload = $canonical | ConvertFrom-Json
    if ([string]$payload.source_sha -cne [string]$Journal.candidate_source_sha -or
        [string]$payload.source_tree -cne [string]$Journal.candidate_source_tree -or
        (Get-Sha256Hex $canonical) -cne [string]$Journal.candidate_manifest_sha256) {
        throw "$Label exact source manifest diverges from the publication journal."
    }
}
$emptySha256 = Get-Sha256Hex ""

function Get-VercelStateRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$StateRootPath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )
    $root = [System.IO.Path]::GetFullPath($StateRootPath).TrimEnd('\', '/')
    $target = [System.IO.Path]::GetFullPath($TargetPath)
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Vercel publication path escapes StateRoot."
    }
    $relative = $target.Substring($prefix.Length) -replace '\\','/'
    if (-not $relative -or $relative -match '(^|/)\.\.(/|$)') {
        throw "Vercel publication StateRoot-relative path is unsafe."
    }
    return $relative
}

function Assert-VercelContainedNonReparsePath {
    param(
        [Parameter(Mandatory = $true)][string]$RootPath,
        [Parameter(Mandatory = $true)][string]$TargetPath
    )
    $root = [System.IO.Path]::GetFullPath($RootPath).TrimEnd('\', '/')
    $target = [System.IO.Path]::GetFullPath($TargetPath)
    $prefix = $root + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Vercel publication write path escapes its governed root."
    }
    # Inspect an existing target leaf as well as every parent.  Starting at
    # only the parent lets a hostile file/junction leaf redirect the write.
    $cursor = $target
    while ($cursor -and $cursor.Length -ge $root.Length) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Vercel publication write path contains a reparse component."
            }
        }
        if ($cursor -ieq $root) { break }
        $next = Split-Path -Parent $cursor
        if ($next -eq $cursor) { throw "Vercel publication write path containment is invalid." }
        $cursor = $next
    }
}

function ConvertTo-VercelCanonicalObject {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $ordered = [ordered]@{}
        foreach ($key in ($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            $ordered[$key] = ConvertTo-VercelCanonicalObject $Value[$key]
        }
        return $ordered
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { ConvertTo-VercelCanonicalObject $_ })
    }
    if ($Value.PSObject -and $Value.PSObject.Properties.Count -gt 0 -and
        $Value -isnot [ValueType] -and $Value -isnot [string]) {
        $ordered = [ordered]@{}
        foreach ($property in ($Value.PSObject.Properties | Sort-Object Name)) {
            $ordered[$property.Name] = ConvertTo-VercelCanonicalObject $property.Value
        }
        return $ordered
    }
    return $Value
}

function ConvertTo-VercelCanonicalJson {
    param([Parameter(Mandatory = $true)][object]$Value)
    return ((ConvertTo-VercelCanonicalObject $Value) | ConvertTo-Json -Depth 40 -Compress)
}

function Invoke-VercelJournalTool {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $python = Get-DawnstrikeApprovedLockInterpreter
    $effectiveArguments = @($Arguments)
    if ($effectiveArguments.Count -gt 0 -and
        [string]$effectiveArguments[0] -in @("verify", "seal", "transition") -and
        "--runtime-root" -notin $effectiveArguments) {
        $effectiveArguments += @("--runtime-root", $resolvedRoot)
    }
    $output = & $python.path -I -B -S $journalHelper @effectiveArguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw "$Label failed." }
    try { return (($output -join "`n") | ConvertFrom-Json) }
    catch { throw "$Label returned invalid journal JSON." }
}

function Get-VercelPublicationJournal {
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) { return $null }
    $verified = Invoke-VercelJournalTool `
        -Arguments @("verify", $journalPath, "--state-root", $resolvedStateRoot) `
        -Label "Vercel publication journal verification"
    return $verified.payload
}

function Acquire-VercelPublicationLock {
    param(
        [Parameter(Mandatory = $true)][string]$CandidateSourceSha,
        [Parameter(Mandatory = $true)][string]$CandidateSourceTree,
        [Parameter(Mandatory = $true)][string]$CandidateMarketDate
    )
    $relativeJournal = Get-VercelStateRelativePath `
        -StateRootPath $resolvedStateRoot -TargetPath $journalPath
    $null = Invoke-VercelJournalTool -Arguments @(
        "acquire-lock", $publicationLockPath,
        "--state-root", $resolvedStateRoot,
        "--owner-id", $publicationLockOwner,
        "--pid", [string]$PID,
        "--candidate-source-sha", $CandidateSourceSha,
        "--candidate-source-tree", $CandidateSourceTree,
        "--candidate-market-date", $CandidateMarketDate,
        "--journal-path", $relativeJournal
    ) -Label "Vercel publication lock acquisition"
    $script:publicationLockAcquired = $true
}

function Release-VercelPublicationLock {
    if (-not $publicationLockAcquired) { return }
    $null = Invoke-VercelJournalTool -Arguments @(
        "release-lock", $publicationLockPath,
        "--state-root", $resolvedStateRoot,
        "--owner-id", $publicationLockOwner,
        "--pid", [string]$PID
    ) -Label "Vercel publication lock release"
    $script:publicationLockAcquired = $false
}

function Assert-VercelRuntimeActivationAbsent {
    # Two-lock handshake with runtime activation: publication owns its global
    # lock before sampling the activation lock, while activation owns its
    # runtime lock before sampling the publication lock. Concurrent starts
    # therefore both stop before provider mutation or a runtime rename.
    $activationLockRoot = Join-Path $resolvedStateRoot 'locks'
    if (-not (Test-Path -LiteralPath $activationLockRoot)) { return }
    Assert-VercelContainedNonReparsePath `
        -RootPath $resolvedStateRoot -TargetPath $activationLockRoot
    $rootItem = Get-Item -LiteralPath $activationLockRoot -Force -ErrorAction Stop
    if (-not $rootItem.PSIsContainer -or
        ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Runtime activation lock root is unsafe.'
    }
    $matches = @(Get-ChildItem -LiteralPath $activationLockRoot -Force |
        Where-Object { [string]$_.Name -ieq 'dawnstrike-runtime-activation.lock' })
    if ($matches.Count -gt 0) {
        foreach ($match in $matches) {
            Assert-VercelContainedNonReparsePath `
                -RootPath $resolvedStateRoot -TargetPath $match.FullName
        }
        throw 'A runtime activation lock exists; Vercel publication and recovery are blocked.'
    }
}

function Assert-VercelJournalBaseMatchesInvocation {
    param([Parameter(Mandatory = $true)][object]$Journal)
    if ([string]$Journal.candidate_source_sha -ne $expectedSourceSha -or
        [string]$Journal.candidate_source_tree -ne $expectedSourceTree) {
        throw "Vercel publication journal does not match the current source SHA/tree."
    }
    if (-not $resolvedExpectedMarketDate -or
        [string]$Journal.candidate_market_date -ne $resolvedExpectedMarketDate -or
        [string]$Journal.expected_market_date -ne $resolvedExpectedMarketDate) {
        throw "Vercel publication journal cannot be reused without an exact ExpectedMarketDate match."
    }
    $journalUsesCasEvidence = [string]$Journal.schema_version -in @(
        'dawnstrike.vercel_publication_journal.v7',
        'dawnstrike.vercel_publication_journal.v8'
    )
    if ([string]$Journal.project_id -ne $ProjectId -or
        [string]$Journal.project_name -ne $ProjectName -or
        [string]$Journal.provider_scope -ne $ProviderScope -or
        ($journalUsesCasEvidence -and [string]$Journal.provider_team_id -ne $ProviderTeamId)) {
        throw "Vercel publication journal does not match the current Vercel project."
    }
    if ([string]$Journal.toolchain_identity_sha256 -cne $toolchainIdentitySha256) {
        throw "Vercel publication journal toolchain identity does not match this host."
    }
    if ([string]$Journal.prepublication_authorization_id -ne $PrepublicationAuthorizationId -or
        [string]$Journal.daily_ledger_authorization_id -ne $DailyLedgerAuthorizationId) {
        throw "Vercel publication journal authorization does not match this invocation."
    }
    $journalAliases = @($Journal.production_aliases | ForEach-Object { [string]$_ })
    if ($journalAliases.Count -ne $allProductionAliases.Count) {
        throw "Vercel publication journal aliases do not match this invocation."
    }
    for ($index = 0; $index -lt $allProductionAliases.Count; $index++) {
        if ($journalAliases[$index] -cne [string]$allProductionAliases[$index]) {
            throw "Vercel publication journal aliases do not match this invocation."
        }
    }
}

function Assert-VercelPriorJournalHistoryTerminal {
    # A dated directory is immutable operation history. Never treat a prior
    # date's COMPLETE as today's completion, and never start a new mutation
    # while an earlier operation still requires rollback compensation.
    $script:pinnedLegacyRollbackConsumed = $false
    if (-not (Test-Path -LiteralPath $journalHistoryRoot -PathType Container)) { return }
    $legacy = Join-Path $journalHistoryRoot "vercel-publication-operation.json"
    if (Test-Path -LiteralPath $legacy -PathType Leaf) {
        throw "Legacy undated Vercel journal must be terminally compensated and migrated before publication."
    }
    $history = @()
    foreach ($directory in @(Get-ChildItem -LiteralPath $journalHistoryRoot -Directory -Force | Sort-Object Name)) {
        if ($directory.Name -notmatch '^\d{4}-\d{2}-\d{2}$') { continue }
        Assert-VercelCompensatedArchivesValid -Directory $directory.FullName -MarketDate $directory.Name
        if ($directory.Name -eq $resolvedExpectedMarketDate) { continue }
        $priorPath = Join-Path $directory.FullName "vercel-publication-operation.json"
        if (-not (Test-Path -LiteralPath $priorPath -PathType Leaf)) { continue }
        $verified = Invoke-VercelJournalTool `
            -Arguments @("verify", $priorPath, "--state-root", $resolvedStateRoot) `
            -Label "Prior-date Vercel publication journal verification"
        if ([string]$verified.payload.candidate_market_date -cne $directory.Name) {
            throw "Prior-date Vercel journal directory identity is invalid."
        }
        if ([string]$verified.payload.project_id -cne $ProjectId -or
            [string]$verified.payload.project_name -cne $ProjectName -or
            [string]$verified.payload.provider_scope -cne $ProviderScope) {
            throw "Prior-date Vercel journal belongs to a foreign provider project or scope."
        }
        $priorAliases = @($verified.payload.production_aliases | ForEach-Object { [string]$_ })
        if ($priorAliases.Count -ne $allProductionAliases.Count) {
            throw "Prior-date Vercel journal aliases do not match the configured alias set."
        }
        for ($index = 0; $index -lt $allProductionAliases.Count; $index++) {
            if ($priorAliases[$index] -cne [string]$allProductionAliases[$index]) {
                throw "Prior-date Vercel journal aliases do not match the configured alias set."
            }
        }
        $history += [pscustomobject]@{
            date = $directory.Name
            root = $directory.FullName
            path = $priorPath
            payload = $verified.payload
            terminal = [string]$verified.payload.phase -in @("COMPLETE", "COMPENSATED")
        }
    }
    $script:pinnedLegacyRollbackConsumed = @($history | Where-Object {
        [string]$_.payload.schema_version -in @(
            'dawnstrike.vercel_publication_journal.v3',
            'dawnstrike.vercel_publication_journal.v7'
        ) -and
        [string]$_.payload.phase -ceq 'COMPLETE'
    }).Count -gt 0
    $otherNonterminal = @($history | Where-Object { -not $_.terminal })
    if ($RecoveryOnly -and $otherNonterminal.Count -gt 0) {
        # Manual recovery is an exact dated operation. Never let a mistyped or
        # later ExpectedMarketDate compensate another journal and then report
        # that the requested date had no operation.
        throw 'RecoveryOnly found a nonterminal Vercel journal outside its exact ExpectedMarketDate.'
    }
    $futureNonterminal = @($history | Where-Object { $_.date -gt $resolvedExpectedMarketDate -and -not $_.terminal })
    if ($futureNonterminal.Count -gt 0) {
        throw "A future-dated Vercel publication journal is nonterminal; provider mutation is blocked."
    }
    $priorNonterminal = @($history | Where-Object { $_.date -lt $resolvedExpectedMarketDate -and -not $_.terminal })
    if ($priorNonterminal.Count -gt 1) {
        throw "Multiple prior-date Vercel publication journals are nonterminal; deterministic compensation is blocked."
    }
    foreach ($entry in $priorNonterminal) {
            if ([string]$entry.payload.toolchain_identity_sha256 -cne $toolchainIdentitySha256) {
                throw "A nonterminal prior-date Vercel journal requires its exact recorded toolchain for recovery."
            }
            $savedJournalRoot = $script:journalRoot
            $savedJournalPath = $script:journalPath
            $savedResultRelativePath = $script:resultRelativePath
            $savedResultPath = $script:resultPath
            try {
                $script:journalRoot = $entry.root
                $script:journalPath = $entry.path
                $script:resultRelativePath = [string]$entry.payload.result_relative_path
                $script:resultPath = Join-Path $resolvedStateRoot `
                    ($script:resultRelativePath -replace '/', '\')
                Invoke-VercelPublicationCompensation `
                    -Journal $entry.payload `
                    -FailureType "prior_date_interrupted_rollover"
                $terminal = Invoke-VercelJournalTool `
                    -Arguments @("verify", $entry.path, "--state-root", $resolvedStateRoot) `
                    -Label "Prior-date terminal compensation verification"
                if ([string]$terminal.payload.phase -cne "COMPENSATED") {
                    throw "Prior-date Vercel compensation did not seal terminal evidence."
                }
            }
            finally {
                $script:journalRoot = $savedJournalRoot
                $script:journalPath = $savedJournalPath
                $script:resultRelativePath = $savedResultRelativePath
                $script:resultPath = $savedResultPath
            }
    }
}

function Assert-VercelPinnedLegacyRollbackAvailable {
    if ($script:pinnedLegacyRollbackConsumed) {
        throw "Pinned legacy rollback authorization was already consumed by a prior successful migration."
    }
}

function Assert-VercelJournalMatchesInvocation {
    param([Parameter(Mandatory = $true)][object]$Journal)
    Assert-VercelJournalBaseMatchesInvocation -Journal $Journal
    if ($Journal.result_payload.promoted -ne [bool]$Promote -or
        $Journal.result_payload.allow_degraded -ne [bool]$AllowDegraded) {
        throw "Complete Vercel publication journal deployment authorization does not match this invocation."
    }
    if ([string]$Journal.result_payload.promoted_deployment_id -ne [string]$Journal.promoted_deployment_id -or
        [string]$Journal.result_payload.production_deployment_id -ne [string]$Journal.promoted_deployment_id) {
        throw "Complete Vercel publication journal deployment identity is inconsistent."
    }
}

function Write-VercelPublicationJournal {
    param(
        [Parameter(Mandatory = $true)][object]$Payload,
        [switch]$Transition
    )
    $temporary = Join-Path $journalRoot (".journal-input-" + [guid]::NewGuid().ToString("N") + ".json")
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $journalPath
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $temporary
    New-Item -ItemType Directory -Path $journalRoot -Force | Out-Null
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $journalPath
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $temporary
    try {
        $json = ConvertTo-VercelCanonicalJson $Payload
        Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $temporary
        [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
        $args = if ($Transition) {
            @("transition", $temporary, $journalPath, "--previous", $journalPath, "--state-root", $resolvedStateRoot)
        }
        else {
            @("seal", $temporary, $journalPath, "--state-root", $resolvedStateRoot)
        }
        return (Invoke-VercelJournalTool -Arguments $args -Label "Vercel publication journal write")
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-VercelCompensatedArchiveIntent {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $Path
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or $item.Length -gt 65536 -or
        ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
        throw "Vercel compensated archive intent is unsafe."
    }
    $raw = [System.IO.File]::ReadAllText($Path)
    Assert-VercelJsonObjectKeysUnique -RawJson $raw
    try { $payload = $raw | ConvertFrom-Json }
    catch { throw "Vercel compensated archive intent is invalid JSON." }
    $expectedKeys = @(
        'archive_relative_path', 'broker_execution_enabled', 'candidate_market_date',
        'compensation_relative_path', 'compensation_sha256', 'intent_self_sha256',
        'journal_sha256', 'project_id', 'project_name', 'provider_scope',
        'research_only', 'schema_version', 'status'
    ) | Sort-Object
    $actualKeys = @($payload.PSObject.Properties.Name | Sort-Object)
    if ((ConvertTo-VercelCanonicalJson $actualKeys) -cne
        (ConvertTo-VercelCanonicalJson $expectedKeys) -or
        [string]$payload.schema_version -cne 'dawnstrike.vercel_publication_archive_intent.v1' -or
        [string]$payload.status -cne 'ARCHIVE_REQUIRED' -or
        [string]$payload.journal_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$payload.compensation_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$payload.intent_self_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $payload.research_only -ne $true -or $payload.broker_execution_enabled -ne $false) {
        throw "Vercel compensated archive intent contract is invalid."
    }
    $unsigned = [ordered]@{}
    foreach ($property in $payload.PSObject.Properties) {
        if ($property.Name -ne 'intent_self_sha256') { $unsigned[$property.Name] = $property.Value }
    }
    if ((Get-VercelResultSha256 $unsigned) -cne [string]$payload.intent_self_sha256 -or
        $raw -cne (ConvertTo-VercelCanonicalJson $payload)) {
        throw "Vercel compensated archive intent self hash or canonical bytes are invalid."
    }
    return $payload
}

function Assert-VercelCompensatedArchivesValid {
    param(
        [Parameter(Mandatory = $true)][string]$Directory,
        [Parameter(Mandatory = $true)][string]$MarketDate
    )
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return }
    $intents = @(Get-ChildItem -LiteralPath $Directory -File -Force |
        Where-Object { $_.Name -like 'vercel-publication-operation-compensated-*.intent.json' })
    $archives = @(Get-ChildItem -LiteralPath $Directory -File -Force |
        Where-Object { $_.Name -like 'vercel-publication-operation-compensated-*.json' -and
            $_.Name -notlike '*.intent.json' })
    foreach ($intentFile in $intents) {
        if ($intentFile.Name -cnotmatch '^vercel-publication-operation-compensated-([0-9a-f]{64})\.intent\.json$') {
            throw "Vercel compensated archive intent filename is invalid."
        }
        $filenameHash = $Matches[1]
        $intent = Get-VercelCompensatedArchiveIntent -Path $intentFile.FullName
        if ([string]$intent.journal_sha256 -cne $filenameHash -or
            [string]$intent.candidate_market_date -cne $MarketDate -or
            [string]$intent.project_id -cne $ProjectId -or
            [string]$intent.project_name -cne $ProjectName -or
            [string]$intent.provider_scope -cne $ProviderScope) {
            throw "Vercel compensated archive intent identity is invalid."
        }
        $archive = Join-Path $resolvedStateRoot ([string]$intent.archive_relative_path -replace '/', '\')
        if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) {
            $canonical = Join-Path $Directory 'vercel-publication-operation.json'
            if (-not (Test-Path -LiteralPath $canonical -PathType Leaf) -or
                (Get-VercelFileSha256 -Path $canonical) -cne $filenameHash) {
                throw "Vercel compensated archive required by durable intent is missing or changed."
            }
            $canonicalVerified = Invoke-VercelJournalTool `
                -Arguments @('verify', $canonical, '--state-root', $resolvedStateRoot) `
                -Label 'Pre-archive compensated Vercel journal verification'
            if ([string]$canonicalVerified.payload.phase -cne 'COMPENSATED') {
                throw "Vercel archive intent canonical journal is not compensated."
            }
            Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $archive
            [System.IO.File]::Move($canonical, $archive)
        }
        if ((Get-VercelFileSha256 -Path $archive) -cne $filenameHash) {
            throw "Vercel compensated archive required by durable intent changed."
        }
        $verified = Invoke-VercelJournalTool `
            -Arguments @('verify', $archive, '--state-root', $resolvedStateRoot) `
            -Label 'Archived compensated Vercel journal verification'
        if ([string]$verified.payload.phase -cne 'COMPENSATED' -or
            [string]$verified.payload.compensation_relative_path -cne
                [string]$intent.compensation_relative_path -or
            [string]$verified.payload.compensation_sha256 -cne [string]$intent.compensation_sha256) {
            throw "Vercel compensated archive does not match its durable intent."
        }
    }
    foreach ($archiveFile in $archives) {
        if ($archiveFile.Name -cnotmatch '^vercel-publication-operation-compensated-([0-9a-f]{64})\.json$') {
            throw "Vercel compensated archive filename is invalid."
        }
        $intentPath = Join-Path $Directory ("vercel-publication-operation-compensated-$($Matches[1]).intent.json")
        if (-not (Test-Path -LiteralPath $intentPath -PathType Leaf)) {
            throw "Vercel compensated archive has no durable archive intent."
        }
    }
}

function Archive-VercelCompensatedCurrentJournal {
    param([Parameter(Mandatory = $true)][object]$Journal)
    if ([string]$Journal.phase -cne "COMPENSATED") {
        throw "Only an exact terminal compensated Vercel journal may be archived."
    }
    $verified = Invoke-VercelJournalTool `
        -Arguments @("verify", $journalPath, "--state-root", $resolvedStateRoot) `
        -Label "Compensated Vercel journal archive verification"
    if ([string]$verified.payload.phase -cne "COMPENSATED") {
        throw "Vercel journal changed before compensated archival."
    }
    $rawHash = Get-VercelFileSha256 -Path $journalPath
    $archivePath = Join-Path $journalRoot ("vercel-publication-operation-compensated-$rawHash.json")
    $intentPath = Join-Path $journalRoot ("vercel-publication-operation-compensated-$rawHash.intent.json")
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $journalPath
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $archivePath
    $intent = [ordered]@{
        schema_version = 'dawnstrike.vercel_publication_archive_intent.v1'
        status = 'ARCHIVE_REQUIRED'
        candidate_market_date = [string]$Journal.candidate_market_date
        project_id = [string]$Journal.project_id
        project_name = [string]$Journal.project_name
        provider_scope = [string]$Journal.provider_scope
        journal_sha256 = $rawHash
        archive_relative_path = Get-VercelStateRelativePath -StateRootPath $resolvedStateRoot -TargetPath $archivePath
        compensation_relative_path = [string]$Journal.compensation_relative_path
        compensation_sha256 = [string]$Journal.compensation_sha256
        research_only = $true
        broker_execution_enabled = $false
    }
    $intent.intent_self_sha256 = Get-VercelResultSha256 $intent
    $intentJson = ConvertTo-VercelCanonicalJson $intent
    if (Test-Path -LiteralPath $intentPath -PathType Leaf) {
        $existingIntent = Get-VercelCompensatedArchiveIntent -Path $intentPath
        if ((ConvertTo-VercelCanonicalJson $existingIntent) -cne $intentJson) {
            throw "Compensated Vercel archive intent collides with another operation."
        }
    }
    else {
        Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $intentPath
        $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($intentJson)
        $stream = [System.IO.File]::Open($intentPath, [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
        try { $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        $null = Get-VercelCompensatedArchiveIntent -Path $intentPath
    }
    if (Test-Path -LiteralPath $archivePath -PathType Leaf) {
        throw "Compensated Vercel journal canonical and archive paths both exist."
    }
    [System.IO.File]::Move($journalPath, $archivePath)
    if (Test-Path -LiteralPath $journalPath) {
        throw "Compensated Vercel journal archival did not remove the current operation path."
    }
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $archivePath
    if ((Get-VercelFileSha256 -Path $archivePath) -cne $rawHash) {
        throw "Compensated Vercel journal archive bytes changed."
    }
    $null = Invoke-VercelJournalTool `
        -Arguments @("verify", $archivePath, "--state-root", $resolvedStateRoot) `
        -Label "Archived compensated Vercel journal verification"
    Assert-VercelCompensatedArchivesValid -Directory $journalRoot `
        -MarketDate ([string]$Journal.candidate_market_date)
    return $archivePath
}

function Test-VercelPromotionSeam {
    param([Parameter(Mandatory = $true)][string]$Point)
    if ($TestFailurePoint -eq $Point) { throw "Test-only Vercel publication failure seam: $Point" }
    if ($TestCrashPoint -eq $Point) { Stop-Process -Id $PID -Force }
}

function Assert-GovernedPublicationAuthorization {
    param(
        [string]$ArtifactRoot = (Join-Path $resolvedRoot "build\public")
    )
    if ([string]::IsNullOrWhiteSpace($resolvedExpectedMarketDate)) {
        throw "ExpectedMarketDate is required for scheduled publication."
    }
    if ([string]::IsNullOrWhiteSpace($PrepublicationAuthorizationId)) {
        throw "Immutable prepublication authorization identity is required."
    }
    $boundaryMode = if ($Promote) { "Production" } else { "Preview" }
    $boundaryScript = Join-Path $codeRoot "scripts\publication_boundary.py"
    $prepublicationScript = Join-Path $codeRoot "scripts\verify_daily_prepublication.py"
    $pythonDependencyContract = Get-DawnstrikeProtectedPythonDependencyContract `
        -ReleaseRoot $codeRoot -ExpectedSha $ExpectedSha
    $tzdataRoot = Join-Path $pythonDependencyContract.dependency_root "Lib\site-packages\tzdata\zoneinfo"
    $authorizationEnvironment = @{
        PYTHONHOME = ""; PYTHONPATH = ""; PYTHONSTARTUP = "";
        PYTHONDONTWRITEBYTECODE = "1"; PYTHONTZPATH = $tzdataRoot;
        VERCEL_TOKEN = ""; VERCEL_ORG_ID = ""; VERCEL_PROJECT_ID = "";
        VERCEL_TEAM_ID = ""; VERCEL_OIDC_TOKEN = ""; TURBO_TOKEN = "";
        HTTP_PROXY = ""; HTTPS_PROXY = ""; ALL_PROXY = ""; NO_PROXY = "";
        SSL_CERT_FILE = ""; SSL_CERT_DIR = ""; REQUESTS_CA_BUNDLE = "";
        CURL_CA_BUNDLE = ""; NODE_EXTRA_CA_CERTS = "";
    }
    $boundaryArguments = @(
        $boundaryScript, "validate",
        "--market-date", $resolvedExpectedMarketDate,
        "--publication-mode", $boundaryMode
    )
    if ($TestNowUtc) {
        if ($env:DAWNSTRIKE_TEST_CLOCK -ne "1") {
            throw "Publication clock override is test-only."
        }
        $boundaryArguments += @("--now-utc", $TestNowUtc)
    }
    $boundary = Invoke-DawnstrikeNativeProcess `
        -FilePath ([string]$approvedPython.path) `
        -ArgumentList $boundaryArguments `
        -LogRoot (Join-Path $resolvedStateRoot "logs") `
        -LogName "vercel_publication_market_boundary" `
        -WorkingDirectory $resolvedRoot `
        -EnvironmentOverrides $authorizationEnvironment `
        -NoSite `
        -SuppressConsoleReplay:$SuppressNativeConsoleReplay
    if ($boundary.exit_code -ne 0) {
        throw "Vercel publication market boundary rejected ExpectedMarketDate."
    }
    $database = Join-Path $resolvedStateRoot "shadow_real.sqlite"
    Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $ArtifactRoot `
        -Label "Authorized public artifact root"
    $verify = Invoke-DawnstrikeNativeProcess `
        -FilePath ([string]$approvedPython.path) `
        -ArgumentList @(
            $prepublicationScript, "--db-path", $database,
            "--artifact-root", $ArtifactRoot, "--market-date", $resolvedExpectedMarketDate,
            "--expected-market-date", $resolvedExpectedMarketDate,
            "--release-sha", $expectedSourceSha, "--runtime-root", $resolvedRoot
        ) `
        -LogRoot (Join-Path $resolvedStateRoot "logs") `
        -LogName "vercel_publication_authorization" `
        -WorkingDirectory $resolvedRoot `
        -EnvironmentOverrides $authorizationEnvironment `
        -NoSite `
        -SuppressConsoleReplay:$SuppressNativeConsoleReplay
    if ($verify.exit_code -ne 0) {
        throw "Vercel publication requires a passing governed prepublication authorization."
    }
    try {
        $payload = (Get-Content -LiteralPath $verify.stdout_path -Raw) | ConvertFrom-Json
    } catch { throw "Governed prepublication authorization returned invalid JSON." }
    if ([string]$payload.expected_market_date -cne $resolvedExpectedMarketDate -or
        [string]$payload.authorization_id -cne $PrepublicationAuthorizationId -or
        [string]$payload.daily_ledger_authorization_id -cne $PrepublicationAuthorizationId) {
        throw "Vercel publication authorization identity is not immutable or does not match the daily ledger."
    }
    foreach ($field in @(
        'build_manifest_sha256', 'release_manifest_raw_sha256',
        'public_artifact_root_sha256'
    )) {
        if ([string]$payload.artifact_identity.$field -cnotmatch '^[0-9a-f]{64}$') {
            throw "Vercel publication authorization omitted exact artifact identity: $field"
        }
    }
    return $payload.artifact_identity
}

function Assert-VercelAuthorizedArtifactIdentity {
    param(
        [Parameter(Mandatory = $true)][object]$Expected,
        [Parameter(Mandatory = $true)][object]$Observed,
        [Parameter(Mandatory = $true)][string]$Label
    )
    foreach ($field in @(
        'build_sha', 'build_id', 'publication_set_sha256',
        'release_manifest_sha256', 'build_manifest_sha256',
        'release_manifest_raw_sha256', 'public_artifact_root_sha256'
    )) {
        if ([string]$Expected.$field -cne [string]$Observed.$field) {
            throw "$Label authorization-to-artifact identity mismatch: $field"
        }
    }
}

function Get-VercelResultSha256 {
    param([Parameter(Mandatory = $true)][object]$Payload)
    return Get-Sha256Hex (ConvertTo-VercelCanonicalJson $Payload)
}

function Publish-VercelFileAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$TemporaryPath,
        [Parameter(Mandatory = $true)][string]$DestinationPath
    )
    if (Test-Path -LiteralPath $DestinationPath -PathType Leaf) {
        $backup = "$DestinationPath.$([guid]::NewGuid().ToString('N')).bak"
        try { [System.IO.File]::Replace($TemporaryPath, $DestinationPath, $backup, $true) }
        finally {
            if (Test-Path -LiteralPath $backup -PathType Leaf) {
                Remove-Item -LiteralPath $backup -Force -ErrorAction SilentlyContinue
            }
        }
    }
    else { [System.IO.File]::Move($TemporaryPath, $DestinationPath) }
}

function Write-VercelResultAtomic {
    param([Parameter(Mandatory = $true)][object]$Payload)
    $json = ConvertTo-VercelCanonicalJson $Payload
    $temporary = "$resultPath.$([guid]::NewGuid().ToString('N')).tmp"
    $runtimeTemporary = "$runtimeResultPath.$([guid]::NewGuid().ToString('N')).tmp"
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $resultPath
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $temporary
    Assert-VercelContainedNonReparsePath -RootPath $resolvedRoot -TargetPath $runtimeResultPath
    Assert-VercelContainedNonReparsePath -RootPath $resolvedRoot -TargetPath $runtimeTemporary
    New-Item -ItemType Directory -Path (Split-Path -Parent $resultPath) -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $runtimeResultPath) -Force | Out-Null
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $resultPath
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $temporary
    Assert-VercelContainedNonReparsePath -RootPath $resolvedRoot -TargetPath $runtimeResultPath
    Assert-VercelContainedNonReparsePath -RootPath $resolvedRoot -TargetPath $runtimeTemporary
    try {
        $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
        Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $temporary
        [System.IO.File]::WriteAllBytes($temporary, $bytes)
        $stream = [System.IO.File]::Open($temporary, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        try { $stream.Flush($true) } finally { $stream.Dispose() }
        Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $resultPath
        Publish-VercelFileAtomic -TemporaryPath $temporary -DestinationPath $resultPath
        Assert-VercelContainedNonReparsePath -RootPath $resolvedRoot -TargetPath $runtimeTemporary
        [System.IO.File]::WriteAllBytes($runtimeTemporary, $bytes)
        $runtimeStream = [System.IO.File]::Open($runtimeTemporary, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        try { $runtimeStream.Flush($true) } finally { $runtimeStream.Dispose() }
        Assert-VercelContainedNonReparsePath -RootPath $resolvedRoot -TargetPath $runtimeResultPath
        Publish-VercelFileAtomic -TemporaryPath $runtimeTemporary -DestinationPath $runtimeResultPath
        if ((Get-VercelFileSha256 -Path $resultPath) -cne (Get-VercelFileSha256 -Path $runtimeResultPath)) {
            throw "Durable StateRoot and runtime Vercel result copies diverge."
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $runtimeTemporary -PathType Leaf) {
            Remove-Item -LiteralPath $runtimeTemporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Repair-VercelResultCopies {
    param(
        [Parameter(Mandatory = $true)][object]$Payload,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    $stateValid = (Test-Path -LiteralPath $resultPath -PathType Leaf) -and
        ((Get-VercelFileSha256 -Path $resultPath) -ceq $ExpectedSha256)
    $runtimeValid = (Test-Path -LiteralPath $runtimeResultPath -PathType Leaf) -and
        ((Get-VercelFileSha256 -Path $runtimeResultPath) -ceq $ExpectedSha256)
    if (-not $stateValid -or -not $runtimeValid) { Write-VercelResultAtomic -Payload $Payload }
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $runtimeResultPath -PathType Leaf) -or
        (Get-VercelFileSha256 -Path $resultPath) -cne $ExpectedSha256 -or
        (Get-VercelFileSha256 -Path $runtimeResultPath) -cne $ExpectedSha256) {
        throw "Durable StateRoot and runtime Vercel result copies are not exact."
    }
}

function Assert-LowerHex64 {
    param([AllowNull()][object]$Value, [string]$Field, [string]$Label)
    if ($null -eq $Value -or ([string]$Value -cnotmatch '^[0-9a-f]{64}$')) {
        throw "$Label $Field must be a lowercase 64-hex value."
    }
}

function Assert-VercelPublicFileHashSet {
    param(
        [Parameter(Mandatory = $true)][object]$BuildManifest,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$PinnedLegacyInventory
    )
    $expectedNames = @(
        'assets/dawnstrike.css',
        'assets/dawnstrike.js',
        'data/calendar.json',
        'data/calendar.json.manifest.json',
        'data/opportunity-projection.json',
        'data/opportunity-projection.json.manifest.json',
        'data/performance.json',
        'data/performance.json.manifest.json',
        'data/publication-set.json',
        'data/scenarios.json',
        'data/scenarios.json.manifest.json',
        'data/v6-learning.json',
        'favicon.svg',
        'index.html',
        'readiness.json',
        'release-manifest.json',
        'stage-manifest.json'
    )
    if ($PinnedLegacyInventory) {
        $expectedNames += 'daily-finalize.jsonl'
    }
    $fileHashes = Get-OptionalJsonProperty -InputObject $BuildManifest -Name 'file_hashes'
    if ($null -eq $fileHashes) { throw "$Label build manifest file_hashes is missing." }
    $expected = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($name in $expectedNames) { $null = $expected.Add($name) }
    $observed = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($property in @($fileHashes.PSObject.Properties)) {
        $name = [string]$property.Name
        if (-not $observed.Add($name) -or -not $expected.Contains($name)) {
            throw "$Label build manifest contains an unexpected or duplicate file hash: $name"
        }
        Assert-LowerHex64 -Value $property.Value -Field "file_hashes.$name" -Label $Label
    }
    $missing = @($expected | Where-Object { -not $observed.Contains($_) })
    if ($missing.Count -gt 0) {
        throw "$Label build manifest is missing exact governed file hashes: $($missing -join ',')"
    }
}

function Assert-VercelAccountSessionReport {
    param(
        [Parameter(Mandatory = $true)][object]$Report,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$SourceSha,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $expected = Get-OptionalJsonProperty -InputObject $Report -Name 'expected_session_count'
    if ([string]$Report.schema_version -cne 'dawnstrike.account_session_report.v1' -or
        [string]$Report.status -cne 'COMPLETE' -or
        [string]$Report.market_date -cne $MarketDate -or
        [string]$Report.code_sha -cne $SourceSha -or
        [string]$Report.account_id -cne 'alphaops_v5_simulated' -or
        [string]$Report.version_bucket -cne 'v5' -or
        [string]$Report.cohort -cne 'official_forward_paper' -or
        [string]$Report.strategy_id -cne 'alphaops_v5' -or
        [string]$Report.strategy_version -cne 'dawnstrike-alphaops-v5.0.0' -or
        $Report.research_only -ne $true -or
        $Report.broker_execution_enabled -ne $false -or
        $Report.unsafe_ledger_count -ne 0 -or
        -not (($expected -is [int]) -or ($expected -is [long])) -or
        [int64]$expected -lt 1 -or
        $Report.ledger_row_count -ne $expected -or
        $Report.complete_count -ne $expected -or
        $Report.missing_count -ne 0 -or
        $Report.partial_count -ne 0 -or
        $Report.quarantined_count -ne 0) {
        throw "$Label account-session report is not exact COMPLETE safe coverage."
    }
    foreach ($field in @(
        'input_hash_sha256', 'expected_calendar_hash_sha256', 'source_hashes_sha256'
    )) {
        Assert-LowerHex64 -Value (Get-OptionalJsonProperty -InputObject $Report -Name $field) `
            -Field "account_session_report.$field" -Label $Label
    }
    $series = @($Report.series)
    if ($series.Count -ne 1 -or
        [string]$series[0].status -cne 'COMPLETE' -or
        [string]$series[0].market_date -cne $MarketDate -or
        [string]$series[0].code_sha -cne $SourceSha -or
        [string]$series[0].account_id -cne 'alphaops_v5_simulated' -or
        [string]$series[0].version_bucket -cne 'v5' -or
        [string]$series[0].cohort -cne 'official_forward_paper' -or
        [string]$series[0].strategy_id -cne 'alphaops_v5' -or
        [string]$series[0].strategy_version -cne 'dawnstrike-alphaops-v5.0.0' -or
        $series[0].expected_session_count -ne $expected -or
        $series[0].ledger_row_count -ne $expected -or
        $series[0].complete_count -ne $expected -or
        $series[0].research_only -ne $true -or
        $series[0].broker_execution_enabled -ne $false) {
        throw "$Label account-session series is ambiguous or incomplete."
    }
    return Get-Sha256Hex (ConvertTo-VercelCanonicalJson $Report)
}

function Assert-PublicationState {
    param(
        [object]$Health,
    [object]$Readiness,
    [object]$BuildManifest,
    [string]$ExpectedSourceSha,
    [object]$ReleaseManifest,
    [string]$Label,
    [string]$ExpectedMarketDate = $resolvedExpectedMarketDate
)
    if ([string]$BuildManifest.schema_version -cne 'dawnstrike.public_build.v1' -or
        $BuildManifest.source_clean -ne $true -or
        $BuildManifest.research_only -ne $true -or
        $BuildManifest.live_trading_enabled -ne $false -or
        $BuildManifest.broker_execution_enabled -ne $false) {
        throw "$Label build manifest safety/source boundary is invalid."
    }
    if ($BuildManifest.source_sha -ne $ExpectedSourceSha) {
        throw "$Label build source SHA does not equal the verified runtime HEAD."
    }
    Assert-VercelPublicFileHashSet -BuildManifest $BuildManifest -Label $Label
    if ($BuildManifest.market_date -notmatch '^\d{4}-\d{2}-\d{2}$') {
        throw "$Label build market date is invalid."
    }
    if ($ExpectedMarketDate -and
        [string]$BuildManifest.market_date -cne $ExpectedMarketDate) {
        throw "$Label build market date does not match ExpectedMarketDate."
    }
    foreach ($field in @(
        "publication_set_sha256", "opportunity_projection_sha256",
        "v6_learning_sha256", "build_sha"
    )) {
        Assert-LowerHex64 -Value (Get-OptionalJsonProperty -InputObject $BuildManifest -Name $field) `
            -Field $field -Label $Label
    }
    $expectedBuildSha = Get-Sha256Hex `
        "$($BuildManifest.source_sha):$($BuildManifest.publication_set_sha256):$($BuildManifest.opportunity_projection_sha256):$($BuildManifest.v6_learning_sha256):$($BuildManifest.market_date)"
    if ($BuildManifest.build_sha -ne $expectedBuildSha) {
        throw "$Label build SHA does not match the strict five-input V6 formula."
    }
    if ($BuildManifest.build_id -ne $expectedBuildSha.Substring(0, 20)) {
        throw "$Label build ID does not match the strict build SHA."
    }
    if ($Health.status -ne "alive") {
        throw "$Label health is not alive."
    }
    if ($Health.source_sha -ne $BuildManifest.source_sha) {
        throw "$Label health source SHA does not match the build manifest."
    }
    if ($Health.build_id -ne $BuildManifest.build_id) {
        throw "$Label health build ID does not match the build manifest."
    }
    $readinessSourceSha = Get-OptionalJsonProperty `
        -InputObject $Readiness `
        -Name "source_sha"
    $readinessBuildId = Get-OptionalJsonProperty `
        -InputObject $Readiness `
        -Name "build_id"
    if ($readinessSourceSha -and $readinessSourceSha -ne $BuildManifest.source_sha) {
        throw "$Label readiness source SHA does not match the build manifest."
    }
    if ($readinessBuildId -and $readinessBuildId -ne $BuildManifest.build_id) {
        throw "$Label readiness build ID does not match the build manifest."
    }
    Assert-LowerHex64 -Value (Get-OptionalJsonProperty -InputObject $Readiness -Name "v6_learning_sha256") `
        -Field "readiness.v6_learning_sha256" -Label $Label
    if ($Readiness.v6_learning_sha256 -ne $BuildManifest.v6_learning_sha256) {
        throw "$Label readiness V6 hash does not match the build manifest."
    }
    if ($Readiness.market_date -ne $BuildManifest.market_date) {
        throw "$Label readiness market date does not match the build manifest."
    }
    if ($Readiness.research_only -ne $true -or $Readiness.broker_execution_enabled -ne $false) {
        throw "$Label readiness safety boundary is not research-only with broker execution disabled."
    }
    if ($Readiness.data_hash_sha256 -ne $BuildManifest.data_hash_sha256) {
        throw "$Label readiness data hash does not match the build manifest."
    }
    if ($Readiness.publication_set_sha256 -ne $BuildManifest.publication_set_sha256) {
        throw "$Label readiness publication-set hash does not match the build manifest."
    }
    $accountSessionReport = Get-OptionalJsonProperty `
        -InputObject $Readiness -Name 'account_session_report'
    if ($null -eq $accountSessionReport) {
        throw "$Label readiness omitted the governed account-session report."
    }
    $null = Assert-VercelAccountSessionReport `
        -Report $accountSessionReport -MarketDate ([string]$BuildManifest.market_date) `
        -SourceSha ([string]$BuildManifest.source_sha) -Label $Label

    $ready = $Readiness.status -eq "ready" -and [int]$Readiness.http_status -eq 200
    $approvedDegraded = (
        $AllowDegraded -and
        $Readiness.status -eq "not_ready" -and
        [int]$Readiness.http_status -eq 503 -and
        $Readiness.snapshot_status -eq "degraded"
    )
    if (-not $ready -and -not $approvedDegraded) {
        throw "$Label readiness is neither ready nor approved degraded."
    }
    if ($null -ne $ReleaseManifest) {
        if ($ReleaseManifest.source_sha -ne $BuildManifest.source_sha -or
            $ReleaseManifest.build_sha -ne $BuildManifest.build_sha -or
            $ReleaseManifest.v6_learning_sha256 -ne $BuildManifest.v6_learning_sha256) {
            throw "$Label release manifest does not match the build manifest."
        }
        Assert-LowerHex64 -Value $ReleaseManifest.v6_learning_sha256 `
            -Field "release_manifest.v6_learning_sha256" -Label $Label
        if ([string]$BuildManifest.release_manifest_sha256 -cne
            [string]$ReleaseManifest.release_manifest_sha256) {
            throw "$Label release-manifest self hash does not match the build manifest."
        }
        $unsignedRelease = [ordered]@{}
        foreach ($property in @($ReleaseManifest.PSObject.Properties | Sort-Object Name)) {
            if ([string]$property.Name -cne 'release_manifest_sha256') {
                $unsignedRelease[[string]$property.Name] = $property.Value
            }
        }
        if ((Get-Sha256Hex (ConvertTo-VercelCanonicalJson $unsignedRelease)) -cne
            [string]$ReleaseManifest.release_manifest_sha256) {
            throw "$Label release-manifest self hash is not independently valid."
        }
        if ([string]$ReleaseManifest.deployment_boundary -cne
            'configured_runtime_and_durable_state' -or
            [string]$ReleaseManifest.deployment_boundary_sha256 -cne
            (Get-Sha256Hex "$resolvedRoot`n$resolvedStateRoot") -or
            [string]$ReleaseManifest.scheduler_version -cne 'dawnstrike-scheduler-v6' -or
            [string]$ReleaseManifest.data_watermark -cne [string]$BuildManifest.market_date -or
            $ReleaseManifest.research_only -ne $true -or
            $ReleaseManifest.broker_execution_enabled -ne $false) {
            throw "$Label release manifest governed semantics are invalid."
        }
        $expectedStrategies = ConvertTo-VercelCanonicalJson ([ordered]@{
            alphaops_v5 = 'dawnstrike-alphaops-v5.0.0'
            alphaops_v6_shadow = 'dawnstrike-alphaops-v6-shadow'
            paperops = 'immutable-strategy-semantics-manifest'
        })
        if ((ConvertTo-VercelCanonicalJson $ReleaseManifest.strategy_versions) -cne
            $expectedStrategies) {
            throw "$Label release manifest strategy versions are invalid."
        }
        $releaseHashes = Get-OptionalJsonProperty -InputObject $ReleaseManifest -Name 'artifact_hashes'
        $buildHashes = Get-OptionalJsonProperty -InputObject $BuildManifest -Name 'file_hashes'
        $expectedReleaseNames = @(
            @($buildHashes.PSObject.Properties | ForEach-Object { [string]$_.Name }) |
                Where-Object { $_ -cne 'release-manifest.json' } | Sort-Object
        )
        $observedReleaseNames = @(
            @($releaseHashes.PSObject.Properties | ForEach-Object { [string]$_.Name }) |
                Sort-Object
        )
        if ((ConvertTo-VercelCanonicalJson $expectedReleaseNames) -cne
            (ConvertTo-VercelCanonicalJson $observedReleaseNames)) {
            throw "$Label release manifest artifact-hash inventory is invalid."
        }
        foreach ($name in $expectedReleaseNames) {
            if ([string]$releaseHashes.$name -cne [string]$buildHashes.$name) {
                throw "$Label release/build artifact hash diverges: $name"
            }
        }
    }
    $embeddedReadiness = Get-OptionalJsonProperty -InputObject $BuildManifest -Name "readiness"
    if ($null -eq $embeddedReadiness -or $embeddedReadiness.research_only -ne $true -or
        $embeddedReadiness.live_trading_enabled -ne $false -or
        [string]$embeddedReadiness.status -cne [string]$Readiness.status -or
        [int]$embeddedReadiness.http_status -ne [int]$Readiness.http_status -or
        [string]$embeddedReadiness.market_date -cne [string]$Readiness.market_date) {
        throw "$Label embedded build readiness does not match the live readiness contract."
    }
    if ((ConvertTo-VercelCanonicalJson $embeddedReadiness.account_session_report) -cne
        (ConvertTo-VercelCanonicalJson $accountSessionReport)) {
        throw "$Label embedded/live account-session evidence diverges."
    }
}

function Assert-ProductionDateLineage {
    param(
        [Parameter(Mandatory = $true)][object]$CurrentBuildManifest,
        [Parameter(Mandatory = $true)][object]$CandidateBuildManifest,
        [Parameter(Mandatory = $true)][object]$CurrentReleaseManifest,
        [Parameter(Mandatory = $true)][object]$CandidateReleaseManifest
    )
    $currentDate = [string](Get-OptionalJsonProperty -InputObject $CurrentBuildManifest -Name "market_date")
    $candidateDate = [string](Get-OptionalJsonProperty -InputObject $CandidateBuildManifest -Name "market_date")
    try {
        $current = [DateTime]::ParseExact($currentDate, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
        $candidate = [DateTime]::ParseExact($candidateDate, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
    } catch { throw "Production lineage market date is invalid." }
    if ($candidate -lt $current) {
        throw "Production promotion is regressive: candidate market date is older than the served build."
    }
    if ($candidate -eq $current) {
        $sameLineage = (
            [string]$CurrentBuildManifest.source_sha -ceq [string]$CandidateBuildManifest.source_sha -and
            [string]$CurrentBuildManifest.build_sha -ceq [string]$CandidateBuildManifest.build_sha -and
            [string]$CurrentBuildManifest.publication_set_sha256 -ceq [string]$CandidateBuildManifest.publication_set_sha256 -and
            [string]$CurrentBuildManifest.release_manifest_sha256 -ceq [string]$CandidateBuildManifest.release_manifest_sha256
        )
        if (-not $sameLineage) {
            throw "Production promotion cannot replace a frozen same-day lineage with divergent hashes."
        }
    }
    foreach ($item in @(
        @{ name = "served"; value = $CurrentReleaseManifest.data_watermark; expected = $currentDate },
        @{ name = "candidate"; value = $CandidateReleaseManifest.data_watermark; expected = $candidateDate }
    )) {
        if ($item.value -and [string]$item.value -notmatch '^\d{4}-\d{2}-\d{2}$') {
            throw "Production $($item.name) release manifest date is invalid."
        }
        if ($item.value -and [string]$item.value -ne [string]$item.expected) {
            throw "Production $($item.name) build/release manifest dates diverge."
        }
    }
}

function Get-VercelAliasObservation {
    param([Parameter(Mandatory = $true)][string]$Alias)
    if ($Alias -notin $allProductionAliases) {
        throw 'Publication alias escaped the governed alias set.'
    }
    $aliasUri = [Uri]::new($Alias, [UriKind]::Absolute)
    $aliasHost = $aliasUri.DnsSafeHost.ToLowerInvariant()
    $encodedAlias = [Uri]::EscapeDataString($aliasHost)
    $observed = Invoke-VercelApiReadJson `
        -RelativeUri ("/v4/aliases/$encodedAlias`?projectId=$governedProjectId" +
            "&teamId=$governedProviderTeamId") `
        -Label "Publication alias inspect for $Alias"
    if ([string](Get-OptionalJsonProperty -InputObject $observed -Name 'alias') -cne $aliasHost -or
        [string](Get-OptionalJsonProperty -InputObject $observed -Name 'projectId') -cne
            $governedProjectId -or
        $null -ne (Get-OptionalJsonProperty -InputObject $observed -Name 'redirect')) {
        throw "Publication alias inspect returned a foreign or redirected alias for $Alias."
    }
    $id = [string](Get-OptionalJsonProperty -InputObject $observed -Name "deploymentId")
    $deployment = Get-OptionalJsonProperty -InputObject $observed -Name 'deployment'
    if (-not $id) { $id = [string](Get-OptionalJsonProperty -InputObject $deployment -Name 'id') }
    $remoteDeployment = Get-VercelFrozenDeployment -DeploymentId $id
    $urlRaw = [string](Get-OptionalJsonProperty -InputObject $remoteDeployment -Name "url")
    $url = if ($urlRaw) {
        Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl $urlRaw
    } else { '' }
    if ($id -cnotmatch '^dpl_[A-Za-z0-9]{20,64}$' -or -not $url -or
        [string](Get-OptionalJsonProperty -InputObject $remoteDeployment -Name 'id') -cne $id -or
        [string](Get-OptionalJsonProperty -InputObject $remoteDeployment -Name 'ownerId') -cne
            $governedProviderTeamId -or
        [string](Get-OptionalJsonProperty -InputObject $remoteDeployment -Name 'projectId') -cne
            $governedProjectId -or
        [string](Get-OptionalJsonProperty -InputObject $remoteDeployment -Name 'readyState') -cne
            'READY') {
        throw "Publication alias inspect is incomplete or foreign for $Alias."
    }
    return [pscustomobject]@{ alias = $Alias; id = $id; url = $url }
}

function Get-VercelProductionDeployments {
    $response = Invoke-VercelApiReadJson `
        -RelativeUri ("/v7/deployments?projectId=$governedProjectId" +
            "&target=production&limit=20&teamId=$governedProviderTeamId") `
        -Label 'Governed Vercel production deployment list' `
        -MaximumResponseBytes 8388608
    $deployments = @(Get-OptionalJsonProperty -InputObject $response -Name 'deployments')
    if ($null -eq $deployments) {
        throw 'Governed Vercel production deployment list is malformed.'
    }
    foreach ($item in $deployments) {
        if ([string](Get-OptionalJsonProperty -InputObject $item -Name 'projectId') -cne
                $governedProjectId -or
            [string](Get-OptionalJsonProperty -InputObject $item -Name 'ownerId') -cne
                $governedProviderTeamId) {
            throw 'Governed Vercel production deployment list contains a foreign entry.'
        }
    }
    return @($deployments)
}

function Test-VercelAliasSetMatches {
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][ValidateSet("prior", "candidate")][string]$Kind
    )
    $observed = @($allProductionAliases | ForEach-Object { Get-VercelAliasObservation ([string]$_) })
    $matches = $true
    foreach ($item in $observed) {
        if ($Kind -eq "prior") {
            $expected = @($Journal.prior_aliases | Where-Object { [string]$_.alias -eq [string]$item.alias })[0]
            if ($null -eq $expected -or [string]$expected.deployment_id -ne [string]$item.id -or
                (Normalize-VercelDeploymentUrl $expected.deployment_url) -ne (Normalize-VercelDeploymentUrl $item.url)) {
                $matches = $false
            }
        }
        else {
            if ([string]$Journal.promoted_deployment_id -ne [string]$item.id -or
                (Normalize-VercelDeploymentUrl $Journal.promoted_deployment_url) -ne (Normalize-VercelDeploymentUrl $item.url)) {
                $matches = $false
            }
        }
    }
    return [bool]$matches
}

function Test-VercelAliasSourceMatchesJournal {
    param([Parameter(Mandatory = $true)][object]$Journal)
    foreach ($alias in $allProductionAliases) {
        try {
            $proof = Get-VercelAliasEndpointProof -AliasUrl ([string]$alias) -CacheBuster ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
            if (-not $proof.source_manifest_available -or
                [string]$proof.source_sha -ne [string]$Journal.candidate_source_sha -or
                [string]$proof.source_tree -ne [string]$Journal.candidate_source_tree -or
                [string]$proof.source_manifest_sha256 -ne [string]$Journal.candidate_manifest_sha256) { return $false }
        }
        catch { return $false }
    }
    return $true
}

function New-VercelPublicationJournalPayload {
    param(
        [Parameter(Mandatory = $true)][string]$Phase,
        [Parameter(Mandatory = $true)][int]$Sequence,
        [Parameter(Mandatory = $true)][object]$CandidateDeployment,
        [Parameter(Mandatory = $true)][object]$PreviewManifest,
        [Parameter(Mandatory = $true)][string]$PackageManifestSha256,
        [Parameter(Mandatory = $true)][string]$PackageMapSha256,
        [Parameter(Mandatory = $true)][string]$RemoteFileAttestationSha256,
        [string]$PromotedRemoteFileAttestationSha256 = $emptySha256,
        [Parameter(Mandatory = $true)][string]$DeploymentOperationId,
        [Parameter(Mandatory = $true)][string]$DeploymentRequestSha256,
        [Parameter(Mandatory = $true)][string]$CandidateBuildManifestSha256,
        [Parameter(Mandatory = $true)][string]$CandidateReleaseManifestSha256,
        [Parameter(Mandatory = $true)][string]$CandidatePublicArtifactRootSha256,
        [string]$CandidateManifestSha256 = "",
        [Parameter(Mandatory = $true)][object[]]$PriorAliases,
        [AllowNull()][object]$PromotedDeployment,
        [AllowNull()][object]$ResultPayload,
        [string]$ExpectedPublicationMarketDate = "",
        [string]$PrepublicationAuthorization = "",
        [string]$DailyLedgerAuthorization = "",
        [string]$CandidateSourceTree = $expectedSourceTree,
        [string]$ResultRelativePath = $resultRelativePath,
        [string]$PriorJournalHash = $emptySha256,
        [string]$CompensationRelativePath = "NONE",
        [string]$CompensationSha256 = $emptySha256
    )
    $currentRollbackContracts = @($PriorAliases | Where-Object {
        Test-VercelObjectProperty -InputObject $_ -Name 'rollback_contract'
    }).Count
    if ($currentRollbackContracts -notin @(0, $PriorAliases.Count)) {
        throw 'Vercel publication journal cannot mix legacy and current rollback contracts.'
    }
    $currentJournalSchema = $currentRollbackContracts -eq $PriorAliases.Count
    $resultHash = if ($null -eq $ResultPayload) { $emptySha256 } else { Get-VercelResultSha256 $ResultPayload }
    if ($currentJournalSchema) {
        foreach ($hashValue in @(
            $PackageMapSha256,
            $RemoteFileAttestationSha256,
            $PromotedRemoteFileAttestationSha256,
            $DeploymentRequestSha256
        )) {
            if ($hashValue -cnotmatch '^[0-9a-f]{64}$') {
                throw 'Vercel publication CAS evidence contains an invalid hash.'
            }
        }
        if ($DeploymentOperationId -cnotmatch '^[0-9a-f]{32}$') {
            throw 'Vercel publication CAS operation identity is invalid.'
        }
    }
    $payload = [ordered]@{
        schema_version = if ($Phase -eq 'COMPENSATED') {
            if ($currentJournalSchema) { 'dawnstrike.vercel_publication_journal.v8' }
            else { 'dawnstrike.vercel_publication_journal.v2' }
        }
        else {
            if ($currentJournalSchema) { 'dawnstrike.vercel_publication_journal.v7' }
            else { 'dawnstrike.vercel_publication_journal.v1' }
        }
        operation = "vercel_publication"
        phase = $Phase
        sequence = $Sequence
        project_id = $ProjectId
        project_name = $ProjectName
        provider_scope = $ProviderScope
        production_aliases = @($allProductionAliases)
        candidate_preview_url = Get-VercelImmutableDeploymentBaseUrl `
            -DeploymentUrl ([string]$CandidateDeployment.url)
        candidate_preview_deployment_id = [string]$CandidateDeployment.id
        candidate_source_sha = [string]$PreviewManifest.source_sha
        candidate_source_tree = $CandidateSourceTree
        toolchain_identity_sha256 = $toolchainIdentitySha256
        candidate_market_date = [string]$PreviewManifest.market_date
        candidate_build_id = [string]$PreviewManifest.build_id
        candidate_build_sha = [string]$PreviewManifest.build_sha
        candidate_build_manifest_sha256 = $CandidateBuildManifestSha256
        candidate_release_manifest_sha256 = $CandidateReleaseManifestSha256
        candidate_public_artifact_root_sha256 = $CandidatePublicArtifactRootSha256
        candidate_manifest_sha256 = if ($CandidateManifestSha256) { $CandidateManifestSha256 } else {
            Get-Sha256Hex (Get-VercelSourceManifestCanonicalJson -Path (Join-Path $stage "vercel-source-manifest.json"))
        }
        candidate_package_manifest_sha256 = $PackageManifestSha256
        prior_aliases = @($PriorAliases)
        promoted_deployment_id = if ($null -eq $PromotedDeployment) { $null } else { [string]$PromotedDeployment.id }
        promoted_deployment_url = if ($null -eq $PromotedDeployment) { $null } else {
            Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl ([string]$PromotedDeployment.url)
        }
        production_result_sha256 = $resultHash
        result_relative_path = $ResultRelativePath
        result_payload = $ResultPayload
        prior_journal_file_sha256 = $PriorJournalHash
        compensation_relative_path = $CompensationRelativePath
        compensation_sha256 = $CompensationSha256
        recorded_at_utc = [DateTimeOffset]::UtcNow.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'")
        research_only = $true
        broker_execution_enabled = $false
    }
    if ($currentJournalSchema) {
        $payload.provider_team_id = $ProviderTeamId
        $payload.candidate_deployment_operation_id = $DeploymentOperationId
        $payload.candidate_deployment_request_sha256 = $DeploymentRequestSha256
        $payload.candidate_package_map_sha256 = $PackageMapSha256
        $payload.candidate_remote_file_attestation_sha256 = $RemoteFileAttestationSha256
        $payload.promoted_remote_file_attestation_sha256 = `
            $PromotedRemoteFileAttestationSha256
    }
    if ($ExpectedPublicationMarketDate -or $PrepublicationAuthorization -or $DailyLedgerAuthorization) {
        if (-not $ExpectedPublicationMarketDate -or
            -not $PrepublicationAuthorization -or
            -not $DailyLedgerAuthorization) {
            throw "Vercel publication journal authorization identity is incomplete."
        }
        $payload.expected_market_date = $ExpectedPublicationMarketDate
        $payload.prepublication_authorization_id = $PrepublicationAuthorization
        $payload.daily_ledger_authorization_id = $DailyLedgerAuthorization
    }
    return $payload
}

function Get-VercelJournalCandidateDeployment {
    param([Parameter(Mandatory = $true)][object]$Journal)
    if ($Journal.promoted_deployment_id -and $Journal.promoted_deployment_url) {
        return [pscustomobject]@{
            id = [string]$Journal.promoted_deployment_id
            url = [string]$Journal.promoted_deployment_url
        }
    }
    $matches = @(Get-VercelProductionDeployments |
        Where-Object {
            $metadata = Get-OptionalJsonProperty -InputObject $_ -Name "meta"
            [string](Get-OptionalJsonProperty -InputObject $_ -Name "target") -ceq "production" -and
            [string](Get-OptionalJsonProperty -InputObject $metadata -Name "action") -ceq "promote" -and
            [string](Get-OptionalJsonProperty -InputObject $metadata -Name "originalDeploymentId") -ceq
                [string]$Journal.candidate_preview_deployment_id
        })
    if ($matches.Count -gt 1) {
        throw "Multiple provider deployments claim the interrupted preview promotion."
    }
    if ($matches.Count -eq 0) { return $null }
    $id = [string](Get-OptionalJsonProperty -InputObject $matches[0] -Name "id")
    $urlRaw = [string](Get-OptionalJsonProperty -InputObject $matches[0] -Name "url")
    $url = if ($urlRaw) {
        Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl $urlRaw
    } else { '' }
    if (-not $id -or -not $url) {
        throw "Interrupted provider promotion metadata is incomplete."
    }
    return [pscustomobject]@{ id = $id; url = $url }
}

function Get-VercelCompensationPlan {
    param([Parameter(Mandatory = $true)][object]$Journal)
    $records = @()
    foreach ($alias in $allProductionAliases) {
        $prior = @($Journal.prior_aliases | Where-Object {
            [string]$_.alias -eq [string]$alias
        })[0]
        if ($null -eq $prior) { throw "No exact prior snapshot exists for $alias." }
        $observed = Get-VercelAliasObservation ([string]$alias)
        $isPrior = [string]$observed.id -ceq [string]$prior.deployment_id -and
            (Normalize-VercelDeploymentUrl $observed.url) -ceq
                (Normalize-VercelDeploymentUrl $prior.deployment_url)
        $records += [pscustomobject]@{
            alias = [string]$alias
            prior = $prior
            observed = $observed
            state = if ($isPrior) { "prior" } else { "unresolved" }
        }
    }
    $unresolved = @($records | Where-Object { $_.state -eq "unresolved" })
    $candidate = if ($unresolved.Count) {
        Get-VercelJournalCandidateDeployment -Journal $Journal
    } else { $null }
    foreach ($record in $unresolved) {
        $isCandidate = $null -ne $candidate -and
            [string]$record.observed.id -ceq [string]$candidate.id -and
            (Normalize-VercelDeploymentUrl $record.observed.url) -ceq
                (Normalize-VercelDeploymentUrl $candidate.url)
        $record.state = if ($isCandidate) { "candidate" } else { "foreign" }
    }
    return [pscustomobject]@{
        records = @($records)
        candidate = $candidate
        foreign_count = @($records | Where-Object { $_.state -eq "foreign" }).Count
        candidate_count = @($records | Where-Object { $_.state -eq "candidate" }).Count
    }
}

function Invoke-VercelPublicationCompensation {
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][string]$FailureType
    )
    $plan = Get-VercelCompensationPlan -Journal $Journal
    if ($plan.foreign_count -gt 0) {
        throw "Vercel compensation found a foreign alias deployment; no provider state was changed."
    }
    $errors = @()
    $rollbackEvidence = @()
    $primaryRecord = @($plan.records | Where-Object {
        [string]$_.alias -eq [string]$ProductionAlias
    })[0]
    if ($null -ne $primaryRecord -and [string]$primaryRecord.state -eq "candidate") {
        $primaryNow = Get-VercelAliasObservation ([string]$ProductionAlias)
        if ([string]$primaryNow.id -cne [string]$primaryRecord.observed.id -or
            (Normalize-VercelDeploymentUrl $primaryNow.url) -cne
                (Normalize-VercelDeploymentUrl $primaryRecord.observed.url)) {
            throw "Primary production alias changed before compensation; no rollback was attempted."
        }
        Request-VercelProductionRollback `
            -DeploymentId ([string]$primaryRecord.prior.deployment_id)
    }
    foreach ($record in @($plan.records)) {
        $alias = [string]$record.alias
        try {
            $prior = $record.prior
            $current = Get-VercelAliasObservation $alias
            $currentIsPrior = [string]$current.id -ceq [string]$prior.deployment_id -and
                (Normalize-VercelDeploymentUrl $current.url) -ceq
                    (Normalize-VercelDeploymentUrl $prior.deployment_url)
            $currentIsCandidate = $null -ne $plan.candidate -and
                [string]$current.id -ceq [string]$plan.candidate.id -and
                (Normalize-VercelDeploymentUrl $current.url) -ceq
                    (Normalize-VercelDeploymentUrl $plan.candidate.url)
            if (-not $currentIsPrior -and -not $currentIsCandidate) {
                throw "Alias changed to a foreign deployment before compensation."
            }
            if ($currentIsCandidate) {
                Set-VercelAlias -DeploymentId ([string]$prior.deployment_id) `
                    -DeploymentUrl ([string]$prior.deployment_url) `
                    -AliasUrl $alias -Label "Compensation rollback for $alias"
            }
            $rollbackEvidence += Assert-VercelAliasRestored `
                -AliasUrl $alias -PriorAlias $prior `
                -CacheBuster ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        }
        catch { $errors += "${alias}: $($_.Exception.Message)" }
    }
    if ($errors.Count -gt 0) { throw "Vercel publication compensation failed: $($errors -join '; ')" }
    if (-not (Test-VercelAliasSetMatches -Journal $Journal -Kind prior)) {
        throw "Vercel publication compensation aliases changed before terminal evidence."
    }
    $journalPromotedFileAttestation = if (Test-VercelObjectProperty `
            -InputObject $Journal -Name 'promoted_remote_file_attestation_sha256') {
        [string]$Journal.promoted_remote_file_attestation_sha256
    }
    else { $emptySha256 }
    $compensation = [ordered]@{
        schema_version = if ([string]$Journal.schema_version -in @(
            'dawnstrike.vercel_publication_journal.v3',
            'dawnstrike.vercel_publication_journal.v4',
            'dawnstrike.vercel_publication_journal.v7',
            'dawnstrike.vercel_publication_journal.v8'
        )) { 'dawnstrike.vercel_publication_compensation.v2' }
        else { 'dawnstrike.vercel_publication_compensation.v1' }
        status = "COMPENSATED"
        operation = "vercel_publication"
        candidate_source_sha = [string]$Journal.candidate_source_sha
        candidate_source_tree = [string]$Journal.candidate_source_tree
        candidate_preview_deployment_id = [string]$Journal.candidate_preview_deployment_id
        promoted_deployment_id = if ($Journal.promoted_deployment_id) { [string]$Journal.promoted_deployment_id } else { $null }
        promoted_deployment_url = if ($Journal.promoted_deployment_url) { [string]$Journal.promoted_deployment_url } else { $null }
        prior_aliases = @($Journal.prior_aliases)
        rollback_evidence = @($rollbackEvidence | Sort-Object -Property alias)
        rollback_status = "ROLLED_BACK"
        failure_type = $FailureType
        research_only = $true
        broker_execution_enabled = $false
        recorded_at_utc = [DateTimeOffset]::UtcNow.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'")
    }
    $compensation.receipt_self_sha256 = Get-VercelResultSha256 $compensation
    $compensationJson = ConvertTo-VercelCanonicalJson $compensation
    $compensationPath = Join-Path $journalRoot ("vercel-publication-compensation-" + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + ".json")
    Assert-VercelContainedNonReparsePath -RootPath $resolvedStateRoot -TargetPath $compensationPath
    [System.IO.File]::WriteAllText($compensationPath, $compensationJson, (New-Object System.Text.UTF8Encoding($false)))
    $null = Invoke-VercelJournalTool `
        -Arguments @("verify-compensation", $compensationPath, "--state-root", $resolvedStateRoot) `
        -Label "Vercel publication compensation verification"
    $compensationHash = Get-VercelFileSha256 -Path $compensationPath
    $invalidatedResult = [ordered]@{
        schema_version = 'dawnstrike.daily_deployment_compensated.v1'
        status = 'COMPENSATED'
        market_date = [string]$Journal.candidate_market_date
        candidate_source_sha = [string]$Journal.candidate_source_sha
        candidate_source_tree = [string]$Journal.candidate_source_tree
        candidate_preview_deployment_id = [string]$Journal.candidate_preview_deployment_id
        compensation_sha256 = $compensationHash
        research_only = $true
        broker_execution_enabled = $false
    }
    # Invalidate both previously written success copies before sealing the
    # terminal compensation journal. A retry can safely repeat this exact write.
    Write-VercelResultAtomic -Payload $invalidatedResult
    $next = New-VercelPublicationJournalPayload `
        -Phase "COMPENSATED" -Sequence 3 `
        -CandidateDeployment ([pscustomobject]@{ url = $Journal.candidate_preview_url; id = $Journal.candidate_preview_deployment_id }) `
        -PreviewManifest ([pscustomobject]@{ source_sha = $Journal.candidate_source_sha; market_date = $Journal.candidate_market_date; build_id = $Journal.candidate_build_id; build_sha = $Journal.candidate_build_sha }) `
        -PackageManifestSha256 ([string]$Journal.candidate_package_manifest_sha256) `
        -PackageMapSha256 ([string]$Journal.candidate_package_map_sha256) `
        -RemoteFileAttestationSha256 ([string]$Journal.candidate_remote_file_attestation_sha256) `
        -PromotedRemoteFileAttestationSha256 $journalPromotedFileAttestation `
        -DeploymentOperationId ([string]$Journal.candidate_deployment_operation_id) `
        -DeploymentRequestSha256 ([string]$Journal.candidate_deployment_request_sha256) `
        -CandidateBuildManifestSha256 ([string]$Journal.candidate_build_manifest_sha256) `
        -CandidateReleaseManifestSha256 ([string]$Journal.candidate_release_manifest_sha256) `
        -CandidatePublicArtifactRootSha256 ([string]$Journal.candidate_public_artifact_root_sha256) `
        -CandidateManifestSha256 ([string]$Journal.candidate_manifest_sha256) `
        -PriorAliases @($Journal.prior_aliases) `
        -PromotedDeployment (if ($Journal.promoted_deployment_id) { [pscustomobject]@{ id = $Journal.promoted_deployment_id; url = $Journal.promoted_deployment_url } } else { $null }) `
        -ResultPayload $invalidatedResult `
        -ExpectedPublicationMarketDate ([string]$Journal.expected_market_date) `
        -PrepublicationAuthorization ([string]$Journal.prepublication_authorization_id) `
        -DailyLedgerAuthorization ([string]$Journal.daily_ledger_authorization_id) `
        -CandidateSourceTree ([string]$Journal.candidate_source_tree) `
        -ResultRelativePath ([string]$Journal.result_relative_path) `
        -PriorJournalHash (Get-Sha256Hex ([System.IO.File]::ReadAllText($journalPath))) `
        -CompensationRelativePath (Get-VercelStateRelativePath `
            -StateRootPath $resolvedStateRoot -TargetPath $compensationPath) `
        -CompensationSha256 $compensationHash
    if (-not (Test-VercelAliasSetMatches -Journal $Journal -Kind prior)) {
        throw "Vercel publication compensation aliases changed at terminal sealing."
    }
    $null = Write-VercelPublicationJournal -Payload $next -Transition
}

function Get-VercelJournalPreviewEvidence {
    param([Parameter(Mandatory = $true)][object]$Journal, [switch]$UsePromoted)
    if ($UsePromoted -and
        -not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $Journal)) {
        throw 'Recovery promoted deployment failed exact full-file attestation.'
    }
    $previewUrl = if ($UsePromoted -and $Journal.promoted_deployment_url) {
        [string]$Journal.promoted_deployment_url
    }
    else { [string]$Journal.candidate_preview_url }
    Assert-VercelAuthorizedManifestBytes `
        -BaseUrl $previewUrl `
        -ExpectedBuildManifestSha256 ([string]$Journal.candidate_build_manifest_sha256) `
        -ExpectedReleaseManifestSha256 ([string]$Journal.candidate_release_manifest_sha256) `
        -Label "Recovery preview"
    $health = Invoke-VercelJson -Arguments @("curl", "$previewUrl/api/health?recovery_verify=1") -Label "Recovery preview health"
    $readiness = Invoke-VercelJson -Arguments @("curl", "$previewUrl/api/readiness?recovery_verify=1") -Label "Recovery preview readiness"
    $manifest = Invoke-VercelJson -Arguments @("curl", "$previewUrl/build-manifest.json?recovery_verify=1") -Label "Recovery preview build manifest"
    $release = Invoke-VercelJson -Arguments @("curl", "$previewUrl/release-manifest.json?recovery_verify=1") -Label "Recovery preview release manifest"
    Assert-VercelJournalSourceManifestLive -BaseUrl $previewUrl -Journal $Journal -Label "Recovery preview"
    Assert-PublicationState -Health $health -Readiness $readiness -BuildManifest $manifest -ReleaseManifest $release -ExpectedSourceSha ([string]$Journal.candidate_source_sha) -Label "Recovery preview"
    if ([string]$manifest.source_sha -ne [string]$Journal.candidate_source_sha -or
        [string]$manifest.build_id -ne [string]$Journal.candidate_build_id -or
        [string]$manifest.build_sha -ne [string]$Journal.candidate_build_sha -or
        [string]$manifest.market_date -ne [string]$Journal.candidate_market_date) {
        throw "Recovery preview identity does not match the journal candidate."
    }
    return [pscustomobject]@{
        url = $previewUrl
        id = [string]$Journal.candidate_preview_deployment_id
        health = $health
        readiness = $readiness
        manifest = $manifest
        release = $release
    }
}

function New-VercelRecoveredResultPayload {
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][object]$Live,
        [Parameter(Mandatory = $true)][object]$Health,
        [Parameter(Mandatory = $true)][object]$Readiness,
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][object]$ReleaseManifest
    )
    Assert-VercelAuthorizedManifestBytes `
        -BaseUrl ([string]$Journal.candidate_preview_url) `
        -ExpectedBuildManifestSha256 ([string]$Journal.candidate_build_manifest_sha256) `
        -ExpectedReleaseManifestSha256 ([string]$Journal.candidate_release_manifest_sha256) `
        -Label "Recovered preview"
    Assert-PublicationState -Health $Health -Readiness $Readiness -BuildManifest $Manifest -ReleaseManifest $ReleaseManifest -ExpectedSourceSha ([string]$Journal.candidate_source_sha) -Label "Recovered production"
    $previewManifest = Invoke-VercelJson `
        -Arguments @("curl", "$($Journal.candidate_preview_url)/build-manifest.json?recovery_asset_verify=1") `
        -Label "Recovered preview governed asset manifest"
    if ([string]$previewManifest.build_sha -cne [string]$Manifest.build_sha -or
        (ConvertTo-VercelCanonicalJson $previewManifest) -cne
        (ConvertTo-VercelCanonicalJson $Manifest)) {
        throw "Recovered preview governed asset manifest diverges from live production."
    }
    $previewArtifactProof = Get-VercelGovernedAssetProof `
        -BaseUrl ([string]$Journal.candidate_preview_url) `
        -BuildManifest $previewManifest -Label "Recovered preview"
    $productionArtifactProofs = @()
    foreach ($alias in @($Journal.production_aliases)) {
        Assert-VercelAuthorizedManifestBytes `
            -BaseUrl ([string]$alias) `
            -ExpectedBuildManifestSha256 ([string]$Journal.candidate_build_manifest_sha256) `
            -ExpectedReleaseManifestSha256 ([string]$Journal.candidate_release_manifest_sha256) `
            -Label "Recovered production alias $alias"
        Assert-VercelJournalSourceManifestLive -BaseUrl ([string]$alias) -Journal $Journal `
            -Label "Recovered production alias $alias"
        $aliasManifest = Invoke-VercelJson `
            -Arguments @("curl", "$alias/build-manifest.json?recovery_asset_verify=1") `
            -Label "Recovered production alias governed asset manifest for $alias"
        if ([string]$aliasManifest.build_sha -cne [string]$Manifest.build_sha -or
            (ConvertTo-VercelCanonicalJson $aliasManifest) -cne
            (ConvertTo-VercelCanonicalJson $Manifest)) {
            throw "Recovered production alias governed asset manifest diverges for $alias."
        }
        $productionArtifactProofs += Get-VercelGovernedAssetProof `
            -BaseUrl ([string]$alias) -BuildManifest $aliasManifest `
            -Label "Recovered production alias $alias"
    }
    return [ordered]@{
        schema_version = "dawnstrike.daily_deployment.v1"
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        project_id = [string]$Journal.project_id
        provider_scope = [string]$Journal.provider_scope
        provider_team_id = [string]$Journal.provider_team_id
        preview_url = [string]$Journal.candidate_preview_url
        preview_deployment_id = [string]$Journal.candidate_preview_deployment_id
        preview_ready_state = "READY"
        source_sha = [string]$Manifest.source_sha
        source_tree = [string]$Journal.candidate_source_tree
        vercel_source_manifest_sha256 = [string]$Journal.candidate_manifest_sha256
        vercel_package_manifest_sha256 = [string]$Journal.candidate_package_manifest_sha256
        vercel_package_map_sha256 = [string]$Journal.candidate_package_map_sha256
        vercel_remote_file_attestation_sha256 = [string]$Journal.candidate_remote_file_attestation_sha256
        vercel_promoted_remote_file_attestation_sha256 = `
            [string]$Journal.promoted_remote_file_attestation_sha256
        vercel_deployment_operation_id = [string]$Journal.candidate_deployment_operation_id
        vercel_deployment_request_sha256 = [string]$Journal.candidate_deployment_request_sha256
        authorized_build_manifest_sha256 = [string]$Journal.candidate_build_manifest_sha256
        authorized_release_manifest_sha256 = [string]$Journal.candidate_release_manifest_sha256
        public_artifact_root_sha256 = [string]$Journal.candidate_public_artifact_root_sha256
        account_session_report = $Readiness.account_session_report
        account_session_report_sha256 = Get-Sha256Hex (ConvertTo-VercelCanonicalJson $Readiness.account_session_report)
        toolchain_identity_sha256 = [string]$Journal.toolchain_identity_sha256
        build_id = [string]$Manifest.build_id
        build_sha = [string]$Manifest.build_sha
        build_manifest_sha256 = Get-Sha256Hex (ConvertTo-VercelCanonicalJson $Manifest)
        data_hash_sha256 = [string]$Manifest.data_hash_sha256
        publication_set_sha256 = [string]$Manifest.publication_set_sha256
        opportunity_projection_sha256 = [string]$Manifest.opportunity_projection_sha256
        v6_learning_sha256 = [string]$Manifest.v6_learning_sha256
        preview_artifact_proof = $previewArtifactProof
        production_artifact_proofs = @($productionArtifactProofs)
        release_manifest_sha256 = Get-OptionalJsonProperty -InputObject $ReleaseManifest -Name "release_manifest_sha256"
        market_date = [string]$Manifest.market_date
        snapshot_status = [string]$Readiness.snapshot_status
        readiness_status = [string]$Readiness.status
        readiness_http_status = $Readiness.http_status
        allow_degraded = $false
        promoted = $true
        expected_market_date = [string]$Journal.candidate_market_date
        prepublication_authorization_id = [string]$Journal.prepublication_authorization_id
        daily_ledger_authorization_id = [string]$Journal.daily_ledger_authorization_id
        prior_production_deployment_id = @($Journal.prior_aliases | Where-Object { [string]$_.alias -eq [string]$ProductionAlias })[0].deployment_id
        production_aliases = @($allProductionAliases)
        promoted_deployment_id = [string]$Live.id
        production_deployment_id = [string]$Live.id
        live_trading_enabled = $false
        broker_execution_enabled = $false
        research_only = $true
        status = "PRODUCTION_VERIFIED"
    }
}

function Complete-VercelJournalRecovery {
    param([Parameter(Mandatory = $true)][object]$Journal)
    $null = Assert-VercelJournalFrozenPreviewEvidence -Journal $Journal
    if (-not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $Journal)) {
        throw 'Recovery promoted deployment failed exact full-file attestation before completion.'
    }
    $evidence = Get-VercelJournalPreviewEvidence -Journal $Journal -UsePromoted
    $live = Get-VercelAliasObservation ([string]$ProductionAlias)
    $freshResult = New-VercelRecoveredResultPayload `
        -Journal $Journal -Live $live -Health $evidence.health `
        -Readiness $evidence.readiness -Manifest $evidence.manifest `
        -ReleaseManifest $evidence.release
    if (-not (Test-VercelAliasSetMatches -Journal $Journal -Kind candidate)) {
        throw "Recovery candidate aliases changed before COMPLETE sealing."
    }
    $freshHash = Get-VercelResultSha256 $freshResult
    Repair-VercelResultCopies -Payload $freshResult -ExpectedSha256 $freshHash
    Test-VercelPromotionSeam "after_result_write_before_complete"
    $complete = [ordered]@{}
    foreach ($property in $Journal.PSObject.Properties) { $complete[$property.Name] = $property.Value }
    $complete.phase = "COMPLETE"
    $complete.sequence = 2
    $complete.result_payload = $freshResult
    $complete.production_result_sha256 = $freshHash
    $complete.prior_journal_file_sha256 = Get-VercelFileSha256 -Path $journalPath
    $complete.recorded_at_utc = [DateTimeOffset]::UtcNow.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'")
    if (-not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $Journal)) {
        throw "Recovery candidate aliases or promotion metadata changed at COMPLETE sealing."
    }
    $null = Write-VercelPublicationJournal -Payload $complete -Transition
    return (Get-Content -Raw -LiteralPath $resultPath)
}

function Resolve-VercelCompletePublicationJournal {
    param([Parameter(Mandatory = $true)][object]$Journal)
    if ($Journal.result_payload.promoted -ne $true -or
        $Journal.result_payload.allow_degraded -ne $false) {
        throw "Complete Vercel recovery journal authorization is invalid."
    }
    if ([string]$Journal.schema_version -in @(
        'dawnstrike.vercel_publication_journal.v7',
        'dawnstrike.vercel_publication_journal.v8'
    )) {
        $null = Assert-VercelJournalFrozenPreviewEvidence -Journal $Journal
    }
    if (-not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $Journal)) {
        throw "Complete Vercel publication journal does not match the full attested live deployment."
    }
    $completeEvidence = Get-VercelJournalPreviewEvidence -Journal $Journal -UsePromoted
    $completeLive = Get-VercelAliasObservation ([string]$ProductionAlias)
    $completeFresh = New-VercelRecoveredResultPayload `
        -Journal $Journal -Live $completeLive `
        -Health $completeEvidence.health -Readiness $completeEvidence.readiness `
        -Manifest $completeEvidence.manifest -ReleaseManifest $completeEvidence.release
    if (-not (Test-VercelAliasSetMatches -Journal $Journal -Kind candidate)) {
        throw "Complete Vercel aliases changed during terminal evidence verification."
    }
    foreach ($field in @(
        "source_sha", "source_tree", "market_date", "build_id", "build_sha",
        "build_manifest_sha256", "authorized_build_manifest_sha256",
        "authorized_release_manifest_sha256", "toolchain_identity_sha256",
        "production_deployment_id", "production_deployment_url",
        "provider_team_id", "vercel_deployment_operation_id",
        "vercel_deployment_request_sha256", "vercel_package_map_sha256",
        "vercel_remote_file_attestation_sha256",
        "vercel_promoted_remote_file_attestation_sha256"
    )) {
        if ([string]$completeFresh[$field] -cne [string]$Journal.result_payload.$field) {
            throw "Complete Vercel journal live verification diverges at $field."
        }
    }
    if (
        (ConvertTo-VercelCanonicalJson $completeFresh.preview_artifact_proof) -cne
            (ConvertTo-VercelCanonicalJson $Journal.result_payload.preview_artifact_proof) -or
        (ConvertTo-VercelCanonicalJson @($completeFresh.production_artifact_proofs)) -cne
            (ConvertTo-VercelCanonicalJson @($Journal.result_payload.production_artifact_proofs))
    ) {
        throw "Complete Vercel journal live governed asset proof diverges."
    }
    Repair-VercelResultCopies -Payload $Journal.result_payload `
        -ExpectedSha256 ([string]$Journal.production_result_sha256)
    if (-not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $Journal)) {
        throw "Complete Vercel aliases or promotion metadata changed after result repair."
    }
    return (Get-Content -Raw -LiteralPath $resultPath)
}

$recoveryRetry = $false
$existingJournal = $null

function Test-VercelPromotedCandidateSetMatchesJournal {
    param([Parameter(Mandatory = $true)][object]$Journal)
    $observed = @($allProductionAliases | ForEach-Object {
        Get-VercelAliasObservation ([string]$_)
    })
    if (-not $observed.Count) { return $false }
    $primary = $observed[0]
    foreach ($item in $observed) {
        if ([string]$item.id -cne [string]$primary.id -or
            (Normalize-VercelDeploymentUrl $item.url) -cne
            (Normalize-VercelDeploymentUrl $primary.url)) { return $false }
    }
    $journalPromotedId = Get-OptionalJsonProperty `
        -InputObject $Journal -Name 'promoted_deployment_id'
    $journalPromotedUrl = Get-OptionalJsonProperty `
        -InputObject $Journal -Name 'promoted_deployment_url'
    $hasJournalPromotedId = -not [string]::IsNullOrWhiteSpace([string]$journalPromotedId)
    $hasJournalPromotedUrl = -not [string]::IsNullOrWhiteSpace([string]$journalPromotedUrl)
    if ($hasJournalPromotedId -ne $hasJournalPromotedUrl) { return $false }
    if ($hasJournalPromotedId -and (
        [string]$primary.id -cne [string]$journalPromotedId -or
        (Normalize-VercelDeploymentUrl $primary.url) -cne
            (Normalize-VercelDeploymentUrl $journalPromotedUrl)
    )) { return $false }
    $matches = @(Get-VercelProductionDeployments |
        Where-Object {
            $metadata = Get-OptionalJsonProperty -InputObject $_ -Name "meta"
            [string](Get-OptionalJsonProperty -InputObject $_ -Name "id") -ceq [string]$primary.id -and
            [string](Get-OptionalJsonProperty -InputObject $_ -Name "target") -ceq "production" -and
            [string](Get-OptionalJsonProperty -InputObject $metadata -Name "action") -ceq "promote" -and
            [string](Get-OptionalJsonProperty -InputObject $metadata -Name "originalDeploymentId") -ceq
                [string]$Journal.candidate_preview_deployment_id
        })
    if ($matches.Count -ne 1) { return $false }
    try {
        $expectedAttestation = ''
        if (Test-VercelObjectProperty `
                -InputObject $Journal -Name 'promoted_remote_file_attestation_sha256') {
            $candidateExpected = [string]$Journal.promoted_remote_file_attestation_sha256
            if ($candidateExpected -and $candidateExpected -cne $emptySha256) {
                $expectedAttestation = $candidateExpected
            }
        }
        $evidence = Assert-VercelPromotedDeploymentFileEvidence `
            -DeploymentId ([string]$primary.id) `
            -PreviewDeploymentId ([string]$Journal.candidate_preview_deployment_id) `
            -PackageMapSha256 ([string]$Journal.candidate_package_map_sha256) `
            -ExpectedAttestationSha256 $expectedAttestation
        if ($hasJournalPromotedUrl -and
            (Normalize-VercelDeploymentUrl $evidence.deployment_url) -cne
                (Normalize-VercelDeploymentUrl $journalPromotedUrl)) {
            return $false
        }
    }
    catch { return $false }
    return $true
}

function Get-VercelGovernedAssetProof {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][object]$BuildManifest,
        [Parameter(Mandatory = $true)][string]$Label,
        [switch]$PinnedLegacyInventory
    )
    $fileHashes = Get-OptionalJsonProperty -InputObject $BuildManifest -Name "file_hashes"
    if ($null -eq $fileHashes) { throw "$Label build manifest file_hashes is missing." }
    Assert-VercelPublicFileHashSet -BuildManifest $BuildManifest -Label $Label `
        -PinnedLegacyInventory:$PinnedLegacyInventory
    $properties = @($fileHashes.PSObject.Properties | Sort-Object Name)
    if ($properties.Count -lt 1 -or $properties.Count -gt 256) {
        throw "$Label governed asset count is outside 1..256."
    }
    $verified = [ordered]@{}
    [int64]$totalBytes = 0
    foreach ($property in $properties) {
        $relative = [string]$property.Name
        $expected = [string]$property.Value
        $unsafeSegment = @($relative.Split('/') | Where-Object { $_ -in @('', '.', '..') }).Count -gt 0
        if ($relative -cnotmatch '^[A-Za-z0-9._/-]+$' -or $relative.StartsWith('/') -or
            $relative.Contains('\') -or $unsafeSegment) {
            throw "$Label governed asset path is unsafe: $relative"
        }
        Assert-LowerHex64 -Value $expected -Field "file_hashes.$relative" -Label $Label
        $encodedPath = (($relative.Split('/') | ForEach-Object { [Uri]::EscapeDataString($_) }) -join '/')
        $response = Invoke-VercelBoundedPublicGet `
            -Url "$($BaseUrl.TrimEnd('/'))/$encodedPath`?asset_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())" `
            -TimeoutSeconds $VercelCommandTimeoutSeconds `
            -MaximumResponseBytes 16777216
        if ([int]$response.status_code -ne 200) {
            throw "$Label governed asset is unavailable: $relative"
        }
        $length = [long]$response.body_bytes.LongLength
        $totalBytes += $length
        if ($totalBytes -gt 134217728) { throw "$Label governed assets exceed 128 MiB total." }
        $observed = Get-VercelDeploymentBytesHash `
            -Bytes $response.body_bytes -Algorithm SHA256
        if ($observed -cne $expected) { throw "$Label governed asset hash mismatch: $relative" }
        $verified[$relative] = $observed
    }
    $mapDigest = Get-Sha256Hex (ConvertTo-VercelCanonicalJson $verified)
    return [ordered]@{
        endpoint = $BaseUrl.TrimEnd('/')
        build_sha = [string]$BuildManifest.build_sha
        asset_count = $properties.Count
        total_bytes = $totalBytes
        file_hashes_sha256 = $mapDigest
    }
}

function Get-VercelRemoteFileSha256 {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][ValidateSet('build-manifest.json', 'release-manifest.json')][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $response = Invoke-VercelBoundedPublicGet `
        -Url "$($BaseUrl.TrimEnd('/'))/$RelativePath`?manifest_bytes=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())" `
        -TimeoutSeconds $VercelCommandTimeoutSeconds -MaximumResponseBytes 4194304
    if ([int]$response.status_code -ne 200) {
        throw "$Label exact $RelativePath bytes are unavailable."
    }
    return Get-VercelDeploymentBytesHash -Bytes $response.body_bytes -Algorithm SHA256
}

function New-VercelReadyRollbackContract {
    param(
        [Parameter(Mandatory = $true)][object]$EndpointProof,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not $EndpointProof.source_manifest_available -or
        [int]$EndpointProof.source_manifest_http_status -ne 200 -or
        [string]$EndpointProof.source_manifest_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        [string]$EndpointProof.readiness_reason -eq '' -or
        @($EndpointProof.readiness_failed_checks).Count -ne 0) {
        throw "$Label READY rollback contract is incomplete."
    }
    return [ordered]@{
        schema_version = 'dawnstrike.vercel_rollback_target.v1'
        mode = 'READY_SOURCE_MANIFEST'
        health_status = [string]$EndpointProof.health_status
        readiness_status = [string]$EndpointProof.readiness_status
        readiness_http_status = [int]$EndpointProof.readiness_http_status
        readiness_reason = [string]$EndpointProof.readiness_reason
        readiness_failed_checks = @()
        source_proof = [ordered]@{
            kind = 'deployed_source_manifest'
            sha256 = [string]$EndpointProof.source_manifest_sha256
        }
    }
}

function Assert-VercelPinnedLegacyRollbackTarget {
    param(
        [Parameter(Mandatory = $true)][string]$DeploymentId,
        [Parameter(Mandatory = $true)][string]$DeploymentUrl,
        [Parameter(Mandatory = $true)][object]$EndpointProof,
        [Parameter(Mandatory = $true)][object]$BuildManifest,
        [Parameter(Mandatory = $true)][object]$ReleaseManifest,
        [Parameter(Mandatory = $true)][string]$BuildManifestSha256,
        [Parameter(Mandatory = $true)][string]$ReleaseManifestSha256,
        [Parameter(Mandatory = $true)][object]$ArtifactProof,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $failedChecks = @($EndpointProof.readiness_failed_checks)
    if ($DeploymentId -cne $pinnedLegacyDeploymentId -or
        $DeploymentUrl -cne $pinnedLegacyDeploymentUrl -or
        -not $EndpointProof.health_available -or
        [string]$EndpointProof.health_status -cne 'alive' -or
        -not $EndpointProof.readiness_available -or
        [string]$EndpointProof.readiness_status -cne 'not_ready' -or
        [int]$EndpointProof.readiness_http_status -ne 503 -or
        [string]$EndpointProof.readiness_reason -cne $pinnedLegacyReadinessReason -or
        (ConvertTo-VercelCanonicalJson $failedChecks) -cne
            (ConvertTo-VercelCanonicalJson $pinnedLegacyFailedChecks) -or
        $EndpointProof.source_manifest_available -or
        [int]$EndpointProof.source_manifest_http_status -ne 404 -or
        [string]$EndpointProof.source_sha -cne $pinnedLegacySourceSha -or
        [string]$BuildManifest.source_sha -cne $pinnedLegacySourceSha -or
        [string]$BuildManifest.market_date -cne $pinnedLegacyMarketDate -or
        [string]$BuildManifest.build_id -cne $pinnedLegacyBuildId -or
        [string]$BuildManifest.build_sha -cne $pinnedLegacyBuildSha -or
        [string]$BuildManifestSha256 -cne $pinnedLegacyBuildManifestSha256 -or
        [string]$ReleaseManifestSha256 -cne $pinnedLegacyReleaseManifestSha256 -or
        [string]$ReleaseManifest.source_sha -cne $pinnedLegacySourceSha -or
        [string]$ReleaseManifest.build_sha -cne $pinnedLegacyBuildSha -or
        $BuildManifest.source_clean -ne $true -or
        $BuildManifest.research_only -ne $true -or
        $BuildManifest.live_trading_enabled -ne $false -or
        $ReleaseManifest.research_only -ne $true -or
        $ReleaseManifest.broker_execution_enabled -ne $false -or
        [string]$ArtifactProof.endpoint -cne $pinnedLegacyDeploymentUrl -or
        [string]$ArtifactProof.build_sha -cne $pinnedLegacyBuildSha -or
        [int]$ArtifactProof.asset_count -ne 18 -or
        [int64]$ArtifactProof.total_bytes -ne $pinnedLegacyArtifactTotalBytes -or
        [string]$ArtifactProof.file_hashes_sha256 -cne $pinnedLegacyArtifactMapSha256) {
        throw "$Label is not the exact authorized pinned legacy rollback target."
    }
    $legacyBuildFormula = Get-Sha256Hex `
        "$($BuildManifest.source_sha):$($BuildManifest.publication_set_sha256):$($BuildManifest.opportunity_projection_sha256):$($BuildManifest.market_date)"
    if ($legacyBuildFormula -cne $pinnedLegacyBuildSha) {
        throw "$Label legacy four-input build identity is invalid."
    }
    return [ordered]@{
        schema_version = 'dawnstrike.vercel_rollback_target.v1'
        mode = 'PINNED_LEGACY_CLOCK_STALE'
        health_status = 'alive'
        readiness_status = 'not_ready'
        readiness_http_status = 503
        readiness_reason = $pinnedLegacyReadinessReason
        readiness_failed_checks = @($pinnedLegacyFailedChecks)
        source_proof = [ordered]@{
            kind = 'pinned_legacy_attestation'
            sha256 = $pinnedLegacyAttestationSha256
        }
    }
}

function Assert-VercelAuthorizedManifestBytes {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][string]$ExpectedBuildManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedReleaseManifestSha256,
        [Parameter(Mandatory = $true)][string]$Label
    )
    Assert-LowerHex64 -Value $ExpectedBuildManifestSha256 `
        -Field 'authorized_build_manifest_sha256' -Label $Label
    Assert-LowerHex64 -Value $ExpectedReleaseManifestSha256 `
        -Field 'authorized_release_manifest_sha256' -Label $Label
    $buildHash = Get-VercelRemoteFileSha256 `
        -BaseUrl $BaseUrl -RelativePath 'build-manifest.json' -Label $Label
    $releaseHash = Get-VercelRemoteFileSha256 `
        -BaseUrl $BaseUrl -RelativePath 'release-manifest.json' -Label $Label
    if ($buildHash -cne $ExpectedBuildManifestSha256 -or
        $releaseHash -cne $ExpectedReleaseManifestSha256) {
        throw "$Label build/release bytes do not match the locally authorized artifact."
    }
}

function Assert-VercelLocalAuthorizedManifestBytes {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedBuildManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedReleaseManifestSha256
    )
    $buildPath = Join-Path $StageRoot 'public\build-manifest.json'
    $releasePath = Join-Path $StageRoot 'public\release-manifest.json'
    if ((Get-VercelFileSha256 -Path $buildPath) -cne $ExpectedBuildManifestSha256 -or
        (Get-VercelFileSha256 -Path $releasePath) -cne $ExpectedReleaseManifestSha256) {
        throw 'Locally authorized build/release manifest bytes changed during publication.'
    }
}
try {
if (-not ($Promote -or $RecoveryOnly)) {
    if (-not $ExpectedMarketDate) {
        throw "Preview publication requires an exact ExpectedMarketDate for serialization."
    }
    Acquire-VercelPublicationLock `
        -CandidateSourceSha $expectedSourceSha `
        -CandidateSourceTree $expectedSourceTree `
        -CandidateMarketDate $ExpectedMarketDate
    Assert-VercelRuntimeActivationAbsent
    Assert-VercelPublicationToolchainStable
}
if ($Promote -or $RecoveryOnly) {
    if (-not $ExpectedMarketDate) {
        throw "Production publication requires an exact ExpectedMarketDate for the operation lock."
    }
    Acquire-VercelPublicationLock `
        -CandidateSourceSha $expectedSourceSha `
        -CandidateSourceTree $expectedSourceTree `
        -CandidateMarketDate $ExpectedMarketDate
    Assert-VercelRuntimeActivationAbsent
    Assert-VercelPublicationToolchainStable
    Assert-VercelPriorJournalHistoryTerminal
    # Read the journal only after taking the global lock. This closes the
    # check-then-act window where two publishers could both observe no journal
    # and proceed to mutate production aliases. The surrounding finally now
    # covers every operation after lock acquisition, including these checks.
    $existingJournal = Get-VercelPublicationJournal
    if ($null -ne $existingJournal) {
        $existingUsesCasEvidence = [string]$existingJournal.schema_version -in @(
            'dawnstrike.vercel_publication_journal.v7',
            'dawnstrike.vercel_publication_journal.v8'
        )
        if ([string]$existingJournal.candidate_market_date -cne $resolvedExpectedMarketDate -or
            [string]$existingJournal.project_id -cne $ProjectId -or
            [string]$existingJournal.project_name -cne $ProjectName -or
            [string]$existingJournal.provider_scope -cne $ProviderScope -or
            ($existingUsesCasEvidence -and
                [string]$existingJournal.provider_team_id -cne $ProviderTeamId)) {
            throw "Existing Vercel recovery journal does not match its dated provider boundary."
        }
        $recoveryAliases = @($existingJournal.production_aliases | ForEach-Object { [string]$_ })
        if ((ConvertTo-VercelCanonicalJson $recoveryAliases) -cne
            (ConvertTo-VercelCanonicalJson @($allProductionAliases))) {
            throw "Existing Vercel recovery journal aliases do not match configuration."
        }
        if ([string]$existingJournal.toolchain_identity_sha256 -cne $toolchainIdentitySha256) {
            throw "Existing Vercel recovery journal was created by a different exact toolchain."
        }
        if (-not $RecoveryOnly) {
            Assert-VercelJournalBaseMatchesInvocation -Journal $existingJournal
        }
    }
}
if ($RecoveryOnly -and $null -eq $existingJournal) {
    Write-Output (ConvertTo-VercelCanonicalJson ([ordered]@{
        schema_version = "dawnstrike.vercel_publication_recovery.v1"
        status = "NO_NONTERMINAL_CURRENT_OPERATION"
        market_date = $resolvedExpectedMarketDate
        project_id = $ProjectId
        project_name = $ProjectName
        provider_scope = $ProviderScope
        provider_team_id = $ProviderTeamId
        production_aliases = @($allProductionAliases)
        research_only = $true
        broker_execution_enabled = $false
    }))
    return
}
if ($null -ne $existingJournal) {
    if ([string]$existingJournal.phase -eq "COMPENSATED") {
        if ($RecoveryOnly) {
            $archivedCompensation = Archive-VercelCompensatedCurrentJournal -Journal $existingJournal
            Write-Output (ConvertTo-VercelCanonicalJson ([ordered]@{
                schema_version = "dawnstrike.vercel_publication_recovery.v1"
                status = "ARCHIVED_COMPENSATED"
                market_date = [string]$existingJournal.candidate_market_date
                project_id = [string]$existingJournal.project_id
                project_name = [string]$existingJournal.project_name
                provider_scope = [string]$existingJournal.provider_scope
                production_aliases = @($existingJournal.production_aliases)
                archived_journal_sha256 = Get-VercelFileSha256 -Path $archivedCompensation
                research_only = $true
                broker_execution_enabled = $false
            }))
            return
        }
        throw "A terminal compensated Vercel publication journal already exists; manual review is required."
    }
    if ([string]$existingJournal.phase -eq "COMPLETE") {
        Write-Output (Resolve-VercelCompletePublicationJournal -Journal $existingJournal)
        return
    }
    $null = Assert-VercelJournalFrozenPreviewEvidence -Journal $existingJournal
    $candidateLive = if ([string]$existingJournal.phase -eq "PRE_MUTATION") {
        Test-VercelPromotedCandidateSetMatchesJournal -Journal $existingJournal
    }
    else { Test-VercelAliasSetMatches -Journal $existingJournal -Kind candidate }
    if ($candidateLive) {
        if ([string]$existingJournal.phase -eq "PRE_MUTATION") {
            $live = Get-VercelAliasObservation ([string]$ProductionAlias)
            $recoveredPromotedEvidence = Assert-VercelPromotedDeploymentFileEvidence `
                -DeploymentId ([string]$live.id) `
                -PreviewDeploymentId ([string]$existingJournal.candidate_preview_deployment_id) `
                -PackageMapSha256 ([string]$existingJournal.candidate_package_map_sha256)
            $promotedRemoteFileAttestationSha256 = `
                [string]$recoveredPromotedEvidence.remote_file_attestation_sha256
            $existingJournal.promoted_remote_file_attestation_sha256 = `
                $promotedRemoteFileAttestationSha256
            $liveHealth = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/api/health?recovery_verify=1") -Label "Recovered production health"
            $liveReadiness = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/api/readiness?recovery_verify=1") -Label "Recovered production readiness"
            $liveManifest = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/build-manifest.json?recovery_verify=1") -Label "Recovered production build manifest"
            $liveRelease = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/release-manifest.json?recovery_verify=1") -Label "Recovered production release manifest"
            $recoveredResult = New-VercelRecoveredResultPayload -Journal $existingJournal -Live $live -Health $liveHealth -Readiness $liveReadiness -Manifest $liveManifest -ReleaseManifest $liveRelease
            if (-not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $existingJournal)) {
                throw "Interrupted promotion metadata or aliases changed before POST_ALIASES sealing."
            }
            $postRecovery = New-VercelPublicationJournalPayload `
                -Phase "POST_ALIASES" -Sequence 1 `
                -CandidateDeployment ([pscustomobject]@{ id = $existingJournal.candidate_preview_deployment_id; url = $existingJournal.candidate_preview_url }) `
                -PreviewManifest ([pscustomobject]@{ source_sha = $existingJournal.candidate_source_sha; market_date = $existingJournal.candidate_market_date; build_id = $existingJournal.candidate_build_id; build_sha = $existingJournal.candidate_build_sha }) `
                -PackageManifestSha256 ([string]$existingJournal.candidate_package_manifest_sha256) `
                -PackageMapSha256 ([string]$existingJournal.candidate_package_map_sha256) `
                -RemoteFileAttestationSha256 ([string]$existingJournal.candidate_remote_file_attestation_sha256) `
                -PromotedRemoteFileAttestationSha256 $promotedRemoteFileAttestationSha256 `
                -DeploymentOperationId ([string]$existingJournal.candidate_deployment_operation_id) `
                -DeploymentRequestSha256 ([string]$existingJournal.candidate_deployment_request_sha256) `
                -CandidateBuildManifestSha256 ([string]$existingJournal.candidate_build_manifest_sha256) `
                -CandidateReleaseManifestSha256 ([string]$existingJournal.candidate_release_manifest_sha256) `
                -CandidatePublicArtifactRootSha256 ([string]$existingJournal.candidate_public_artifact_root_sha256) `
                -CandidateManifestSha256 ([string]$existingJournal.candidate_manifest_sha256) `
                -PriorAliases @($existingJournal.prior_aliases) `
                -PromotedDeployment $live `
                -ResultPayload $recoveredResult `
                -ExpectedPublicationMarketDate ([string]$existingJournal.expected_market_date) `
                -PrepublicationAuthorization ([string]$existingJournal.prepublication_authorization_id) `
                -DailyLedgerAuthorization ([string]$existingJournal.daily_ledger_authorization_id) `
                -PriorJournalHash (Get-VercelFileSha256 -Path $journalPath)
            $null = Write-VercelPublicationJournal -Payload $postRecovery -Transition
            $existingJournal = Get-VercelPublicationJournal
        }
        Write-Output (Complete-VercelJournalRecovery -Journal $existingJournal)
        return
    }
    if ($RecoveryOnly) {
        Invoke-VercelPublicationCompensation `
            -Journal $existingJournal `
            -FailureType "recovery_only_nonterminal_convergence"
        $terminalRecovery = Get-VercelPublicationJournal
        if ([string]$terminalRecovery.phase -cne "COMPENSATED") {
            throw "Recovery-only Vercel compensation did not seal terminal evidence."
        }
        $archivedCompensation = Archive-VercelCompensatedCurrentJournal -Journal $terminalRecovery
        Write-Output (ConvertTo-VercelCanonicalJson ([ordered]@{
            schema_version = "dawnstrike.vercel_publication_recovery.v1"
            status = "COMPENSATED"
            market_date = [string]$terminalRecovery.candidate_market_date
            project_id = [string]$terminalRecovery.project_id
            project_name = [string]$terminalRecovery.project_name
            provider_scope = [string]$terminalRecovery.provider_scope
            production_aliases = @($terminalRecovery.production_aliases)
            archived_journal_sha256 = Get-VercelFileSha256 -Path $archivedCompensation
            research_only = $true
            broker_execution_enabled = $false
        }))
        return
    }
    if (-not (Test-VercelAliasSetMatches -Journal $existingJournal -Kind prior)) {
        try { Invoke-VercelPublicationCompensation -Journal $existingJournal -FailureType "mixed_or_uncertain_alias_state_on_retry" }
        catch { throw "Vercel publication retry is mixed or uncertain and compensation did not complete: $($_.Exception.Message)" }
        throw "Vercel publication retry found mixed or uncertain aliases; publication was compensated."
    }
    $recoveryRetry = $true
    foreach ($priorRecord in @($existingJournal.prior_aliases)) {
        $priorProductionAliases[[string]$priorRecord.alias] = [pscustomobject]@{
            id = [string]$priorRecord.deployment_id
            url = [string]$priorRecord.deployment_url
            health_available = $true
            health_status = [string]$priorRecord.health_status
            readiness_available = $true
            readiness_status = [string]$priorRecord.readiness_status
            readiness_http_status = [int]$priorRecord.readiness_http_status
            source_manifest_available = (
                -not (Test-VercelObjectProperty -InputObject $priorRecord -Name 'rollback_contract') -or
                [string]$priorRecord.rollback_contract.mode -ceq 'READY_SOURCE_MANIFEST'
            )
            source_sha = [string]$priorRecord.source_sha
            source_tree = [string]$priorRecord.source_tree
            source_manifest_sha256 = [string]$priorRecord.source_manifest_sha256
            build_manifest_sha256 = [string]$priorRecord.build_manifest_sha256
            release_manifest_sha256 = [string]$priorRecord.release_manifest_sha256
            artifact_proof = $priorRecord.artifact_proof
            rollback_contract = if (Test-VercelObjectProperty -InputObject $priorRecord -Name 'rollback_contract') {
                $priorRecord.rollback_contract
            } else { $null }
        }
    }
    $recoveryPreview = Get-VercelJournalPreviewEvidence -Journal $existingJournal
    $deployment = [pscustomobject]@{ id = [string]$existingJournal.candidate_preview_deployment_id; url = [string]$existingJournal.candidate_preview_url }
    $deploymentId = [string]$existingJournal.candidate_preview_deployment_id
    $previewUrl = [string]$existingJournal.candidate_preview_url
    $previewHealth = $recoveryPreview.health
    $previewReadiness = $recoveryPreview.readiness
    $previewManifest = $recoveryPreview.manifest
    $previewReleaseManifest = $recoveryPreview.release
    $packageManifestSha256 = [string]$existingJournal.candidate_package_manifest_sha256
    $packageMapSha256 = [string]$existingJournal.candidate_package_map_sha256
    $remoteFileAttestationSha256 = [string]$existingJournal.candidate_remote_file_attestation_sha256
    $promotedRemoteFileAttestationSha256 = `
        [string]$existingJournal.promoted_remote_file_attestation_sha256
    $deploymentCasOperationId = [string]$existingJournal.candidate_deployment_operation_id
    $deploymentCasRequestSha256 = [string]$existingJournal.candidate_deployment_request_sha256
    $candidateManifestSha256 = [string]$existingJournal.candidate_manifest_sha256
    $candidateBuildManifestSha256 = [string]$existingJournal.candidate_build_manifest_sha256
    $candidateReleaseManifestSha256 = [string]$existingJournal.candidate_release_manifest_sha256
    $authorizedArtifactIdentity = [pscustomobject]@{
        build_sha = [string]$existingJournal.candidate_build_sha
        build_id = [string]$existingJournal.candidate_build_id
        publication_set_sha256 = [string]$previewManifest.publication_set_sha256
        release_manifest_sha256 = [string]$previewReleaseManifest.release_manifest_sha256
        build_manifest_sha256 = [string]$existingJournal.candidate_build_manifest_sha256
        release_manifest_raw_sha256 = [string]$existingJournal.candidate_release_manifest_sha256
        public_artifact_root_sha256 = [string]$existingJournal.candidate_public_artifact_root_sha256
    }
    $journalPriorAliases = @($existingJournal.prior_aliases)
    $journalCandidate = [pscustomobject]@{ id = $deploymentId; url = $previewUrl }
    $priorProduction = [pscustomobject]@{
        id = @($existingJournal.prior_aliases | Where-Object { [string]$_.alias -eq [string]$ProductionAlias })[0].deployment_id
    }
}

if ($RecoveryOnly) {
    throw "Recovery-only Vercel convergence reached a fresh publication path."
}

if ($Promote -or $PrepublicationAuthorizationId) {
    # Every path that can still initiate a provider mutation rechecks the live
    # current-session boundary, including a retry of a sealed PRE_MUTATION.
    $authorizedArtifactIdentity = Assert-GovernedPublicationAuthorization
}
if (-not $recoveryRetry) {
 Assert-VercelPublicationToolchainStable
 Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
 Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $publicArtifactRoot -Label "Public artifact root"
 & (Join-Path $codeRoot "scripts\build_vercel_public_stage.ps1") `
    -ProjectRoot $resolvedRoot `
    -StageRoot $StageRoot `
    -ExpectedSourceSha $expectedSourceSha `
    -ExpectedSourceTree $expectedSourceTree
& (Join-Path $codeRoot "scripts\verify_vercel_candidate.ps1") `
    -ProjectRoot $resolvedRoot `
    -StageRoot $StageRoot `
    -ExpectedSourceSha $expectedSourceSha `
    -ExpectedSourceTree $expectedSourceTree `
    -AllowDegraded:$AllowDegraded
 $candidateBuildManifestSha256 = Get-VercelFileSha256 `
    -Path (Join-Path $stage "public\build-manifest.json")
 $candidateReleaseManifestSha256 = Get-VercelFileSha256 `
    -Path (Join-Path $stage "public\release-manifest.json")
 $stagedArtifactIdentity = Assert-GovernedPublicationAuthorization `
    -ArtifactRoot (Join-Path $stage "public")
 Assert-VercelAuthorizedArtifactIdentity `
    -Expected $authorizedArtifactIdentity `
    -Observed $stagedArtifactIdentity `
    -Label "Staged public artifact"
Assert-VercelGitSourceStable `
    -Root $resolvedRoot `
    -ExpectedSourceSha $expectedSourceSha `
    -ExpectedSourceTree $expectedSourceTree `
    -AllowedStageRoot $stage
$deploymentPackageBoundary = $null
try {
    Assert-VercelGitSourceStable `
        -Root $resolvedRoot `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree `
        -AllowedStageRoot $stage
    # Construct Build Output API v3 directly from exact Git/public inputs and
    # the two digest-pinned wheel archives. No local Node, Python, uv, Vercel
    # CLI, shell, or registry-resolving child receives the publication token or
    # reads this writer-owned stage.
    New-VercelDeterministicBuiltPackage -StageRoot $stage
    Assert-VercelNoEnvironmentArtifacts -StageRoot $stage
    Add-VercelFunctionPublicBindings -StageRoot $stage
    $packageManifestSha256 = Assert-VercelBuiltPackage `
        -StageRoot $stage `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree
    # Seal the complete post-build output inventory before any deploy command;
    # later checks must compare against this in-memory hash, never authorize a
    # rewritten manifest from the mutable stage directory.
    Assert-VercelGitSourceStable `
        -Root $resolvedRoot `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree `
        -AllowedStageRoot $stage
    Assert-VercelStagedSourceManifest `
        -StageRoot $stage `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree
    Add-VercelFunctionPublicBindings -StageRoot $stage
    Assert-VercelNoEnvironmentArtifacts -StageRoot $stage
    $null = Assert-VercelBuiltPackage `
        -StageRoot $stage `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree `
        -ExpectedPackageManifestSha256 $packageManifestSha256
    Assert-VercelLocalAuthorizedManifestBytes `
        -StageRoot $stage `
        -ExpectedBuildManifestSha256 $candidateBuildManifestSha256 `
        -ExpectedReleaseManifestSha256 $candidateReleaseManifestSha256
    $predeployArtifactIdentity = Assert-GovernedPublicationAuthorization `
        -ArtifactRoot (Join-Path $stage "public")
    Assert-VercelAuthorizedArtifactIdentity `
        -Expected $authorizedArtifactIdentity `
        -Observed $predeployArtifactIdentity `
        -Label "Predeploy public artifact"
    Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
    Assert-VercelPublicationToolchainStable
    # Freeze every provider path, mode, and byte through retained handles. The
    # provider receives only this explicit content-addressed map; it never
    # receives or reopens the mutable stage pathname.
    $deploymentPackageBoundary = Open-VercelDeploymentPackageBoundary `
        -StageRoot $stage `
        -ExpectedPackageManifestSha256 $packageManifestSha256
    $packageRootRelativePath = [string]$deploymentPackageBoundary.package_root_relative_path
    $packageTreeSha256 = [string]$deploymentPackageBoundary.snapshot_sha256
    $packageMapSha256 = [string]$deploymentPackageBoundary.map_sha256
    $null = Assert-VercelDeploymentPackageBoundaryStable -Boundary $deploymentPackageBoundary
    $frozenDeployment = New-VercelFrozenPreviewDeployment `
        -Boundary $deploymentPackageBoundary `
        -SourceSha $expectedSourceSha -SourceTree $expectedSourceTree
    $deployment = $frozenDeployment.deployment
    $deploymentCasOperationId = [string]$frozenDeployment.operation_id
    $deploymentCasRequestSha256 = [string]$frozenDeployment.request_sha256
    $packageMapSha256 = [string]$frozenDeployment.package_map_sha256
    $remoteFileAttestationSha256 = [string]$frozenDeployment.remote_file_attestation_sha256
}
finally {
    if ($null -ne $deploymentPackageBoundary) {
        Close-VercelDeploymentPackageBoundary -Boundary $deploymentPackageBoundary
    }
}
}

$deploymentId = Get-OptionalJsonProperty -InputObject $deployment -Name "id"
$previewUrl = [string](
    Get-OptionalJsonProperty -InputObject $deployment -Name "url"
)
if (-not $deploymentId -or -not $previewUrl) {
    throw "Vercel prebuilt deploy did not return a deployment ID and URL."
}
if (-not $previewUrl.StartsWith("http")) {
    $previewUrl = "https://$previewUrl"
}
$previewHealth = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/api/health") `
    -Label "Preview health"
$previewReadiness = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/api/readiness") `
    -Label "Preview readiness"
$previewManifest = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/build-manifest.json") `
    -Label "Preview build manifest"
$previewReleaseManifest = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/release-manifest.json") `
    -Label "Preview release manifest"
Assert-RemoteVercelSourceManifest `
    -Url "$previewUrl/vercel-source-manifest.json" `
    -Label "Preview"
Assert-PublicationState `
    -Health $previewHealth `
    -Readiness $previewReadiness `
    -BuildManifest $previewManifest `
    -ReleaseManifest $previewReleaseManifest `
    -ExpectedSourceSha $expectedSourceSha `
    -Label "Preview"
Assert-VercelAuthorizedManifestBytes `
    -BaseUrl $previewUrl `
    -ExpectedBuildManifestSha256 $candidateBuildManifestSha256 `
    -ExpectedReleaseManifestSha256 $candidateReleaseManifestSha256 `
    -Label "Preview"
$previewArtifactProof = Get-VercelGovernedAssetProof `
    -BaseUrl $previewUrl -BuildManifest $previewManifest -Label "Preview"
$productionArtifactProofs = @()
if (-not $candidateManifestSha256) {
    $candidateManifestSha256 = Get-Sha256Hex (Get-VercelSourceManifestCanonicalJson -Path (Join-Path $stage "vercel-source-manifest.json"))
}

if ($Promote) {
    # Every alias is independent external state.  Snapshot each target before
    # promotion so an uncertain promotion can restore the exact deployment
    # that was serving that alias, rather than copying the primary alias to
    # every hostname.
    $legacyPriorAliasCount = 0
    foreach ($alias in $allProductionAliases) {
        $snapshot = Get-VercelAliasObservation -Alias ([string]$alias)
        $snapshotId = Get-OptionalJsonProperty -InputObject $snapshot -Name "id"
        $snapshotUrl = [string](
            Get-OptionalJsonProperty -InputObject $snapshot -Name "url"
        )
        if (-not $snapshotId -or -not $snapshotUrl) {
            throw "Prior production inspect did not return a deployment ID and URL for $alias."
        }
        $snapshotBaseUrl = Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl $snapshotUrl
        $endpointProof = Get-VercelAliasEndpointProof `
            -AliasUrl $snapshotBaseUrl `
            -CacheBuster ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
        $priorProductionAliases[[string]$alias] = [pscustomobject]@{
            id = [string]$snapshotId
            url = $snapshotBaseUrl
            health_available = [bool]$endpointProof.health_available
            health_status = [string]$endpointProof.health_status
            readiness_available = [bool]$endpointProof.readiness_available
            readiness_status = [string]$endpointProof.readiness_status
            readiness_http_status = $endpointProof.readiness_http_status
            source_manifest_available = [bool]$endpointProof.source_manifest_available
            source_sha = [string]$endpointProof.source_sha
            source_tree = [string]$endpointProof.source_tree
            source_manifest_sha256 = if ($endpointProof.source_manifest_available) {
                [string]$endpointProof.source_manifest_sha256
            } else { $emptySha256 }
            build_manifest = $null
            release_manifest = $null
            rollback_contract = $null
        }
        try {
            $priorProductionAliases[[string]$alias].build_manifest = Invoke-VercelJson `
                -Arguments @("curl", "$snapshotBaseUrl/build-manifest.json?rollback_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())") `
                -Label "Prior production build manifest for $alias"
            $priorProductionAliases[[string]$alias].release_manifest = Invoke-VercelJson `
                -Arguments @("curl", "$snapshotBaseUrl/release-manifest.json?rollback_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())") `
                -Label "Prior production release manifest for $alias"
            $priorBuildRawSha = Get-VercelRemoteFileSha256 `
                -BaseUrl $snapshotBaseUrl -RelativePath 'build-manifest.json' `
                -Label "Prior production alias $alias"
            $priorReleaseRawSha = Get-VercelRemoteFileSha256 `
                -BaseUrl $snapshotBaseUrl -RelativePath 'release-manifest.json' `
                -Label "Prior production alias $alias"
            $isPinnedLegacyIdentity = [string]$snapshotId -ceq $pinnedLegacyDeploymentId -and
                $snapshotBaseUrl -ceq $pinnedLegacyDeploymentUrl
            $priorArtifactProof = Get-VercelGovernedAssetProof -BaseUrl $snapshotBaseUrl `
                -BuildManifest $priorProductionAliases[[string]$alias].build_manifest `
                -Label "Prior production alias $alias" `
                -PinnedLegacyInventory:$isPinnedLegacyIdentity
            if ($isPinnedLegacyIdentity) {
                Assert-VercelPinnedLegacyRollbackAvailable
                $priorProductionAliases[[string]$alias].source_tree = $pinnedLegacySourceTree
                $priorProductionAliases[[string]$alias].rollback_contract = `
                    Assert-VercelPinnedLegacyRollbackTarget `
                        -DeploymentId ([string]$snapshotId) `
                        -DeploymentUrl $snapshotBaseUrl `
                        -EndpointProof $endpointProof `
                        -BuildManifest $priorProductionAliases[[string]$alias].build_manifest `
                        -ReleaseManifest $priorProductionAliases[[string]$alias].release_manifest `
                        -BuildManifestSha256 $priorBuildRawSha `
                        -ReleaseManifestSha256 $priorReleaseRawSha `
                        -ArtifactProof $priorArtifactProof `
                        -Label "Prior production alias $alias"
                $legacyPriorAliasCount++
            }
            else {
                if (-not $endpointProof.source_manifest_available) {
                    throw "Prior production source manifest is unavailable for $alias."
                }
                Assert-PublicationState `
                    -Health $endpointProof.health `
                    -Readiness $endpointProof.readiness `
                    -BuildManifest $priorProductionAliases[[string]$alias].build_manifest `
                    -ReleaseManifest $priorProductionAliases[[string]$alias].release_manifest `
                    -ExpectedSourceSha ([string]$endpointProof.source_sha) `
                    -ExpectedMarketDate ([string]$priorProductionAliases[[string]$alias].build_manifest.market_date) `
                    -Label "Prior production alias $alias"
                if ([string]$endpointProof.source_sha -cne
                    [string]$priorProductionAliases[[string]$alias].build_manifest.source_sha) {
                    throw "Prior production source manifest/build source diverged for $alias."
                }
                $priorProductionAliases[[string]$alias].rollback_contract = `
                    New-VercelReadyRollbackContract `
                        -EndpointProof $endpointProof -Label "Prior production alias $alias"
            }
            $priorProductionAliases[[string]$alias] | Add-Member `
                -NotePropertyName build_manifest_sha256 -NotePropertyValue $priorBuildRawSha
            $priorProductionAliases[[string]$alias] | Add-Member `
                -NotePropertyName release_manifest_sha256 -NotePropertyValue $priorReleaseRawSha
            $priorProductionAliases[[string]$alias] | Add-Member `
                -NotePropertyName artifact_proof -NotePropertyValue $priorArtifactProof
        }
        catch {
            throw "Prior production build/release lineage is unavailable for $alias`: $($_.Exception.Message)"
        }
        if ([string]$alias -eq [string]$ProductionAlias) {
            $priorProduction = $snapshot
        }
    }
    if ($legacyPriorAliasCount -notin @(0, $allProductionAliases.Count)) {
        throw 'Pinned legacy rollback authorization requires the exact full governed alias set.'
    }
    $journalPriorAliases = @($allProductionAliases | ForEach-Object {
        $prior = $priorProductionAliases[[string]$_]
        [ordered]@{
            alias = [string]$_
            deployment_id = [string]$prior.id
            deployment_url = [string]$prior.url
            health_status = [string]$prior.health_status
            readiness_status = [string]$prior.readiness_status
            readiness_http_status = [int]$prior.readiness_http_status
            source_sha = [string]$prior.source_sha
            source_tree = [string]$prior.source_tree
            source_manifest_sha256 = [string]$prior.source_manifest_sha256
            build_manifest_sha256 = [string]$prior.build_manifest_sha256
            release_manifest_sha256 = [string]$prior.release_manifest_sha256
            artifact_proof = $prior.artifact_proof
            rollback_contract = $prior.rollback_contract
        }
    })
    $journalCandidate = [pscustomobject]@{ id = [string]$deploymentId; url = [string]$previewUrl }
    foreach ($alias in $allProductionAliases) {
        $priorRecord = $priorProductionAliases[[string]$alias]
        Assert-ProductionDateLineage `
            -CurrentBuildManifest $priorRecord.build_manifest `
            -CandidateBuildManifest $previewManifest `
            -CurrentReleaseManifest $priorRecord.release_manifest `
            -CandidateReleaseManifest $previewReleaseManifest
        if ([string]$priorRecord.build_manifest.market_date -ceq
            [string]$previewManifest.market_date -and
            [string]$priorRecord.artifact_proof.file_hashes_sha256 -cne
            [string]$previewArtifactProof.file_hashes_sha256) {
            throw "Same-day full artifact identity conflicts with prior alias $alias."
        }
    }
    $premutationArtifactIdentity = Assert-GovernedPublicationAuthorization `
        -ArtifactRoot (Join-Path $stage "public")
    Assert-VercelAuthorizedArtifactIdentity `
        -Expected $authorizedArtifactIdentity `
        -Observed $premutationArtifactIdentity `
        -Label "Premutation public artifact"
    # All prior bytes came from immutable deployment URLs.  Re-inspect every
    # mutable alias immediately before sealing PRE_MUTATION; drift restarts the
    # operation without changing provider state.
    Assert-VercelPriorAliasSnapshotsCurrent
    $preMutationJournal = New-VercelPublicationJournalPayload `
        -Phase "PRE_MUTATION" -Sequence 0 `
        -CandidateDeployment $journalCandidate `
        -PreviewManifest $previewManifest `
        -PackageManifestSha256 $packageManifestSha256 `
        -PackageMapSha256 $packageMapSha256 `
        -RemoteFileAttestationSha256 $remoteFileAttestationSha256 `
        -DeploymentOperationId $deploymentCasOperationId `
        -DeploymentRequestSha256 $deploymentCasRequestSha256 `
        -CandidateBuildManifestSha256 $candidateBuildManifestSha256 `
        -CandidateReleaseManifestSha256 $candidateReleaseManifestSha256 `
        -CandidatePublicArtifactRootSha256 ([string]$authorizedArtifactIdentity.public_artifact_root_sha256) `
        -CandidateManifestSha256 $candidateManifestSha256 `
        -PriorAliases $journalPriorAliases `
        -ExpectedPublicationMarketDate $resolvedExpectedMarketDate `
        -PrepublicationAuthorization $PrepublicationAuthorizationId `
        -DailyLedgerAuthorization $DailyLedgerAuthorizationId
    $null = Write-VercelPublicationJournal -Payload $preMutationJournal
}

try {
    if ($Promote) {
        Assert-VercelPublicationToolchainStable
        Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
        Assert-VercelGitSourceStable `
            -Root $resolvedRoot `
            -ExpectedSourceSha $expectedSourceSha `
            -ExpectedSourceTree $expectedSourceTree `
            -AllowedStageRoot $stage
        Assert-VercelStagedSourceManifest `
            -StageRoot $stage `
            -ExpectedSourceSha $expectedSourceSha `
            -ExpectedSourceTree $expectedSourceTree
        Add-VercelFunctionPublicBindings -StageRoot $stage
        Assert-VercelNoEnvironmentArtifacts -StageRoot $stage
        $null = Assert-VercelBuiltPackage `
            -StageRoot $stage `
            -ExpectedSourceSha $expectedSourceSha `
            -ExpectedSourceTree $expectedSourceTree `
            -ExpectedPackageManifestSha256 $packageManifestSha256
        Assert-VercelLocalAuthorizedManifestBytes `
            -StageRoot $stage `
            -ExpectedBuildManifestSha256 $candidateBuildManifestSha256 `
            -ExpectedReleaseManifestSha256 $candidateReleaseManifestSha256
        $prepromotionArtifactIdentity = Assert-GovernedPublicationAuthorization `
            -ArtifactRoot (Join-Path $stage "public")
        Assert-VercelAuthorizedArtifactIdentity `
            -Expected $authorizedArtifactIdentity `
            -Observed $prepromotionArtifactIdentity `
            -Label "Prepromotion public artifact"
        Assert-VercelPriorAliasSnapshotsCurrent
        # Mark the external state as potentially mutated before starting the
        # command. A timeout can occur after Vercel accepted the promotion, so
        # every promotion failure must enter the existing rollback boundary.
        $promoted = $true
        $null = Request-VercelProductionPromotion -PreviewDeploymentId $deploymentId
        Test-VercelPromotionSeam "after_promote"
        $promotionVerificationError = $null
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                $listedDeployments = @(Get-VercelProductionDeployments)
                if (-not $listedDeployments.Count) {
                    throw "Promoted deployment list did not return deployments."
                }
                $promotedCandidates = @(
                    $listedDeployments |
                        Where-Object {
                            $target = Get-OptionalJsonProperty `
                                -InputObject $_ `
                                -Name "target"
                            $metadata = Get-OptionalJsonProperty `
                                -InputObject $_ `
                                -Name "meta"
                            $action = Get-OptionalJsonProperty `
                                -InputObject $metadata `
                                -Name "action"
                            $originalDeploymentId = Get-OptionalJsonProperty `
                                -InputObject $metadata `
                                -Name "originalDeploymentId"
                            $target -eq "production" -and
                            $action -eq "promote" -and
                            $originalDeploymentId -eq $deploymentId
                        } |
                        Sort-Object -Property createdAt -Descending
                )
                if ($promotedCandidates.Count -ne 1) {
                    throw "The exact single promoted clone of the verified preview is not visible yet."
                }
                $promotedDeploymentId = [string](Get-OptionalJsonProperty `
                    -InputObject $promotedCandidates[0] -Name 'id')
                $promotedDeployment = Get-VercelFrozenDeployment `
                    -DeploymentId $promotedDeploymentId
                $promotedMetadata = Get-OptionalJsonProperty `
                    -InputObject $promotedDeployment -Name 'meta'
                if ([string](Get-OptionalJsonProperty -InputObject $promotedDeployment -Name 'ownerId') -cne
                        $governedProviderTeamId -or
                    [string](Get-OptionalJsonProperty -InputObject $promotedDeployment -Name 'projectId') -cne
                        $governedProjectId -or
                    [string](Get-OptionalJsonProperty -InputObject $promotedDeployment -Name 'target') -cne
                        'production' -or
                    [string](Get-OptionalJsonProperty -InputObject $promotedDeployment -Name 'readyState') -cne
                        'READY' -or
                    [string](Get-OptionalJsonProperty -InputObject $promotedMetadata -Name 'action') -cne
                        'promote' -or
                    [string](Get-OptionalJsonProperty -InputObject $promotedMetadata -Name 'originalDeploymentId') -cne
                        $deploymentId) {
                    throw 'Promoted deployment identity is not the governed preview clone.'
                }
                $promotedFileEvidence = Assert-VercelPromotedDeploymentFileEvidence `
                    -DeploymentId $promotedDeploymentId `
                    -PreviewDeploymentId $deploymentId `
                    -PackageMapSha256 $packageMapSha256
                $promotedDeployment = $promotedFileEvidence.deployment
                $promotedUrl = [string]$promotedFileEvidence.deployment_url
                $promotedRemoteFileAttestationSha256 = `
                    [string]$promotedFileEvidence.remote_file_attestation_sha256
                $cacheBuster = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                $promotedHealth = Invoke-VercelJson `
                    -Arguments @("curl", "$promotedUrl/api/health?verify=$cacheBuster") `
                    -Label "Promoted deployment health"
                $promotedReadiness = Invoke-VercelJson `
                    -Arguments @("curl", "$promotedUrl/api/readiness?verify=$cacheBuster") `
                    -Label "Promoted deployment readiness"
                $promotedManifest = Invoke-VercelJson `
                    -Arguments @(
                        "curl",
                        "$promotedUrl/build-manifest.json?verify=$cacheBuster"
                    ) `
                    -Label "Promoted deployment build manifest"
                $promotedReleaseManifest = Invoke-VercelJson `
                    -Arguments @(
                        "curl",
                        "$promotedUrl/release-manifest.json?verify=$cacheBuster"
                    ) `
                    -Label "Promoted deployment release manifest"
                Assert-RemoteVercelSourceManifest `
                    -Url "$promotedUrl/vercel-source-manifest.json?verify=$cacheBuster" `
                    -Label "Promoted deployment"
                Assert-PublicationState `
                    -Health $promotedHealth `
                    -Readiness $promotedReadiness `
                    -BuildManifest $promotedManifest `
                    -ReleaseManifest $promotedReleaseManifest `
                    -ExpectedSourceSha $expectedSourceSha `
                    -Label "Promoted deployment"
                if (
                    (ConvertTo-VercelCanonicalJson $promotedManifest) -cne
                    (ConvertTo-VercelCanonicalJson $previewManifest) -or
                    $promotedManifest.source_sha -ne $previewManifest.source_sha -or
                    $promotedManifest.build_id -ne $previewManifest.build_id -or
                    $promotedManifest.data_hash_sha256 -ne
                    $previewManifest.data_hash_sha256 -or
                    $promotedManifest.build_sha -ne $previewManifest.build_sha -or
                    $promotedManifest.publication_set_sha256 -ne $previewManifest.publication_set_sha256 -or
                    $promotedManifest.opportunity_projection_sha256 -ne $previewManifest.opportunity_projection_sha256 -or
                    $promotedManifest.v6_learning_sha256 -ne $previewManifest.v6_learning_sha256
                ) {
                    throw "Promoted deployment does not match the verified preview."
                }
                $promotionVerificationError = $null
                break
            }
            catch {
                $promotionVerificationError = $_.Exception.Message
                if ($attempt -lt 10) {
                    Start-Sleep -Seconds 3
                }
            }
        }
        if ($promotionVerificationError) {
            throw "Promoted deployment verification did not converge: $promotionVerificationError"
        }

        $promotedDeploymentId = Get-OptionalJsonProperty `
            -InputObject $promotedDeployment `
            -Name "id"
        $promotedUrl = Get-VercelImmutableDeploymentBaseUrl -DeploymentUrl ([string](
            Get-OptionalJsonProperty -InputObject $promotedDeployment -Name "url"
        ))
        if (-not $promotedDeploymentId -or -not $promotedUrl) {
            throw "Promoted deployment inspect did not return a deployment ID and URL."
        }
        foreach ($alias in $allProductionAliases) {
            Set-VercelAlias `
                -DeploymentId ([string]$promotedDeploymentId) `
                -DeploymentUrl $promotedUrl `
                -AliasUrl ([string]$alias) `
                -Label "Production alias assignment for $alias"
        }
        Test-VercelPromotionSeam "after_aliases"
        foreach ($alias in $allProductionAliases) {
            $aliasAfterPromotion = Get-VercelAliasObservation ([string]$alias)
            if ([string]$aliasAfterPromotion.id -ne [string]$promotedDeploymentId -or
                (Normalize-VercelDeploymentUrl $aliasAfterPromotion.url) -ne (Normalize-VercelDeploymentUrl $promotedUrl)) {
                throw "Production alias verification resolved the wrong deployment for $alias."
            }
        }

        $productionVerificationError = $null
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                $cacheBuster = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                $production = Get-VercelAliasObservation -Alias $ProductionAlias
                $productionHealth = Invoke-VercelJson `
                    -Arguments @("curl", "$ProductionAlias/api/health?verify=$cacheBuster") `
                    -Label "Production health"
                $productionReadiness = Invoke-VercelJson `
                    -Arguments @("curl", "$ProductionAlias/api/readiness?verify=$cacheBuster") `
                    -Label "Production readiness"
                $productionManifest = Invoke-VercelJson `
                    -Arguments @("curl", "$ProductionAlias/build-manifest.json?verify=$cacheBuster") `
                    -Label "Production build manifest"
                $productionReleaseManifest = Invoke-VercelJson `
                    -Arguments @(
                        "curl",
                        "$ProductionAlias/release-manifest.json?verify=$cacheBuster"
                    ) `
                    -Label "Production release manifest"
                Assert-RemoteVercelSourceManifest `
                    -Url "$ProductionAlias/vercel-source-manifest.json?verify=$cacheBuster" `
                    -Label "Production"
                Assert-PublicationState `
                    -Health $productionHealth `
                    -Readiness $productionReadiness `
                    -BuildManifest $productionManifest `
                    -ReleaseManifest $productionReleaseManifest `
                    -ExpectedSourceSha $expectedSourceSha `
                    -Label "Production"
                if (
                    (ConvertTo-VercelCanonicalJson $productionManifest) -cne
                    (ConvertTo-VercelCanonicalJson $previewManifest) -or
                    $productionManifest.source_sha -ne $previewManifest.source_sha -or
                    $productionManifest.build_id -ne $previewManifest.build_id -or
                    $productionManifest.data_hash_sha256 -ne
                    $previewManifest.data_hash_sha256 -or
                    $productionManifest.build_sha -ne $previewManifest.build_sha -or
                    $productionManifest.publication_set_sha256 -ne $previewManifest.publication_set_sha256 -or
                    $productionManifest.opportunity_projection_sha256 -ne $previewManifest.opportunity_projection_sha256 -or
                    $productionManifest.v6_learning_sha256 -ne $previewManifest.v6_learning_sha256
                ) {
                    throw "Production does not match the verified preview."
                }
                $productionVerificationError = $null
                break
            }
            catch {
                $productionVerificationError = $_.Exception.Message
                if ($attempt -lt 10) {
                    Start-Sleep -Seconds 3
                }
            }
        }
        if ($productionVerificationError) {
            throw "Production verification did not converge: $productionVerificationError"
        }
        foreach ($alias in $allProductionAliases) {
            Assert-VercelAuthorizedManifestBytes `
                -BaseUrl ([string]$alias) `
                -ExpectedBuildManifestSha256 $candidateBuildManifestSha256 `
                -ExpectedReleaseManifestSha256 $candidateReleaseManifestSha256 `
                -Label "Production alias $alias"
            $aliasManifest = Invoke-VercelJson `
                -Arguments @("curl", "$alias/build-manifest.json?asset_manifest_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())") `
                -Label "Production alias build manifest for $alias"
            if ([string]$aliasManifest.build_sha -cne [string]$previewManifest.build_sha -or
                (ConvertTo-VercelCanonicalJson $aliasManifest) -cne
                (ConvertTo-VercelCanonicalJson $previewManifest)) {
                throw "Production alias governed asset manifest diverges for $alias."
            }
            $productionArtifactProofs += Get-VercelGovernedAssetProof `
                -BaseUrl ([string]$alias) -BuildManifest $aliasManifest -Label "Production alias $alias"
        }
        Test-VercelPromotionSeam "after_production_verification"
    }
    else {
        $production = $null
        $productionHealth = $null
        $productionReadiness = $null
        $productionManifest = $null
    }
    if ($Promote) {
        $preResultJournal = Get-VercelPublicationJournal
        if ($null -eq $preResultJournal -or
            -not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $preResultJournal)) {
            throw "Production aliases or promotion metadata changed before terminal result sealing."
        }
    }
    $result = [ordered]@{
        schema_version = "dawnstrike.daily_deployment.v1"
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        project_id = $ProjectId
        provider_scope = $ProviderScope
        provider_team_id = $ProviderTeamId
        preview_url = $previewUrl
        preview_deployment_id = $deploymentId
        preview_ready_state = Get-OptionalJsonProperty -InputObject $deployment -Name "readyState"
        source_sha = $previewManifest.source_sha
        source_tree = $expectedSourceTree
        vercel_source_manifest_sha256 = Get-VercelFileSha256 -Path (Join-Path $stage "vercel-source-manifest.json")
        vercel_package_manifest_sha256 = $packageManifestSha256
        vercel_package_map_sha256 = $packageMapSha256
        vercel_remote_file_attestation_sha256 = $remoteFileAttestationSha256
        vercel_promoted_remote_file_attestation_sha256 = `
            if ($Promote) { $promotedRemoteFileAttestationSha256 } else { $emptySha256 }
        vercel_deployment_operation_id = $deploymentCasOperationId
        vercel_deployment_request_sha256 = $deploymentCasRequestSha256
        authorized_build_manifest_sha256 = $candidateBuildManifestSha256
        authorized_release_manifest_sha256 = $candidateReleaseManifestSha256
        public_artifact_root_sha256 = [string]$authorizedArtifactIdentity.public_artifact_root_sha256
        account_session_report = $previewReadiness.account_session_report
        account_session_report_sha256 = Get-Sha256Hex (ConvertTo-VercelCanonicalJson $previewReadiness.account_session_report)
        toolchain_identity_sha256 = $toolchainIdentitySha256
        build_id = $previewManifest.build_id
        build_sha = $previewManifest.build_sha
        build_manifest_sha256 = Get-Sha256Hex (ConvertTo-VercelCanonicalJson $previewManifest)
        data_hash_sha256 = $previewManifest.data_hash_sha256
        publication_set_sha256 = $previewManifest.publication_set_sha256
        opportunity_projection_sha256 = $previewManifest.opportunity_projection_sha256
        v6_learning_sha256 = $previewManifest.v6_learning_sha256
        preview_artifact_proof = $previewArtifactProof
        production_artifact_proofs = @($productionArtifactProofs)
        release_manifest_sha256 = Get-OptionalJsonProperty -InputObject $previewReleaseManifest -Name "release_manifest_sha256"
        market_date = $previewManifest.market_date
        expected_market_date = $resolvedExpectedMarketDate
        prepublication_authorization_id = $PrepublicationAuthorizationId
        daily_ledger_authorization_id = $DailyLedgerAuthorizationId
        snapshot_status = $previewReadiness.snapshot_status
        readiness_status = $previewReadiness.status
        readiness_http_status = $previewReadiness.http_status
        allow_degraded = [bool]$AllowDegraded
        promoted = [bool]$Promote
        prior_production_deployment_id = Get-OptionalJsonProperty -InputObject $priorProduction -Name "id"
        production_aliases = if ($Promote) { @($allProductionAliases) } else { @() }
        promoted_deployment_id = Get-OptionalJsonProperty -InputObject $promotedDeployment -Name "id"
        production_deployment_id = Get-OptionalJsonProperty -InputObject $production -Name "id"
        live_trading_enabled = $false
        broker_execution_enabled = $false
        research_only = $true
        status = if ($Promote) { "PRODUCTION_VERIFIED" } else { "PREVIEW_VERIFIED" }
    }
    $journalNeedsPostTransition = $null -eq $existingJournal -or [string]$existingJournal.phase -eq "PRE_MUTATION"
    if ($Promote -and $journalNeedsPostTransition) {
        $postJournal = New-VercelPublicationJournalPayload `
            -Phase "POST_ALIASES" -Sequence 1 `
            -CandidateDeployment $journalCandidate `
            -PreviewManifest $previewManifest `
            -PackageManifestSha256 $packageManifestSha256 `
            -PackageMapSha256 $packageMapSha256 `
            -RemoteFileAttestationSha256 $remoteFileAttestationSha256 `
            -PromotedRemoteFileAttestationSha256 $promotedRemoteFileAttestationSha256 `
            -DeploymentOperationId $deploymentCasOperationId `
            -DeploymentRequestSha256 $deploymentCasRequestSha256 `
            -CandidateBuildManifestSha256 $candidateBuildManifestSha256 `
            -CandidateReleaseManifestSha256 $candidateReleaseManifestSha256 `
            -CandidatePublicArtifactRootSha256 ([string]$authorizedArtifactIdentity.public_artifact_root_sha256) `
            -CandidateManifestSha256 $candidateManifestSha256 `
            -PriorAliases $journalPriorAliases `
            -PromotedDeployment ([pscustomobject]@{ id = [string]$promotedDeploymentId; url = [string]$promotedUrl }) `
            -ResultPayload $result `
            -ExpectedPublicationMarketDate $resolvedExpectedMarketDate `
            -PrepublicationAuthorization $PrepublicationAuthorizationId `
            -DailyLedgerAuthorization $DailyLedgerAuthorizationId `
            -PriorJournalHash (Get-VercelFileSha256 -Path $journalPath)
        $null = Write-VercelPublicationJournal -Payload $postJournal -Transition
    }
    Test-VercelPromotionSeam "result_write"
    Write-VercelResultAtomic -Payload $result
    if ($Promote) {
        Test-VercelPromotionSeam "after_result_write_before_complete"
        $preCompleteJournal = Get-VercelPublicationJournal
        if ($null -eq $preCompleteJournal -or
            -not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $preCompleteJournal)) {
            throw "Production aliases or promotion metadata changed before COMPLETE sealing."
        }
        $completeJournal = New-VercelPublicationJournalPayload `
            -Phase "COMPLETE" -Sequence 2 `
            -CandidateDeployment $journalCandidate `
            -PreviewManifest $previewManifest `
            -PackageManifestSha256 $packageManifestSha256 `
            -PackageMapSha256 $packageMapSha256 `
            -RemoteFileAttestationSha256 $remoteFileAttestationSha256 `
            -PromotedRemoteFileAttestationSha256 $promotedRemoteFileAttestationSha256 `
            -DeploymentOperationId $deploymentCasOperationId `
            -DeploymentRequestSha256 $deploymentCasRequestSha256 `
            -CandidateBuildManifestSha256 $candidateBuildManifestSha256 `
            -CandidateReleaseManifestSha256 $candidateReleaseManifestSha256 `
            -CandidatePublicArtifactRootSha256 ([string]$authorizedArtifactIdentity.public_artifact_root_sha256) `
            -CandidateManifestSha256 $candidateManifestSha256 `
            -PriorAliases $journalPriorAliases `
            -PromotedDeployment ([pscustomobject]@{ id = [string]$promotedDeploymentId; url = [string]$promotedUrl }) `
            -ResultPayload $result `
            -ExpectedPublicationMarketDate $resolvedExpectedMarketDate `
            -PrepublicationAuthorization $PrepublicationAuthorizationId `
            -DailyLedgerAuthorization $DailyLedgerAuthorizationId `
            -PriorJournalHash (Get-VercelFileSha256 -Path $journalPath)
        if (-not (Test-VercelPromotedCandidateSetMatchesJournal -Journal $preCompleteJournal)) {
            throw "Production aliases or promotion metadata changed at COMPLETE sealing."
        }
        $null = Write-VercelPublicationJournal -Payload $completeJournal -Transition
    }
}
catch {
    $publicationError = $_.Exception.Message
    # The transition helper uses atomic replace.  It may have durably sealed
    # COMPLETE even when its stdout/return path faults.  Re-read the strict
    # journal before any compensating provider mutation; terminal success must
    # converge the candidate, never roll it back under a contradictory receipt.
    if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
        try { $caughtJournal = Get-VercelPublicationJournal }
        catch {
            throw "$publicationError Current Vercel journal could not be revalidated; no provider compensation was attempted."
        }
        if ([string]$caughtJournal.phase -eq 'COMPLETE') {
            Write-Output (Resolve-VercelCompletePublicationJournal -Journal $caughtJournal)
            return
        }
        if ([string]$caughtJournal.phase -eq 'COMPENSATED') {
            throw "$publicationError Vercel publication is already terminally compensated."
        }
    }
    if ($promoted -and $priorProductionAliases.Count -eq $allProductionAliases.Count) {
        if ($null -eq $caughtJournal) {
            throw "$publicationError No strict Vercel journal is available for compensation classification."
        }
        $catchPlan = Get-VercelCompensationPlan -Journal $caughtJournal
        if ($catchPlan.foreign_count -gt 0) {
            throw "$publicationError A production alias is foreign to the prior/candidate journal; no provider rollback was attempted."
        }
        if ($catchPlan.candidate_count -eq 0) {
            Invoke-VercelPublicationCompensation `
                -Journal $caughtJournal -FailureType "publication_failed_before_provider_mutation"
            throw $publicationError
        }
        $rollbackErrors = @()
        $rollbackProofs = @()
        $primaryRollbackProof = $null
        $rollbackCacheBuster = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        try {
            $priorPrimary = $priorProductionAliases[[string]$ProductionAlias]
            if ($null -eq $priorPrimary -or -not $priorPrimary.id) {
                throw "No complete prior primary deployment snapshot exists."
            }
            $primaryPlan = @($catchPlan.records | Where-Object {
                [string]$_.alias -eq [string]$ProductionAlias
            })[0]
            if ([string]$primaryPlan.state -eq 'candidate') {
                $primaryBeforeRollback = Get-VercelAliasObservation ([string]$ProductionAlias)
                if ([string]$primaryBeforeRollback.id -cne [string]$primaryPlan.observed.id -or
                    (Normalize-VercelDeploymentUrl $primaryBeforeRollback.url) -cne
                        (Normalize-VercelDeploymentUrl $primaryPlan.observed.url)) {
                    throw "Primary production alias changed before rollback."
                }
                Request-VercelProductionRollback -DeploymentId ([string]$priorPrimary.id)
            }
            $primaryAfterRollback = Get-VercelAliasObservation -Alias $ProductionAlias
            $primaryAfterId = [string](Get-OptionalJsonProperty `
                -InputObject $primaryAfterRollback -Name "id")
            if ($primaryAfterId -ne [string]$priorPrimary.id) {
                throw "Primary production rollback resolved to an unexpected deployment."
            }
            $primaryRollbackProof = [ordered]@{
                alias = [string]$ProductionAlias
                expected_deployment_id = [string]$priorPrimary.id
                observed_deployment_id = $primaryAfterId
                restored = $true
            }
        }
        catch {
            $rollbackErrors += "Primary production rollback: $($_.Exception.Message)"
        }
        foreach ($alias in $allProductionAliases) {
            try {
                $priorAlias = $priorProductionAliases[[string]$alias]
                if ($null -eq $priorAlias -or -not $priorAlias.url) {
                    throw "No complete prior deployment snapshot exists for $alias."
                }
                $currentAlias = Get-VercelAliasObservation ([string]$alias)
                $isPrior = [string]$currentAlias.id -ceq [string]$priorAlias.id -and
                    (Normalize-VercelDeploymentUrl $currentAlias.url) -ceq
                        (Normalize-VercelDeploymentUrl $priorAlias.url)
                $isCandidate = $null -ne $catchPlan.candidate -and
                    [string]$currentAlias.id -ceq [string]$catchPlan.candidate.id -and
                    (Normalize-VercelDeploymentUrl $currentAlias.url) -ceq
                        (Normalize-VercelDeploymentUrl $catchPlan.candidate.url)
                if (-not $isPrior -and -not $isCandidate) {
                    throw "Production alias changed to a foreign deployment before rollback: $alias"
                }
                if ($isCandidate) {
                    Set-VercelAlias `
                        -DeploymentId ([string]$priorAlias.id) `
                        -DeploymentUrl ([string]$priorAlias.url) `
                        -AliasUrl ([string]$alias) `
                        -Label "Production rollback for $alias"
                }
                $rollbackProofs += Assert-VercelAliasRestored `
                    -AliasUrl ([string]$alias) `
                    -PriorAlias $priorAlias `
                    -CacheBuster $rollbackCacheBuster
            }
            catch {
                $rollbackErrors += $_.Exception.Message
            }
        }
        $rollbackSucceeded = $rollbackErrors.Count -eq 0
        $rollbackReceipt = [ordered]@{
            schema_version = "dawnstrike.daily_deployment_rollback.v1"
            generated_at = [DateTimeOffset]::UtcNow.ToString("o")
            project_id = $ProjectId
            candidate_preview_deployment_id = Get-OptionalJsonProperty `
                -InputObject $deployment -Name "id"
            candidate_promoted_deployment_id = Get-OptionalJsonProperty `
                -InputObject $promotedDeployment -Name "id"
            candidate_source_sha = $expectedSourceSha
            candidate_source_tree = $expectedSourceTree
            candidate_vercel_source_manifest_sha256 = if (Test-Path -LiteralPath (Join-Path $stage "vercel-source-manifest.json") -PathType Leaf) {
                Get-VercelFileSha256 -Path (Join-Path $stage "vercel-source-manifest.json")
            }
            else { $null }
            candidate_vercel_package_manifest_sha256 = $packageManifestSha256
            primary_rollback_proof = $primaryRollbackProof
            aliases = @($rollbackProofs)
            alias_errors = @($rollbackErrors)
            candidate_no_longer_live = [bool]$rollbackSucceeded
            status = if ($rollbackSucceeded) { "ROLLED_BACK" } else { "ROLLBACK_FAILED" }
            publication_error = $publicationError
        }
        try {
            $rollbackJson = $rollbackReceipt | ConvertTo-Json -Depth 20
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            $receiptPath = $rollbackResultPath
            if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
                $receiptPath = Join-Path `
                    (Split-Path -Parent $rollbackResultPath) `
                    "daily-deployment-rollback-result-$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()).json"
            }
            Assert-VercelContainedNonReparsePath -RootPath $resolvedRoot -TargetPath $receiptPath
            [System.IO.File]::WriteAllText($receiptPath, $rollbackJson, $utf8NoBom)
        }
        catch {
            throw "$publicationError Rollback receipt write failed: $($_.Exception.Message)"
        }
        if (-not $rollbackSucceeded) {
            throw "$publicationError Rollback errors: $($rollbackErrors -join '; ')"
        }
        if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
            try {
                $failedJournal = Get-VercelPublicationJournal
                if ($null -ne $failedJournal -and [string]$failedJournal.phase -ne "COMPLETE") {
                    Invoke-VercelPublicationCompensation `
                        -Journal $failedJournal `
                        -FailureType "publication_failure_rollback_completed"
                }
            }
            catch {
                throw "$publicationError Rollback completed but terminal compensation could not be persisted: $($_.Exception.Message)"
            }
        }
    }
    elseif ($promoted) {
        throw "$publicationError Rollback blocked: a complete per-alias production snapshot was not captured."
    }
    throw $publicationError
}
}
finally {
    Release-VercelPublicationLock
}
