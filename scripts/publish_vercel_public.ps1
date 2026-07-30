[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [string]$StageRoot = "build\vercel-stage",
    [string]$ProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [string]$ProjectName = "dawnstrike-command-center-x3",
    [string]$ProductionAlias = "https://dawnstrike-command-center-x3.vercel.app",
    [string[]]$AdditionalProductionAliases = @(
        "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
        "https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app"
    ),
    [switch]$AllowDegraded,
    [switch]$Promote
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$stage = Join-Path $resolvedRoot $StageRoot
$resultPath = Join-Path $resolvedRoot "build\daily-deployment-result.json"
$vercel = @("--yes", "vercel@58.4.0")
$promoted = $false
$priorProduction = $null
$promotedDeployment = $null
$allProductionAliases = @($ProductionAlias) + @($AdditionalProductionAliases) |
    Select-Object -Unique

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    $pythonScripts = (& py.exe -c "import sysconfig; print(sysconfig.get_path('scripts'))").Trim()
    $uvCandidate = Join-Path $pythonScripts "uv.exe"
    if (Test-Path -LiteralPath $uvCandidate -PathType Leaf) {
        $env:PATH = "$pythonScripts;$env:PATH"
    }
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is required for the pinned Vercel Python prebuild and was not found."
}

function Convert-VercelJson {
    param(
        [object[]]$Output,
        [string]$Label
    )
    $text = (($Output | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if (-not $text) {
        throw "$Label returned no JSON output."
    }
    try {
        $start = $text.IndexOf("{")
        $end = $text.LastIndexOf("}")
        if ($start -lt 0 -or $end -lt $start) {
            throw "No JSON object found in CLI output."
        }
        return $text.Substring($start, $end - $start + 1) | ConvertFrom-Json
    }
    catch {
        throw "$Label returned invalid JSON: $text"
    }
}

function Invoke-VercelJson {
    param(
        [string[]]$Arguments,
        [string]$Label
    )
    # PowerShell's ErrorActionPreference=Stop turns Vercel's normal Node
    # warnings on stderr into terminating errors. Merge streams, then parse
    # the JSON object from the combined output.
    $output = & npx @vercel @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
    return Convert-VercelJson -Output @($output) -Label $Label
}

function Set-VercelAlias {
    param(
        [string]$DeploymentUrl,
        [string]$AliasUrl,
        [string]$Label
    )
    $deploymentHost = ($DeploymentUrl -replace "^https?://", "").TrimEnd("/")
    $aliasHost = ($AliasUrl -replace "^https?://", "").TrimEnd("/")
    $null = & npx @vercel alias set $deploymentHost $aliasHost 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Assert-PublicationState {
    param(
        [object]$Health,
        [object]$Readiness,
        [object]$BuildManifest,
        [string]$Label
    )
    if ($Health.status -ne "alive") {
        throw "$Label health is not alive."
    }
    if ($Health.source_sha -ne $BuildManifest.source_sha) {
        throw "$Label health source SHA does not match the build manifest."
    }
    if ($Health.build_id -ne $BuildManifest.build_id) {
        throw "$Label health build ID does not match the build manifest."
    }
    if ($Readiness.source_sha -and $Readiness.source_sha -ne $BuildManifest.source_sha) {
        throw "$Label readiness source SHA does not match the build manifest."
    }
    if ($Readiness.build_id -and $Readiness.build_id -ne $BuildManifest.build_id) {
        throw "$Label readiness build ID does not match the build manifest."
    }
    if ($Readiness.data_hash_sha256 -ne $BuildManifest.data_hash_sha256) {
        throw "$Label readiness data hash does not match the build manifest."
    }

    $ready = $Readiness.status -eq "ready" -and [int]$Readiness.http_status -eq 200
    $approvedDegraded = (
        $AllowDegraded -and
        $Readiness.status -eq "not_ready" -and
        [int]$Readiness.http_status -eq 503 -and
        $Readiness.snapshot_status -eq "degraded"
    )
    if (-not $ready -and -not $approvedDegraded) {
        throw "$Label readiness is neither ready nor approved degraded."
    }
}

& (Join-Path $resolvedRoot "scripts\build_vercel_public_stage.ps1") `
    -ProjectRoot $resolvedRoot `
    -StageRoot $StageRoot
& (Join-Path $resolvedRoot "scripts\verify_vercel_candidate.ps1") `
    -ProjectRoot $resolvedRoot `
    -StageRoot $StageRoot `
    -AllowDegraded:$AllowDegraded

Push-Location $stage
try {
    & npx @vercel build --yes --project $ProjectId
    if ($LASTEXITCODE -ne 0) {
        throw "Vercel prebuild failed with exit code $LASTEXITCODE."
    }
    $deploymentResponse = Invoke-VercelJson `
        -Arguments @("deploy", "--prebuilt", "--project", $ProjectId, "--yes", "--json") `
        -Label "Vercel prebuilt deploy"
    $deployment = if ($deploymentResponse.deployment) {
        $deploymentResponse.deployment
    }
    else {
        $deploymentResponse
    }
}
finally {
    Pop-Location
}

$previewUrl = [string]$deployment.url
if (-not $deployment.id -or -not $previewUrl) {
    throw "Vercel prebuilt deploy did not return a deployment ID and URL."
}
if (-not $previewUrl.StartsWith("http")) {
    $previewUrl = "https://$previewUrl"
}
$previewHealth = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/api/health") `
    -Label "Preview health"
$previewReadiness = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/api/readiness") `
    -Label "Preview readiness"
$previewManifest = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/build-manifest.json") `
    -Label "Preview build manifest"
Assert-PublicationState `
    -Health $previewHealth `
    -Readiness $previewReadiness `
    -BuildManifest $previewManifest `
    -Label "Preview"

if ($Promote) {
    $priorProduction = Invoke-VercelJson `
        -Arguments @("inspect", $ProductionAlias, "--json") `
        -Label "Prior production inspect"
    $null = & npx @vercel promote $previewUrl --yes 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Vercel promotion failed with exit code $LASTEXITCODE."
    }
    $promoted = $true
}

try {
    if ($Promote) {
        $promotionVerificationError = $null
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                $deploymentsResponse = Invoke-VercelJson `
                    -Arguments @("list", $ProjectName, "--json", "--limit", "20") `
                    -Label "Promoted deployment list"
                $promotedCandidates = @(
                    $deploymentsResponse.deployments |
                        Where-Object {
                            $_.target -eq "production" -and
                            $_.meta.action -eq "promote" -and
                            $_.meta.originalDeploymentId -eq $deployment.id
                        } |
                        Sort-Object -Property createdAt -Descending
                )
                if (-not $promotedCandidates.Count) {
                    throw "No promoted clone of the verified preview is visible yet."
                }
                $promotedUrl = [string]$promotedCandidates[0].url
                if (-not $promotedUrl.StartsWith("http")) {
                    $promotedUrl = "https://$promotedUrl"
                }
                $cacheBuster = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                $promotedDeployment = Invoke-VercelJson `
                    -Arguments @("inspect", $promotedUrl, "--json") `
                    -Label "Promoted deployment inspect"
                $promotedHealth = Invoke-VercelJson `
                    -Arguments @("curl", "$promotedUrl/api/health?verify=$cacheBuster") `
                    -Label "Promoted deployment health"
                $promotedReadiness = Invoke-VercelJson `
                    -Arguments @("curl", "$promotedUrl/api/readiness?verify=$cacheBuster") `
                    -Label "Promoted deployment readiness"
                $promotedManifest = Invoke-VercelJson `
                    -Arguments @(
                        "curl",
                        "$promotedUrl/build-manifest.json?verify=$cacheBuster"
                    ) `
                    -Label "Promoted deployment build manifest"
                Assert-PublicationState `
                    -Health $promotedHealth `
                    -Readiness $promotedReadiness `
                    -BuildManifest $promotedManifest `
                    -Label "Promoted deployment"
                if (
                    $promotedManifest.source_sha -ne $previewManifest.source_sha -or
                    $promotedManifest.build_id -ne $previewManifest.build_id -or
                    $promotedManifest.data_hash_sha256 -ne
                    $previewManifest.data_hash_sha256
                ) {
                    throw "Promoted deployment does not match the verified preview."
                }
                $promotionVerificationError = $null
                break
            }
            catch {
                $promotionVerificationError = $_.Exception.Message
                if ($attempt -lt 10) {
                    Start-Sleep -Seconds 3
                }
            }
        }
        if ($promotionVerificationError) {
            throw "Promoted deployment verification did not converge: $promotionVerificationError"
        }

        $promotedUrl = [string]$promotedDeployment.url
        if (-not $promotedDeployment.id -or -not $promotedUrl) {
            throw "Promoted deployment inspect did not return a deployment ID and URL."
        }
        foreach ($alias in $allProductionAliases) {
            Set-VercelAlias `
                -DeploymentUrl $promotedUrl `
                -AliasUrl ([string]$alias) `
                -Label "Production alias assignment for $alias"
        }

        $productionVerificationError = $null
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                $cacheBuster = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
                $production = Invoke-VercelJson `
                    -Arguments @("inspect", $ProductionAlias, "--json") `
                    -Label "Production inspect"
                $productionHealth = Invoke-VercelJson `
                    -Arguments @("curl", "$ProductionAlias/api/health?verify=$cacheBuster") `
                    -Label "Production health"
                $productionReadiness = Invoke-VercelJson `
                    -Arguments @("curl", "$ProductionAlias/api/readiness?verify=$cacheBuster") `
                    -Label "Production readiness"
                $productionManifest = Invoke-VercelJson `
                    -Arguments @("curl", "$ProductionAlias/build-manifest.json?verify=$cacheBuster") `
                    -Label "Production build manifest"
                Assert-PublicationState `
                    -Health $productionHealth `
                    -Readiness $productionReadiness `
                    -BuildManifest $productionManifest `
                    -Label "Production"
                if (
                    $productionManifest.source_sha -ne $previewManifest.source_sha -or
                    $productionManifest.build_id -ne $previewManifest.build_id -or
                    $productionManifest.data_hash_sha256 -ne
                    $previewManifest.data_hash_sha256
                ) {
                    throw "Production does not match the verified preview."
                }
                $productionVerificationError = $null
                break
            }
            catch {
                $productionVerificationError = $_.Exception.Message
                if ($attempt -lt 10) {
                    Start-Sleep -Seconds 3
                }
            }
        }
        if ($productionVerificationError) {
            throw "Production verification did not converge: $productionVerificationError"
        }
    }
    else {
        $production = $null
        $productionHealth = $null
        $productionReadiness = $null
        $productionManifest = $null
    }
}
catch {
    $publicationError = $_.Exception.Message
    if ($promoted -and $priorProduction.id -and $priorProduction.url) {
        $rollbackErrors = @()
        foreach ($alias in $allProductionAliases) {
            try {
                Set-VercelAlias `
                    -DeploymentUrl ([string]$priorProduction.url) `
                    -AliasUrl ([string]$alias) `
                    -Label "Production rollback for $alias"
            }
            catch {
                $rollbackErrors += $_.Exception.Message
            }
        }
        if ($rollbackErrors.Count) {
            throw "$publicationError Rollback errors: $($rollbackErrors -join '; ')"
        }
    }
    throw $publicationError
}

$result = [ordered]@{
    schema_version = "dawnstrike.daily_deployment.v1"
    generated_at = [DateTimeOffset]::UtcNow.ToString("o")
    project_id = $ProjectId
    preview_url = $previewUrl
    preview_deployment_id = $deployment.id
    preview_ready_state = $deployment.readyState
    source_sha = $previewManifest.source_sha
    build_id = $previewManifest.build_id
    data_hash_sha256 = $previewManifest.data_hash_sha256
    market_date = $previewManifest.market_date
    snapshot_status = $previewReadiness.snapshot_status
    readiness_status = $previewReadiness.status
    readiness_http_status = $previewReadiness.http_status
    allow_degraded = [bool]$AllowDegraded
    promoted = [bool]$Promote
    prior_production_deployment_id = if ($priorProduction) { $priorProduction.id } else { $null }
    production_aliases = if ($Promote) { @($allProductionAliases) } else { @() }
    promoted_deployment_id = if ($promotedDeployment) { $promotedDeployment.id } else { $null }
    production_deployment_id = if ($production) { $production.id } else { $null }
    live_trading_enabled = $false
    research_only = $true
    status = if ($Promote) { "PRODUCTION_VERIFIED" } else { "PREVIEW_VERIFIED" }
}
$json = $result | ConvertTo-Json -Depth 20
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($resultPath, $json, $utf8NoBom)
$json
