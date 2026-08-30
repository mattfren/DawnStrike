function Resolve-DawnstrikeNotificationFailureCode {
    param(
        [Parameter(Mandatory = $true)][string]$ReceiptRoot,
        [Parameter(Mandatory = $true)][string]$Stage,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$FallbackErrorCode,
        [AllowNull()][object]$ProcessReceipt = $null
    )
    $receiptPath = Join-Path $ReceiptRoot "notification-preflight-$Stage-$MarketDate.json"
    if (Test-Path -LiteralPath $receiptPath -PathType Leaf) {
        try {
            $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
            if (
                [string]$receipt.schema_version -ceq "dawnstrike.notification_preflight.v1" -and
                [string]$receipt.status -ceq "FAILED" -and
                [string]$receipt.error_code -ceq "notification_credentials_missing" -and
                [string]$receipt.stage -ceq $Stage -and
                [string]$receipt.market_date -ceq $MarketDate -and
                [string]$receipt.channel -ceq "telegram" -and
                [bool]$receipt.research_only -and
                -not [bool]$receipt.broker_execution_enabled
            ) {
                $recordedAt = [DateTimeOffset]::Parse([string]$receipt.recorded_at)
                if ($null -ne $ProcessReceipt) {
                    $startedAt = [DateTimeOffset]::Parse([string]$ProcessReceipt.started_at)
                    $completedAt = [DateTimeOffset]::Parse([string]$ProcessReceipt.completed_at)
                    if ($recordedAt -lt $startedAt -or $recordedAt -gt $completedAt) {
                        return $FallbackErrorCode
                    }
                }
                return "notification_credentials_missing"
            }
        }
        catch {
            # Preserve the child failure code when an auxiliary diagnostic is
            # malformed; the stage itself remains failed closed.
        }
    }
    return $FallbackErrorCode
}

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

function Restore-DawnstrikePriorAlphaCycleArtifact {
    param(
        [Parameter(Mandatory = $true)][string]$ArtifactPath,
        [AllowNull()][string]$ArchivePath,
        [switch]$QuarantineReplacement
    )
    if (Test-Path -LiteralPath $ArtifactPath -PathType Leaf) {
        if (-not $QuarantineReplacement) {
            return $false
        }
        # A failed attempt may have left an invalid replacement in the
        # canonical location. Preserve it for diagnosis, but clear the path
        # before restoring the last known-good artifact. Move-Item is used
        # with literal, bounded paths so this remains recoverable and does
        # not silently overwrite either artifact.
        $quarantineRoot = if (-not [string]::IsNullOrWhiteSpace($ArchivePath)) {
            Split-Path -Parent $ArchivePath
        } else {
            Join-Path (Split-Path -Parent $ArtifactPath) "attempt_archive"
        }
        New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
        $stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssfffffffZ")
        $quarantinePath = Join-Path $quarantineRoot "alpha_cycle.invalid.$stamp.$([guid]::NewGuid().ToString('N')).json"
        Move-Item -LiteralPath $ArtifactPath -Destination $quarantinePath
    }
    if (
        [string]::IsNullOrWhiteSpace($ArchivePath) -or
        -not (Test-Path -LiteralPath $ArchivePath -PathType Leaf)
    ) {
        return $false
    }
    Move-Item -LiteralPath $ArchivePath -Destination $ArtifactPath
    return $true
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
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [string]$ReleaseSha = "",
        [switch]$RequireCoreCoverage,
        [switch]$AllowCoreShortfall
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
    if (-not [string]::IsNullOrWhiteSpace($ReleaseSha)) {
        $expectedReleaseSha = $ReleaseSha.ToLowerInvariant()
        if ($expectedReleaseSha -cnotmatch '^[0-9a-f]{40}$') {
            throw "Scheduled AlphaOps release SHA is invalid."
        }
        if (
            [string]$payload.code_sha -cne $expectedReleaseSha -or
            [string]$contract.code_sha -cne $expectedReleaseSha -or
            [string]$payload.source_summary.code_sha -cne $expectedReleaseSha
        ) {
            throw "AlphaOps cycle artifact release identity does not match the scheduled runtime."
        }
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
    $coreFieldsPresent = $contract.PSObject.Properties.Name -contains "core_universe_status"
    $coreCoverageUnavailable = $coreFieldsPresent -and [string]$contract.core_universe_status -eq "DATA_UNAVAILABLE"
    $coreCoverageRequired = $RequireCoreCoverage -or ($coreFieldsPresent -and -not $coreCoverageUnavailable) -or ($coreCoverageUnavailable -and -not $AllowCoreShortfall)
    if ($coreCoverageRequired) {
        if ([string]$contract.core_universe_status -ne "READY") {
            throw "AlphaOps cycle artifact core universe is not READY; full core coverage is unavailable."
        }
        if ([string]$contract.core_universe_market_date -ne $MarketDate) {
            throw "AlphaOps cycle artifact core universe market date does not match the scheduled session."
        }
        $indexVerdicts = $contract.core_index_verdicts
        foreach ($indexName in @("S&P 500", "Nasdaq-100")) {
            $index = $indexVerdicts.PSObject.Properties[$indexName].Value
            if ($null -eq $index -or [string]$index.status -ne "READY") {
                throw "AlphaOps cycle artifact index verdict is not READY: $indexName"
            }
        }
        $rawHashes = @($contract.core_raw_artifact_hashes)
        if ($rawHashes.Count -lt 1) { throw "AlphaOps cycle artifact is missing raw artifact hashes." }
        foreach ($digest in $rawHashes) {
            if ([string]$digest -cnotmatch '^[0-9a-f]{64}$') { throw "AlphaOps cycle artifact has an invalid raw artifact hash." }
        }
        if ([string]$contract.core_member_set_hash_sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw "AlphaOps cycle artifact is missing the canonical core member-set hash."
        }
        $laneCounts = $contract.lane_counts
        foreach ($lane in @("mover", "core")) {
            $laneValue = $laneCounts.PSObject.Properties[$lane].Value
            if ($null -eq $laneValue) { throw "AlphaOps cycle artifact is missing lane counts: $lane" }
            foreach ($field in @("member_count", "snapshot_count", "eligible_count", "ranked_count")) {
                if (-not (Test-DawnstrikeNonNegativeInteger -Value $laneValue.PSObject.Properties[$field].Value)) {
                    throw "AlphaOps cycle artifact lane count is invalid: $lane.$field"
                }
            }
        }
        if ([string]$contract.slate_market_date -ne $MarketDate -or [string]$contract.slate_id -notmatch '^luna-slate-[0-9a-f]{24}$' -or [string]$contract.slate_content_hash_sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw "AlphaOps cycle artifact slate identity is missing or invalid."
        }
        if (-not (Test-DawnstrikeNonNegativeInteger -Value $contract.slate_published_count)) {
            throw "AlphaOps cycle artifact slate count is invalid."
        }
        if ([int64]$contract.slate_published_count -ne @($contract.slate_selection_ids).Count) {
            throw "AlphaOps cycle artifact slate count is inconsistent."
        }
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
    $selectionOutcome = [string]$contract.selection_outcome
    if (
        [string]::IsNullOrWhiteSpace($selectionOutcome) -or
        $selectionOutcome -notin @(
            "watchlist_ready",
            "valid_no_edge",
            "rehearsal_complete",
            "data_ineligible",
            "source_failed"
        )
    ) {
        throw "AlphaOps cycle artifact has an invalid selection_outcome."
    }
    return [pscustomobject]@{
        scan_id = $scanId
        signal_count = [int64]$contract.signal_count
        research_candidate_count = [int64]$contract.research_candidate_count
        research_symbols = @($researchSymbols)
        selection_outcome = $selectionOutcome
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
