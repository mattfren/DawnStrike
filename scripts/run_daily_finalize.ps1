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
$paperOpsRoot = Join-Path $resolvedRoot "data\v2_paper_ops_live"
$outputPath = Join-Path $resolvedRoot "build\public"
$marketDateWasExplicit = $PSBoundParameters.ContainsKey("MarketDate")

if (-not $marketDateWasExplicit) {
    Push-Location $resolvedRoot
    try {
        $isMarketDay = (& py.exe -c "from datetime import date; from intraday_scanner.services.market_calendar import is_market_day; print('1' if is_market_day(date.today()) else '0')").Trim()
    }
    finally {
        Pop-Location
    }
    if ($isMarketDay -ne "1") {
        Write-Output "Skipping non-market date $MarketDate; no publication was attempted."
        exit 0
    }
}

if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) {
    throw "Dawnstrike database not found: $dbPath"
}

Push-Location $resolvedRoot
try {
    & py.exe scripts\build_public.py `
        --db-path $dbPath `
        --paper-ops-root $paperOpsRoot `
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
