function Move-DawnstrikePriorAlphaCycleArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)][string]$ArchiveRoot
    )
    if (-not (Test-Path -LiteralPath $ArtifactPath -PathType Leaf)) {
        return $null
    }
    New-Item -ItemType Directory -Path $ArchiveRoot -Force | Out-Null
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffffffZ")
    $archivePath = Join-Path $ArchiveRoot "alpha_cycle.$stamp.json"
    Move-Item -LiteralPath $ArtifactPath -Destination $archivePath
    return $archivePath
}

function Test-DawnstrikeNonNegativeInteger {
    param([AllowNull()][object]$Value)
    if ($null -eq $Value) { return $false }
    $integerTypes = @(
        [sbyte], [byte], [int16], [uint16], [int32], [uint32], [int64], [uint64]
    )
    foreach ($integerType in $integerTypes) {
        if ($Value -is $integerType) {
            return [decimal]$Value -ge 0
        }
    }
    return $false
}

function Test-DawnstrikeAlphaCycleArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [Parameter(Mandatory = $true)][object]$ProcessReceipt,
        [Parameter(Mandatory = $true)][string]$MarketDate
    )
    if (-not (Test-Path -LiteralPath $ArtifactPath -PathType Leaf)) {
        throw "Current AlphaOps cycle artifact is missing: $ArtifactPath"
    }
    $startedAtText = [string]$ProcessReceipt.started_at
    if ([string]::IsNullOrWhiteSpace($startedAtText)) {
        throw "AlphaOps process receipt is missing started_at."
    }
    try {
        $startedAt = [datetimeoffset]::Parse($startedAtText).UtcDateTime
    }
    catch {
        throw "AlphaOps process receipt started_at is invalid."
    }
    $artifactInfo = Get-Item -LiteralPath $ArtifactPath
    if ($artifactInfo.LastWriteTimeUtc -lt $startedAt) {
        throw "AlphaOps cycle artifact predates the current process attempt."
    }
    try {
        $payload = Get-Content -LiteralPath $ArtifactPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "AlphaOps cycle artifact is malformed JSON."
    }
    if ($null -eq $payload.run_contract) {
        throw "AlphaOps cycle artifact is missing run_contract."
    }
    $contract = $payload.run_contract
    if ([string]$contract.schema_version -ne "alphaops.run_contract.v1") {
        throw "AlphaOps cycle artifact has the wrong run-contract schema."
    }
    if ([string]$contract.producer -ne "alphaops") {
        throw "AlphaOps cycle artifact has the wrong producer."
    }
    if ([string]$contract.market_date -ne $MarketDate) {
        throw "AlphaOps cycle artifact market_date does not match the scheduled session."
    }
    $scanId = [string]$payload.scan_id
    $producerRunId = [string]$contract.producer_run_id
    if (
        [string]::IsNullOrWhiteSpace($scanId) -or
        [string]::IsNullOrWhiteSpace($producerRunId) -or
        $scanId -ne $producerRunId
    ) {
        throw "AlphaOps cycle artifact scan identity is missing or inconsistent."
    }
    if ([string]$contract.source_status -notin @("success", "ok")) {
        throw "AlphaOps cycle artifact source_status is not successful."
    }
    $payloadHasCount = $payload.PSObject.Properties.Name -contains "signal_count"
    $contractHasCount = $contract.PSObject.Properties.Name -contains "signal_count"
    if (-not $payloadHasCount -or -not $contractHasCount) {
        throw "AlphaOps cycle artifact is missing signal_count."
    }
    if (
        -not (Test-DawnstrikeNonNegativeInteger -Value $payload.signal_count) -or
        -not (Test-DawnstrikeNonNegativeInteger -Value $contract.signal_count)
    ) {
        throw "AlphaOps cycle artifact signal_count must be a nonnegative integer."
    }
    if ([decimal]$payload.signal_count -ne [decimal]$contract.signal_count) {
        throw "AlphaOps cycle artifact signal_count is inconsistent."
    }
    return [pscustomobject]@{
        scan_id = $scanId
        signal_count = [int64]$contract.signal_count
        market_date = $MarketDate
        source_status = [string]$contract.source_status
        artifact_last_write_utc = $artifactInfo.LastWriteTimeUtc.ToString("o")
        process_started_at_utc = $startedAt.ToString("o")
    }
}

function Resolve-DawnstrikeMorningOutcome {
    param(
        [Parameter(Mandatory = $true)][int]$CoreExitCode,
        [AllowNull()][Nullable[int]]$ScenarioExitCode,
        [Parameter(Mandatory = $true)][bool]$RecordStageFailed
    )
    $finalExit = $CoreExitCode
    if ($CoreExitCode -eq 0 -and $null -ne $ScenarioExitCode) {
        $finalExit = [int]$ScenarioExitCode
    }
    if ($RecordStageFailed) {
        $finalExit = 2
    }
    return [pscustomobject]@{
        core_status = if ($CoreExitCode -eq 0) { "COMPLETE" } else { "FAILED" }
        core_exit_code = $CoreExitCode
        final_exit_code = $finalExit
    }
}
