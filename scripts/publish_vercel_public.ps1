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
    [string]$StateRoot = "",
    [ValidatePattern('^$|^\d{4}-\d{2}-\d{2}$')][string]$ExpectedMarketDate = "",
    [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$PrepublicationAuthorizationId = "",
    [ValidatePattern('^$|^[0-9a-f]{64}$')][string]$DailyLedgerAuthorizationId = "",
    [string]$TestNowUtc = "",
    [ValidateSet("", "after_promote", "after_aliases", "after_production_verification", "after_result_write_before_complete")]
    [string]$TestCrashPoint = "",
    [ValidateSet("", "after_promote", "after_aliases", "after_production_verification", "result_write", "after_result_write_before_complete")]
    [string]$TestFailurePoint = "",
    [ValidateRange(1, 3600)][int]$VercelBuildTimeoutSeconds = 600,
    [ValidateRange(1, 3600)][int]$VercelCommandTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
if ($Promote -and $AllowDegraded) {
    throw "Production promotion requires readiness HTTP 200; -AllowDegraded cannot be combined with -Promote."
}
. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_process_runner.ps1")
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
$rollbackResultPath = Join-Path $resolvedRoot "build\daily-deployment-rollback-result.json"
$resolvedStateRoot = if ([string]::IsNullOrWhiteSpace($StateRoot)) {
    $resolvedRoot
}
else {
    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    (Resolve-Path $StateRoot).Path
}
$resolvedExpectedMarketDate = $ExpectedMarketDate.Trim()
if ($Promote -and [string]::IsNullOrWhiteSpace($resolvedExpectedMarketDate)) {
    throw "Direct -Promote is blocked: ExpectedMarketDate requires governed finalization authorization."
}
if ($Promote -and [string]::IsNullOrWhiteSpace($PrepublicationAuthorizationId)) {
    throw "Direct -Promote is blocked: immutable prepublication authorization is required."
}
if ($Promote -and [string]::IsNullOrWhiteSpace($DailyLedgerAuthorizationId)) {
    throw "Direct -Promote is blocked: daily-ledger authorization is required."
}
if ($PrepublicationAuthorizationId -and $DailyLedgerAuthorizationId -and
    $PrepublicationAuthorizationId -cne $DailyLedgerAuthorizationId) {
    throw "Prepublication and daily-ledger authorization identities must be identical."
}
$journalRoot = Join-Path $resolvedStateRoot "outputs\daily_finalize\vercel-publication"
$journalPath = Join-Path $journalRoot "vercel-publication-operation.json"
$publicationLockPath = Join-Path $journalRoot "vercel-publication-operation.lock"
$publicationLockOwner = [guid]::NewGuid().ToString("N")
$publicationLockAcquired = $false
$journalHelper = Join-Path $resolvedRoot "scripts\vercel_publication_journal.py"
$resultRelativePath = "build/daily-deployment-result.json"
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
$priorProductionAliases = @{}
$promotedDeployment = $null
$packageManifestSha256 = $null
$allProductionAliases = @($ProductionAlias) + @($AdditionalProductionAliases) |
    Select-Object -Unique | Sort-Object

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
        $detail = if ($result.Stderr) { " provider diagnostics suppressed" } else { "" }
        throw "$Label failed with exit code $($result.ExitCode).$detail"
    }
    return $result
}

function Assert-RemoteVercelSourceManifest {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $remote = Invoke-VercelProcess `
        -Arguments @("curl", $Url) `
        -Label "$Label source manifest" `
        -TimeoutSeconds $VercelCommandTimeoutSeconds
    $expectedCanonical = Get-VercelSourceManifestCanonicalJson `
        -Path (Join-Path $stage "vercel-source-manifest.json")
    Assert-VercelSourceManifestJson `
        -RawJson ([string]$remote.Stdout) `
        -ExpectedCanonicalJson $expectedCanonical `
        -Label $Label
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

function Normalize-VercelDeploymentUrl {
    param([AllowNull()][object]$Value)
    return ([string]$Value -replace "^https?://", "").TrimEnd("/").ToLowerInvariant()
}

function Assert-VercelAliasRestored {
    param(
        [Parameter(Mandatory = $true)][string]$AliasUrl,
        [Parameter(Mandatory = $true)][object]$PriorAlias,
        [Parameter(Mandatory = $true)][int64]$CacheBuster
    )
    $restored = Invoke-VercelJson `
        -Arguments @("inspect", [string]$AliasUrl, "--json") `
        -Label "Rollback verification inspect for $AliasUrl"
    $restoredId = [string](Get-OptionalJsonProperty -InputObject $restored -Name "id")
    $restoredUrl = [string](Get-OptionalJsonProperty -InputObject $restored -Name "url")
    if (-not $restoredId -or $restoredId -ne [string]$PriorAlias.id) {
        throw "Rollback verification for $AliasUrl resolved the wrong deployment ID."
    }
    if (
        -not $restoredUrl -or
        (Normalize-VercelDeploymentUrl $restoredUrl) -ne
        (Normalize-VercelDeploymentUrl $PriorAlias.url)
    ) {
        throw "Rollback verification for $AliasUrl resolved the wrong deployment URL."
    }

    $proof = Get-VercelAliasEndpointProof -AliasUrl $AliasUrl -CacheBuster $CacheBuster
    if ($PriorAlias.health_available -and -not $proof.health_available) {
        throw "Rollback verification health is unavailable for $AliasUrl."
    }
    if ($PriorAlias.health_status -and $proof.health_status -ne $PriorAlias.health_status) {
        throw "Rollback verification health status changed for $AliasUrl."
    }
    if ($PriorAlias.readiness_available -and -not $proof.readiness_available) {
        throw "Rollback verification readiness is unavailable for $AliasUrl."
    }
    if ($PriorAlias.readiness_status -and $proof.readiness_status -ne $PriorAlias.readiness_status) {
        throw "Rollback verification readiness status changed for $AliasUrl."
    }
    if ($null -ne $PriorAlias.readiness_http_status -and
        $proof.readiness_http_status -ne $PriorAlias.readiness_http_status) {
        throw "Rollback verification readiness HTTP status changed for $AliasUrl."
    }
    if ($PriorAlias.source_manifest_available) {
        if (-not $proof.source_manifest_available) {
            throw "Rollback verification source manifest is unavailable for $AliasUrl."
        }
        if ($proof.source_manifest_sha256 -ne $PriorAlias.source_manifest_sha256) {
            throw "Rollback verification source manifest changed for $AliasUrl."
        }
    }
    return [ordered]@{
        alias = $AliasUrl
        deployment_id = $restoredId
        deployment_url = $restoredUrl
        health_available = [bool]$proof.health_available
        health_status = $proof.health_status
        readiness_available = [bool]$proof.readiness_available
        readiness_status = $proof.readiness_status
        readiness_http_status = $proof.readiness_http_status
        source_manifest_available = [bool]$proof.source_manifest_available
        source_sha = [string]$proof.source_sha
        source_tree = [string]$proof.source_tree
        source_manifest_sha256 = [string]$proof.source_manifest_sha256
    }
}

function Get-VercelAliasEndpointProof {
    param(
        [Parameter(Mandatory = $true)][string]$AliasUrl,
        [Parameter(Mandatory = $true)][int64]$CacheBuster,
        [switch]$RequireHealthReadiness
    )
    $proof = [ordered]@{
        health_available = $false
        health_status = $null
        readiness_available = $false
        readiness_status = $null
        readiness_http_status = $null
        source_manifest_available = $false
        source_sha = $null
        source_tree = $null
        source_manifest_sha256 = $null
    }
    try {
        $health = Invoke-VercelJson `
            -Arguments @("curl", "$AliasUrl/api/health?rollback_verify=$CacheBuster") `
            -Label "Alias health proof for $AliasUrl"
        if ($null -ne $health) {
            $proof.health_available = $true
            $proof.health_status = Get-OptionalJsonProperty -InputObject $health -Name "status"
        }
    }
    catch { }
    try {
        $readiness = Invoke-VercelJson `
            -Arguments @("curl", "$AliasUrl/api/readiness?rollback_verify=$CacheBuster") `
            -Label "Alias readiness proof for $AliasUrl"
        if ($null -ne $readiness) {
            $proof.readiness_available = $true
            $proof.readiness_status = Get-OptionalJsonProperty -InputObject $readiness -Name "status"
            $proof.readiness_http_status = Get-OptionalJsonProperty -InputObject $readiness -Name "http_status"
        }
    }
    catch { }
    try {
        $manifestProcess = Invoke-VercelProcess `
            -Arguments @("curl", "$AliasUrl/vercel-source-manifest.json?rollback_verify=$CacheBuster") `
            -Label "Alias source manifest proof for $AliasUrl" `
            -TimeoutSeconds $VercelCommandTimeoutSeconds
        $manifestCanonical = Convert-VercelSourceManifestToCanonicalJson `
            -RawJson ([string]$manifestProcess.Stdout).Trim()
        $manifest = $manifestCanonical | ConvertFrom-Json
        $proof.source_manifest_available = $true
        $proof.source_sha = [string]$manifest.source_sha
        $proof.source_tree = [string]$manifest.source_tree
        $proof.source_manifest_sha256 = Get-Sha256Hex $manifestCanonical
    }
    catch { }
    if ($RequireHealthReadiness -and -not $proof.health_available) {
        throw "Prior production health proof is unavailable for $AliasUrl."
    }
    if ($RequireHealthReadiness -and $proof.health_status -ne "alive") {
        throw "Prior production health status is not alive for $AliasUrl."
    }
    if ($RequireHealthReadiness -and -not $proof.readiness_available) {
        throw "Prior production readiness proof is unavailable for $AliasUrl."
    }
    if ($RequireHealthReadiness -and $proof.readiness_status -ne "ready") {
        throw "Prior production readiness status is not ready for $AliasUrl."
    }
    if ($RequireHealthReadiness -and $proof.readiness_http_status -ne 200) {
        throw "Prior production readiness HTTP status is not 200 for $AliasUrl."
    }
    return $proof
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
$emptySha256 = Get-Sha256Hex ""

function ConvertTo-VercelCanonicalObject {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [System.Collections.IDictionary]) {
        $ordered = [ordered]@{}
        foreach ($key in ($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object)) {
            $ordered[$key] = ConvertTo-VercelCanonicalObject $Value[$key]
        }
        return $ordered
    }
    if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
        return @($Value | ForEach-Object { ConvertTo-VercelCanonicalObject $_ })
    }
    if ($Value.PSObject -and $Value.PSObject.Properties.Count -gt 0 -and
        $Value -isnot [ValueType] -and $Value -isnot [string]) {
        $ordered = [ordered]@{}
        foreach ($property in ($Value.PSObject.Properties | Sort-Object Name)) {
            $ordered[$property.Name] = ConvertTo-VercelCanonicalObject $property.Value
        }
        return $ordered
    }
    return $Value
}

function ConvertTo-VercelCanonicalJson {
    param([Parameter(Mandatory = $true)][object]$Value)
    return ((ConvertTo-VercelCanonicalObject $Value) | ConvertTo-Json -Depth 40 -Compress)
}

function Invoke-VercelJournalTool {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $python = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($null -eq $python) { throw "py.exe is required for the Vercel publication journal." }
    $output = & $python.Source -3.13 $journalHelper @Arguments 2>$null
    if ($LASTEXITCODE -ne 0) { throw "$Label failed." }
    try { return (($output -join "`n") | ConvertFrom-Json) }
    catch { throw "$Label returned invalid journal JSON." }
}

function Get-VercelPublicationJournal {
    if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) { return $null }
    $verified = Invoke-VercelJournalTool `
        -Arguments @("verify", $journalPath, "--state-root", $resolvedStateRoot) `
        -Label "Vercel publication journal verification"
    return $verified.payload
}

function Acquire-VercelPublicationLock {
    param(
        [Parameter(Mandatory = $true)][string]$CandidateSourceSha,
        [Parameter(Mandatory = $true)][string]$CandidateSourceTree,
        [Parameter(Mandatory = $true)][string]$CandidateMarketDate
    )
    $relativeJournal = ([System.IO.Path]::GetRelativePath($resolvedStateRoot, $journalPath)) -replace '\\','/'
    $null = Invoke-VercelJournalTool -Arguments @(
        "acquire-lock", $publicationLockPath,
        "--state-root", $resolvedStateRoot,
        "--owner-id", $publicationLockOwner,
        "--pid", [string]$PID,
        "--candidate-source-sha", $CandidateSourceSha,
        "--candidate-source-tree", $CandidateSourceTree,
        "--candidate-market-date", $CandidateMarketDate,
        "--journal-path", $relativeJournal
    ) -Label "Vercel publication lock acquisition"
    $script:publicationLockAcquired = $true
}

function Release-VercelPublicationLock {
    if (-not $publicationLockAcquired) { return }
    $null = Invoke-VercelJournalTool -Arguments @(
        "release-lock", $publicationLockPath,
        "--state-root", $resolvedStateRoot,
        "--owner-id", $publicationLockOwner,
        "--pid", [string]$PID
    ) -Label "Vercel publication lock release"
    $script:publicationLockAcquired = $false
}

function Assert-VercelJournalBaseMatchesInvocation {
    param([Parameter(Mandatory = $true)][object]$Journal)
    if ([string]$Journal.candidate_source_sha -ne $expectedSourceSha -or
        [string]$Journal.candidate_source_tree -ne $expectedSourceTree) {
        throw "Vercel publication journal does not match the current source SHA/tree."
    }
    if (-not $resolvedExpectedMarketDate -or
        [string]$Journal.candidate_market_date -ne $resolvedExpectedMarketDate -or
        [string]$Journal.expected_market_date -ne $resolvedExpectedMarketDate) {
        throw "Vercel publication journal cannot be reused without an exact ExpectedMarketDate match."
    }
    if ([string]$Journal.project_id -ne $ProjectId -or [string]$Journal.project_name -ne $ProjectName) {
        throw "Vercel publication journal does not match the current Vercel project."
    }
    if ([string]$Journal.prepublication_authorization_id -ne $PrepublicationAuthorizationId -or
        [string]$Journal.daily_ledger_authorization_id -ne $DailyLedgerAuthorizationId) {
        throw "Vercel publication journal authorization does not match this invocation."
    }
    $journalAliases = @($Journal.production_aliases | ForEach-Object { [string]$_ })
    if ($journalAliases.Count -ne $allProductionAliases.Count) {
        throw "Vercel publication journal aliases do not match this invocation."
    }
    for ($index = 0; $index -lt $allProductionAliases.Count; $index++) {
        if ($journalAliases[$index] -cne [string]$allProductionAliases[$index]) {
            throw "Vercel publication journal aliases do not match this invocation."
        }
    }
}

function Assert-VercelJournalMatchesInvocation {
    param([Parameter(Mandatory = $true)][object]$Journal)
    Assert-VercelJournalBaseMatchesInvocation -Journal $Journal
    if ($Journal.result_payload.promoted -ne [bool]$Promote -or
        $Journal.result_payload.allow_degraded -ne [bool]$AllowDegraded) {
        throw "Complete Vercel publication journal deployment authorization does not match this invocation."
    }
    if ([string]$Journal.result_payload.promoted_deployment_id -ne [string]$Journal.promoted_deployment_id -or
        [string]$Journal.result_payload.production_deployment_id -ne [string]$Journal.promoted_deployment_id) {
        throw "Complete Vercel publication journal deployment identity is inconsistent."
    }
}

function Write-VercelPublicationJournal {
    param(
        [Parameter(Mandatory = $true)][object]$Payload,
        [switch]$Transition
    )
    New-Item -ItemType Directory -Path $journalRoot -Force | Out-Null
    $temporary = Join-Path $journalRoot (".journal-input-" + [guid]::NewGuid().ToString("N") + ".json")
    try {
        $json = ConvertTo-VercelCanonicalJson $Payload
        [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
        $args = if ($Transition) {
            @("transition", $temporary, $journalPath, "--previous", $journalPath, "--state-root", $resolvedStateRoot)
        }
        else {
            @("seal", $temporary, $journalPath, "--state-root", $resolvedStateRoot)
        }
        return (Invoke-VercelJournalTool -Arguments $args -Label "Vercel publication journal write")
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Test-VercelPromotionSeam {
    param([Parameter(Mandatory = $true)][string]$Point)
    if ($TestFailurePoint -eq $Point) { throw "Test-only Vercel publication failure seam: $Point" }
    if ($TestCrashPoint -eq $Point) { Stop-Process -Id $PID -Force }
}

function Assert-GovernedPublicationAuthorization {
    if ([string]::IsNullOrWhiteSpace($resolvedExpectedMarketDate)) {
        throw "ExpectedMarketDate is required for scheduled publication."
    }
    if ([string]::IsNullOrWhiteSpace($PrepublicationAuthorizationId)) {
        throw "Immutable prepublication authorization identity is required."
    }
    $boundaryMode = if ($Promote) { "Production" } else { "Preview" }
    $boundaryArguments = @(
        "scripts\publication_boundary.py", "validate",
        "--market-date", $resolvedExpectedMarketDate,
        "--publication-mode", $boundaryMode
    )
    if ($TestNowUtc) {
        if ($env:DAWNSTRIKE_TEST_CLOCK -ne "1") {
            throw "Publication clock override is test-only."
        }
        $boundaryArguments += @("--now-utc", $TestNowUtc)
    }
    $boundary = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList $boundaryArguments `
        -LogRoot (Join-Path $resolvedStateRoot "logs") `
        -LogName "vercel_publication_market_boundary"
    if ($boundary.exit_code -ne 0) {
        throw "Vercel publication market boundary rejected ExpectedMarketDate."
    }
    $database = Join-Path $resolvedStateRoot "shadow_real.sqlite"
    $artifactRoot = Join-Path $resolvedRoot "build\public"
    $verify = Invoke-DawnstrikeNativeProcess `
        -FilePath "py.exe" `
        -ArgumentList @(
            "scripts\verify_daily_prepublication.py", "--db-path", $database,
            "--artifact-root", $artifactRoot, "--market-date", $resolvedExpectedMarketDate,
            "--expected-market-date", $resolvedExpectedMarketDate,
            "--release-sha", $expectedSourceSha, "--runtime-root", $resolvedRoot
        ) `
        -LogRoot (Join-Path $resolvedStateRoot "logs") `
        -LogName "vercel_publication_authorization"
    if ($verify.exit_code -ne 0) {
        throw "Vercel publication requires a passing governed prepublication authorization."
    }
    try {
        $payload = (Get-Content -LiteralPath $verify.stdout_path -Raw) | ConvertFrom-Json
    } catch { throw "Governed prepublication authorization returned invalid JSON." }
    if ([string]$payload.expected_market_date -cne $resolvedExpectedMarketDate -or
        [string]$payload.authorization_id -cne $PrepublicationAuthorizationId -or
        [string]$payload.daily_ledger_authorization_id -cne $PrepublicationAuthorizationId) {
        throw "Vercel publication authorization identity is not immutable or does not match the daily ledger."
    }
}

function Get-VercelResultSha256 {
    param([Parameter(Mandatory = $true)][object]$Payload)
    return Get-Sha256Hex (ConvertTo-VercelCanonicalJson $Payload)
}

function Write-VercelResultAtomic {
    param([Parameter(Mandatory = $true)][object]$Payload)
    $json = ConvertTo-VercelCanonicalJson $Payload
    $temporary = "$resultPath.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
        $stream = [System.IO.File]::Open($temporary, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        try { $stream.Flush($true) } finally { $stream.Dispose() }
        [System.IO.File]::Move($temporary, $resultPath, $true)
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
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
    if ($resolvedExpectedMarketDate -and
        [string]$BuildManifest.market_date -cne $resolvedExpectedMarketDate) {
        throw "$Label build market date does not match ExpectedMarketDate."
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

function Assert-ProductionDateLineage {
    param(
        [Parameter(Mandatory = $true)][object]$CurrentBuildManifest,
        [Parameter(Mandatory = $true)][object]$CandidateBuildManifest,
        [Parameter(Mandatory = $true)][object]$CurrentReleaseManifest,
        [Parameter(Mandatory = $true)][object]$CandidateReleaseManifest
    )
    $currentDate = [string](Get-OptionalJsonProperty -InputObject $CurrentBuildManifest -Name "market_date")
    $candidateDate = [string](Get-OptionalJsonProperty -InputObject $CandidateBuildManifest -Name "market_date")
    try {
        $current = [DateTime]::ParseExact($currentDate, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
        $candidate = [DateTime]::ParseExact($candidateDate, "yyyy-MM-dd", [Globalization.CultureInfo]::InvariantCulture)
    } catch { throw "Production lineage market date is invalid." }
    if ($candidate -lt $current) {
        throw "Production promotion is regressive: candidate market date is older than the served build."
    }
    if ($candidate -eq $current) {
        $sameLineage = (
            [string]$CurrentBuildManifest.source_sha -ceq [string]$CandidateBuildManifest.source_sha -and
            [string]$CurrentBuildManifest.build_sha -ceq [string]$CandidateBuildManifest.build_sha -and
            [string]$CurrentBuildManifest.publication_set_sha256 -ceq [string]$CandidateBuildManifest.publication_set_sha256 -and
            [string]$CurrentBuildManifest.release_manifest_sha256 -ceq [string]$CandidateBuildManifest.release_manifest_sha256
        )
        if (-not $sameLineage) {
            throw "Production promotion cannot replace a frozen same-day lineage with divergent hashes."
        }
    }
    foreach ($item in @(
        @{ name = "served"; value = $CurrentReleaseManifest.data_watermark; expected = $currentDate },
        @{ name = "candidate"; value = $CandidateReleaseManifest.data_watermark; expected = $candidateDate }
    )) {
        if ($item.value -and [string]$item.value -notmatch '^\d{4}-\d{2}-\d{2}$') {
            throw "Production $($item.name) release manifest date is invalid."
        }
        if ($item.value -and [string]$item.value -ne [string]$item.expected) {
            throw "Production $($item.name) build/release manifest dates diverge."
        }
    }
}

function Get-VercelAliasObservation {
    param([Parameter(Mandatory = $true)][string]$Alias)
    $observed = Invoke-VercelJson `
        -Arguments @("inspect", $Alias, "--json") `
        -Label "Publication alias inspect for $Alias"
    $id = [string](Get-OptionalJsonProperty -InputObject $observed -Name "id")
    $url = [string](Get-OptionalJsonProperty -InputObject $observed -Name "url")
    if (-not $id -or -not $url) { throw "Publication alias inspect is incomplete for $Alias." }
    return [pscustomobject]@{ alias = $Alias; id = $id; url = $url }
}

function Test-VercelAliasSetMatches {
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][ValidateSet("prior", "candidate")][string]$Kind
    )
    $observed = @($allProductionAliases | ForEach-Object { Get-VercelAliasObservation ([string]$_) })
    $matches = $true
    foreach ($item in $observed) {
        if ($Kind -eq "prior") {
            $expected = @($Journal.prior_aliases | Where-Object { [string]$_.alias -eq [string]$item.alias })[0]
            if ($null -eq $expected -or [string]$expected.deployment_id -ne [string]$item.id -or
                (Normalize-VercelDeploymentUrl $expected.deployment_url) -ne (Normalize-VercelDeploymentUrl $item.url)) {
                $matches = $false
            }
        }
        else {
            if ([string]$Journal.promoted_deployment_id -ne [string]$item.id -or
                (Normalize-VercelDeploymentUrl $Journal.promoted_deployment_url) -ne (Normalize-VercelDeploymentUrl $item.url)) {
                $matches = $false
            }
        }
    }
    return [bool]$matches
}

function Test-VercelAliasSourceMatchesJournal {
    param([Parameter(Mandatory = $true)][object]$Journal)
    foreach ($alias in $allProductionAliases) {
        try {
            $proof = Get-VercelAliasEndpointProof -AliasUrl ([string]$alias) -CacheBuster ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())
            if (-not $proof.source_manifest_available -or
                [string]$proof.source_sha -ne [string]$Journal.candidate_source_sha -or
                [string]$proof.source_tree -ne [string]$Journal.candidate_source_tree -or
                [string]$proof.source_manifest_sha256 -ne [string]$Journal.candidate_manifest_sha256) { return $false }
        }
        catch { return $false }
    }
    return $true
}

function New-VercelPublicationJournalPayload {
    param(
        [Parameter(Mandatory = $true)][string]$Phase,
        [Parameter(Mandatory = $true)][int]$Sequence,
        [Parameter(Mandatory = $true)][object]$CandidateDeployment,
        [Parameter(Mandatory = $true)][object]$PreviewManifest,
        [Parameter(Mandatory = $true)][string]$PackageManifestSha256,
        [string]$CandidateManifestSha256 = "",
        [Parameter(Mandatory = $true)][object[]]$PriorAliases,
        [AllowNull()][object]$PromotedDeployment,
        [AllowNull()][object]$ResultPayload,
        [string]$ExpectedPublicationMarketDate = "",
        [string]$PrepublicationAuthorization = "",
        [string]$DailyLedgerAuthorization = "",
        [string]$PriorJournalHash = $emptySha256,
        [string]$CompensationRelativePath = "NONE",
        [string]$CompensationSha256 = $emptySha256
    )
    $resultHash = if ($null -eq $ResultPayload) { $emptySha256 } else { Get-VercelResultSha256 $ResultPayload }
    $payload = [ordered]@{
        schema_version = if ($Phase -eq "COMPENSATED") { "dawnstrike.vercel_publication_journal.v2" } else { "dawnstrike.vercel_publication_journal.v1" }
        operation = "vercel_publication"
        phase = $Phase
        sequence = $Sequence
        project_id = $ProjectId
        project_name = $ProjectName
        production_aliases = @($allProductionAliases)
        candidate_preview_url = [string]$CandidateDeployment.url
        candidate_preview_deployment_id = [string]$CandidateDeployment.id
        candidate_source_sha = [string]$PreviewManifest.source_sha
        candidate_source_tree = $expectedSourceTree
        candidate_market_date = [string]$PreviewManifest.market_date
        candidate_build_id = [string]$PreviewManifest.build_id
        candidate_build_sha = [string]$PreviewManifest.build_sha
        candidate_manifest_sha256 = if ($CandidateManifestSha256) { $CandidateManifestSha256 } else {
            Get-Sha256Hex (Get-VercelSourceManifestCanonicalJson -Path (Join-Path $stage "vercel-source-manifest.json"))
        }
        candidate_package_manifest_sha256 = $PackageManifestSha256
        prior_aliases = @($PriorAliases)
        promoted_deployment_id = if ($null -eq $PromotedDeployment) { $null } else { [string]$PromotedDeployment.id }
        promoted_deployment_url = if ($null -eq $PromotedDeployment) { $null } else { [string]$PromotedDeployment.url }
        production_result_sha256 = $resultHash
        result_relative_path = $resultRelativePath
        result_payload = $ResultPayload
        prior_journal_file_sha256 = $PriorJournalHash
        compensation_relative_path = $CompensationRelativePath
        compensation_sha256 = $CompensationSha256
        recorded_at_utc = [DateTimeOffset]::UtcNow.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'")
        research_only = $true
        broker_execution_enabled = $false
    }
    if ($ExpectedPublicationMarketDate -or $PrepublicationAuthorization -or $DailyLedgerAuthorization) {
        if (-not $ExpectedPublicationMarketDate -or
            -not $PrepublicationAuthorization -or
            -not $DailyLedgerAuthorization) {
            throw "Vercel publication journal authorization identity is incomplete."
        }
        $payload.expected_market_date = $ExpectedPublicationMarketDate
        $payload.prepublication_authorization_id = $PrepublicationAuthorization
        $payload.daily_ledger_authorization_id = $DailyLedgerAuthorization
    }
    return $payload
}

function Invoke-VercelPublicationCompensation {
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][string]$FailureType
    )
    $errors = @()
    $rollbackEvidence = @()
    foreach ($alias in $allProductionAliases) {
        try {
            $prior = @($Journal.prior_aliases | Where-Object { [string]$_.alias -eq [string]$alias })[0]
            if ($null -eq $prior) { throw "No exact prior snapshot exists for $alias." }
            Set-VercelAlias -DeploymentUrl ([string]$prior.deployment_url) -AliasUrl ([string]$alias) -Label "Compensation rollback for $alias"
            $after = Get-VercelAliasObservation ([string]$alias)
            if ([string]$after.id -ne [string]$prior.deployment_id -or
                (Normalize-VercelDeploymentUrl $after.url) -ne (Normalize-VercelDeploymentUrl $prior.deployment_url)) {
                throw "Compensation rollback resolved the wrong deployment for $alias."
            }
            $rollbackEvidence += [ordered]@{
                alias = [string]$alias
                expected_deployment_id = [string]$prior.deployment_id
                expected_deployment_url = [string]$prior.deployment_url
                observed_deployment_id = [string]$after.id
                observed_deployment_url = [string]$after.url
                restored = $true
            }
        }
        catch { $errors += "${alias}: $($_.Exception.Message)" }
    }
    if ($errors.Count -gt 0) { throw "Vercel publication compensation failed: $($errors -join '; ')" }
    $compensation = [ordered]@{
        schema_version = "dawnstrike.vercel_publication_compensation.v1"
        status = "COMPENSATED"
        operation = "vercel_publication"
        candidate_source_sha = [string]$Journal.candidate_source_sha
        candidate_source_tree = [string]$Journal.candidate_source_tree
        candidate_preview_deployment_id = [string]$Journal.candidate_preview_deployment_id
        promoted_deployment_id = if ($Journal.promoted_deployment_id) { [string]$Journal.promoted_deployment_id } else { $null }
        promoted_deployment_url = if ($Journal.promoted_deployment_url) { [string]$Journal.promoted_deployment_url } else { $null }
        prior_aliases = @($Journal.prior_aliases)
        rollback_evidence = @($rollbackEvidence | Sort-Object -Property alias)
        rollback_status = "ROLLED_BACK"
        failure_type = $FailureType
        research_only = $true
        broker_execution_enabled = $false
        recorded_at_utc = [DateTimeOffset]::UtcNow.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'")
    }
    $compensation.receipt_self_sha256 = Get-VercelResultSha256 $compensation
    $compensationJson = ConvertTo-VercelCanonicalJson $compensation
    $compensationPath = Join-Path $journalRoot ("vercel-publication-compensation-" + [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() + ".json")
    [System.IO.File]::WriteAllText($compensationPath, $compensationJson, (New-Object System.Text.UTF8Encoding($false)))
    $null = Invoke-VercelJournalTool `
        -Arguments @("verify-compensation", $compensationPath, "--state-root", $resolvedStateRoot) `
        -Label "Vercel publication compensation verification"
    $compensationHash = Get-VercelFileSha256 -Path $compensationPath
    $next = New-VercelPublicationJournalPayload `
        -Phase "COMPENSATED" -Sequence 3 `
        -CandidateDeployment ([pscustomobject]@{ url = $Journal.candidate_preview_url; id = $Journal.candidate_preview_deployment_id }) `
        -PreviewManifest ([pscustomobject]@{ source_sha = $Journal.candidate_source_sha; market_date = $Journal.candidate_market_date; build_id = $Journal.candidate_build_id; build_sha = $Journal.candidate_build_sha }) `
        -PackageManifestSha256 ([string]$Journal.candidate_package_manifest_sha256) `
        -CandidateManifestSha256 ([string]$Journal.candidate_manifest_sha256) `
        -PriorAliases @($Journal.prior_aliases) `
        -PromotedDeployment (if ($Journal.promoted_deployment_id) { [pscustomobject]@{ id = $Journal.promoted_deployment_id; url = $Journal.promoted_deployment_url } } else { $null }) `
        -ResultPayload $Journal.result_payload `
        -ExpectedPublicationMarketDate ([string]$Journal.expected_market_date) `
        -PrepublicationAuthorization ([string]$Journal.prepublication_authorization_id) `
        -DailyLedgerAuthorization ([string]$Journal.daily_ledger_authorization_id) `
        -PriorJournalHash (Get-Sha256Hex ([System.IO.File]::ReadAllText($journalPath))) `
        -CompensationRelativePath (([System.IO.Path]::GetRelativePath($resolvedStateRoot, $compensationPath)) -replace '\\','/') `
        -CompensationSha256 $compensationHash
    $null = Write-VercelPublicationJournal -Payload $next -Transition
}

function Get-VercelJournalPreviewEvidence {
    param([Parameter(Mandatory = $true)][object]$Journal, [switch]$UsePromoted)
    $previewUrl = if ($UsePromoted -and $Journal.promoted_deployment_url) {
        [string]$Journal.promoted_deployment_url
    }
    else { [string]$Journal.candidate_preview_url }
    $health = Invoke-VercelJson -Arguments @("curl", "$previewUrl/api/health?recovery_verify=1") -Label "Recovery preview health"
    $readiness = Invoke-VercelJson -Arguments @("curl", "$previewUrl/api/readiness?recovery_verify=1") -Label "Recovery preview readiness"
    $manifest = Invoke-VercelJson -Arguments @("curl", "$previewUrl/build-manifest.json?recovery_verify=1") -Label "Recovery preview build manifest"
    $release = Invoke-VercelJson -Arguments @("curl", "$previewUrl/release-manifest.json?recovery_verify=1") -Label "Recovery preview release manifest"
    Assert-PublicationState -Health $health -Readiness $readiness -BuildManifest $manifest -ReleaseManifest $release -ExpectedSourceSha $expectedSourceSha -Label "Recovery preview"
    if ([string]$manifest.source_sha -ne [string]$Journal.candidate_source_sha -or
        [string]$manifest.build_id -ne [string]$Journal.candidate_build_id -or
        [string]$manifest.build_sha -ne [string]$Journal.candidate_build_sha -or
        [string]$manifest.market_date -ne [string]$Journal.candidate_market_date) {
        throw "Recovery preview identity does not match the journal candidate."
    }
    return [pscustomobject]@{
        url = $previewUrl
        id = [string]$Journal.candidate_preview_deployment_id
        health = $health
        readiness = $readiness
        manifest = $manifest
        release = $release
    }
}

function New-VercelRecoveredResultPayload {
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][object]$Live,
        [Parameter(Mandatory = $true)][object]$Health,
        [Parameter(Mandatory = $true)][object]$Readiness,
        [Parameter(Mandatory = $true)][object]$Manifest,
        [Parameter(Mandatory = $true)][object]$ReleaseManifest
    )
    Assert-PublicationState -Health $Health -Readiness $Readiness -BuildManifest $Manifest -ReleaseManifest $ReleaseManifest -ExpectedSourceSha $expectedSourceSha -Label "Recovered production"
    return [ordered]@{
        schema_version = "dawnstrike.daily_deployment.v1"
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        project_id = [string]$Journal.project_id
        preview_url = [string]$Journal.candidate_preview_url
        preview_deployment_id = [string]$Journal.candidate_preview_deployment_id
        preview_ready_state = "READY"
        source_sha = [string]$Manifest.source_sha
        source_tree = [string]$Journal.candidate_source_tree
        vercel_source_manifest_sha256 = [string]$Journal.candidate_manifest_sha256
        vercel_package_manifest_sha256 = [string]$Journal.candidate_package_manifest_sha256
        build_id = [string]$Manifest.build_id
        build_sha = [string]$Manifest.build_sha
        data_hash_sha256 = [string]$Manifest.data_hash_sha256
        publication_set_sha256 = [string]$Manifest.publication_set_sha256
        opportunity_projection_sha256 = [string]$Manifest.opportunity_projection_sha256
        v6_learning_sha256 = [string]$Manifest.v6_learning_sha256
        release_manifest_sha256 = Get-OptionalJsonProperty -InputObject $ReleaseManifest -Name "release_manifest_sha256"
        market_date = [string]$Manifest.market_date
        snapshot_status = [string]$Readiness.snapshot_status
        readiness_status = [string]$Readiness.status
        readiness_http_status = $Readiness.http_status
        allow_degraded = $false
        promoted = $true
        expected_market_date = [string]$Journal.candidate_market_date
        prepublication_authorization_id = [string]$Journal.prepublication_authorization_id
        daily_ledger_authorization_id = [string]$Journal.daily_ledger_authorization_id
        prior_production_deployment_id = @($Journal.prior_aliases | Where-Object { [string]$_.alias -eq [string]$ProductionAlias })[0].deployment_id
        production_aliases = @($allProductionAliases)
        promoted_deployment_id = [string]$Live.id
        production_deployment_id = [string]$Live.id
        live_trading_enabled = $false
        research_only = $true
        status = "PRODUCTION_VERIFIED"
    }
}

if ($Promote -or $PrepublicationAuthorizationId) {
    # This runs before any provider command.  The publisher is a consumer of
    # the read-only daily ledger authorization, never its creator.
    Assert-GovernedPublicationAuthorization
}

function Complete-VercelJournalRecovery {
    param([Parameter(Mandatory = $true)][object]$Journal)
    if (-not (Test-VercelAliasSetMatches -Journal $Journal -Kind candidate)) {
        throw "Recovery candidate aliases are not exact."
    }
    $evidence = Get-VercelJournalPreviewEvidence -Journal $Journal -UsePromoted
    if ($null -eq $Journal.result_payload) { throw "Recovery journal has no exact production result payload." }
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf) -or
        (Get-VercelFileSha256 -Path $resultPath) -ne [string]$Journal.production_result_sha256) {
        Write-VercelResultAtomic -Payload $Journal.result_payload
    }
    if ((Get-VercelFileSha256 -Path $resultPath) -ne [string]$Journal.production_result_sha256) {
        throw "Recovery result bytes do not match the journal result hash."
    }
    Test-VercelPromotionSeam "after_result_write_before_complete"
    $complete = [ordered]@{}
    foreach ($property in $Journal.PSObject.Properties) { $complete[$property.Name] = $property.Value }
    $complete.phase = "COMPLETE"
    $complete.sequence = 2
    $complete.prior_journal_file_sha256 = Get-VercelFileSha256 -Path $journalPath
    $complete.recorded_at_utc = [DateTimeOffset]::UtcNow.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'")
    $null = Write-VercelPublicationJournal -Payload $complete -Transition
    return (Get-Content -Raw -LiteralPath $resultPath)
}

$recoveryRetry = $false
$existingJournal = $null
if ($Promote) {
    if (-not $ExpectedMarketDate) {
        throw "Production publication requires an exact ExpectedMarketDate for the operation lock."
    }
    Acquire-VercelPublicationLock `
        -CandidateSourceSha $expectedSourceSha `
        -CandidateSourceTree $expectedSourceTree `
        -CandidateMarketDate $ExpectedMarketDate
    # Read the journal only after taking the global lock. This closes the
    # check-then-act window where two publishers could both observe no journal
    # and proceed to mutate production aliases.
    $existingJournal = Get-VercelPublicationJournal
    if ($null -ne $existingJournal) {
        Assert-VercelJournalBaseMatchesInvocation -Journal $existingJournal
    }
}
try {
if ($null -ne $existingJournal) {
    if ([string]$existingJournal.phase -eq "COMPENSATED") {
        throw "A terminal compensated Vercel publication journal already exists; manual review is required."
    }
    if ([string]$existingJournal.phase -eq "COMPLETE") {
        Assert-VercelJournalMatchesInvocation -Journal $existingJournal
        if (-not (Test-VercelAliasSetMatches -Journal $existingJournal -Kind candidate)) {
            throw "Complete Vercel publication journal does not match the live aliases."
        }
        if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf) -or
            (Get-VercelFileSha256 -Path $resultPath) -ne [string]$existingJournal.production_result_sha256) {
            Write-VercelResultAtomic -Payload $existingJournal.result_payload
        }
        if ((Get-VercelFileSha256 -Path $resultPath) -ne [string]$existingJournal.production_result_sha256) {
            throw "Complete Vercel publication result is not byte-exact."
        }
        Write-Output (Get-Content -Raw -LiteralPath $resultPath)
        return
    }
    $candidateLive = ([string]$existingJournal.phase -ne "PRE_MUTATION" -and
        (Test-VercelAliasSetMatches -Journal $existingJournal -Kind candidate)) -or
        ([string]$existingJournal.phase -eq "PRE_MUTATION" -and
        (Test-VercelAliasSourceMatchesJournal -Journal $existingJournal))
    if ($candidateLive) {
        if ([string]$existingJournal.phase -eq "PRE_MUTATION") {
            $live = Get-VercelAliasObservation ([string]$ProductionAlias)
            $liveHealth = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/api/health?recovery_verify=1") -Label "Recovered production health"
            $liveReadiness = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/api/readiness?recovery_verify=1") -Label "Recovered production readiness"
            $liveManifest = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/build-manifest.json?recovery_verify=1") -Label "Recovered production build manifest"
            $liveRelease = Invoke-VercelJson -Arguments @("curl", "$ProductionAlias/release-manifest.json?recovery_verify=1") -Label "Recovered production release manifest"
            $recoveredResult = New-VercelRecoveredResultPayload -Journal $existingJournal -Live $live -Health $liveHealth -Readiness $liveReadiness -Manifest $liveManifest -ReleaseManifest $liveRelease
            $postRecovery = New-VercelPublicationJournalPayload `
                -Phase "POST_ALIASES" -Sequence 1 `
                -CandidateDeployment ([pscustomobject]@{ id = $existingJournal.candidate_preview_deployment_id; url = $existingJournal.candidate_preview_url }) `
                -PreviewManifest ([pscustomobject]@{ source_sha = $existingJournal.candidate_source_sha; market_date = $existingJournal.candidate_market_date; build_id = $existingJournal.candidate_build_id; build_sha = $existingJournal.candidate_build_sha }) `
                -PackageManifestSha256 ([string]$existingJournal.candidate_package_manifest_sha256) `
                -CandidateManifestSha256 ([string]$existingJournal.candidate_manifest_sha256) `
                -PriorAliases @($existingJournal.prior_aliases) `
                -PromotedDeployment $live `
                -ResultPayload $recoveredResult `
                -ExpectedPublicationMarketDate ([string]$existingJournal.expected_market_date) `
                -PrepublicationAuthorization ([string]$existingJournal.prepublication_authorization_id) `
                -DailyLedgerAuthorization ([string]$existingJournal.daily_ledger_authorization_id) `
                -PriorJournalHash (Get-VercelFileSha256 -Path $journalPath)
            $null = Write-VercelPublicationJournal -Payload $postRecovery -Transition
            $existingJournal = Get-VercelPublicationJournal
        }
        Write-Output (Complete-VercelJournalRecovery -Journal $existingJournal)
        return
    }
    if (-not (Test-VercelAliasSetMatches -Journal $existingJournal -Kind prior)) {
        try { Invoke-VercelPublicationCompensation -Journal $existingJournal -FailureType "mixed_or_uncertain_alias_state_on_retry" }
        catch { throw "Vercel publication retry is mixed or uncertain and compensation did not complete: $($_.Exception.Message)" }
        throw "Vercel publication retry found mixed or uncertain aliases; publication was compensated."
    }
    $recoveryRetry = $true
    foreach ($priorRecord in @($existingJournal.prior_aliases)) {
        $priorProductionAliases[[string]$priorRecord.alias] = [pscustomobject]@{
            id = [string]$priorRecord.deployment_id
            url = [string]$priorRecord.deployment_url
            health_available = $false
            health_status = $null
            readiness_available = $false
            readiness_status = $null
            readiness_http_status = $null
            source_manifest_available = $false
            source_sha = $null
            source_tree = $null
            source_manifest_sha256 = $null
        }
    }
    $recoveryPreview = Get-VercelJournalPreviewEvidence -Journal $existingJournal
    $deployment = [pscustomobject]@{ id = [string]$existingJournal.candidate_preview_deployment_id; url = [string]$existingJournal.candidate_preview_url }
    $deploymentId = [string]$existingJournal.candidate_preview_deployment_id
    $previewUrl = [string]$existingJournal.candidate_preview_url
    $previewHealth = $recoveryPreview.health
    $previewReadiness = $recoveryPreview.readiness
    $previewManifest = $recoveryPreview.manifest
    $previewReleaseManifest = $recoveryPreview.release
    $packageManifestSha256 = [string]$existingJournal.candidate_package_manifest_sha256
    $candidateManifestSha256 = [string]$existingJournal.candidate_manifest_sha256
    $journalPriorAliases = @($existingJournal.prior_aliases)
    $journalCandidate = [pscustomobject]@{ id = $deploymentId; url = $previewUrl }
    $priorProduction = [pscustomobject]@{
        id = @($existingJournal.prior_aliases | Where-Object { [string]$_.alias -eq [string]$ProductionAlias })[0].deployment_id
    }
}

if (-not $recoveryRetry) {
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
    # Provider build output can contain environment values; never echo it.
    $previewEnvironmentFile = Join-Path $stage ".vercel\.env.preview.local"
    if (Test-Path -LiteralPath $previewEnvironmentFile -PathType Leaf) {
        Remove-Item -LiteralPath $previewEnvironmentFile -Force
    }
    Assert-VercelNoEnvironmentArtifacts -StageRoot $stage
    Add-VercelFunctionPublicBindings -StageRoot $stage
    $packageManifestSha256 = Assert-VercelBuiltPackage `
        -StageRoot $stage `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree
    # Seal the complete post-build output inventory before any deploy command;
    # later checks must compare against this in-memory hash, never authorize a
    # rewritten manifest from the mutable stage directory.
    Assert-VercelGitSourceStable `
        -Root $resolvedRoot `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree `
        -AllowedStageRoot $stage
    Assert-VercelStagedSourceManifest `
        -StageRoot $stage `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree
    Add-VercelFunctionPublicBindings -StageRoot $stage
    Assert-VercelNoEnvironmentArtifacts -StageRoot $stage
    $null = Assert-VercelBuiltPackage `
        -StageRoot $stage `
        -ExpectedSourceSha $expectedSourceSha `
        -ExpectedSourceTree $expectedSourceTree `
        -ExpectedPackageManifestSha256 $packageManifestSha256
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
Assert-RemoteVercelSourceManifest `
    -Url "$previewUrl/vercel-source-manifest.json" `
    -Label "Preview"
Assert-PublicationState `
    -Health $previewHealth `
    -Readiness $previewReadiness `
    -BuildManifest $previewManifest `
    -ReleaseManifest $previewReleaseManifest `
    -ExpectedSourceSha $expectedSourceSha `
    -Label "Preview"
if (-not $candidateManifestSha256) {
    $candidateManifestSha256 = Get-Sha256Hex (Get-VercelSourceManifestCanonicalJson -Path (Join-Path $stage "vercel-source-manifest.json"))
}

if ($Promote) {
    # Every alias is independent external state.  Snapshot each target before
    # promotion so an uncertain promotion can restore the exact deployment
    # that was serving that alias, rather than copying the primary alias to
    # every hostname.
    foreach ($alias in $allProductionAliases) {
        $snapshot = Invoke-VercelJson `
            -Arguments @("inspect", [string]$alias, "--json") `
            -Label "Prior production inspect for $alias"
        $snapshotId = Get-OptionalJsonProperty -InputObject $snapshot -Name "id"
        $snapshotUrl = [string](
            Get-OptionalJsonProperty -InputObject $snapshot -Name "url"
        )
        if (-not $snapshotId -or -not $snapshotUrl) {
            throw "Prior production inspect did not return a deployment ID and URL for $alias."
        }
        $endpointProof = Get-VercelAliasEndpointProof `
            -AliasUrl ([string]$alias) `
            -CacheBuster ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()) `
            -RequireHealthReadiness
        $priorProductionAliases[[string]$alias] = [pscustomobject]@{
            id = [string]$snapshotId
            url = $snapshotUrl
            health_available = [bool]$endpointProof.health_available
            health_status = [string]$endpointProof.health_status
            readiness_available = [bool]$endpointProof.readiness_available
            readiness_status = [string]$endpointProof.readiness_status
            readiness_http_status = $endpointProof.readiness_http_status
            source_manifest_available = [bool]$endpointProof.source_manifest_available
            source_sha = [string]$endpointProof.source_sha
            source_tree = [string]$endpointProof.source_tree
            source_manifest_sha256 = [string]$endpointProof.source_manifest_sha256
            build_manifest = $null
            release_manifest = $null
        }
        try {
            $priorProductionAliases[[string]$alias].build_manifest = Invoke-VercelJson `
                -Arguments @("curl", "$alias/build-manifest.json?rollback_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())") `
                -Label "Prior production build manifest for $alias"
            $priorProductionAliases[[string]$alias].release_manifest = Invoke-VercelJson `
                -Arguments @("curl", "$alias/release-manifest.json?rollback_verify=$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds())") `
                -Label "Prior production release manifest for $alias"
        }
        catch {
            throw "Prior production build/release lineage is unavailable for $alias."
        }
        if ([string]$alias -eq [string]$ProductionAlias) {
            $priorProduction = $snapshot
        }
    }
    $journalPriorAliases = @($allProductionAliases | ForEach-Object {
        $prior = $priorProductionAliases[[string]$_]
        [ordered]@{
            alias = [string]$_
            deployment_id = [string]$prior.id
            deployment_url = [string]$prior.url
        }
    })
    $journalCandidate = [pscustomobject]@{ id = [string]$deploymentId; url = [string]$previewUrl }
    $priorPrimaryRecord = $priorProductionAliases[[string]$ProductionAlias]
    Assert-ProductionDateLineage `
        -CurrentBuildManifest $priorPrimaryRecord.build_manifest `
        -CandidateBuildManifest $previewManifest `
        -CurrentReleaseManifest $priorPrimaryRecord.release_manifest `
        -CandidateReleaseManifest $previewReleaseManifest
    $preMutationJournal = New-VercelPublicationJournalPayload `
        -Phase "PRE_MUTATION" -Sequence 0 `
        -CandidateDeployment $journalCandidate `
        -PreviewManifest $previewManifest `
        -PackageManifestSha256 $packageManifestSha256 `
        -CandidateManifestSha256 $candidateManifestSha256 `
        -PriorAliases $journalPriorAliases `
        -ExpectedPublicationMarketDate $resolvedExpectedMarketDate `
        -PrepublicationAuthorization $PrepublicationAuthorizationId `
        -DailyLedgerAuthorization $DailyLedgerAuthorizationId
    $null = Write-VercelPublicationJournal -Payload $preMutationJournal
}

try {
    if ($Promote) {
        Assert-VercelGitSourceStable `
            -Root $resolvedRoot `
            -ExpectedSourceSha $expectedSourceSha `
            -ExpectedSourceTree $expectedSourceTree `
            -AllowedStageRoot $stage
        Assert-VercelStagedSourceManifest `
            -StageRoot $stage `
            -ExpectedSourceSha $expectedSourceSha `
            -ExpectedSourceTree $expectedSourceTree
        Add-VercelFunctionPublicBindings -StageRoot $stage
        Assert-VercelNoEnvironmentArtifacts -StageRoot $stage
        $null = Assert-VercelBuiltPackage `
            -StageRoot $stage `
            -ExpectedSourceSha $expectedSourceSha `
            -ExpectedSourceTree $expectedSourceTree `
            -ExpectedPackageManifestSha256 $packageManifestSha256
        # Mark the external state as potentially mutated before starting the
        # command. A timeout can occur after Vercel accepted the promotion, so
        # every promotion failure must enter the existing rollback boundary.
        $promoted = $true
        $null = Invoke-VercelProcess `
            -Arguments @("promote", $previewUrl, "--yes") `
            -Label "Vercel promotion" `
            -TimeoutSeconds $VercelCommandTimeoutSeconds
        Test-VercelPromotionSeam "after_promote"
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
                Assert-RemoteVercelSourceManifest `
                    -Url "$promotedUrl/vercel-source-manifest.json?verify=$cacheBuster" `
                    -Label "Promoted deployment"
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
        Test-VercelPromotionSeam "after_aliases"
        foreach ($alias in $allProductionAliases) {
            $aliasAfterPromotion = Get-VercelAliasObservation ([string]$alias)
            if ([string]$aliasAfterPromotion.id -ne [string]$promotedDeploymentId -or
                (Normalize-VercelDeploymentUrl $aliasAfterPromotion.url) -ne (Normalize-VercelDeploymentUrl $promotedUrl)) {
                throw "Production alias verification resolved the wrong deployment for $alias."
            }
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
                Assert-RemoteVercelSourceManifest `
                    -Url "$ProductionAlias/vercel-source-manifest.json?verify=$cacheBuster" `
                    -Label "Production"
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
        Test-VercelPromotionSeam "after_production_verification"
    }
    else {
        $production = $null
        $productionHealth = $null
        $productionReadiness = $null
        $productionManifest = $null
    }
    $result = [ordered]@{
        schema_version = "dawnstrike.daily_deployment.v1"
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        project_id = $ProjectId
        preview_url = $previewUrl
        preview_deployment_id = $deploymentId
        preview_ready_state = Get-OptionalJsonProperty -InputObject $deployment -Name "readyState"
        source_sha = $previewManifest.source_sha
        source_tree = $expectedSourceTree
        vercel_source_manifest_sha256 = Get-VercelFileSha256 -Path (Join-Path $stage "vercel-source-manifest.json")
        vercel_package_manifest_sha256 = $packageManifestSha256
        build_id = $previewManifest.build_id
        build_sha = $previewManifest.build_sha
        data_hash_sha256 = $previewManifest.data_hash_sha256
        publication_set_sha256 = $previewManifest.publication_set_sha256
        opportunity_projection_sha256 = $previewManifest.opportunity_projection_sha256
        v6_learning_sha256 = $previewManifest.v6_learning_sha256
        release_manifest_sha256 = Get-OptionalJsonProperty -InputObject $previewReleaseManifest -Name "release_manifest_sha256"
        market_date = $previewManifest.market_date
        expected_market_date = $resolvedExpectedMarketDate
        prepublication_authorization_id = $PrepublicationAuthorizationId
        daily_ledger_authorization_id = $DailyLedgerAuthorizationId
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
    $journalNeedsPostTransition = $null -eq $existingJournal -or [string]$existingJournal.phase -eq "PRE_MUTATION"
    if ($Promote -and $journalNeedsPostTransition) {
        $postJournal = New-VercelPublicationJournalPayload `
            -Phase "POST_ALIASES" -Sequence 1 `
            -CandidateDeployment $journalCandidate `
            -PreviewManifest $previewManifest `
            -PackageManifestSha256 $packageManifestSha256 `
            -CandidateManifestSha256 $candidateManifestSha256 `
            -PriorAliases $journalPriorAliases `
            -PromotedDeployment ([pscustomobject]@{ id = [string]$promotedDeploymentId; url = [string]$promotedUrl }) `
            -ResultPayload $result `
            -ExpectedPublicationMarketDate $resolvedExpectedMarketDate `
            -PrepublicationAuthorization $PrepublicationAuthorizationId `
            -DailyLedgerAuthorization $DailyLedgerAuthorizationId `
            -PriorJournalHash (Get-VercelFileSha256 -Path $journalPath)
        $null = Write-VercelPublicationJournal -Payload $postJournal -Transition
    }
    Test-VercelPromotionSeam "result_write"
    Write-VercelResultAtomic -Payload $result
    if ($Promote) {
        Test-VercelPromotionSeam "after_result_write_before_complete"
        $completeJournal = New-VercelPublicationJournalPayload `
            -Phase "COMPLETE" -Sequence 2 `
            -CandidateDeployment $journalCandidate `
            -PreviewManifest $previewManifest `
            -PackageManifestSha256 $packageManifestSha256 `
            -CandidateManifestSha256 $candidateManifestSha256 `
            -PriorAliases $journalPriorAliases `
            -PromotedDeployment ([pscustomobject]@{ id = [string]$promotedDeploymentId; url = [string]$promotedUrl }) `
            -ResultPayload $result `
            -ExpectedPublicationMarketDate $resolvedExpectedMarketDate `
            -PrepublicationAuthorization $PrepublicationAuthorizationId `
            -DailyLedgerAuthorization $DailyLedgerAuthorizationId `
            -PriorJournalHash (Get-VercelFileSha256 -Path $journalPath)
        $null = Write-VercelPublicationJournal -Payload $completeJournal -Transition
    }
}
catch {
    $publicationError = $_.Exception.Message
    if ($promoted -and $priorProductionAliases.Count -eq $allProductionAliases.Count) {
        $rollbackErrors = @()
        $rollbackProofs = @()
        $primaryRollbackProof = $null
        $rollbackCacheBuster = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
        try {
            $priorPrimary = $priorProductionAliases[[string]$ProductionAlias]
            if ($null -eq $priorPrimary -or -not $priorPrimary.id) {
                throw "No complete prior primary deployment snapshot exists."
            }
            $null = Invoke-VercelProcess `
                -Arguments @("rollback", [string]$priorPrimary.id, "--yes") `
                -Label "Primary production rollback" `
                -TimeoutSeconds $VercelCommandTimeoutSeconds
            $primaryAfterRollback = Invoke-VercelJson `
                -Arguments @("inspect", $ProductionAlias, "--json") `
                -Label "Primary production rollback inspect"
            $primaryAfterId = [string](Get-OptionalJsonProperty `
                -InputObject $primaryAfterRollback -Name "id")
            if ($primaryAfterId -ne [string]$priorPrimary.id) {
                throw "Primary production rollback resolved to an unexpected deployment."
            }
            $primaryRollbackProof = [ordered]@{
                alias = [string]$ProductionAlias
                expected_deployment_id = [string]$priorPrimary.id
                observed_deployment_id = $primaryAfterId
                restored = $true
            }
        }
        catch {
            $rollbackErrors += "Primary production rollback: $($_.Exception.Message)"
        }
        foreach ($alias in $allProductionAliases) {
            try {
                $priorAlias = $priorProductionAliases[[string]$alias]
                if ($null -eq $priorAlias -or -not $priorAlias.url) {
                    throw "No complete prior deployment snapshot exists for $alias."
                }
                Set-VercelAlias `
                    -DeploymentUrl ([string]$priorAlias.url) `
                    -AliasUrl ([string]$alias) `
                    -Label "Production rollback for $alias"
                $rollbackProofs += Assert-VercelAliasRestored `
                    -AliasUrl ([string]$alias) `
                    -PriorAlias $priorAlias `
                    -CacheBuster $rollbackCacheBuster
            }
            catch {
                $rollbackErrors += $_.Exception.Message
            }
        }
        $rollbackSucceeded = $rollbackErrors.Count -eq 0
        $rollbackReceipt = [ordered]@{
            schema_version = "dawnstrike.daily_deployment_rollback.v1"
            generated_at = [DateTimeOffset]::UtcNow.ToString("o")
            project_id = $ProjectId
            candidate_preview_deployment_id = Get-OptionalJsonProperty `
                -InputObject $deployment -Name "id"
            candidate_promoted_deployment_id = Get-OptionalJsonProperty `
                -InputObject $promotedDeployment -Name "id"
            candidate_source_sha = $expectedSourceSha
            candidate_source_tree = $expectedSourceTree
            candidate_vercel_source_manifest_sha256 = if (Test-Path -LiteralPath (Join-Path $stage "vercel-source-manifest.json") -PathType Leaf) {
                Get-VercelFileSha256 -Path (Join-Path $stage "vercel-source-manifest.json")
            }
            else { $null }
            candidate_vercel_package_manifest_sha256 = $packageManifestSha256
            primary_rollback_proof = $primaryRollbackProof
            aliases = @($rollbackProofs)
            alias_errors = @($rollbackErrors)
            candidate_no_longer_live = [bool]$rollbackSucceeded
            status = if ($rollbackSucceeded) { "ROLLED_BACK" } else { "ROLLBACK_FAILED" }
            publication_error = $publicationError
        }
        try {
            $rollbackJson = $rollbackReceipt | ConvertTo-Json -Depth 20
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            $receiptPath = $rollbackResultPath
            if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
                $receiptPath = Join-Path `
                    (Split-Path -Parent $rollbackResultPath) `
                    "daily-deployment-rollback-result-$([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()).json"
            }
            [System.IO.File]::WriteAllText($receiptPath, $rollbackJson, $utf8NoBom)
        }
        catch {
            throw "$publicationError Rollback receipt write failed: $($_.Exception.Message)"
        }
        if (-not $rollbackSucceeded) {
            throw "$publicationError Rollback errors: $($rollbackErrors -join '; ')"
        }
        if (Test-Path -LiteralPath $journalPath -PathType Leaf) {
            try {
                $failedJournal = Get-VercelPublicationJournal
                if ($null -ne $failedJournal -and [string]$failedJournal.phase -ne "COMPLETE") {
                    Invoke-VercelPublicationCompensation `
                        -Journal $failedJournal `
                        -FailureType "publication_failure_rollback_completed"
                }
            }
            catch {
                throw "$publicationError Rollback completed but terminal compensation could not be persisted: $($_.Exception.Message)"
            }
        }
    }
    elseif ($promoted) {
        throw "$publicationError Rollback blocked: a complete per-alias production snapshot was not captured."
    }
    throw $publicationError
}
}
finally {
    Release-VercelPublicationLock
}
