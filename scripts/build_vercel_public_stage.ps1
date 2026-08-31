[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage",
    [string]$ExpectedSourceSha = "",
    [string]$ExpectedSourceTree = ""
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
. (Join-Path $resolvedRoot "scripts\vercel_source_contract.ps1")
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
if (Test-Path -LiteralPath $stage) {
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
New-Item -ItemType Directory -Force -Path $stagePublic, $functionPublic | Out-Null
Copy-Item -Path (Join-Path $publicSource "*") -Destination $stagePublic -Recurse -Force
Copy-Item -Path (Join-Path $publicSource "*") -Destination $functionPublic -Recurse -Force
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
Copy-Item -LiteralPath (Join-Path $resolvedRoot "api\public_state.py") -Destination (Join-Path $stage "api\public_state.py") -Force

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
Write-Output "Staged minimal Vercel publication at $stage"
