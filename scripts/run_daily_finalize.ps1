[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$MarketDate = (Get-Date).ToString("yyyy-MM-dd"),
    [int]$RetryLimit = 2,
    [int]$RetryDelaySeconds = 900,
    [ValidateSet("LocalOnly", "Preview", "Production")]
    [string]$PublicationMode = "Production",
    [string]$VercelProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [string]$ProductionUrl = "https://dawnstrike-command-center-x3.vercel.app"
)

$ErrorActionPreference = "Stop"
$runtime = (Resolve-Path $RuntimeRoot).Path
New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
$state = (Resolve-Path $StateRoot).Path
. (Join-Path $PSScriptRoot "import_dawnstrike_environment.ps1")
Import-DawnstrikeEnvironment -StateRoot $state
$dbPath = Join-Path $state "shadow_real.sqlite"
$paperOpsRoot = Join-Path $state "v2_paper_ops_live"
$outputPath = Join-Path $runtime "build\public"
$resultPath = Join-Path $outputPath "daily-finalize-result.json"
$deploymentResult = Join-Path $runtime "build\daily-deployment-result.json"
$marketDateWasExplicit = $PSBoundParameters.ContainsKey("MarketDate")

if (-not (Test-Path -LiteralPath $dbPath -PathType Leaf)) {
    throw "Dawnstrike durable state database not found: $dbPath"
}
New-Item -ItemType Directory -Path $paperOpsRoot -Force | Out-Null

Push-Location $runtime
try {
    if (-not $marketDateWasExplicit) {
        & py.exe -m intraday_scanner.services.market_calendar --date $MarketDate
        $calendarExit = $LASTEXITCODE
        if ($calendarExit -eq 10) {
            Write-Output (
                "Skipping non-market date $MarketDate; no publication was attempted."
            )
            exit 0
        }
        if ($calendarExit -ne 0) {
            throw "Market calendar failed with exit code $calendarExit"
        }
    }

    & py.exe scripts\build_public.py `
        --db-path $dbPath `
        --state-root $state `
        --paper-ops-root $paperOpsRoot `
        --out-dir $outputPath `
        --date $MarketDate `
        --retry-limit $RetryLimit `
        --retry-delay-seconds $RetryDelaySeconds
    $buildExit = $LASTEXITCODE

    if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
        & py.exe scripts\send_daily_finalize_notification.py `
            --result-file $resultPath `
            --db-path $dbPath
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Daily finalize notification could not be recorded or sent."
        }
    }
    if ($buildExit -ne 0) {
        Write-Output (
            "Daily finalize remained degraded or failed; production was not " +
            "updated. Exit code: $buildExit"
        )
        exit $buildExit
    }
    if ($PublicationMode -eq "LocalOnly") {
        exit 0
    }

    try {
        & (Join-Path $runtime "scripts\publish_vercel_public.ps1") `
            -ProjectRoot $runtime `
            -ProjectId $VercelProjectId `
            -Promote:($PublicationMode -eq "Production")
    }
    catch {
        $failure = $_.Exception.Message
        & py.exe scripts\record_daily_stage.py `
            --db-path $dbPath `
            --market-date $MarketDate `
            --stage publication `
            --status FAILED `
            --runtime-root $runtime `
            --state-root $state `
            --exit-code 2 `
            --error-code vercel_publication_failed `
            --error-detail $failure
        throw
    }

    if (-not (Test-Path -LiteralPath $deploymentResult -PathType Leaf)) {
        throw "Verified deployment result was not written: $deploymentResult"
    }
    & py.exe scripts\record_daily_stage.py `
        --db-path $dbPath `
        --market-date $MarketDate `
        --stage publication `
        --status COMPLETE `
        --runtime-root $runtime `
        --state-root $state `
        --exit-code 0 `
        --result-file $deploymentResult `
        --output-file $deploymentResult
    if ($LASTEXITCODE -ne 0) {
        throw "Verified deployment could not be recorded in the shared run ledger."
    }
    & py.exe scripts\send_daily_finalize_notification.py `
        --result-file $resultPath `
        --db-path $dbPath `
        --deployment-url $ProductionUrl
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Post-deployment notification could not be recorded or sent."
    }
}
finally {
    Pop-Location
}
