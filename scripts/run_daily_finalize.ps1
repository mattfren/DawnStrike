[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [int]$RetryLimit = 2,
    [int]$RetryDelaySeconds = 900
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$dbPath = Join-Path $resolvedRoot "data\shadow_real.sqlite"
$outputPath = Join-Path $resolvedRoot "build\public"

if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) {
    throw "Dawnstrike database not found: $dbPath"
}

Push-Location $resolvedRoot
try {
    & py.exe scripts\build_public.py `
        --db-path $dbPath `
        --out-dir $outputPath `
        --date $MarketDate `
        --retry-limit $RetryLimit `
        --retry-delay-seconds $RetryDelaySeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Daily finalize failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
