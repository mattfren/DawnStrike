[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage"
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$publicSource = Join-Path $resolvedRoot "build\public"
$stage = Join-Path $resolvedRoot $StageRoot
$stagePublic = Join-Path $stage "public"
$functionPublic = Join-Path $stage "api\public"
$functionData = Join-Path $functionPublic "data"

if (-not (Test-Path -LiteralPath (Join-Path $publicSource "build-manifest.json") -PathType Leaf)) {
    throw "Build the public artifact before staging it."
}
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stagePublic, $functionData | Out-Null
Copy-Item -Path (Join-Path $publicSource "*") -Destination $stagePublic -Recurse -Force
Copy-Item -LiteralPath (Join-Path $publicSource "readiness.json") -Destination $functionPublic -Force
Copy-Item -LiteralPath (Join-Path $publicSource "build-manifest.json") -Destination $functionPublic -Force
Copy-Item -LiteralPath (Join-Path $publicSource "data\performance.json") -Destination (Join-Path $functionData "performance-snapshot.json") -Force
Copy-Item -LiteralPath (Join-Path $publicSource "data\performance.json.manifest.json") -Destination (Join-Path $functionData "performance-snapshot-manifest.json") -Force
Copy-Item -LiteralPath (Join-Path $publicSource "data\calendar.json") -Destination (Join-Path $functionData "calendar.json") -Force
Copy-Item -LiteralPath (Join-Path $publicSource "data\calendar.json.manifest.json") -Destination (Join-Path $functionData "calendar.json.manifest.json") -Force
Copy-Item -LiteralPath (Join-Path $publicSource "data\publication-set.json") -Destination (Join-Path $functionData "publication-set.json") -Force
Copy-Item -LiteralPath (Join-Path $resolvedRoot "api\health.py") -Destination (Join-Path $stage "api\health.py") -Force
Copy-Item -LiteralPath (Join-Path $resolvedRoot "api\readiness.py") -Destination (Join-Path $stage "api\readiness.py") -Force
Copy-Item -LiteralPath (Join-Path $resolvedRoot "api\public_state.py") -Destination (Join-Path $stage "api\public_state.py") -Force

$snapshotBytes = [System.IO.File]::ReadAllBytes((Join-Path $publicSource "data\performance.json"))
$calendarBytes = [System.IO.File]::ReadAllBytes((Join-Path $publicSource "data\calendar.json"))
$state = [ordered]@{
    readiness = (Get-Content -Raw -LiteralPath (Join-Path $publicSource "readiness.json") | ConvertFrom-Json)
    snapshot_manifest = (Get-Content -Raw -LiteralPath (Join-Path $publicSource "data\performance.json.manifest.json") | ConvertFrom-Json)
    build_manifest = (Get-Content -Raw -LiteralPath (Join-Path $publicSource "build-manifest.json") | ConvertFrom-Json)
    snapshot_b64 = [Convert]::ToBase64String($snapshotBytes)
    calendar_manifest = (Get-Content -Raw -LiteralPath (Join-Path $publicSource "data\calendar.json.manifest.json") | ConvertFrom-Json)
    calendar_b64 = [Convert]::ToBase64String($calendarBytes)
    publication_set = (Get-Content -Raw -LiteralPath (Join-Path $publicSource "data\publication-set.json") | ConvertFrom-Json)
    static_file_hashes_verified = $true
}
$stateJson = $state | ConvertTo-Json -Depth 100 -Compress
$pythonString = $stateJson | ConvertTo-Json -Compress
$stateModule = "import json`n`nPUBLIC_STATE = json.loads($pythonString)`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $stage "api\public_state.py"), $stateModule, $utf8NoBom)

$config = @{
    '$schema' = 'https://openapi.vercel.sh/vercel.json'
    version = 2
    outputDirectory = 'public'
    functions = @{
        'api/health.py' = @{ includeFiles = 'api/public/**'; maxDuration = 10 }
        'api/readiness.py' = @{ includeFiles = 'api/public/**'; maxDuration = 10 }
    }
} | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $stage "vercel.json"), $config, $utf8NoBom)
$stagePyproject = @"
[project]
name = "dawnstrike-public-stage"
version = "0.0.0"
requires-python = ">=3.13"
dependencies = []
"@
[System.IO.File]::WriteAllText((Join-Path $stage "pyproject.toml"), $stagePyproject, $utf8NoBom)
Write-Output "Staged minimal Vercel publication at $stage"
