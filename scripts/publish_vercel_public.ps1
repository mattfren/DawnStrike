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
    [switch]$Promote,
    [ValidateRange(1, 3600)][int]$VercelBuildTimeoutSeconds = 600,
    [ValidateRange(1, 3600)][int]$VercelCommandTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
if ($Promote -and $AllowDegraded) {
    throw "Production promotion requires readiness HTTP 200; -AllowDegraded cannot be combined with -Promote."
}
. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
. (Join-Path $resolvedRoot "scripts\vercel_source_contract.ps1")
$expectedSourceSha = (& git.exe -C $resolvedRoot rev-parse HEAD).Trim().ToLowerInvariant()
$expectedSourceTree = (& git.exe -C $resolvedRoot rev-parse 'HEAD^{tree}').Trim().ToLowerInvariant()
if ($LASTEXITCODE -ne 0 -or $expectedSourceSha -notmatch '^[0-9a-f]{40}$') {
    throw "Could not resolve the exact runtime source SHA before Vercel publication."
}
if ($expectedSourceTree -notmatch '^[0-9a-f]{40}$') {
    throw "Could not resolve the exact Git tree before Vercel publication."
}
$stage = Join-Path $resolvedRoot $StageRoot
$resultPath = Join-Path $resolvedRoot "build\daily-deployment-result.json"
$vercel = @("--yes", "vercel@58.4.0")
$vercelAuth = @()
if (-not [string]::IsNullOrWhiteSpace($env:VERCEL_TOKEN)) {
    $vercelAuth = @("--token", $env:VERCEL_TOKEN)
}
$nodeCommand = Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $nodeCommand) {
    throw "node.exe is required for bounded Vercel publication."
}
$nodePath = $nodeCommand.Source
$npxCliPath = Join-Path `
    (Split-Path -Parent $nodePath) `
    "node_modules\npm\bin\npx-cli.js"
if (-not (Test-Path -LiteralPath $npxCliPath -PathType Leaf)) {
    throw "The npm npx-cli.js entry point was not found beside node.exe."
}
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
    # Vercel and its bundled curl write banners, warnings, and transfer
    # progress to stderr even when the command succeeds. The bounded runner
    # keeps stderr separate: curl progress can otherwise be interleaved inside
    # a multiline JSON body.
    $result = Invoke-VercelProcess `
        -Arguments $Arguments `
        -Label $Label `
        -TimeoutSeconds $VercelCommandTimeoutSeconds
    return Convert-VercelJson -Output @($result.Stdout) -Label $Label
}

function Invoke-VercelProcess {
    param(
        [string[]]$Arguments,
        [string]$Label,
        [int]$TimeoutSeconds
    )
    $allArguments = @($npxCliPath) + $vercel + $Arguments + $vercelAuth
    $environment = @{
        CI = "1"
        NO_COLOR = "1"
        FORCE_COLOR = "0"
        VERCEL_TELEMETRY_DISABLED = "1"
        NPM_CONFIG_UPDATE_NOTIFIER = "false"
        NPM_CONFIG_FUND = "false"
        NPM_CONFIG_AUDIT = "false"
        NPM_CONFIG_YES = "true"
    }
    $result = Invoke-DawnstrikeJobProcess `
        -FilePath $nodePath `
        -ArgumentList $allArguments `
        -WorkingDirectory ([string](Get-Location).Path) `
        -Label $Label `
        -TimeoutSeconds $TimeoutSeconds `
        -OutputDrainTimeoutSeconds 5 `
        -EnvironmentOverrides $environment
    if ($result.ExitCode -ne 0) {
        $detail = if ($result.Stderr) { " stderr: $($result.Stderr)" } else { "" }
        throw "$Label failed with exit code $($result.ExitCode).$detail"
    }
    return $result
}

function Get-OptionalJsonProperty {
    param(
        [AllowNull()][object]$InputObject,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if ($null -eq $InputObject) {
        return $null
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $null
    }
    return $property.Value
}

function Set-VercelAlias {
    param(
        [string]$DeploymentUrl,
        [string]$AliasUrl,
        [string]$Label
    )
    $deploymentHost = ($DeploymentUrl -replace "^https?://", "").TrimEnd("/")
    $aliasHost = ($AliasUrl -replace "^https?://", "").TrimEnd("/")
    $null = Invoke-VercelProcess `
        -Arguments @("alias", "set", $deploymentHost, $aliasHost) `
        -Label $Label `
        -TimeoutSeconds $VercelCommandTimeoutSeconds
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Value)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value)
    $hash = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hash.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $hash.Dispose()
    }
}

function Assert-LowerHex64 {
    param([AllowNull()][object]$Value, [string]$Field, [string]$Label)
    if ($null -eq $Value -or ([string]$Value -cnotmatch '^[0-9a-f]{64}$')) {
        throw "$Label $Field must be a lowercase 64-hex value."
    }
}

function Assert-PublicationState {
    param(
        [object]$Health,
    [object]$Readiness,
    [object]$BuildManifest,
    [string]$ExpectedSourceSha,
    [object]$ReleaseManifest,
    [string]$Label
)
    if ($BuildManifest.source_sha -ne $ExpectedSourceSha) {
        throw "$Label build source SHA does not equal the verified runtime HEAD."
    }
    if ($BuildManifest.market_date -notmatch '^\d{4}-\d{2}-\d{2}$') {
        throw "$Label build market date is invalid."
    }
    foreach ($field in @(
        "publication_set_sha256", "opportunity_projection_sha256",
        "v6_learning_sha256", "build_sha"
    )) {
        Assert-LowerHex64 -Value (Get-OptionalJsonProperty -InputObject $BuildManifest -Name $field) `
            -Field $field -Label $Label
    }
    $expectedBuildSha = Get-Sha256Hex `
        "$($BuildManifest.source_sha):$($BuildManifest.publication_set_sha256):$($BuildManifest.opportunity_projection_sha256):$($BuildManifest.v6_learning_sha256):$($BuildManifest.market_date)"
    if ($BuildManifest.build_sha -ne $expectedBuildSha) {
        throw "$Label build SHA does not match the strict five-input V6 formula."
    }
    if ($BuildManifest.build_id -ne $expectedBuildSha.Substring(0, 20)) {
        throw "$Label build ID does not match the strict build SHA."
    }
    if ($Health.status -ne "alive") {
        throw "$Label health is not alive."
    }
    if ($Health.source_sha -ne $BuildManifest.source_sha) {
        throw "$Label health source SHA does not match the build manifest."
    }
    if ($Health.build_id -ne $BuildManifest.build_id) {
        throw "$Label health build ID does not match the build manifest."
    }
    $readinessSourceSha = Get-OptionalJsonProperty `
        -InputObject $Readiness `
        -Name "source_sha"
    $readinessBuildId = Get-OptionalJsonProperty `
        -InputObject $Readiness `
        -Name "build_id"
    if ($readinessSourceSha -and $readinessSourceSha -ne $BuildManifest.source_sha) {
        throw "$Label readiness source SHA does not match the build manifest."
    }
    if ($readinessBuildId -and $readinessBuildId -ne $BuildManifest.build_id) {
        throw "$Label readiness build ID does not match the build manifest."
    }
    Assert-LowerHex64 -Value (Get-OptionalJsonProperty -InputObject $Readiness -Name "v6_learning_sha256") `
        -Field "readiness.v6_learning_sha256" -Label $Label
    if ($Readiness.v6_learning_sha256 -ne $BuildManifest.v6_learning_sha256) {
        throw "$Label readiness V6 hash does not match the build manifest."
    }
    if ($Readiness.market_date -ne $BuildManifest.market_date) {
        throw "$Label readiness market date does not match the build manifest."
    }
    if ($Readiness.research_only -ne $true -or $Readiness.broker_execution_enabled -ne $false) {
        throw "$Label readiness safety boundary is not research-only with broker execution disabled."
    }
    if ($Readiness.data_hash_sha256 -ne $BuildManifest.data_hash_sha256) {
        throw "$Label readiness data hash does not match the build manifest."
    }
    if ($Readiness.publication_set_sha256 -ne $BuildManifest.publication_set_sha256) {
        throw "$Label readiness publication-set hash does not match the build manifest."
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
    if ($null -ne $ReleaseManifest) {
        if ($ReleaseManifest.source_sha -ne $BuildManifest.source_sha -or
            $ReleaseManifest.build_sha -ne $BuildManifest.build_sha -or
            $ReleaseManifest.v6_learning_sha256 -ne $BuildManifest.v6_learning_sha256) {
            throw "$Label release manifest does not match the build manifest."
        }
        Assert-LowerHex64 -Value $ReleaseManifest.v6_learning_sha256 `
            -Field "release_manifest.v6_learning_sha256" -Label $Label
    }
}

& (Join-Path $resolvedRoot "scripts\build_vercel_public_stage.ps1") `
    -ProjectRoot $resolvedRoot `
    -StageRoot $StageRoot `
    -ExpectedSourceSha $expectedSourceSha `
    -ExpectedSourceTree $expectedSourceTree
& (Join-Path $resolvedRoot "scripts\verify_vercel_candidate.ps1") `
    -ProjectRoot $resolvedRoot `
    -StageRoot $StageRoot `
    -ExpectedSourceSha $expectedSourceSha `
    -ExpectedSourceTree $expectedSourceTree `
    -AllowDegraded:$AllowDegraded

Assert-VercelGitSourceStable `
    -Root $resolvedRoot `
    -ExpectedSourceSha $expectedSourceSha `
    -ExpectedSourceTree $expectedSourceTree `
    -AllowedStageRoot $stage
Push-Location $stage
try {
    Assert-VercelGitSourceStable `
        -Root $resolvedRoot `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree `
        -AllowedStageRoot $stage
    $buildResult = Invoke-VercelProcess `
        -Arguments @("build", "--yes", "--project", $ProjectId) `
        -Label "Vercel prebuild" `
        -TimeoutSeconds $VercelBuildTimeoutSeconds
    if ($buildResult.Stdout) {
        [Console]::Out.WriteLine($buildResult.Stdout)
    }
    if ($buildResult.Stderr) {
        [Console]::Error.WriteLine($buildResult.Stderr)
    }
    Assert-VercelGitSourceStable `
        -Root $resolvedRoot `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree `
        -AllowedStageRoot $stage
    $deploymentResponse = Invoke-VercelJson `
        -Arguments @("deploy", "--prebuilt", "--project", $ProjectId, "--yes", "--json") `
        -Label "Vercel prebuilt deploy"
    $wrappedDeployment = Get-OptionalJsonProperty `
        -InputObject $deploymentResponse `
        -Name "deployment"
    $deployment = if ($null -ne $wrappedDeployment) {
        $wrappedDeployment
    }
    else {
        $deploymentResponse
    }
}
finally {
    Pop-Location
}

$deploymentId = Get-OptionalJsonProperty -InputObject $deployment -Name "id"
$previewUrl = [string](
    Get-OptionalJsonProperty -InputObject $deployment -Name "url"
)
if (-not $deploymentId -or -not $previewUrl) {
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
$previewReleaseManifest = Invoke-VercelJson `
    -Arguments @("curl", "$previewUrl/release-manifest.json") `
    -Label "Preview release manifest"
Assert-PublicationState `
    -Health $previewHealth `
    -Readiness $previewReadiness `
    -BuildManifest $previewManifest `
    -ReleaseManifest $previewReleaseManifest `
    -ExpectedSourceSha $expectedSourceSha `
    -Label "Preview"

if ($Promote) {
    $priorProduction = Invoke-VercelJson `
        -Arguments @("inspect", $ProductionAlias, "--json") `
        -Label "Prior production inspect"
}

try {
    if ($Promote) {
        Assert-VercelGitSourceStable `
            -Root $resolvedRoot `
            -ExpectedSourceSha $expectedSourceSha `
            -ExpectedSourceTree $expectedSourceTree `
            -AllowedStageRoot $stage
        # Mark the external state as potentially mutated before starting the
        # command. A timeout can occur after Vercel accepted the promotion, so
        # every promotion failure must enter the existing rollback boundary.
        $promoted = $true
        $null = Invoke-VercelProcess `
            -Arguments @("promote", $previewUrl, "--yes") `
            -Label "Vercel promotion" `
            -TimeoutSeconds $VercelCommandTimeoutSeconds
        $promotionVerificationError = $null
        for ($attempt = 1; $attempt -le 10; $attempt++) {
            try {
                $deploymentsResponse = Invoke-VercelJson `
                    -Arguments @("list", $ProjectName, "--json", "--limit", "20") `
                    -Label "Promoted deployment list"
                $listedDeployments = @(
                    Get-OptionalJsonProperty `
                        -InputObject $deploymentsResponse `
                        -Name "deployments"
                )
                if (-not $listedDeployments.Count) {
                    throw "Promoted deployment list did not return deployments."
                }
                $promotedCandidates = @(
                    $listedDeployments |
                        Where-Object {
                            $target = Get-OptionalJsonProperty `
                                -InputObject $_ `
                                -Name "target"
                            $metadata = Get-OptionalJsonProperty `
                                -InputObject $_ `
                                -Name "meta"
                            $action = Get-OptionalJsonProperty `
                                -InputObject $metadata `
                                -Name "action"
                            $originalDeploymentId = Get-OptionalJsonProperty `
                                -InputObject $metadata `
                                -Name "originalDeploymentId"
                            $target -eq "production" -and
                            $action -eq "promote" -and
                            $originalDeploymentId -eq $deploymentId
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
                $promotedReleaseManifest = Invoke-VercelJson `
                    -Arguments @(
                        "curl",
                        "$promotedUrl/release-manifest.json?verify=$cacheBuster"
                    ) `
                    -Label "Promoted deployment release manifest"
                Assert-PublicationState `
                    -Health $promotedHealth `
                    -Readiness $promotedReadiness `
                    -BuildManifest $promotedManifest `
                    -ReleaseManifest $promotedReleaseManifest `
                    -ExpectedSourceSha $expectedSourceSha `
                    -Label "Promoted deployment"
                if (
                    $promotedManifest.source_sha -ne $previewManifest.source_sha -or
                    $promotedManifest.build_id -ne $previewManifest.build_id -or
                    $promotedManifest.data_hash_sha256 -ne
                    $previewManifest.data_hash_sha256 -or
                    $promotedManifest.build_sha -ne $previewManifest.build_sha -or
                    $promotedManifest.publication_set_sha256 -ne $previewManifest.publication_set_sha256 -or
                    $promotedManifest.opportunity_projection_sha256 -ne $previewManifest.opportunity_projection_sha256 -or
                    $promotedManifest.v6_learning_sha256 -ne $previewManifest.v6_learning_sha256
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

        $promotedDeploymentId = Get-OptionalJsonProperty `
            -InputObject $promotedDeployment `
            -Name "id"
        $promotedUrl = [string](
            Get-OptionalJsonProperty -InputObject $promotedDeployment -Name "url"
        )
        if (-not $promotedDeploymentId -or -not $promotedUrl) {
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
                $productionReleaseManifest = Invoke-VercelJson `
                    -Arguments @(
                        "curl",
                        "$ProductionAlias/release-manifest.json?verify=$cacheBuster"
                    ) `
                    -Label "Production release manifest"
                Assert-PublicationState `
                    -Health $productionHealth `
                    -Readiness $productionReadiness `
                    -BuildManifest $productionManifest `
                    -ReleaseManifest $productionReleaseManifest `
                    -ExpectedSourceSha $expectedSourceSha `
                    -Label "Production"
                if (
                    $productionManifest.source_sha -ne $previewManifest.source_sha -or
                    $productionManifest.build_id -ne $previewManifest.build_id -or
                    $productionManifest.data_hash_sha256 -ne
                    $previewManifest.data_hash_sha256 -or
                    $productionManifest.build_sha -ne $previewManifest.build_sha -or
                    $productionManifest.publication_set_sha256 -ne $previewManifest.publication_set_sha256 -or
                    $productionManifest.opportunity_projection_sha256 -ne $previewManifest.opportunity_projection_sha256 -or
                    $productionManifest.v6_learning_sha256 -ne $previewManifest.v6_learning_sha256
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
    $priorProductionId = Get-OptionalJsonProperty `
        -InputObject $priorProduction `
        -Name "id"
    $priorProductionUrl = [string](
        Get-OptionalJsonProperty -InputObject $priorProduction -Name "url"
    )
    if ($promoted -and $priorProductionId -and $priorProductionUrl) {
        $rollbackErrors = @()
        foreach ($alias in $allProductionAliases) {
            try {
                Set-VercelAlias `
                    -DeploymentUrl $priorProductionUrl `
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
    preview_deployment_id = $deploymentId
    preview_ready_state = Get-OptionalJsonProperty -InputObject $deployment -Name "readyState"
    source_sha = $previewManifest.source_sha
    build_id = $previewManifest.build_id
    build_sha = $previewManifest.build_sha
    data_hash_sha256 = $previewManifest.data_hash_sha256
    publication_set_sha256 = $previewManifest.publication_set_sha256
    opportunity_projection_sha256 = $previewManifest.opportunity_projection_sha256
    v6_learning_sha256 = $previewManifest.v6_learning_sha256
    release_manifest_sha256 = Get-OptionalJsonProperty -InputObject $previewReleaseManifest -Name "release_manifest_sha256"
    market_date = $previewManifest.market_date
    snapshot_status = $previewReadiness.snapshot_status
    readiness_status = $previewReadiness.status
    readiness_http_status = $previewReadiness.http_status
    allow_degraded = [bool]$AllowDegraded
    promoted = [bool]$Promote
    prior_production_deployment_id = Get-OptionalJsonProperty -InputObject $priorProduction -Name "id"
    production_aliases = if ($Promote) { @($allProductionAliases) } else { @() }
    promoted_deployment_id = Get-OptionalJsonProperty -InputObject $promotedDeployment -Name "id"
    production_deployment_id = Get-OptionalJsonProperty -InputObject $production -Name "id"
    live_trading_enabled = $false
    research_only = $true
    status = if ($Promote) { "PRODUCTION_VERIFIED" } else { "PREVIEW_VERIFIED" }
}
$json = $result | ConvertTo-Json -Depth 20
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($resultPath, $json, $utf8NoBom)
$json
