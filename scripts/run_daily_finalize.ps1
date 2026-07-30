[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$SourceRoot = "C:\Users\MattFields\Dawnstrike",
    [string]$PaperOpsRoot = "",
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [int]$RetryLimit = 2,
    [int]$RetryDelaySeconds = 900,
    [ValidateSet("LocalOnly", "Preview", "Production")]
    [string]$PublicationMode = "LocalOnly",
    [string]$VercelProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [switch]$AllowDegraded
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$resolvedSourceRoot = (Resolve-Path $SourceRoot).Path
$sourceDbPath = Join-Path $resolvedSourceRoot "data\shadow_real.sqlite"
$dbPath = Join-Path $resolvedRoot "data\daily_publication.sqlite"
if (-not $PaperOpsRoot) {
    $PaperOpsRoot = Join-Path $resolvedSourceRoot "data\v2_paper_ops_live"
}
$resolvedPaperOpsRoot = (Resolve-Path $PaperOpsRoot).Path
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

if (-not (Test-Path -LiteralPath $sourceDbPath -PathType Leaf)) {
    throw "Dawnstrike source database not found: $sourceDbPath"
}

Push-Location $resolvedRoot
try {
    & py.exe scripts\snapshot_sqlite.py `
        --source-db $sourceDbPath `
        --target-db $dbPath
    if ($LASTEXITCODE -ne 0) {
        throw "Read-only SQLite snapshot failed with exit code $LASTEXITCODE"
    }

    & py.exe scripts\build_public.py `
        --db-path $dbPath `
        --paper-ops-root $resolvedPaperOpsRoot `
        --out-dir $outputPath `
        --date $MarketDate `
        --retry-limit $RetryLimit `
        --retry-delay-seconds $RetryDelaySeconds
    if ($LASTEXITCODE -ne 0) {
        throw "Daily finalize failed with exit code $LASTEXITCODE"
    }

    if ($PublicationMode -ne "LocalOnly") {
        $publishArgs = @{
            ProjectRoot = $resolvedRoot
            ProjectId = $VercelProjectId
            AllowDegraded = [bool]$AllowDegraded
            Promote = ($PublicationMode -eq "Production")
        }
    & .\scripts\publish_vercel_public.ps1 @publishArgs
    }

    & py.exe scripts\send_daily_finalize_notification.py `
        --result-file (Join-Path $outputPath "daily-finalize-result.json") `
        --db-path $dbPath
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Daily finalize notification could not be recorded or sent."
    }
}
finally {
    Pop-Location
}
