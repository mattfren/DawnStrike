[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage",
    [string]$ExpectedSourceSha = "",
    [string]$ExpectedSourceTree = ""
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$executingRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not [string]::Equals(
    [System.IO.Path]::GetFullPath($resolvedRoot).TrimEnd('\'),
    $executingRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "Vercel stage builder must execute from the exact ProjectRoot being admitted."
}
. (Join-Path $resolvedRoot "scripts\vercel_source_contract.ps1")

function Assert-VercelTreeHasNoReparseDescendants {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) { return }
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue([System.IO.Path]::GetFullPath($Root))
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "$Label contains a reparse descendant: $($item.FullName)"
            }
            if ($item.PSIsContainer) { $pending.Enqueue($item.FullName) }
        }
    }
}

$script:VercelPublicArtifactFiles = @(
    'assets/dawnstrike.css',
    'assets/dawnstrike.js',
    'build-manifest.json',
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

function Assert-VercelPublicArtifactInventory {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowSourceManifest
    )
    if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
        throw "Public artifact inventory root is missing: $Root"
    }
    Assert-VercelTreeHasNoReparseDescendants -Root $Root -Label 'Public artifact inventory'
    $expected = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($name in $script:VercelPublicArtifactFiles) { $null = $expected.Add($name) }
    if ($AllowSourceManifest) { $null = $expected.Add('vercel-source-manifest.json') }
    $observed = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    foreach ($file in @(Get-ChildItem -LiteralPath $rootPath -File -Recurse -Force -ErrorAction Stop)) {
        $relative = $file.FullName.Substring($rootPath.Length + 1).Replace('\', '/')
        if (-not $observed.Add($relative)) {
            throw "Public artifact contains a duplicate relative path: $relative"
        }
    }
    $unexpected = @($observed | Where-Object { -not $expected.Contains($_) } | Sort-Object)
    $missing = @($expected | Where-Object { -not $observed.Contains($_) } | Sort-Object)
    if ($unexpected.Count -gt 0 -or $missing.Count -gt 0) {
        throw "Public artifact inventory mismatch; unexpected=$($unexpected -join ','); missing=$($missing -join ',')"
    }

    $manifestPath = Join-Path $rootPath 'build-manifest.json'
    $rawManifest = [System.IO.File]::ReadAllText($manifestPath)
    Assert-VercelJsonObjectKeysUnique -RawJson $rawManifest
    try { $manifest = $rawManifest | ConvertFrom-Json }
    catch { throw 'Public build manifest is unreadable.' }
    $hashProperties = @($manifest.file_hashes.PSObject.Properties)
    $expectedHashes = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($name in $script:VercelPublicArtifactFiles) {
        if ($name -cne 'build-manifest.json') { $null = $expectedHashes.Add($name) }
    }
    $observedHashes = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
    foreach ($property in $hashProperties) {
        $relative = [string]$property.Name
        if (-not $observedHashes.Add($relative) -or -not $expectedHashes.Contains($relative)) {
            throw "Public build manifest contains an unexpected or duplicate file hash: $relative"
        }
        if ([string]$property.Value -cnotmatch '^[0-9a-f]{64}$') {
            throw "Public build manifest contains an invalid file hash: $relative"
        }
        $actual = Get-VercelFileSha256 -Path (Join-Path $rootPath ($relative.Replace('/', '\')))
        if ($actual -cne [string]$property.Value) {
            throw "Public build manifest file hash mismatch: $relative"
        }
    }
    $missingHashes = @($expectedHashes | Where-Object { -not $observedHashes.Contains($_) })
    if ($missingHashes.Count -gt 0) {
        throw "Public build manifest is missing file hashes: $($missingHashes -join ',')"
    }
}

function Get-VercelPublicArtifactFingerprint {
    param([Parameter(Mandatory = $true)][string]$Root)
    Assert-VercelPublicArtifactInventory -Root $Root
    $lines = foreach ($name in @($script:VercelPublicArtifactFiles | Sort-Object)) {
        $path = Join-Path $Root ($name.Replace('/', '\'))
        "$name=$(Get-VercelFileSha256 -Path $path)"
    }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($lines -join "`n"))
    $digest = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($digest.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $digest.Dispose() }
}
$publicSource = Join-Path $resolvedRoot "build\public"
$buildRoot = [System.IO.Path]::GetFullPath((Join-Path $resolvedRoot "build"))
$stageCandidate = if ([System.IO.Path]::IsPathRooted($StageRoot)) {
    $StageRoot
} else {
    Join-Path $resolvedRoot $StageRoot
}
$stage = [System.IO.Path]::GetFullPath($stageCandidate)
$buildPrefix = $buildRoot.TrimEnd("\") + "\"
$publicSourcePath = [System.IO.Path]::GetFullPath($publicSource)
$publicSourcePrefix = $publicSourcePath.TrimEnd("\") + "\"
if (
    $stage.Equals($buildRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    -not $stage.StartsWith($buildPrefix, [System.StringComparison]::OrdinalIgnoreCase)
) {
    throw "StageRoot must resolve inside the project build directory: $buildRoot"
}
if (
    $stage.Equals($publicSourcePath, [System.StringComparison]::OrdinalIgnoreCase) -or
    $stage.StartsWith($publicSourcePrefix, [System.StringComparison]::OrdinalIgnoreCase)
) {
    throw "StageRoot must not overlap the source public artifact: $publicSourcePath"
}
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $buildRoot -Label "Vercel build root"
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $publicSourcePath -Label "Public artifact root"
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
if (Test-Path -LiteralPath $stage) {
    Assert-VercelTreeHasNoReparseDescendants -Root $stage -Label "Vercel stage cleanup"
    Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage cleanup"
    Remove-Item -LiteralPath $stage -Recurse -Force
}
$source = Get-VercelGitSourceContract -Root $resolvedRoot
if (-not $ExpectedSourceSha) { $ExpectedSourceSha = $source.head }
if (-not $ExpectedSourceTree) { $ExpectedSourceTree = $source.tree }
Assert-VercelGitSourceStable `
    -Root $resolvedRoot `
    -ExpectedSourceSha $ExpectedSourceSha `
    -ExpectedSourceTree $ExpectedSourceTree
$stagePublic = Join-Path $stage "public"
$functionPublic = Join-Path $stage "api\public"

if (-not (Test-Path -LiteralPath (Join-Path $publicSource "build-manifest.json") -PathType Leaf)) {
    throw "Build the public artifact before staging it."
}
Assert-VercelPublicArtifactInventory -Root $publicSourcePath
$authorizedSourceFingerprint = Get-VercelPublicArtifactFingerprint -Root $publicSourcePath
New-Item -ItemType Directory -Force -Path $stagePublic, $functionPublic | Out-Null
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $publicSourcePath -Label "Public artifact root"
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
Assert-VercelTreeHasNoReparseDescendants -Root $publicSourcePath -Label "Public artifact copy source"
Assert-VercelTreeHasNoReparseDescendants -Root $stage -Label "Vercel stage copy destination"
Copy-Item -Path (Join-Path $publicSource "*") -Destination $stagePublic -Recurse -Force
$stagedFingerprint = Get-VercelPublicArtifactFingerprint -Root $stagePublic
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $publicSourcePath -Label "Public artifact root"
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
Assert-VercelTreeHasNoReparseDescendants -Root $publicSourcePath -Label "Public artifact copy source"
Assert-VercelTreeHasNoReparseDescendants -Root $stage -Label "Vercel stage copy destination"
$sourceAfterFingerprint = Get-VercelPublicArtifactFingerprint -Root $publicSourcePath
if ($authorizedSourceFingerprint -cne $stagedFingerprint -or
    $authorizedSourceFingerprint -cne $sourceAfterFingerprint) {
    throw "Public artifact changed while the immutable Vercel snapshot was copied."
}
# The function package is derived only from the already verified snapshot.  It
# never rereads mutable build/public, so static and function bytes cannot split.
Copy-Item -Path (Join-Path $stagePublic "*") -Destination $functionPublic -Recurse -Force
$functionFingerprint = Get-VercelPublicArtifactFingerprint -Root $functionPublic
if ($functionFingerprint -cne $stagedFingerprint) {
    throw "Static and function public artifact snapshots diverged."
}
Assert-VercelTreeHasNoReparseDescendants -Root $stage -Label "Vercel staged public copies"
# Extract executable entrypoints from the verified commit, rather than copying
# a mutable working-tree file.  This also makes a staged API-byte mismatch
# independently detectable before any Vercel command is run.
$stageApi = Join-Path $stage "api"
New-Item -ItemType Directory -Force -Path $stageApi | Out-Null
foreach ($apiPath in @("api/health.py", "api/readiness.py")) {
    Write-VercelGitBlob `
        -Root $resolvedRoot `
        -Commit $ExpectedSourceSha `
        -RelativePath $apiPath `
        -Destination (Join-Path $stage ($apiPath -replace "/", "\"))
}
# The API reads the exact packaged files under api/public.  Keep this module
# metadata-only: embedding snapshots or manifests creates duplicate sources of
# truth and can let a stale caller/environment state shadow packaged bytes.
$stateModule = "PUBLIC_STATE = {'static_file_hashes_verified': True}`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $stage "api\public_state.py"), $stateModule, $utf8NoBom)

$securityHeaders = @(
    [ordered]@{
        key = 'Content-Security-Policy'
        value = "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; upgrade-insecure-requests"
    },
    [ordered]@{ key = 'X-Content-Type-Options'; value = 'nosniff' },
    [ordered]@{ key = 'Referrer-Policy'; value = 'no-referrer' },
    [ordered]@{ key = 'Permissions-Policy'; value = 'camera=(), microphone=(), geolocation=(), payment=(), usb=()' },
    [ordered]@{ key = 'X-Frame-Options'; value = 'DENY' },
    [ordered]@{ key = 'Cross-Origin-Opener-Policy'; value = 'same-origin' },
    [ordered]@{ key = 'Cross-Origin-Resource-Policy'; value = 'same-origin' }
)
$config = @{
    '$schema' = 'https://openapi.vercel.sh/vercel.json'
    version = 2
    outputDirectory = 'public'
    functions = @{
        'api/health.py' = @{ includeFiles = 'api/public/**'; maxDuration = 10 }
        'api/readiness.py' = @{ includeFiles = 'api/public/**'; maxDuration = 10 }
    }
    headers = @(
        [ordered]@{ source = '/(.*)'; headers = $securityHeaders }
    )
} | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText((Join-Path $stage "vercel.json"), $config, $utf8NoBom)
$stagePyproject = @"
[project]
name = "dawnstrike-public-stage"
version = "0.0.0"
requires-python = ">=3.13"
dependencies = []
"@
[System.IO.File]::WriteAllText((Join-Path $stage "pyproject.toml"), $stagePyproject, $utf8NoBom)
Assert-VercelGitSourceStable `
    -Root $resolvedRoot `
    -ExpectedSourceSha $ExpectedSourceSha `
    -ExpectedSourceTree $ExpectedSourceTree `
    -AllowedStageRoot $stage
$sourceManifest = [ordered]@{
    schema_version = "dawnstrike.vercel_source_manifest.v1"
    source_sha = $ExpectedSourceSha.ToLowerInvariant()
    source_tree = $ExpectedSourceTree.ToLowerInvariant()
    api_sha256 = [ordered]@{
        "api/health.py" = Get-VercelFileSha256 -Path (Join-Path $stage "api\health.py")
        "api/readiness.py" = Get-VercelFileSha256 -Path (Join-Path $stage "api\readiness.py")
    }
}
$sourceManifestJson = $sourceManifest | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText(
    (Join-Path $stage "vercel-source-manifest.json"),
    $sourceManifestJson,
    $utf8NoBom
)
Assert-VercelContainedPathNoReparse -Root $resolvedRoot -Target $stage -Label "Vercel stage root"
# Keep the same provenance receipt in the static/function package so the
# promoted deployment can be inspected remotely without trusting build logs.
Copy-Item -LiteralPath (Join-Path $stage "vercel-source-manifest.json") `
    -Destination (Join-Path $stagePublic "vercel-source-manifest.json") -Force
Copy-Item -LiteralPath (Join-Path $stage "vercel-source-manifest.json") `
    -Destination (Join-Path $functionPublic "vercel-source-manifest.json") -Force
Assert-VercelPublicArtifactInventory -Root $stagePublic -AllowSourceManifest
Assert-VercelPublicArtifactInventory -Root $functionPublic -AllowSourceManifest
Write-Output "Staged minimal Vercel publication at $stage"
