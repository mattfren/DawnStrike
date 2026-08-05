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
        [AllowNull()][object]$ProcessReceipt = $null,
        [Parameter(Mandatory = $true)][string]$MarketDate
    )
    if (-not (Test-Path -LiteralPath $ArtifactPath -PathType Leaf)) {
        throw "Current AlphaOps cycle artifact is missing: $ArtifactPath"
    }
    $startedAt = $null
    if ($null -ne $ProcessReceipt) {
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
    }
    $artifactInfo = Get-Item -LiteralPath $ArtifactPath
    if ($null -ne $startedAt -and $artifactInfo.LastWriteTimeUtc -lt $startedAt) {
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
    $contractHasResearchCount = $contract.PSObject.Properties.Name -contains "research_candidate_count"
    $contractHasResearchSymbols = $contract.PSObject.Properties.Name -contains "research_symbols"
    if (-not $contractHasResearchCount -or -not $contractHasResearchSymbols) {
        throw "AlphaOps cycle artifact is missing its research candidate universe."
    }
    if (-not (Test-DawnstrikeNonNegativeInteger -Value $contract.research_candidate_count)) {
        throw "AlphaOps cycle artifact research_candidate_count must be a nonnegative integer."
    }
    $researchSymbols = @($contract.research_symbols)
    if ([decimal]$contract.research_candidate_count -ne $researchSymbols.Count) {
        throw "AlphaOps cycle artifact research candidate count is inconsistent."
    }
    $uniqueResearchSymbols = @($researchSymbols | Select-Object -Unique)
    if ($uniqueResearchSymbols.Count -ne $researchSymbols.Count) {
        throw "AlphaOps cycle artifact research symbols must be unique."
    }
    foreach ($symbol in $researchSymbols) {
        if ([string]$symbol -cnotmatch '^[A-Z][A-Z0-9.-]{0,14}$') {
            throw "AlphaOps cycle artifact contains an invalid research symbol."
        }
    }
    return [pscustomobject]@{
        scan_id = $scanId
        signal_count = [int64]$contract.signal_count
        research_candidate_count = [int64]$contract.research_candidate_count
        research_symbols = @($researchSymbols)
        market_date = $MarketDate
        source_status = [string]$contract.source_status
        artifact_last_write_utc = $artifactInfo.LastWriteTimeUtc.ToString("o")
        process_started_at_utc = if ($null -eq $startedAt) { $null } else { $startedAt.ToString("o") }
    }
}

function Resolve-DawnstrikeCoreOptionalOutcome {
    param(
        [Parameter(Mandatory = $true)][int]$CoreExitCode,
        [AllowNull()][Nullable[int]]$OptionalExitCode,
        [Parameter(Mandatory = $true)][bool]$RecordStageFailed
    )
    $finalExit = $CoreExitCode
    if ($CoreExitCode -eq 0 -and $null -ne $OptionalExitCode) {
        $finalExit = [int]$OptionalExitCode
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

function Resolve-DawnstrikeMorningOutcome {
    param(
        [Parameter(Mandatory = $true)][int]$CoreExitCode,
        [AllowNull()][Nullable[int]]$ScenarioExitCode,
        [Parameter(Mandatory = $true)][bool]$RecordStageFailed
    )
    return Resolve-DawnstrikeCoreOptionalOutcome `
        -CoreExitCode $CoreExitCode `
        -OptionalExitCode $ScenarioExitCode `
        -RecordStageFailed $RecordStageFailed
}
