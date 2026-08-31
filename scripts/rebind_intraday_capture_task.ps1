[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha = "",
    [Parameter(Mandatory = $true)][string]$SymbolsManifest,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$SymbolsManifestSha256,
    [Parameter(Mandatory = $true)][string]$EntitlementReceipt,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$EntitlementReceiptSha256,
    [Parameter(Mandatory = $true)][string]$SourceConfig,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$SourceConfigSha256,
    [string]$ReceiptPath = "",
    [switch]$Enable,
    [switch]$InjectFailureAfterMutation,
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
$captureRebindRuntimeRoot = $RuntimeRoot
$captureRebindStateRoot = $StateRoot
$captureRebindTimeout = $ProcessTimeoutSeconds
. (Join-Path $PSScriptRoot "activate_dawnstrike_runtime.ps1")
. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
$RuntimeRoot = $captureRebindRuntimeRoot
$StateRoot = $captureRebindStateRoot
$ProcessTimeoutSeconds = $captureRebindTimeout

function Get-DawnstrikeAuxiliarySectionHash {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Xml, [Parameter(Mandatory = $true)][string]$Name)
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $nodes = @($document.SelectNodes("//*[local-name()='$Name']"))
        if ($nodes.Count -ne 1) { throw "expected one $Name section" }
        return Get-DawnstrikeSha256Text ([string]$nodes[0].OuterXml)
    }
    catch { throw "Auxiliary task XML has an invalid $Name section." }
}

function Get-DawnstrikeNormalizedAuxiliaryXml {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][string]$OldSha,
        [Parameter(Mandatory = $true)][string]$NewSha
    )
    $value = $Xml.Replace($OldSha, "DAWNSTRIKE_CANDIDATE_SHA").Replace($NewSha, "DAWNSTRIKE_CANDIDATE_SHA")
    return $value
}

function Assert-DawnstrikeCaptureInput {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$SymbolsManifest,
        [Parameter(Mandatory = $true)][string]$SymbolsManifestSha256,
        [Parameter(Mandatory = $true)][string]$EntitlementReceipt,
        [Parameter(Mandatory = $true)][string]$EntitlementReceiptSha256,
        [Parameter(Mandatory = $true)][string]$SourceConfig,
        [Parameter(Mandatory = $true)][string]$SourceConfigSha256
    )
    foreach ($entry in @(
        @($SymbolsManifest, $SymbolsManifestSha256, "Symbols manifest"),
        @($EntitlementReceipt, $EntitlementReceiptSha256, "Entitlement receipt"),
        @($SourceConfig, $SourceConfigSha256, "Source config")
    )) {
        Assert-DawnstrikeNoReparseComponents $entry[0] $entry[2]
        $item = Get-Item -LiteralPath $entry[0] -Force -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$($entry[2]) must be a regular file."
        }
        if ((Get-DawnstrikeSha256File $entry[0]) -ne [string]$entry[1]) {
            throw "$($entry[2]) hash does not match the supplied identity."
        }
    }
    try { $manifest = Get-Content -LiteralPath $SymbolsManifest -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Symbols manifest is invalid JSON." }
    if (
        $null -eq $manifest -or
        [string]::IsNullOrWhiteSpace([string]$manifest.membership_policy) -or
        @($manifest.symbols).Count -lt 1 -or
        $manifest.point_in_time_membership -notin @($false, "research_control_only", "not_claimed")
    ) { throw "Symbols manifest violates the bounded capture contract." }
    try { $entitlement = Get-Content -LiteralPath $EntitlementReceipt -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Entitlement receipt is invalid JSON." }
    $provenEndpoints = @($entitlement.proven_endpoints | Where-Object { $_ -in @("bars", "trades", "quotes") })
    if (
        [string]$entitlement.provider -ne "alpaca" -or
        [string]$entitlement.feed -ne "sip" -or
        [string]$entitlement.probe_status -ne "PASS" -or
        $provenEndpoints.Count -lt 3 -or
        $entitlement.retention_allowed -ne $true -or
        $entitlement.approved_plan -ne $true -or
        $entitlement.research_only -ne $true -or
        [string]$entitlement.broker_execution -ne "disabled" -or
        [string]::IsNullOrWhiteSpace([string]$entitlement.entitlement) -or
        [string]::IsNullOrWhiteSpace($(if ($null -ne $entitlement.receipt) { [string]$entitlement.receipt } else { [string]$entitlement.proof_id }))
    ) { throw "Entitlement receipt does not prove the approved SIP research inputs." }
    return [pscustomobject]@{ symbols = $manifest; entitlement = $entitlement }
}

if (-not $Enable) { throw "Exact-SHA capture rebind requires explicit -Enable." }
$runtime = Resolve-DawnstrikeActivationRoot $RuntimeRoot "RuntimeRoot"
$state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
Assert-DawnstrikeRootIsolation $state @($runtime) "StateRoot"
$git = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0].Source
$python = @(Get-Command py.exe -CommandType Application -ErrorAction Stop)[0].Source
$runtimeContract = Get-DawnstrikeGitContract $git $runtime $ProcessTimeoutSeconds
if ([string]::IsNullOrWhiteSpace($CandidateSha)) { $CandidateSha = [string]$runtimeContract.head }
if ($runtimeContract.head -ne $CandidateSha) { throw "Runtime HEAD is not the requested exact candidate SHA." }
$origin = Get-DawnstrikeGitValue $git $runtime @("remote", "get-url", "origin") "Capture rebind origin verification" $ProcessTimeoutSeconds
Assert-DawnstrikeSafeOrigin $origin
$remoteMain = Get-DawnstrikeGitValue $git $runtime @("rev-parse", "refs/remotes/origin/main") "Capture rebind origin/main verification" $ProcessTimeoutSeconds
if ($remoteMain.ToLowerInvariant() -ne $CandidateSha) { throw "Runtime HEAD is not the exact origin/main SHA." }
$inputs = Assert-DawnstrikeCaptureInput `
    -SymbolsManifest $SymbolsManifest -SymbolsManifestSha256 $SymbolsManifestSha256 `
    -EntitlementReceipt $EntitlementReceipt -EntitlementReceiptSha256 $EntitlementReceiptSha256 `
    -SourceConfig $SourceConfig -SourceConfigSha256 $SourceConfigSha256

$activationReceiptRoot = Join-Path $state "receipts\runtime-activation"
Assert-DawnstrikeNoReparseComponents $activationReceiptRoot "Activation receipt root"
$activationReceipts = @()
foreach ($item in @(Get-ChildItem -LiteralPath $activationReceiptRoot -Filter "runtime-activation-*.json" -File -ErrorAction SilentlyContinue)) {
    try {
        Assert-DawnstrikeNoReparseComponents $item.FullName "Activation receipt"
        $candidateReceipt = Invoke-DawnstrikeActivationProcess $python @(
            (Join-Path $PSScriptRoot "runtime_activation_contract.py"),
            "verify-receipt", "--receipt", $item.FullName, "--expected-status", "COMPLETE"
        ) $PSScriptRoot "Capture rebind activation receipt verification" $ProcessTimeoutSeconds
        $parsedCandidateReceipt = [string]$candidateReceipt.Stdout | ConvertFrom-Json
        if (
            [string]$parsedCandidateReceipt.candidate_sha -eq $CandidateSha -and
            [string]$parsedCandidateReceipt.candidate_tree -eq [string]$runtimeContract.tree -and
            $parsedCandidateReceipt.auxiliary_capture_present -eq $true -and
            [string]$parsedCandidateReceipt.auxiliary_capture_state_after -eq "Disabled" -and
            [string]$parsedCandidateReceipt.auxiliary_capture_action -eq "DISABLED_UNTIL_EXACT_SHA_REBIND"
        ) {
            $activationReceipts += [pscustomobject]@{
                payload = $parsedCandidateReceipt
                path = $item.FullName
                name = $item.Name
                sha256 = Get-DawnstrikeSha256File $item.FullName
            }
        }
    }
    catch { }
}
if ($activationReceipts.Count -ne 1) {
    throw "Exact-SHA capture rebind requires one and only one matching COMPLETE activation receipt."
}
$activationReceipt = $activationReceipts[0]
$activationId = [string]$activationReceipt.payload.activation_id
$activationReceiptName = [string]$activationReceipt.name
$activationReceiptSha256 = [string]$activationReceipt.sha256

if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $CandidateSha + ".json")
}
$receiptRoot = [System.IO.Path]::GetFullPath((Join-Path $state "receipts\capture-task")).TrimEnd('\') + '\'
$receiptFull = [System.IO.Path]::GetFullPath($ReceiptPath)
Assert-DawnstrikeNoReparseComponents $receiptFull "Capture-task receipt"
if (-not $receiptFull.StartsWith($receiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Capture-task receipt must remain inside the durable capture-task receipt root."
}

$auxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
if (-not $auxiliary.present) { throw "Auxiliary capture task is absent; registration and rebind are separate governed actions." }
$captureContract = Join-Path $PSScriptRoot "capture_task_contract.py"
if (Test-Path -LiteralPath $receiptFull -PathType Leaf) {
    $existingReceipt = Invoke-DawnstrikeActivationProcess $python @($captureContract, "verify-receipt", "--receipt", $receiptFull, "--candidate-sha", $CandidateSha, "--candidate-tree", $runtimeContract.tree) $PSScriptRoot "Existing capture-task receipt verification" $ProcessTimeoutSeconds
    try { $existingPayload = [string]$existingReceipt.Stdout | ConvertFrom-Json }
    catch { throw "Existing capture-task receipt verification did not return valid JSON." }
    if (
        [string]$existingPayload.activation_id -eq $activationId -and
        [string]$existingPayload.activation_receipt_name -eq $activationReceiptName -and
        [string]$existingPayload.activation_receipt_sha256 -eq $activationReceiptSha256 -and
        $auxiliary.state -eq "Ready" -and
        $auxiliary.action_contract_sha256 -eq [string]$existingPayload.action_after_sha256 -and
        (Get-DawnstrikeSha256Text ([string]$auxiliary.xml)) -eq [string]$existingPayload.xml_after_sha256
    ) {
        Write-Output ([string]$existingReceipt.Stdout).Trim()
        return
    }
    throw "Existing capture-task receipt does not match the current task; rebind is ambiguous."
}
if ($auxiliary.state -ne "Disabled") { throw "Auxiliary capture task must be Disabled before exact-SHA rebind." }
$rebindLock = Enter-DawnstrikeRuntimeActivationLock $state
try {
$lockedAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
if (
    -not $lockedAuxiliary.present -or
    $lockedAuxiliary.state -ne $auxiliary.state -or
    $lockedAuxiliary.xml_sha256 -ne $auxiliary.xml_sha256 -or
    $lockedAuxiliary.action_contract_sha256 -ne $auxiliary.action_contract_sha256 -or
    $lockedAuxiliary.definition_contract_sha256 -ne $auxiliary.definition_contract_sha256
) { throw "Auxiliary capture task changed while acquiring the rebind lock." }
$auxiliary = $lockedAuxiliary
$task = @(Get-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -ErrorAction Stop)
if ($task.Count -ne 1) { throw "Auxiliary capture task name is not unique." }
$actions = @($task[0].Actions)
$shaMatches = @()
foreach ($action in $actions) {
    $shaMatches += [regex]::Matches([string]$action.Arguments, '(?i)(?<![A-Za-z0-9_-])--candidate-sha(?:=|\s+)(?:"([0-9a-f]{40})"|([0-9a-f]{40}))')
}
if ($shaMatches.Count -ne 1) { throw "Auxiliary capture action candidate SHA pin is missing or ambiguous." }
$shaMatch = $shaMatches[0]
$previousSha = if ($shaMatch.Groups[1].Success) { [string]$shaMatch.Groups[1].Value } else { [string]$shaMatch.Groups[2].Value }
$xmlBefore = [string]$auxiliary.xml
$oldShaOccurrences = @([regex]::Matches($xmlBefore, [regex]::Escape($previousSha))).Count
if ($oldShaOccurrences -ne 1) {
    throw "Auxiliary capture XML must contain exactly one candidate SHA pin."
}
$principalHashBefore = Get-DawnstrikeAuxiliarySectionHash $xmlBefore "Principal"
$triggerHashBefore = Get-DawnstrikeAuxiliarySectionHash $xmlBefore "Triggers"
$settingsHashBefore = Get-DawnstrikeAuxiliarySectionHash $xmlBefore "Settings"
$actionHashBefore = [string]$auxiliary.action_contract_sha256
$definitionHashBefore = [string]$auxiliary.definition_contract_sha256
$preparedPath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $CandidateSha + ".prepared.json")
$failurePath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $CandidateSha + ".failed.json")
Assert-DawnstrikeNoReparseComponents $preparedPath "Capture-task prepared record"
Assert-DawnstrikeNoReparseComponents $failurePath "Capture-task failure record"
if (Test-Path -LiteralPath $preparedPath -PathType Leaf) {
    if (-not (Test-Path -LiteralPath $failurePath -PathType Leaf)) {
        throw "A previous capture-task rebind is PREPARED and requires recovery review."
    }
    try {
        $failedPayload = Get-Content -LiteralPath $failurePath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (
            [string]$failedPayload.status -ne "FAILED_RESTORED_EXACT_DISABLED" -or
            $auxiliary.state -ne "Disabled" -or
            [string]$failedPayload.original_xml_sha256 -ne [string]$auxiliary.xml_sha256 -or
            [string]$failedPayload.original_action_sha256 -ne [string]$auxiliary.action_contract_sha256
        ) { throw "Previous capture-task failure evidence does not prove the exact disabled boundary." }
        Remove-Item -LiteralPath $preparedPath -Force -ErrorAction Stop
    }
    catch {
        throw "A previous capture-task PREPARED record requires recovery review."
    }
}
$preparedPayload = [ordered]@{
    schema_version = "dawnstrike.capture_task_rebind_prepared.v1"
    status = "PREPARED"
    task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
    candidate_sha = $CandidateSha
    candidate_tree = [string]$runtimeContract.tree
    activation_id = $activationId
    activation_receipt_name = $activationReceiptName
    activation_receipt_sha256 = $activationReceiptSha256
    previous_candidate_sha = $previousSha
    xml_before_sha256 = Get-DawnstrikeSha256Text $xmlBefore
    action_before_sha256 = $actionHashBefore
    definition_before_sha256 = $definitionHashBefore
    principal_sha256 = $principalHashBefore
    trigger_sha256 = $triggerHashBefore
    settings_sha256 = $settingsHashBefore
    enablement_before = "Disabled"
    compensation = "RESTORE_EXACT_XML_AND_DISABLED"
    research_only = $true
    broker_execution_enabled = $false
}
Write-DawnstrikeActivationJson $preparedPayload $preparedPath

$newActions = @()
foreach ($action in $actions) {
    $arguments = [string]$action.Arguments
    $arguments = [regex]::Replace(
        $arguments,
        '(?i)(?<![A-Za-z0-9_-])(--candidate-sha(?:=|\s+))("?)[0-9a-f]{40}("?)',
        { param($match) $match.Groups[1].Value + $match.Groups[2].Value + $CandidateSha + $match.Groups[3].Value }
    )
    $newActions += New-ScheduledTaskAction -Execute ([string]$action.Execute) -Argument $arguments -WorkingDirectory ([string]$action.WorkingDirectory)
}
try {
    Set-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath ([string]$auxiliary.task_path) -Action $newActions -ErrorAction Stop | Out-Null
    $boundDisabled = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if ($boundDisabled.state -ne "Disabled") { throw "Capture task became enabled before rebind verification." }
    if ($boundDisabled.action_contract_sha256 -eq $actionHashBefore) { throw "Capture-task action SHA was not changed." }
    $xmlBound = [string]$boundDisabled.xml
    $newShaOccurrences = @([regex]::Matches($xmlBound, [regex]::Escape($CandidateSha))).Count
    if ($newShaOccurrences -ne 1) {
        throw "Auxiliary capture XML must contain exactly one replacement candidate SHA pin."
    }
    if ((Get-DawnstrikeNormalizedAuxiliaryXml $xmlBound $previousSha $CandidateSha) -ne (Get-DawnstrikeNormalizedAuxiliaryXml $xmlBefore $previousSha $CandidateSha)) {
        throw "Capture-task XML changed outside the candidate SHA field."
    }
    if (
        (Get-DawnstrikeAuxiliarySectionHash $xmlBound "Principal") -ne $principalHashBefore -or
        (Get-DawnstrikeAuxiliarySectionHash $xmlBound "Triggers") -ne $triggerHashBefore -or
        (Get-DawnstrikeAuxiliarySectionHash $xmlBound "Settings") -ne $settingsHashBefore
    ) { throw "Capture-task principal, triggers, or settings changed during rebind." }
    if ($InjectFailureAfterMutation) { throw "Injected capture-task post-mutation failure." }
    Enable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath ([string]$boundDisabled.task_path) -ErrorAction Stop | Out-Null
    $final = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if ($final.state -ne "Ready") { throw "Capture task did not become exactly Ready after rebind." }
    if (-not ([string]$final.xml).ToLowerInvariant().Contains($CandidateSha)) { throw "Capture-task XML does not contain the exact runtime SHA pin." }
    $payload = [ordered]@{
    schema_version = "dawnstrike.capture_task_rebind_receipt.v1"
    status = "COMPLETE"
    task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
    candidate_sha = $CandidateSha
    candidate_tree = [string]$runtimeContract.tree
    activation_id = $activationId
    activation_receipt_name = $activationReceiptName
    activation_receipt_sha256 = $activationReceiptSha256
    runtime_origin_sha256 = Get-DawnstrikeSha256Text $origin
    previous_candidate_sha = $previousSha
    xml_before_sha256 = Get-DawnstrikeSha256Text $xmlBefore
    xml_after_sha256 = Get-DawnstrikeSha256Text ([string]$final.xml)
    action_before_sha256 = $actionHashBefore
    action_after_sha256 = [string]$final.action_contract_sha256
    definition_before_sha256 = $definitionHashBefore
    definition_after_sha256 = [string]$final.definition_contract_sha256
    principal_sha256 = $principalHashBefore
    trigger_sha256 = $triggerHashBefore
    settings_sha256 = $settingsHashBefore
    symbols_manifest_sha256 = $SymbolsManifestSha256
    entitlement_receipt_sha256 = $EntitlementReceiptSha256
    source_config_sha256 = $SourceConfigSha256
    enablement_before = "Disabled"
    enablement_after = "Ready"
    changed_field = "candidate_sha"
    preserved_contract = $true
    completed_at_utc = [DateTime]::UtcNow.ToString("o")
    research_only = $true
    broker_execution_enabled = $false
    }
    $inputReceipt = "$receiptFull.$([guid]::NewGuid().ToString('N')).input.json"
    Assert-DawnstrikeNoReparseComponents $inputReceipt "Capture-task input receipt"
    Write-DawnstrikeActivationJson $payload $inputReceipt
    $receiptSealed = $false
    $result = Invoke-DawnstrikeActivationProcess $python @($captureContract, "seal-receipt", "--input", $inputReceipt, "--output", $receiptFull) $PSScriptRoot "Capture-task rebind receipt sealing" $ProcessTimeoutSeconds
    $receiptSealed = $true
    [string]$result.Stdout | ConvertFrom-Json | ConvertTo-Json -Depth 8 -Compress
}
catch {
    $failure = $_
    if ($receiptSealed) {
        throw "Capture-task rebind receipt is COMPLETE but prepared-record cleanup failed; operator recovery is required."
    }
    try {
        $null = Restore-DawnstrikeAuxiliaryCaptureTask -Expected $auxiliary -RuntimeRoot $runtime -StateRoot $state
        $restored = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
        if (
            $restored.state -ne "Disabled" -or
            [string]$restored.xml_sha256 -ne [string]$auxiliary.xml_sha256 -or
            [string]$restored.definition_contract_sha256 -ne [string]$auxiliary.definition_contract_sha256 -or
            [string]$restored.action_contract_sha256 -ne [string]$auxiliary.action_contract_sha256
        ) { throw "Capture-task compensation did not restore the exact disabled task." }
        $failureEvidence = [ordered]@{
            schema_version = "dawnstrike.capture_task_rebind_failure.v1"
            status = "FAILED_RESTORED_EXACT_DISABLED"
            task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
            candidate_sha = $CandidateSha
            candidate_tree = [string]$runtimeContract.tree
            activation_id = $activationId
            activation_receipt_name = $activationReceiptName
            activation_receipt_sha256 = $activationReceiptSha256
            original_xml_sha256 = [string]$auxiliary.xml_sha256
            original_action_sha256 = [string]$auxiliary.action_contract_sha256
            recovery_evidence = "EXACT_XML_RESTORED_AND_DISABLED"
            error_type = $failure.Exception.GetType().Name
            research_only = $true
            broker_execution_enabled = $false
        }
        Write-DawnstrikeActivationJson $failureEvidence $failurePath
    }
    catch {
        throw "Capture-task rebind failed and exact disabled XML compensation could not be proven; operator recovery is required."
    }
    throw "Capture-task rebind failed; exact disabled XML compensation was proven."
}
finally {
    if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) { Remove-Item -LiteralPath $inputReceipt -Force }
}
try {
    Remove-Item -LiteralPath $preparedPath -Force -ErrorAction Stop
}
catch {
    throw "Capture-task rebind receipt is COMPLETE but its PREPARED record could not be removed; operator recovery is required."
}
}
finally {
    Exit-DawnstrikeRuntimeActivationLock $rebindLock
    if (Test-Path -LiteralPath $rebindLock.path -PathType Leaf) {
        throw "Capture-task rebind lock could not be released; operator recovery is required."
    }
}
