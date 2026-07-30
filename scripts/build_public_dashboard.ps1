[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [int]$RetryLimit = 2,
    [int]$RetryDelaySeconds = 900
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
Push-Location $resolvedRoot
try {
    & py.exe scripts\build_public.py `
        --db-path (Join-Path $resolvedRoot "data\shadow_real.sqlite") `
        --out-dir (Join-Path $resolvedRoot "build\public") `
        --date $MarketDate `
        --retry-limit $RetryLimit `
        --retry-delay-seconds $RetryDelaySeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Public dashboard build failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
