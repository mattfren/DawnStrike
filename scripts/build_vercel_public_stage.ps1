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

if (-not (Test-Path -LiteralPath (Join-Path $publicSource "build-manifest.json") -PathType Leaf)) {
    throw "Build the public artifact before staging it."
}
if (Test-Path -LiteralPath $stage) {
    Remove-Item -LiteralPath $stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stagePublic, (Join-Path $stage "api") | Out-Null
Copy-Item -Path (Join-Path $publicSource "*") -Destination $stagePublic -Recurse -Force
Copy-Item -LiteralPath (Join-Path $resolvedRoot "api\health.py") -Destination (Join-Path $stage "api\health.py") -Force
Copy-Item -LiteralPath (Join-Path $resolvedRoot "api\readiness.py") -Destination (Join-Path $stage "api\readiness.py") -Force

$config = @{
    '$schema' = 'https://openapi.vercel.sh/vercel.json'
    version = 2
    outputDirectory = 'public'
    functions = @{
        'api/health.py' = @{ includeFiles = 'public/**'; maxDuration = 10 }
        'api/readiness.py' = @{ includeFiles = 'public/**'; maxDuration = 10 }
    }
} | ConvertTo-Json -Depth 6
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
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
