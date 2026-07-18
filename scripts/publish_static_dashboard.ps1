[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')]
    [string]$RunDate,

    [string]$PaperOpsRoot = $(
        if ($env:DAWNSTRIKE_PAPER_OPS_ROOT) {
            $env:DAWNSTRIKE_PAPER_OPS_ROOT
        }
        else {
            'data\v2_paper_ops_live'
        }
    ),

    [string]$DatabasePath = $(
        if ($env:DAWNSTRIKE_DB_PATH) {
            $env:DAWNSTRIKE_DB_PATH
        }
        else {
            'data\shadow_real.sqlite'
        }
    ),

    [string]$OutputPath = 'assets\dashboard-data.json',

    [string]$PublishTarget = $(
        if ($env:DAWNSTRIKE_STATIC_DASHBOARD_PUBLISH_MODE) {
            $env:DAWNSTRIKE_STATIC_DASHBOARD_PUBLISH_MODE
        }
        else {
            'production'
        }
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $repoRoot

$vercelCliPackage = 'vercel@56.3.1'
$expectedVercelScope = 'mattfrens-projects'
$expectedVercelProject = 'dawnstrike-command-center-x3'
$expectedVercelProjectId = 'prj_5pef3EZF1u5YadebEz3dFjnkWOXy'
$canonicalProductionHost = 'dawnstrike-command-center-x3.vercel.app'

function Resolve-RepositoryPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PathValue
    )

    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $repoRoot $PathValue))
}

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $commandOutput = @(& $Command @Arguments 2>&1)
        $commandExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $commandOutput | ForEach-Object { Write-Host $_ }
    if ($commandExitCode -ne 0) {
        throw "$FailureMessage (exit $commandExitCode)."
    }
}

function Get-Sha256 {
    param(
        [Parameter(Mandatory = $true)]
        [string]$LiteralPath
    )

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $LiteralPath).Hash.ToLowerInvariant()
}

function Assert-DashboardPayload {
    param(
        [Parameter(Mandatory = $true)]
        [pscustomobject]$Payload,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedRunDate,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if ($Payload.schemaVersion -ne 'dawnstrike.static-dashboard.v3') {
        throw "$Label has unsupported schemaVersion '$($Payload.schemaVersion)'."
    }
    if ($Payload.latestRunDate -ne $ExpectedRunDate) {
        throw "$Label latestRunDate '$($Payload.latestRunDate)' does not equal '$ExpectedRunDate'."
    }
    if ($Payload.freshness.asOfDate -ne $ExpectedRunDate) {
        throw "$Label freshness date '$($Payload.freshness.asOfDate)' does not equal '$ExpectedRunDate'."
    }
    if ($Payload.freshness.statusAtGeneration -notin @('fresh', 'stale')) {
        throw "$Label has invalid freshness status '$($Payload.freshness.statusAtGeneration)'."
    }
    if (-not $Payload.freshness.deadlineAt) {
        throw "$Label is missing its freshness deadline."
    }
    if ($Payload.evidence.calendarTruthStatus -ne 'passed') {
        throw "$Label calendar truth is not passed."
    }
    if ($Payload.evidence.sourceBarTruthStatus -ne 'passed') {
        throw "$Label source-bar truth is not passed."
    }
    if ($Payload.evidence.paperOpsCalendarSha256 -notmatch '^[a-fA-F0-9]{64}$') {
        throw "$Label is missing a valid PaperOps calendar evidence hash."
    }
    if ($Payload.evidence.alphaDatabaseSha256 -notmatch '^[a-fA-F0-9]{64}$') {
        throw "$Label is missing a valid Alpha database evidence hash."
    }

    $latestAlpha = @(
        $Payload.alphaOps.days |
            Where-Object { $_.date -eq $ExpectedRunDate }
    ) | Select-Object -First 1
    if ($null -eq $latestAlpha) {
        throw "$Label is missing AlphaOps scan evidence for $ExpectedRunDate."
    }
    $alphaUnavailable = (
        -not $latestAlpha.sourceStatus -or
        -not $latestAlpha.status -or
        [string]$latestAlpha.sourceStatus -match '(?i)(no[_ ]?data|unavailable|failed|error)' -or
        [string]$latestAlpha.status -match '(?i)(no[_ ]?data|unavailable|failed|error)'
    )
    if ($alphaUnavailable) {
        throw "$Label has unavailable AlphaOps scan evidence for $ExpectedRunDate."
    }
    $explicitNoTrade = [string]$latestAlpha.decision -match '(?i)^NO[_ ]?(TRADE|PICKS)$'
    if ([int]$latestAlpha.pickCount -le 0 -and -not $explicitNoTrade) {
        throw "$Label has no retained AlphaOps picks or explicit no-trade decision for $ExpectedRunDate."
    }
}

function Assert-VercelProjectIdentity {
    $arguments = @(
        '--yes', $vercelCliPackage,
        'project', 'inspect', $expectedVercelProject,
        '--scope', $expectedVercelScope,
        '--no-color'
    )
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $projectOutput = @(& npx @arguments 2>&1)
        $projectExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $projectOutput | ForEach-Object { Write-Host $_ }
    if ($projectExit -ne 0) {
        throw "Could not verify the intended Vercel project (exit $projectExit)."
    }
    $projectText = $projectOutput -join "`n"
    $expectedProjectPath = "$expectedVercelScope/$expectedVercelProject"
    if (
        $projectText -notmatch [regex]::Escape($expectedProjectPath) -or
        $projectText -notmatch [regex]::Escape($expectedVercelProjectId)
    ) {
        throw "Vercel identity mismatch; expected $expectedProjectPath ($expectedVercelProjectId)."
    }
}

function Assert-RemoteDashboardArtifact {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,

        [Parameter(Mandatory = $true)]
        [string]$Label,

        [Parameter(Mandatory = $true)]
        [string]$ExpectedRunDate,

        [Parameter(Mandatory = $true)]
        [pscustomobject]$LocalPayload,

        [Parameter(Mandatory = $true)]
        [string]$ArtifactSha256
    )

    $remoteResponse = $null
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        try {
            $remoteResponse = Invoke-WebRequest -UseBasicParsing -Uri $Uri -Headers @{
                'Cache-Control' = 'no-cache'
            }
            if ($remoteResponse.StatusCode -eq 200) {
                break
            }
        }
        catch {
            if ($attempt -eq 6) {
                throw
            }
        }
        Start-Sleep -Seconds 2
    }
    if ($null -eq $remoteResponse -or $remoteResponse.StatusCode -ne 200) {
        throw "$Label dashboard asset could not be retrieved from $Uri."
    }

    $remoteContent = [string]$remoteResponse.Content
    $remotePayload = $remoteContent | ConvertFrom-Json
    Assert-DashboardPayload -Payload $remotePayload -ExpectedRunDate $ExpectedRunDate -Label $Label

    if ($remotePayload.evidence.paperOpsCalendarSha256 -ne $LocalPayload.evidence.paperOpsCalendarSha256) {
        throw "$Label PaperOps calendar evidence hash does not match the local verified artifact."
    }
    if ($remotePayload.evidence.alphaDatabaseSha256 -ne $LocalPayload.evidence.alphaDatabaseSha256) {
        throw "$Label Alpha database evidence hash does not match the local verified artifact."
    }

    $remoteBytes = [System.Text.Encoding]::UTF8.GetBytes($remoteContent)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $remoteSha256 = ([System.BitConverter]::ToString($sha.ComputeHash($remoteBytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
    if ($remoteSha256 -ne $ArtifactSha256) {
        throw "$Label artifact hash mismatch; expected $ArtifactSha256, received $remoteSha256."
    }
    Write-Host "$Label dashboard artifact verified: $Uri"
}

$normalizedTarget = $PublishTarget.Trim().ToLowerInvariant()
if ($normalizedTarget -notin @('production', 'preview', 'local', 'disabled')) {
    throw "Invalid publication target '$PublishTarget'. Expected production, preview, local, or disabled."
}

if ($normalizedTarget -eq 'disabled') {
    Write-Warning "Static dashboard publication is explicitly disabled for $RunDate; the hosted calendar will not be refreshed."
    exit 0
}

$paperRoot = Resolve-RepositoryPath -PathValue $PaperOpsRoot
$database = Resolve-RepositoryPath -PathValue $DatabasePath
$output = Resolve-RepositoryPath -PathValue $OutputPath

if (-not (Test-Path -LiteralPath $paperRoot -PathType Container)) {
    throw "Canonical PaperOps root does not exist: $paperRoot"
}
if (-not (Test-Path -LiteralPath $database -PathType Leaf)) {
    throw "Canonical Alpha database does not exist: $database"
}

$outputDirectory = Split-Path -Parent $output
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
$candidate = Join-Path $outputDirectory "dashboard-data-$RunDate-$([guid]::NewGuid().ToString('N')).candidate.json"
$replacementBackup = Join-Path $outputDirectory "dashboard-data-$RunDate-$([guid]::NewGuid().ToString('N')).backup.json"
$previousCandidateEnvironment = $env:DAWNSTRIKE_STATIC_DASHBOARD_CANDIDATE

try {
    Write-Host "[$(Get-Date -Format o)] Exporting a truth-gated dashboard candidate for $RunDate."
    Invoke-CheckedCommand -Command 'py' -Arguments @(
        '-m',
        'intraday_scanner.dashboard.static_dashboard_export',
        '--paper-ops-root', $paperRoot,
        '--db', $database,
        '--output', $candidate,
        '--date', $RunDate
    ) -FailureMessage 'Static dashboard export failed; no deployment was attempted'

    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw 'Static dashboard exporter returned success without writing its candidate artifact.'
    }

    $candidatePayload = Get-Content -Raw -LiteralPath $candidate | ConvertFrom-Json
    Assert-DashboardPayload -Payload $candidatePayload -ExpectedRunDate $RunDate -Label 'Local candidate'

    $env:DAWNSTRIKE_STATIC_DASHBOARD_CANDIDATE = $candidate
    Invoke-CheckedCommand -Command 'py' -Arguments @(
        '-m', 'pytest',
        'tests/test_static_dashboard_publish_contract.py',
        '--tb=short', '-q'
    ) -FailureMessage 'Static dashboard publication contract failed; no deployment was attempted'

    if (Test-Path -LiteralPath $output -PathType Leaf) {
        [System.IO.File]::Replace($candidate, $output, $replacementBackup)
        Remove-Item -Force -LiteralPath $replacementBackup
    }
    else {
        [System.IO.File]::Move($candidate, $output)
    }
    $artifactSha256 = Get-Sha256 -LiteralPath $output
    $localPayload = Get-Content -Raw -LiteralPath $output | ConvertFrom-Json
    Assert-DashboardPayload -Payload $localPayload -ExpectedRunDate $RunDate -Label 'Published local asset'

    Invoke-CheckedCommand -Command 'py' -Arguments @(
        '-m', 'pytest',
        'tests/test_static_dashboard_contract.py',
        '--tb=short', '-q'
    ) -FailureMessage 'Rendered static dashboard contract failed; no deployment was attempted'

    Write-Host "Local dashboard asset verified: date=$RunDate sha256=$artifactSha256"
    if ($normalizedTarget -eq 'local') {
        Write-Host 'Local publication completed; remote deployment was intentionally skipped.'
        exit 0
    }

    if ($localPayload.freshness.statusAtGeneration -ne 'fresh') {
        throw 'Remote publication is blocked because the generated dashboard artifact is stale.'
    }

    $canonicalOutput = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'assets\dashboard-data.json'))
    if ($output -ne $canonicalOutput) {
        throw "Remote publication requires the canonical asset path: $canonicalOutput"
    }

    Assert-VercelProjectIdentity

    $deployArguments = @(
        '--yes', $vercelCliPackage,
        'deploy', '--yes',
        '--project', $expectedVercelProject,
        '--scope', $expectedVercelScope
    )
    if ($normalizedTarget -eq 'production') {
        $deployArguments += '--prod'
    }

    Write-Host "[$(Get-Date -Format o)] Starting $normalizedTarget Vercel deployment after export and contract gates passed."
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $deployOutput = @(& npx @deployArguments 2>&1)
        $deployExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $deployOutput | ForEach-Object { Write-Host $_ }
    if ($deployExit -ne 0) {
        throw "Vercel $normalizedTarget deployment failed (exit $deployExit)."
    }

    $deploymentUrls = @(
        $deployOutput |
            ForEach-Object { [regex]::Matches([string]$_, 'https://[A-Za-z0-9.-]+\.vercel\.app') } |
            ForEach-Object { $_.Value }
    )
    if ($deploymentUrls.Count -eq 0) {
        throw 'Vercel reported success without a verifiable deployment URL.'
    }
    $deploymentUrl = $deploymentUrls[-1].TrimEnd('/')
    $remoteUri = "$deploymentUrl/assets/dashboard-data.json?artifact=$artifactSha256"

    Assert-RemoteDashboardArtifact -Uri $remoteUri -Label 'Deployment URL' -ExpectedRunDate $RunDate -LocalPayload $localPayload -ArtifactSha256 $artifactSha256
    if ($normalizedTarget -eq 'production') {
        $canonicalUri = "https://$canonicalProductionHost/assets/dashboard-data.json?artifact=$artifactSha256"
        Assert-RemoteDashboardArtifact -Uri $canonicalUri -Label 'Canonical production host' -ExpectedRunDate $RunDate -LocalPayload $localPayload -ArtifactSha256 $artifactSha256
    }

    Write-Host "Static dashboard publication verified: target=$normalizedTarget url=$deploymentUrl date=$RunDate sha256=$artifactSha256"
}
finally {
    if (Test-Path -LiteralPath $candidate) {
        Remove-Item -Force -LiteralPath $candidate
    }
    if (Test-Path -LiteralPath $replacementBackup) {
        Remove-Item -Force -LiteralPath $replacementBackup
    }
    if ($null -eq $previousCandidateEnvironment) {
        Remove-Item Env:DAWNSTRIKE_STATIC_DASHBOARD_CANDIDATE -ErrorAction SilentlyContinue
    }
    else {
        $env:DAWNSTRIKE_STATIC_DASHBOARD_CANDIDATE = $previousCandidateEnvironment
    }
}
