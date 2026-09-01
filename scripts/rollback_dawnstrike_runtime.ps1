[CmdletBinding()]
param(
    [string]$ActivationReceipt = "",
    [string]$ContractRoot = "",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "C:\r\dawnstrike-state-backups",
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300,
    [pscredential]$RunAsCredential
)

$ErrorActionPreference = "Stop"
if (
    [int]$PSVersionTable.PSVersion.Major -lt 5 -or
    [string]$PSVersionTable.PSEdition -ne "Desktop"
) {
    throw "Dawnstrike rollback requires Windows PowerShell 5.1 or later (Desktop edition)."
}
$rollbackRuntimeRoot = $RuntimeRoot
$rollbackStateRoot = $StateRoot
$rollbackBackupRoot = $BackupRoot
$rollbackTimeout = $ProcessTimeoutSeconds
$rollbackRunAsCredential = $RunAsCredential
. (Join-Path $PSScriptRoot "activate_dawnstrike_runtime.ps1")
$RuntimeRoot = $rollbackRuntimeRoot
$StateRoot = $rollbackStateRoot
$BackupRoot = $rollbackBackupRoot
$RunAsCredential = $rollbackRunAsCredential
$ProcessTimeoutSeconds = $rollbackTimeout

function Get-DawnstrikeActivationAuxiliaryRecoveryContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )

    if (-not [bool]$Activation.auxiliary_capture_present) {
        return [pscustomobject]@{
            present = $false
            task_path = "NONE"
            xml = ""
            xml_sha256 = Get-DawnstrikeSha256Text ""
            enabled = $false
        }
    }
    $backupName = [string]$Activation.scheduler_backup_name
    $manifestPath = Join-Path $StateRoot "scheduler-backups\$backupName\manifest.json"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Activation scheduler backup manifest"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Activation auxiliary capture XML backup is missing."
    }
    if ((Get-DawnstrikeSha256File $manifestPath) -ne [string]$Activation.scheduler_backup_manifest_sha256) {
        throw "Activation auxiliary capture XML backup manifest hash does not match the receipt."
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Activation auxiliary capture XML backup manifest is invalid JSON."
    }
    $auxiliary = $manifest.auxiliary_capture
    if ($null -eq $auxiliary -or $auxiliary.present -ne $true) {
        throw "Activation auxiliary capture XML backup does not attest the governed task."
    }
    $xmlPath = Join-Path (Split-Path -Parent $manifestPath) ([string]$auxiliary.file_name)
    Assert-DawnstrikeNoReparseComponents $xmlPath "Activation auxiliary XML backup"
    if (-not (Test-Path -LiteralPath $xmlPath -PathType Leaf)) {
        throw "Activation auxiliary capture XML file is missing."
    }
    $xml = [System.IO.File]::ReadAllText($xmlPath)
    Assert-DawnstrikeNoReparseComponents $xmlPath "Activation auxiliary XML backup"
    if ((Get-DawnstrikeSha256File $xmlPath) -ne [string]$auxiliary.xml_file_sha256) {
        throw "Activation auxiliary capture XML backup changed during read."
    }
    if (
        (Get-DawnstrikeSha256File $xmlPath) -ne [string]$auxiliary.xml_file_sha256 -or
        (Get-DawnstrikeSha256Text $xml) -ne [string]$auxiliary.xml_sha256 -or
        [string]$auxiliary.xml_sha256 -ne [string]$Activation.auxiliary_capture_xml_sha256 -or
        [string]$auxiliary.xml_file_sha256 -ne [string]$Activation.auxiliary_capture_xml_file_sha256
    ) {
        throw "Activation auxiliary capture XML backup does not match the receipt."
    }
    return [pscustomobject]@{
        present = $true
        task_path = [string]$auxiliary.task_path
        xml = $xml
        xml_sha256 = [string]$auxiliary.xml_sha256
        xml_file_sha256 = [string]$auxiliary.xml_file_sha256
        definition_contract_sha256 = [string]$auxiliary.definition_contract_sha256
        action_contract_sha256 = [string]$auxiliary.action_contract_sha256
        enabled = ([string]$Activation.auxiliary_capture_state_before -eq "Ready")
    }
}

function Assert-DawnstrikeCapturePreparedRecovery {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][object]$Auxiliary,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PythonPath
    )

    if (-not $Auxiliary.present -or $Auxiliary.state -ne "Ready") {
        throw "Prepared capture-task recovery requires the current auxiliary task to be Ready."
    }
    $receiptRoot = Join-Path $StateRoot "receipts\capture-task"
    Assert-DawnstrikeNoReparseComponents $receiptRoot "Capture-task receipt root"
    $preparedPath = Join-Path $receiptRoot ("capture-task-rebind-" + $CandidateSha + ".prepared.json")
    Assert-DawnstrikeNoReparseComponents $preparedPath "Capture-task prepared recovery record"
    if (-not (Test-Path -LiteralPath $preparedPath -PathType Leaf)) {
        throw "Ready auxiliary capture task has no COMPLETE or PREPARED recovery chain."
    }
    $captureContract = Join-Path $PSScriptRoot "capture_task_contract.py"
    $activationPath = Join-Path $StateRoot ("receipts\runtime-activation\runtime-activation-" + [string]$Activation.activation_id + ".json")
    Assert-DawnstrikeNoReparseComponents $activationPath "Prepared recovery activation receipt"
    if (-not (Test-Path -LiteralPath $activationPath -PathType Leaf)) {
        throw "Capture-task PREPARED recovery activation receipt is missing."
    }
    $activationItem = Get-Item -LiteralPath $activationPath -Force -ErrorAction Stop
    $hashFields = @(
        "activation_receipt_sha256", "xml_before_sha256", "action_before_sha256",
        "definition_before_sha256", "normalized_definition_before_sha256",
        "principal_sha256", "trigger_sha256", "settings_sha256",
        "symbols_manifest_sha256", "entitlement_receipt_sha256", "source_config_sha256"
    )
    try {
        $preparedResult = Invoke-DawnstrikeActivationProcess $PythonPath @(
            $captureContract, "verify-prepared", "--prepared", $preparedPath,
            "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
        ) $PSScriptRoot "Capture-task PREPARED recovery verification" $ProcessTimeoutSeconds
        $prepared = [string]$preparedResult.Stdout | ConvertFrom-Json
    }
    catch {
        throw "Capture-task PREPARED recovery record is not a strict self-hashed contract."
    }
    foreach ($field in $hashFields) {
        if ([string]$prepared.$field -notmatch '^[0-9a-f]{64}$') {
            throw "Capture-task PREPARED recovery hash is invalid: $field"
        }
    }
    if (
        [string]$prepared.schema_version -ne "dawnstrike.capture_task_rebind_prepared.v1" -or
        [string]$prepared.status -ne "PREPARED" -or
        [string]$prepared.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName -or
        [string]$prepared.candidate_sha -ne $CandidateSha -or
        [string]$prepared.candidate_tree -ne $CandidateTree -or
        [string]$Activation.candidate_sha -ne $CandidateSha -or
        [string]$Activation.candidate_tree -ne $CandidateTree -or
        [string]$prepared.activation_id -ne [string]$Activation.activation_id -or
        [string]$prepared.activation_receipt_name -ne [string]$activationItem.Name -or
        [string]$prepared.activation_receipt_sha256 -ne (Get-DawnstrikeSha256File $activationItem) -or
        [string]$prepared.xml_before_sha256 -ne [string]$Activation.auxiliary_capture_xml_sha256 -or
        [string]$prepared.action_before_sha256 -ne [string]$Activation.auxiliary_capture_action_contract_sha256 -or
        [string]$prepared.definition_before_sha256 -ne [string]$Activation.auxiliary_capture_definition_contract_sha256 -or
        [string]$prepared.enablement_before -ne "Disabled" -or
        [string]$prepared.compensation -ne "RESTORE_EXACT_XML_AND_DISABLED" -or
        $prepared.research_only -ne $true -or
        $prepared.broker_execution_enabled -ne $false
    ) {
        throw "Capture-task PREPARED recovery record does not bind to the activation receipt."
    }
    if ([string]$prepared.previous_candidate_sha -notmatch '^[0-9a-f]{40}$') {
        throw "Capture-task PREPARED previous candidate SHA is invalid."
    }
    $original = Get-DawnstrikeActivationAuxiliaryRecoveryContract -Activation $Activation -StateRoot $StateRoot
    if (
        [string]$prepared.xml_before_sha256 -ne [string]$original.xml_sha256 -or
        [string]$prepared.action_before_sha256 -ne [string]$original.action_contract_sha256 -or
        [string]$prepared.definition_before_sha256 -ne [string]$original.definition_contract_sha256
    ) { throw "Capture-task PREPARED original XML is not bound to the activation backup." }
    foreach ($input in @(
        @("symbols_manifest_path", "symbols_manifest_sha256"),
        @("entitlement_receipt_path", "entitlement_receipt_sha256"),
        @("source_config_path", "source_config_sha256")
    )) {
        $inputPath = [string]$prepared.($input[0])
        Assert-DawnstrikeNoReparseComponents $inputPath "Capture-task PREPARED input"
        $inputItem = Get-Item -LiteralPath $inputPath -Force -ErrorAction Stop
        if ($inputItem.PSIsContainer -or ($inputItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Capture-task PREPARED input is not a regular file."
        }
        if ((Get-DawnstrikeSha256File $inputPath) -ne [string]$prepared.($input[1])) {
            throw "Capture-task PREPARED input hash does not match the supplied file."
        }
    }
    $candidateOccurrences = @([regex]::Matches([string]$Auxiliary.xml, [regex]::Escape($CandidateSha))).Count
    if (
        $candidateOccurrences -ne 1 -or
        [string]$Auxiliary.action_contract_sha256 -eq [string]$prepared.action_before_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Principal") -ne [string]$prepared.principal_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Triggers") -ne [string]$prepared.trigger_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Settings") -ne [string]$prepared.settings_sha256
    ) {
        throw "Ready auxiliary task does not prove the exact post-mutation PREPARED boundary."
    }
    return $prepared
}

function Archive-DawnstrikeTerminalRollbackAttemptBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][string]$JournalSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [switch]$AllowMissing
    )

    if ($JournalSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Terminal rollback attempt journal hash is invalid."
    }
    if ($ExpectedManifestSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Terminal rollback attempt backup hash is invalid."
    }
    if ($ActivationId -notmatch '^[0-9a-f]{24}$') {
        throw "Terminal rollback attempt activation id is invalid."
    }
    $backupRoot = Join-Path $StateRoot ("scheduler-backups\runtime-rollback-" + $ActivationId)
    $manifestPath = Join-Path $backupRoot "manifest.json"
    $archiveRoot = Join-Path $StateRoot "scheduler-backups\archive"
    $archiveRootPath = Join-Path $archiveRoot ("runtime-rollback-" + $ActivationId + "-" + $JournalSha256)
    $archiveManifestPath = Join-Path $archiveRootPath "manifest.json"
    Assert-DawnstrikeNoReparseComponents $backupRoot "Terminal rollback attempt backup"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Terminal rollback attempt backup manifest"
    Assert-DawnstrikeNoReparseComponents $archiveRoot "Terminal rollback backup archive root"
    Assert-DawnstrikeNoReparseComponents $archiveRootPath "Terminal rollback backup archive"
    Assert-DawnstrikeNoReparseComponents $archiveManifestPath "Terminal rollback backup archive manifest"

    $sourceExists = Test-Path -LiteralPath $backupRoot -PathType Container
    $archiveExists = Test-Path -LiteralPath $archiveRootPath -PathType Container
    if (-not $sourceExists -and -not $archiveExists) {
        if ($AllowMissing) {
            return [pscustomobject]@{
                path = $null
                manifest_path = $null
                manifest_sha256 = $ExpectedManifestSha256
                archived = $false
            }
        }
        throw "Terminal rollback attempt backup is missing before compensation tombstone cleanup."
    }
    if ($sourceExists) {
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            throw "Terminal rollback attempt backup manifest is missing."
        }
        if ((Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256) {
            throw "Terminal rollback attempt backup manifest changed before archival."
        }
        if ($archiveExists) {
            throw "Terminal rollback attempt backup has both live and archived copies."
        }
        New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
        [IO.Directory]::Move($backupRoot, $archiveRootPath)
        if (Test-Path -LiteralPath $backupRoot) {
            throw "Terminal rollback attempt backup remained after archival."
        }
    }
    if (-not (Test-Path -LiteralPath $archiveManifestPath -PathType Leaf)) {
        throw "Terminal rollback attempt backup archive manifest is missing."
    }
    if ((Get-DawnstrikeSha256File $archiveManifestPath) -ne $ExpectedManifestSha256) {
        throw "Terminal rollback attempt backup archive manifest was not proven."
    }
    return [pscustomobject]@{
        path = $archiveRootPath
        manifest_path = $archiveManifestPath
        manifest_sha256 = $ExpectedManifestSha256
        archived = $sourceExists
    }
}

function Assert-DawnstrikeRollbackCompleteTerminal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][object]$Activation,
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PreviousSha,
        [Parameter(Mandatory = $true)][string]$PreviousTree,
        [Parameter(Mandatory = $true)][string]$OriginIdentity,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][object]$StateDeclaration
    )

    if ([string]$Journal.payload.operation -ne "runtime_rollback" -or
        [string]$Journal.payload.phase -ne "COMPLETE" -or
        [string]$Journal.payload.candidate_sha -ne $CandidateSha -or
        [string]$Journal.payload.candidate_tree -ne $CandidateTree -or
        [string]$Journal.payload.current_sha -ne $PreviousSha -or
        [string]$Journal.payload.current_tree -ne $PreviousTree -or
        [string]$Journal.payload.origin_identity -ne $OriginIdentity) {
        throw "Complete rollback journal identity is not exact."
    }
    Assert-DawnstrikeNoReparseComponents $ReceiptPath "Complete rollback receipt"
    if (-not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
        throw "Complete rollback journal has no exact complete receipt."
    }
    $statePrefix = [IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'
    $receiptFull = [IO.Path]::GetFullPath($ReceiptPath)
    if (-not $receiptFull.StartsWith($statePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Complete rollback receipt escaped StateRoot."
    }
    $receiptRelative = ($receiptFull.Substring($statePrefix.Length) -replace '\\','/')
    if ([string]$Journal.payload.complete_receipt_relative_path -cne $receiptRelative -or
        [string]$Journal.payload.complete_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Journal.payload.complete_receipt_sha256 -cne (Get-DawnstrikeSha256File $ReceiptPath)) {
        throw "Complete rollback journal is not bound to the exact receipt bytes."
    }
    $verified = Invoke-DawnstrikeContractCli $PythonPath $CandidateRoot `
        -Arguments @("verify-receipt", "--receipt", $ReceiptPath, "--expected-status", "ROLLED_BACK") `
        -Label "Complete rollback receipt terminal validation" -TimeoutSeconds $TimeoutSeconds
    if ([string]$verified.candidate_sha -ne $CandidateSha -or
        [string]$verified.candidate_tree -ne $CandidateTree -or
        [string]$verified.previous_sha -ne $PreviousSha -or
        [string]$verified.previous_tree -ne $PreviousTree -or
        [string]$verified.market_date -ne $MarketDate) {
        throw "Complete rollback receipt is not bound to the exact terminal identity."
    }
    $live = Get-DawnstrikeGitContract $GitPath $RuntimeRoot $TimeoutSeconds $PreviousSha
    if ($live.tree -ne $PreviousTree) { throw "Complete rollback runtime HEAD/tree is not exact." }
    $liveOrigin = Get-DawnstrikeGitValue $GitPath $RuntimeRoot @("remote", "get-url", "origin") `
        "Complete rollback runtime origin validation" $TimeoutSeconds
    Assert-DawnstrikeSafeOrigin $liveOrigin
    $receiptOriginHash = [string]$verified.runtime_origin_sha256
    if ($receiptOriginHash -notmatch '^[0-9a-f]{64}$' -or
        (Convert-DawnstrikeCanonicalOriginIdentity $liveOrigin) -ne $OriginIdentity -or
        (Get-DawnstrikeSha256Text $liveOrigin) -ne $receiptOriginHash -or
        [string]$verified.runtime_origin_sha256 -ne [string]$Activation.runtime_origin_sha256) {
        throw "Complete rollback runtime origin is not receipt/journal-bound."
    }
    $tasks = Get-DawnstrikeTaskContract $RuntimeRoot $StateRoot
    $null = Assert-DawnstrikeCanonicalTaskSemantics `
        -RuntimeRoot $RuntimeRoot -StateRoot $StateRoot -ExpectedSha $PreviousSha
    if ([string]$tasks.task_contract_sha256 -ne [string]$verified.task_contract_sha256 -or
        [string]$tasks.task_definition_contract_sha256 -ne [string]$verified.task_definition_contract_sha256 -or
        [string]$tasks.task_action_contract_sha256 -ne [string]$verified.task_action_contract_sha256 -or
        [int]$tasks.enabled_count -ne 5 -or [int]$tasks.disabled_count -ne 0) {
        throw "Complete rollback canonical task contract is not exact."
    }
    # The scheduler backup captures the candidate-bound task contract before
    # rollback.  The terminal receipt carries the *restored* previous-SHA
    # contract, so never compare those two different identities directly.
    # Archive/move is compensation-only; consuming this directory here would
    # make the validator unable to prove the same receipt on a crash/retry.
    $backupManifest = Get-DawnstrikeTaskXmlBackupManifest `
        -StateRoot $StateRoot -BackupName ([string]$verified.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$verified.scheduler_backup_manifest_sha256)
    $null = Assert-DawnstrikeTaskXmlBackup `
        -StateRoot $StateRoot -BackupName ([string]$verified.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$verified.scheduler_backup_manifest_sha256) `
        -ExpectedTaskContractSha256 ([string]$backupManifest.task_contract_sha256) `
        -ExpectedTaskDefinitionContractSha256 ([string]$backupManifest.task_definition_contract_sha256) `
        -ExpectedTaskActionContractSha256 ([string]$backupManifest.task_action_contract_sha256)
    $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
        -Receipt $Activation -StateRoot $StateRoot -BackupRoot $BackupRoot `
        -ToolRoot $CandidateRoot -GitPath $GitPath -PythonPath $PythonPath `
        -TimeoutSeconds $TimeoutSeconds
    if ($StateDeclaration.required -and $Activation.PSObject.Properties.Name -contains "state_preparation_receipt_sha256") {
        $proof = Get-DawnstrikeStatePreparationProof `
            -CandidateRoot $CandidateRoot -StateRoot $StateRoot -BackupRoot $BackupRoot `
            -CandidateSha $CandidateSha -CandidateTree $CandidateTree `
            -PythonPath $PythonPath -TimeoutSeconds $TimeoutSeconds
        if ([string]$verified.state_preparation_receipt_sha256 -ne [string]$proof.receipt_sha256 -or
            [string]$verified.state_preparation_after_db_sha256 -ne [string]$proof.after_db_sha256 -or
            [string]$verified.state_preparation_inventory_sha256 -ne [string]$proof.inventory_sha256) {
            throw "Complete rollback state-preparation lineage is not exact."
        }
    }
    return $verified
}

function Invoke-DawnstrikeRuntimeRollback {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ActivationReceipt,
        [Parameter(Mandatory = $true)][string]$ContractRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][int]$ProcessTimeoutSeconds,
        [pscredential]$RunAsCredential
    )

    $contract = Resolve-DawnstrikeActivationRoot $ContractRoot "ContractRoot"
    $state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
    $safeBackupRoot = Resolve-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    $runtime = Get-DawnstrikeFutureActivationRoot $RuntimeRoot "RuntimeRoot"
    Assert-DawnstrikeRootIsolation $safeBackupRoot @($contract, $runtime, $state) "BackupRoot"
    Assert-DawnstrikeNoReparseComponents $ActivationReceipt "Activation receipt"
    $receiptPath = [System.IO.Path]::GetFullPath($ActivationReceipt)
    $approvedReceiptRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $state "receipts\runtime-activation")
    ).TrimEnd('\') + '\'
    if (-not $receiptPath.StartsWith($approvedReceiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Activation receipt must be inside the durable activation receipt root."
    }
    Assert-DawnstrikeNoReparseComponents $receiptPath "Activation receipt"
    $receiptItem = Get-Item -LiteralPath $receiptPath -Force
    if (
        $receiptItem.PSIsContainer -or
        ($receiptItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Activation receipt cannot be a reparse point."
    }
    Assert-DawnstrikeNoReparseComponents $receiptPath "Activation receipt"

    $gitPath = (Get-DawnstrikeApprovedGit).path
    $pythonPath = (Get-DawnstrikeApprovedLockInterpreter).path
    $toolRoot = Resolve-DawnstrikeActivationRoot (Join-Path $PSScriptRoot "..") "ToolRoot"
    if (-not [string]::Equals(
        $contract,
        $toolRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "ContractRoot must be the exact checkout containing the rollback tool."
    }
    . (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")

    try {
        $receiptHint = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Activation receipt is not valid JSON."
    }
    if (
        [string]$receiptHint.schema_version -notin @("dawnstrike.runtime_activation_receipt.v1", "dawnstrike.runtime_activation_receipt.v2") -or
        [string]$receiptHint.candidate_sha -notmatch '^[0-9a-f]{40}$' -or
        [string]$receiptHint.candidate_tree -notmatch '^[0-9a-f]{40}$'
    ) {
        throw "Activation receipt cannot identify an exact candidate checkout."
    }
    $contractGit = Get-DawnstrikeGitContract `
        $gitPath `
        $contract `
        $ProcessTimeoutSeconds `
        ([string]$receiptHint.candidate_sha)
    if ($contractGit.tree -ne [string]$receiptHint.candidate_tree) {
        throw "ContractRoot tree does not match the activation receipt candidate."
    }
    $null = Assert-DawnstrikeHelpersBoundToHead `
        -GitPath $gitPath -Root $contract -TimeoutSeconds $ProcessTimeoutSeconds
    . (Join-Path $PSScriptRoot "capture_task_safety.ps1")
    . (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")

    $activation = Invoke-DawnstrikeContractCli `
        -PythonPath $pythonPath `
        -CandidateRoot $contract `
        -Arguments @("verify-receipt", "--receipt", $receiptPath) `
        -Label "Activation receipt verification" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    if ($activation.schema_version -notin @("dawnstrike.runtime_activation_receipt.v1", "dawnstrike.runtime_activation_receipt.v2")) {
        throw "Rollback requires an activation receipt."
    }
    if (
        $contractGit.head -ne [string]$activation.candidate_sha -or
        $contractGit.tree -ne [string]$activation.candidate_tree
    ) {
        throw "Validated ContractRoot does not match the sealed activation receipt."
    }
    $activationId = [string]$activation.activation_id
    $candidateSha = [string]$activation.candidate_sha
    $previousSha = [string]$activation.previous_sha
    $previousTree = [string]$activation.previous_tree
    $marketDate = [string]$activation.market_date
    $rollbackRoot = Join-Path $state "runtime-rollbacks\$activationId"
    $rollbackCheckout = Join-Path $rollbackRoot "previous-runtime"
    $rollbackBundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    $rollbackStage = "$runtime.rollback-stage-$activationId"
    $deactivatedCandidate = Join-Path $rollbackRoot "deactivated-candidate-runtime"
    $rollbackReceiptRoot = Join-Path $state "receipts\runtime-rollback"
    $rollbackReceipt = Join-Path $rollbackReceiptRoot "runtime-rollback-$activationId.json"
    $rollbackReadyReceipt = Join-Path $rollbackReceiptRoot "runtime-rollback-$activationId.ready.json"
    $rollbackSchedulerBackupName = "runtime-rollback-$activationId"
    $rollbackSchedulerBackupPath = Join-Path $state "scheduler-backups\$rollbackSchedulerBackupName"
    $operationJournalPath = Join-Path $state "receipts\runtime-operation\runtime-rollback-$activationId.json"
    $journalPreparedRelativePath = "receipts/runtime-activation/runtime-activation-$activationId.json"
    $journalCompleteRelativePath = "receipts/runtime-rollback/runtime-rollback-$activationId.json"
    $journalReadyRelativePath = "receipts/runtime-rollback/runtime-rollback-$activationId.ready.json"
    $journalEmptySha256 = Get-DawnstrikeSha256Text ""
    Assert-DawnstrikeNoReparseComponents $rollbackReceiptRoot "Rollback receipt root"
    Assert-DawnstrikeNoReparseComponents $rollbackReceipt "Rollback receipt"
    Assert-DawnstrikeNoReparseComponents $rollbackReadyReceipt "Rollback ready receipt"
    Assert-DawnstrikeNoReparseComponents $operationJournalPath "Rollback operation journal"
    Assert-DawnstrikeSameVolume @($runtime, $rollbackStage, $rollbackRoot)
    $approvedJournalInterpreter = Get-DawnstrikeApprovedLockInterpreter
    $contractOrigin = Get-DawnstrikeGitValue `
        $gitPath $contract @("remote", "get-url", "origin") `
        "Rollback contract origin verification" $ProcessTimeoutSeconds
    Assert-DawnstrikeSafeOrigin $contractOrigin
    $contractOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $contractOrigin
    # Resolve the declaration before any existing COMPLETE receipt fast path
    # so every terminal return uses the same exact candidate contract.  This
    # is read-only and does not authorize a stale/nonterminal rollback.
    $stateDeclaration = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $contract `
        -GitPath $gitPath `
        -CandidateSha $candidateSha `
        -CandidateTree $activation.candidate_tree `
        -PythonPath $pythonPath `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $contract `
        -CandidateSha $candidateSha `
        -CandidateTree $activation.candidate_tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds

    # A runtime that merely happens to have the recorded previous SHA is not a
    # reusable rollback target.  Require the activation receipt's explicit
    # authorization disposition and re-prove the referenced prior COMPLETE
    # activation/journal chain from durable state before any rollback lock or
    # task mutation.  The activation transaction can still compensate its own
    # failed swap, but a legacy/unapproved prior runtime stays quarantined.
    if (
        -not ($activation.PSObject.Properties.Name -contains "previous_runtime_rollback_authorized") -or
        $activation.previous_runtime_rollback_authorized -ne $true -or
        [string]$activation.previous_runtime_disposition -ne "AUTHORIZED_COMPLETE_CHAIN"
    ) {
        throw "Rollback denied: the prior runtime is quarantined and lacks an authorized COMPLETE activation chain."
    }
    $priorAuthorization = Get-DawnstrikePriorRuntimeAuthorization `
        -StateRoot $state -CandidateRoot $contract `
        -PreviousSha $previousSha -PreviousTree $previousTree `
        -OriginIdentity $contractOriginIdentity `
        -OriginSha256 ([string]$activation.runtime_origin_sha256) `
        -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds
    if (
        $priorAuthorization.authorized -ne $true -or
        [string]$priorAuthorization.receipt_sha256 -cne [string]$activation.previous_runtime_authorization_receipt_sha256 -or
        [string]$priorAuthorization.journal_sha256 -cne [string]$activation.previous_runtime_authorization_journal_sha256
    ) {
        throw "Rollback denied: prior runtime authorization receipt/journal chain is missing or changed."
    }

    $existingRollbackReceipt = $null
    if (Test-Path -LiteralPath $rollbackReceipt -PathType Leaf) {
        Assert-DawnstrikeNoReparseComponents $rollbackReceipt "Existing rollback receipt"
        $existingRollbackReceipt = Invoke-DawnstrikeContractCli $pythonPath $contract @("verify-receipt", "--receipt", $rollbackReceipt, "--expected-status", "ROLLED_BACK") "Existing rollback receipt verification" $ProcessTimeoutSeconds
        if (-not (Test-Path -LiteralPath $operationJournalPath -PathType Leaf)) {
            throw "Existing rollback receipt has no durable operation journal."
        }
        $approvedJournalInterpreter = Get-DawnstrikeApprovedLockInterpreter
        $existingJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $approvedJournalInterpreter.path $approvedJournalInterpreter.sha256
        if (
            [string]$existingJournal.payload.operation -ne "runtime_rollback" -or
            [string]$existingJournal.payload.candidate_sha -ne $candidateSha -or
            [string]$existingJournal.payload.candidate_tree -ne [string]$activation.candidate_tree -or
            [string]$existingJournal.payload.previous_sha -ne $previousSha -or
            [string]$existingJournal.payload.previous_tree -ne $previousTree -or
            [string]$existingJournal.payload.complete_receipt_relative_path -ne $journalCompleteRelativePath -or
            (
                [string]$existingJournal.payload.phase -eq "COMPLETE" -and
                [string]$existingJournal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $rollbackReceipt)
            )
        ) { throw "Existing rollback receipt is not bound to the exact rollback journal." }
        if ([string]$existingJournal.payload.phase -ne "COMPLETE") {
            if ([string]$existingJournal.payload.phase -notin @("POST_SWAP", "POST_SWAP_READY")) {
                throw "Existing rollback receipt has an invalid non-COMPLETE journal phase."
            }
        }
        if ([string]$existingJournal.payload.phase -eq "COMPLETE") {
        $null = Assert-DawnstrikeRollbackCompleteTerminal `
            -Journal $existingJournal -Activation $activation -ReceiptPath $rollbackReceipt `
            -CandidateRoot $contract -RuntimeRoot $runtime -StateRoot $state `
            -BackupRoot $safeBackupRoot -GitPath $gitPath -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $candidateSha `
            -CandidateTree ([string]$activation.candidate_tree) -PreviousSha $previousSha `
            -PreviousTree $previousTree -OriginIdentity ([string]$existingJournal.payload.origin_identity) `
            -MarketDate $marketDate -StateDeclaration $stateDeclaration
        if (-not (Test-Path -LiteralPath $runtime -PathType Container)) {
            throw "Rollback receipt exists but the runtime is missing."
        }
        $current = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $previousSha
        if ($current.tree -ne $previousTree) {
            throw "Rollback receipt exists but the runtime tree does not match."
        }
        $currentOrigin = Get-DawnstrikeGitValue `
            $gitPath `
            $runtime `
            @("remote", "get-url", "origin") `
            "Existing rollback origin verification" `
            $ProcessTimeoutSeconds
        Assert-DawnstrikeSafeOrigin $currentOrigin
        if ((Get-DawnstrikeSha256Text $currentOrigin) -ne [string]$existingRollbackReceipt.runtime_origin_sha256) {
            throw "Rollback receipt exists but the runtime origin does not match."
        }
        $existingTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        if (
            $existingTasks.task_contract_sha256 -ne [string]$existingRollbackReceipt.task_contract_sha256 -or
            $existingTasks.task_definition_contract_sha256 -ne
                [string]$existingRollbackReceipt.task_definition_contract_sha256 -or
            $existingTasks.task_action_contract_sha256 -ne
                [string]$existingRollbackReceipt.task_action_contract_sha256
        ) {
            throw "Rollback receipt exists but exact Ready task XML does not match."
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256) `
            -ExpectedTaskContractSha256 ([string](Get-DawnstrikeTaskXmlBackupManifest `
                -StateRoot $state -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
                -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256)).task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string](Get-DawnstrikeTaskXmlBackupManifest `
                -StateRoot $state -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
                -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256)).task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string](Get-DawnstrikeTaskXmlBackupManifest `
                -StateRoot $state -BackupName ([string]$existingRollbackReceipt.scheduler_backup_name) `
                -ExpectedManifestSha256 ([string]$existingRollbackReceipt.scheduler_backup_manifest_sha256)).task_action_contract_sha256)
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $existingRollbackReceipt `
            -StateRoot $state `
            -BackupRoot $safeBackupRoot `
            -ToolRoot $contract `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
        $completeLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
        $completeLock = $null
        $completeDailyLock = $null
        $completeLockRoot = Join-Path $state "locks"
        Assert-DawnstrikeNoReparseComponents $completeLockRoot "Completed rollback lock root"
        $expectedCompleteDailyPath = Join-Path $completeLockRoot ("dawnstrike-daily-" + $marketDate + ".lock")
        $completeDailyPaths = @(
            Get-ChildItem -LiteralPath $completeLockRoot -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue |
                ForEach-Object { [System.IO.Path]::GetFullPath($_.FullName) }
        )
        $unexpectedCompleteDaily = @($completeDailyPaths | Where-Object {
            $_ -ne [System.IO.Path]::GetFullPath($expectedCompleteDailyPath)
        })
        if ($unexpectedCompleteDaily.Count -gt 0) {
            throw "Completed rollback has a foreign or multiple daily lock set."
        }
        if (Test-Path -LiteralPath $completeLockPath -PathType Leaf) {
            $completeInterpreter = Get-DawnstrikeApprovedLockInterpreter
            $completeLockSnapshot = Get-DawnstrikeStrictRuntimeLock $completeLockPath $completeInterpreter.path $completeInterpreter.sha256
            if ([string]$completeLockSnapshot.payload.operation -ne "runtime_rollback") {
                throw "Completed rollback lock belongs to a different operation."
            }
            $completeLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                -StateRoot $state -JournalPath $operationJournalPath `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $currentOrigin) `
                -PythonPath $completeInterpreter.path -PythonSha256 $completeInterpreter.sha256
            $completeJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                $operationJournalPath $completeInterpreter.path $completeInterpreter.sha256
            if (
                [string]$completeJournal.payload.phase -ne "COMPLETE" -or
                [string]$completeJournal.payload.prepared_receipt_sha256 -ne (Get-DawnstrikeSha256File $receiptPath) -or
                [string]$completeJournal.payload.backup_contract_sha256 -ne [string]$existingRollbackReceipt.scheduler_backup_manifest_sha256 -or
                [string]$completeJournal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $rollbackReceipt)
            ) { throw "Completed rollback lock recovery changed the sealed journal." }
            if (Test-Path -LiteralPath $expectedCompleteDailyPath -PathType Leaf) {
                $completeDailyLock = Enter-DawnstrikeDailyRunLock `
                    -StateRoot $state -MarketDate $marketDate -Owner "runtime_rollback"
                if (-not $completeDailyLock.acquired) {
                    throw "Completed rollback could not reacquire its exact daily lock."
                }
                Confirm-DawnstrikeActivationDailyLockHandshake `
                    -StateRoot $state -ActivationLock $completeLock -DailyLock $completeDailyLock | Out-Null
                Exit-DawnstrikeDailyRunLock -Lock $completeDailyLock
                if (Test-Path -LiteralPath $expectedCompleteDailyPath -PathType Leaf) {
                    throw "Completed rollback daily lock release was not proven."
                }
                $completeDailyLock = $null
            }
            Exit-DawnstrikeGovernedRuntimeLock $completeLock
            if (Test-Path -LiteralPath $completeLockPath -PathType Leaf) {
                throw "Completed rollback runtime lock release was not proven."
            }
            $completeLock = $null
        }
        else {
            Assert-DawnstrikeNoDailyLocks $state
        }
        return $existingRollbackReceipt
        }
    }

    # A failed rollback may restore the activated candidate and exact Ready
    # tasks.  That is a terminal compensation boundary, not POST_SWAP retry
    # state (POST_SWAP requires the previous runtime and Disabled tasks).
    if (Test-Path -LiteralPath $operationJournalPath -PathType Leaf) {
        $compensatedJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
            $operationJournalPath $approvedJournalInterpreter.path $approvedJournalInterpreter.sha256
        if ([string]$compensatedJournal.payload.phase -eq "COMPENSATED") {
            $compensatedRelative = [string]$compensatedJournal.payload.compensation_receipt_relative_path
            $compensatedPath = Join-Path $state ($compensatedRelative.Replace('/', '\'))
            $compensationCheck = & $approvedJournalInterpreter.path -I -B -S (Join-Path $PSScriptRoot "runtime_operation_journal.py") verify-compensation `
                --receipt $compensatedPath --state-root $state 2>$null
            if ($LASTEXITCODE -ne 0) { throw "Compensated rollback receipt failed strict validation." }
            $compensationPayload = (($compensationCheck -join "") | ConvertFrom-Json).payload
            $compensatedRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $candidateSha
            $compensationOrigin = Get-DawnstrikeGitValue `
                $gitPath $runtime @("remote", "get-url", "origin") `
                "Compensated rollback origin verification" $ProcessTimeoutSeconds
            Assert-DawnstrikeSafeOrigin $compensationOrigin
            if ((Get-DawnstrikeSha256Text $compensationOrigin) -ne [string]$activation.runtime_origin_sha256) {
                throw "Compensated rollback origin does not match the activation receipt."
            }
            $compensationOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $compensationOrigin
            $compensatedTasks = Get-DawnstrikeTaskContract $runtime $state
            if (
                $compensationPayload.operation -ne "runtime_rollback" -or
                $compensationPayload.candidate_sha -ne $candidateSha -or
                $compensationPayload.candidate_tree -ne [string]$activation.candidate_tree -or
                $compensationPayload.prior_journal_file_sha256 -ne [string]$compensatedJournal.payload.prior_journal_file_sha256 -or
                $compensationPayload.task_state -ne "Ready" -or
                $compensatedRuntime.tree -ne [string]$activation.candidate_tree -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensatedJournal.payload.task_contract_sha256 -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensationPayload.task_contract_sha256 -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensationPayload.task_xml_sha256 -or
                $compensatedTasks.task_action_contract_sha256 -ne [string]$compensationPayload.task_action_contract_sha256 -or
                $compensatedTasks.task_definition_contract_sha256 -ne [string]$compensationPayload.task_definition_contract_sha256 -or
                $compensatedJournal.payload.compensation_receipt_sha256 -ne (Get-DawnstrikeSha256File $compensatedPath)
            ) { throw "Compensated rollback tombstone does not attest the exact restored boundary." }
            $compensationLock = $null
            $compensationLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
            if (Test-Path -LiteralPath $compensationLockPath -PathType Leaf) {
                $compensationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                    -StateRoot $state -JournalPath $operationJournalPath -CandidateSha $candidateSha `
                    -CandidateTree ([string]$activation.candidate_tree) -OriginIdentity $compensationOriginIdentity `
                    -PythonPath $approvedJournalInterpreter.path -PythonSha256 $approvedJournalInterpreter.sha256
            }
            if (@(Get-ChildItem -LiteralPath (Join-Path $state "locks") -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue).Count -gt 0) {
                if ($null -eq $compensationLock) { throw "Compensated rollback has a daily lock without its runtime lock." }
                $compensationDaily = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $marketDate -Owner "runtime_rollback"
                if (-not $compensationDaily.acquired) { throw "Compensated rollback could not recover its daily lock." }
                Exit-DawnstrikeDailyRunLock $compensationDaily
            }
            # The failed cycle's scheduler backup is terminal evidence.  A
            # recursive retry must not encounter it as a live fresh-attempt
            # backup (the cycle-1 -> cycle-2 poison).  Move it under an
            # immutable prior-journal-hash name only after proving its exact
            # sealed manifest; the helper is idempotent for a crash between
            # archival and tombstone cleanup.
            $archiveAttemptArgs = @{
                StateRoot = $state
                ActivationId = $activationId
                JournalSha256 = [string]$compensatedJournal.raw_file_sha256
                ExpectedManifestSha256 = [string]$compensatedJournal.payload.backup_contract_sha256
            }
            if (-not $tasksInitiallyEnabled) { $archiveAttemptArgs.AllowMissing = $true }
            # Invoke on every terminal retry.  The helper proves source/archive
            # exclusivity and the sealed manifest even when the source was
            # already moved by a previous interrupted cleanup.
            $null = Archive-DawnstrikeTerminalRollbackAttemptBackup @archiveAttemptArgs
            if ($null -ne $compensationLock) { Exit-DawnstrikeGovernedRuntimeLock $compensationLock }
            Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournalPath `
                -Operation runtime_rollback -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -OriginIdentity $compensationOriginIdentity -PythonPath $approvedJournalInterpreter.path -PythonSha256 $approvedJournalInterpreter.sha256
            return Invoke-DawnstrikeRuntimeRollback @PSBoundParameters
        }
    }
    if (-not (Test-Path -LiteralPath $rollbackBundle -PathType Leaf)) {
        throw "Integrity-sealed rollback bundle is missing."
    }
    if ((Get-DawnstrikeSha256File $rollbackBundle) -ne [string]$activation.rollback_bundle_sha256) {
        throw "Rollback bundle hash does not match the activation receipt."
    }
    $null = Invoke-DawnstrikeActivationProcess $gitPath @("bundle", "verify", $rollbackBundle) $contract "Rollback bundle verification" $ProcessTimeoutSeconds

    $currentContract = $null
    if (Test-Path -LiteralPath $runtime -PathType Container) {
        $currentContract = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds
        if ($currentContract.head -notin @($candidateSha, $previousSha)) {
            throw "Active runtime is neither the activated candidate nor the recorded previous SHA."
        }
    }
    if ($null -ne $currentContract -and $currentContract.head -eq $previousSha) {
        if ($currentContract.tree -ne $previousTree) {
            throw "Restored runtime tree does not match the activation receipt."
        }
    }
    elseif (-not (Test-Path -LiteralPath $rollbackCheckout -PathType Container)) {
        throw "Previous runtime checkout is missing; rollback cannot recover its approved origin."
    }

    $origin = ""
    if (Test-Path -LiteralPath $rollbackCheckout -PathType Container) {
        $previous = Get-DawnstrikeGitContract $gitPath $rollbackCheckout $ProcessTimeoutSeconds $previousSha
        if ($previous.tree -ne $previousTree) {
            throw "Previous runtime checkout tree does not match the activation receipt."
        }
        $origin = Get-DawnstrikeGitValue $gitPath $rollbackCheckout @("remote", "get-url", "origin") "Rollback origin verification" $ProcessTimeoutSeconds
    }
    elseif ($null -ne $currentContract -and $currentContract.head -eq $previousSha) {
        $origin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Restored origin verification" $ProcessTimeoutSeconds
    }
    Assert-DawnstrikeSafeOrigin $origin
    if ((Get-DawnstrikeSha256Text $origin) -ne [string]$activation.runtime_origin_sha256) {
        throw "Rollback origin does not match the activation receipt."
    }

    $taskBefore = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
    # Always inventory the auxiliary.  A task present during rollback without
    # an explicit governed sidecar declaration is an ungoverned task and must
    # fail closed rather than being silently carried through a legacy path.
    $auxiliaryBefore = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if (-not $stateDeclaration.required -and $auxiliaryBefore.present) {
        throw "Auxiliary capture task is present but the activation candidate did not declare its governed sidecar contract."
    }
    if ($stateDeclaration.required -and $activation.PSObject.Properties.Name -contains "auxiliary_capture_present") {
        if ([bool]$activation.auxiliary_capture_present -ne [bool]$auxiliaryBefore.present) {
            throw "Rollback auxiliary capture presence does not match the activation receipt."
        }
        if ($auxiliaryBefore.present) {
            if ($auxiliaryBefore.state -eq "Disabled") {
                if (
                    $auxiliaryBefore.definition_contract_sha256 -ne [string]$activation.auxiliary_capture_definition_contract_sha256 -or
                    $auxiliaryBefore.action_contract_sha256 -ne [string]$activation.auxiliary_capture_action_contract_sha256
                ) { throw "Rollback auxiliary capture task is not the exact disabled activation task." }
            }
            elseif ($auxiliaryBefore.state -eq "Ready") {
                $captureReceiptPath = Join-Path $state ("receipts\capture-task\capture-task-rebind-" + $candidateSha + ".json")
                Assert-DawnstrikeNoReparseComponents $captureReceiptPath "Capture-task complete receipt"
                if (Test-Path -LiteralPath $captureReceiptPath -PathType Leaf) {
                    $null = Assert-DawnstrikeCaptureRebindChain `
                        -ActivationReceipt $activation -Auxiliary $auxiliaryBefore `
                        -CandidateRoot $contract -StateRoot $state -CandidateSha $candidateSha `
                        -CandidateTree $activation.candidate_tree -PythonPath $pythonPath `
                        -TimeoutSeconds $ProcessTimeoutSeconds
                }
                else {
                    $null = Assert-DawnstrikeCapturePreparedRecovery `
                        -Activation $activation -Auxiliary $auxiliaryBefore `
                        -StateRoot $state -CandidateSha $candidateSha `
                        -CandidateTree $activation.candidate_tree -PythonPath $pythonPath
                }
            }
            else { throw "Rollback auxiliary capture task is in an ambiguous state." }
        }
    }
    $allowedTaskActionContracts = @([string]$activation.task_action_contract_sha256)
    if (Test-Path -LiteralPath $rollbackReadyReceipt -PathType Leaf) {
        $readyHint = Invoke-DawnstrikeContractCli $pythonPath $contract `
            @("verify-receipt", "--receipt", $rollbackReadyReceipt, "--expected-status", "PREPARED") `
            "Rollback ready receipt preflight verification" $ProcessTimeoutSeconds
        if (
            [string]$readyHint.activation_id -ne $activationId -or
            [string]$readyHint.candidate_sha -ne $candidateSha -or
            [string]$readyHint.candidate_tree -ne [string]$activation.candidate_tree -or
            [string]$readyHint.previous_sha -ne $previousSha -or
            [string]$readyHint.previous_tree -ne $previousTree
        ) { throw "Rollback ready receipt is not bound to the exact activation." }
        $allowedTaskActionContracts += [string]$readyHint.task_action_contract_sha256
    }
    if ($taskBefore.task_action_contract_sha256 -notin $allowedTaskActionContracts) {
        throw "Task actions do not match the activation or exact rollback-ready receipt."
    }
    if (
        $taskBefore.task_definition_contract_sha256 -ne
            [string]$activation.task_definition_contract_sha256
    ) {
        throw "Task definitions do not match the activation receipt."
    }
    $tasksInitiallyEnabled = $taskBefore.enabled_count -eq 5 -and $taskBefore.disabled_count -eq 0
    $tasksInitiallyDisabled = $taskBefore.disabled_count -eq 5 -and $taskBefore.enabled_count -eq 0
    if (-not $tasksInitiallyEnabled -and -not $tasksInitiallyDisabled) {
        throw "Canonical tasks have a mixed or ambiguous state before rollback."
    }
    if (
        $tasksInitiallyEnabled -and
        $taskBefore.task_contract_sha256 -ne [string]$activation.task_contract_sha256
    ) {
        throw "Enabled task XML does not match the activation receipt."
    }
    if ($tasksInitiallyDisabled) {
        $activationTaskBackup = Join-Path $state ("scheduler-backups\" + [string]$activation.scheduler_backup_name + "\manifest.json")
        if (
            -not (Test-Path -LiteralPath $activationTaskBackup -PathType Leaf) -or
            (Get-DawnstrikeSha256File $activationTaskBackup) -ne
                [string]$activation.scheduler_backup_manifest_sha256
        ) {
            throw "Disabled-task crash recovery lacks its exact activation XML backup."
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName ([string]$activation.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$activation.scheduler_backup_manifest_sha256) `
            -ExpectedTaskContractSha256 ([string]$activation.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$activation.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$activation.task_action_contract_sha256)
    }
    elseif (Test-Path -LiteralPath $rollbackSchedulerBackupPath) {
        throw "Rollback scheduler backup already exists and requires review."
    }
    $dbPath = Join-Path $state "shadow_real.sqlite"
    $stateInfo = Invoke-DawnstrikeContractCli $pythonPath $contract @("inspect-state", "--db-path", $dbPath) "Rollback state validation" $ProcessTimeoutSeconds
    if ([int]$stateInfo.schema_version -ne [int]$activation.state_schema_version) {
        throw "Current durable state schema is incompatible with the previous runtime."
    }

    $activationLock = $null
    $dailyLock = $null
    $candidateMoved = $false
    $previousInstalled = $false
    $tasksDisabled = $tasksInitiallyDisabled
    $auxiliaryDisabled = $false
    $taskBackup = $null
    $preserveLocks = $false
    $journalPhase = "INIT"
    $operationJournal = $null
    $journalTaskContractSha256 = [string]$taskBefore.task_contract_sha256
    $stageContractSha256 = Get-DawnstrikeSha256Text ("dawnstrike-runtime-rollback-stage.v1|" + $activationId + "|" + $candidateSha + "|" + [string]$activation.candidate_tree + "|" + $previousSha + "|" + $previousTree + "|" + $rollbackStage)
    try {
        $lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
        $lockInterpreter = Get-DawnstrikeApprovedLockInterpreter
        $enterJournalArgs = @{
            StateRoot = $state
            JournalPath = $operationJournalPath
            Operation = "runtime_rollback"
            CandidateSha = $candidateSha
            CandidateTree = [string]$activation.candidate_tree
            CurrentSha = $candidateSha
            CurrentTree = [string]$activation.candidate_tree
            PreviousSha = $previousSha
            PreviousTree = $previousTree
            OriginIdentity = $lockOrigin
            PreparedReceiptRelativePath = $journalPreparedRelativePath
            CompleteReceiptRelativePath = $journalCompleteRelativePath
            TaskContractSha256 = $journalTaskContractSha256
            PythonPath = $lockInterpreter.path
            PythonSha256 = $lockInterpreter.sha256
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -in @("after_init", "after_lock")) {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            $enterJournalArgs.TestCrashPoint = $env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT
        }
        $operationLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
        $hasJournal = Test-Path -LiteralPath $operationJournalPath -PathType Leaf
        $hasOperationLock = Test-Path -LiteralPath $operationLockPath -PathType Leaf
        if ($hasJournal) {
            $preexistingJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
            if (
                [string]$preexistingJournal.payload.operation -ne "runtime_rollback" -or
                [string]$preexistingJournal.payload.candidate_sha -ne $candidateSha -or
                [string]$preexistingJournal.payload.candidate_tree -ne [string]$activation.candidate_tree -or
                [string]$preexistingJournal.payload.previous_sha -ne $previousSha -or
                [string]$preexistingJournal.payload.previous_tree -ne $previousTree -or
                [string]$preexistingJournal.payload.origin_identity -ne $lockOrigin
            ) { throw "Existing rollback journal source identity is invalid." }
            $journalTaskContractSha256 = [string]$preexistingJournal.payload.task_contract_sha256
            $enterJournalArgs.TaskContractSha256 = $journalTaskContractSha256
        }
        if ($hasJournal -and $hasOperationLock) {
            # A crash after INIT leaves both artifacts. Adopt the exact stale
            # lock first; Enter-DawnstrikeDailyRunLock then performs the
            # governed same-date dead-owner recovery and rejects foreign or
            # active daily locks.
            $activationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                -StateRoot $state -JournalPath $operationJournalPath `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -OriginIdentity $lockOrigin -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
        }
        else {
            if ($hasJournal -and -not $hasOperationLock) {
                $orphan = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
                if ([string]$orphan.payload.phase -ne "INIT") { throw "Rollback journal exists without its exact runtime lock." }
            }
            Assert-DawnstrikeNoDailyLocks $state
            $activationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal @enterJournalArgs
        }
        $operationJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
        $journalPhase = [string]$operationJournal.payload.phase
        if (
            [string]$operationJournal.payload.prepared_receipt_sha256 -ne (Get-DawnstrikeSha256File $receiptPath) -and
            $journalPhase -ne "INIT"
        ) { throw "Rollback journal is not bound to the exact activation receipt." }
        if (
            [string]$operationJournal.payload.runtime_stage_contract_sha256 -notin @($journalEmptySha256, $stageContractSha256)
        ) { throw "Rollback journal stage identity is invalid." }
        $dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $marketDate -Owner "runtime_rollback"
        if (-not $dailyLock.acquired) {
            throw "Runtime rollback could not acquire the daily run lock."
        }
        Confirm-DawnstrikeActivationDailyLockHandshake `
            -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
        $taskLocked = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        if ($taskLocked.task_action_contract_sha256 -ne $taskBefore.task_action_contract_sha256) {
            throw "Task definitions changed during rollback preflight."
        }
        if ($journalPhase -ne "INIT") {
            $taskBackup = [pscustomobject]@{
                backup_name = if (Test-Path -LiteralPath $rollbackSchedulerBackupPath) { $rollbackSchedulerBackupName } else { [string]$activation.scheduler_backup_name }
                manifest_sha256 = [string]$operationJournal.payload.backup_contract_sha256
            }
        }
        elseif ($tasksInitiallyEnabled) {
            if ($taskLocked.task_contract_sha256 -ne $taskBefore.task_contract_sha256) {
                throw "Task XML changed during rollback preflight."
            }
            $taskBackup = New-DawnstrikeTaskXmlBackup `
                -StateRoot $state `
                -BackupName $rollbackSchedulerBackupName `
                -ActivationId $activationId `
                -TaskContract $taskLocked `
                -AuxiliaryCapture $auxiliaryBefore
            $tasksDisabled = $false
        }
        elseif ($tasksInitiallyDisabled) {
            $taskBackup = [pscustomobject]@{
                backup_name = [string]$activation.scheduler_backup_name
                manifest_sha256 = [string]$activation.scheduler_backup_manifest_sha256
            }
        }
        else { throw "Rollback task backup state is ambiguous." }
        if ($journalPhase -eq "INIT") {
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase PRE_SWAP `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $candidateSha -CurrentTree ([string]$activation.candidate_tree) `
                -PreviousSha $previousSha -PreviousTree $previousTree -OriginIdentity $lockOrigin `
                -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalCompleteRelativePath `
                -CompleteReceiptSha256 $journalEmptySha256 `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "PRE_SWAP"
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_pre_swap") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        if ($journalPhase -in @("POST_SWAP", "POST_SWAP_READY")) {
            $recoveryTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if ($recoveryTasks.enabled_count -gt 0) {
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
            }
            $tasksDisabled = $true
        }
        elseif ($tasksInitiallyEnabled -and -not $tasksDisabled) {
            Disable-DawnstrikeCanonicalTasks
            $tasksDisabled = $true
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_task_disable") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_THROW_POINT -eq "after_task_disable") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback failure injection is test-only." }
            throw "Injected ordinary rollback failure after PRE_SWAP task disable."
        }
        $taskSwapBoundary = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        $expectedSwapActionContract = if ($journalPhase -eq "POST_SWAP_READY") {
            [string]$readyHint.task_action_contract_sha256
        }
        else { [string]$activation.task_action_contract_sha256 }
        if (
            $taskSwapBoundary.disabled_count -ne 5 -or
            $taskSwapBoundary.enabled_count -ne 0 -or
            $taskSwapBoundary.task_definition_contract_sha256 -ne
                [string]$activation.task_definition_contract_sha256 -or
            $taskSwapBoundary.task_action_contract_sha256 -ne
                $expectedSwapActionContract
        ) {
            throw "Canonical tasks did not enter the exact disabled rollback boundary."
        }
        if ($auxiliaryBefore.present) {
            $auxiliaryDisabled = $true
            $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName $taskBackup.backup_name `
            -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
            -ExpectedTaskContractSha256 ([string]$activation.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$activation.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$activation.task_action_contract_sha256)

        if ($journalPhase -eq "POST_SWAP") {
            $previousInstalled = $true
        }
        elseif ($null -eq $currentContract) {
            if (Test-Path -LiteralPath $rollbackStage -PathType Container) {
                $staged = Get-DawnstrikeGitContract $gitPath $rollbackStage $ProcessTimeoutSeconds $previousSha
                if ($staged.tree -ne $previousTree) { throw "Rollback stage tree does not match the activation receipt." }
                [System.IO.Directory]::Move($rollbackStage, $runtime)
                $previousInstalled = $true
            }
            elseif (Test-Path -LiteralPath $rollbackCheckout -PathType Container) {
                [System.IO.Directory]::Move($rollbackCheckout, $runtime)
                $previousInstalled = $true
            }
            else { throw "Rollback journal PRE_SWAP has no previous-runtime stage or checkout." }
        }
        elseif ($currentContract.head -eq $candidateSha) {
            if (-not (Test-Path -LiteralPath $rollbackStage -PathType Container)) {
                $null = Invoke-DawnstrikeActivationProcess $gitPath @("clone", "--no-checkout", "--quiet", $rollbackBundle, $rollbackStage) (Split-Path -Parent $runtime) "Previous runtime staging" $ProcessTimeoutSeconds
                $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $rollbackStage, "checkout", "--detach", "--quiet", $previousSha) $rollbackStage "Previous runtime checkout" $ProcessTimeoutSeconds
                $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $rollbackStage, "remote", "set-url", "origin", $origin) $rollbackStage "Previous origin binding" $ProcessTimeoutSeconds
            }
            $staged = Get-DawnstrikeGitContract $gitPath $rollbackStage $ProcessTimeoutSeconds $previousSha
            if ($staged.tree -ne $previousTree) { throw "Rollback stage tree does not match the activation receipt." }
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_stage_prepare") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
            if (Test-Path -LiteralPath $deactivatedCandidate) {
                throw "Deactivated candidate preservation path already exists."
            }
            [System.IO.Directory]::Move($runtime, $deactivatedCandidate)
            $candidateMoved = $true
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_first_rename") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
            [System.IO.Directory]::Move($rollbackStage, $runtime)
            $previousInstalled = $true
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_second_rename") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
        }

        $restored = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $previousSha
        if ($restored.tree -ne $previousTree) {
            throw "Restored runtime tree does not match the activation receipt."
        }
        $restoredOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Restored origin verification" $ProcessTimeoutSeconds
        if ((Get-DawnstrikeSha256Text $restoredOrigin) -ne [string]$activation.runtime_origin_sha256) {
            throw "Restored runtime origin does not match the activation receipt."
        }
        # The restored runtime must be disabled and rebound to the previous
        # runtime SHA before any task can be enabled.  The pre-rollback backup
        # remains candidate-bound evidence; it is intentionally distinct from
        # this final previous-SHA task contract.
        Set-DawnstrikeCanonicalTaskExpectedSha `
            -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $previousSha
        $taskAfterDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        $null = Assert-DawnstrikeCanonicalTaskSemantics `
            -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $previousSha -AllowDisabled
        if (
            $taskAfterDisabled.disabled_count -ne 5 -or
            $taskAfterDisabled.enabled_count -ne 0
        ) {
            throw "Canonical tasks were not rebound to the exact disabled previous-SHA boundary."
        }
        if ($journalPhase -eq "PRE_SWAP") {
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase POST_SWAP `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalCompleteRelativePath -CompleteReceiptSha256 $journalEmptySha256 `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "POST_SWAP"
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_post_swap") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        $auxiliaryAfterDisabled = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
        if ($auxiliaryBefore.present -and (
            $auxiliaryAfterDisabled.state -ne "Disabled" -or
            $auxiliaryAfterDisabled.definition_contract_sha256 -ne $auxiliaryBefore.definition_contract_sha256
        )) { throw "Auxiliary capture task changed across the rollback swap." }
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $activation `
            -StateRoot $state `
            -BackupRoot $safeBackupRoot `
            -ToolRoot $contract `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
        if ($stateDeclaration.required -and $activation.PSObject.Properties.Name -contains "auxiliary_capture_present") {
            $expectedAuxiliary = Get-DawnstrikeActivationAuxiliaryRecoveryContract `
                -Activation $activation `
                -StateRoot $state
            if ($expectedAuxiliary.present) {
                $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                    -Expected $expectedAuxiliary `
                    -RuntimeRoot $runtime `
                    -StateRoot $state `
                    -RunAsCredential $RunAsCredential
            }
            $auxiliaryDisabled = $false
        }
        $taskAfter = $taskAfterDisabled
        $payload = $null
        if ($journalPhase -eq "POST_SWAP_READY") {
            if (-not (Test-Path -LiteralPath $rollbackReadyReceipt -PathType Leaf)) {
                throw "POST_SWAP_READY rollback journal has no exact ready receipt."
            }
            $sealedRollback = Invoke-DawnstrikeContractCli $pythonPath $contract `
                @("verify-receipt", "--receipt", $rollbackReadyReceipt, "--expected-status", "PREPARED") `
                "Rollback ready receipt verification" $ProcessTimeoutSeconds
            $payload = [ordered]@{}
            foreach ($property in $sealedRollback.PSObject.Properties) {
                if ($property.Name -ne "receipt_sha256") { $payload[$property.Name] = $property.Value }
            }
        }
        if ($null -eq $existingRollbackReceipt -and $journalPhase -ne "POST_SWAP_READY") {
        $payload = [ordered]@{
            schema_version = "dawnstrike.runtime_rollback_receipt.v2"
            status = "PREPARED"
            activation_id = $activationId
            market_date = $marketDate
            candidate_sha = $candidateSha
            candidate_tree = [string]$activation.candidate_tree
            previous_sha = $previousSha
            previous_tree = $previousTree
            restored_sha = $previousSha
            ci_evidence_sha256 = [string]$activation.ci_evidence_sha256
            sol_evidence_sha256 = [string]$activation.sol_evidence_sha256
            state_backup_id = [string]$activation.state_backup_id
            state_backup_db_sha256 = [string]$activation.state_backup_db_sha256
            state_schema_version = [int]$stateInfo.schema_version
            state_quick_check = [string]$stateInfo.quick_check
            rollback_bundle_sha256 = [string]$activation.rollback_bundle_sha256
            task_count = [int]$taskAfter.task_count
            task_contract_sha256 = [string]$taskAfter.task_contract_sha256
            task_definition_contract_sha256 = [string]$taskAfter.task_definition_contract_sha256
            task_action_contract_sha256 = [string]$taskAfter.task_action_contract_sha256
            task_paths_unchanged = $true
            task_enablement_restored = $false
            scheduler_backup_name = [string]$taskBackup.backup_name
            scheduler_backup_manifest_sha256 = [string]$taskBackup.manifest_sha256
            runtime_origin_sha256 = [string]$activation.runtime_origin_sha256
            swap_contract = "same_volume_two_rename_with_immediate_restore"
            prepared_at_utc = [string]$activation.prepared_at_utc
            completed_at_utc = $null
            research_only = $true
            broker_execution_enabled = $false
        }
    if ($stateDeclaration.required -and $activation.PSObject.Properties.Name -contains "auxiliary_capture_present") {
            $payload.previous_runtime_rollback_authorized = [bool]$activation.previous_runtime_rollback_authorized
            $payload.previous_runtime_disposition = [string]$activation.previous_runtime_disposition
            $payload.previous_runtime_authorization_receipt_sha256 = [string]$activation.previous_runtime_authorization_receipt_sha256
            $payload.previous_runtime_authorization_journal_sha256 = [string]$activation.previous_runtime_authorization_journal_sha256
            $payload.state_preparation_required = $true
            $payload.state_preparation_contract = [string]$activation.state_preparation_contract
            $payload.state_preparation_receipt_sha256 = [string]$activation.state_preparation_receipt_sha256
            $payload.state_preparation_after_db_sha256 = [string]$activation.state_preparation_after_db_sha256
            $payload.state_preparation_after_wal_sha256 = [string]$activation.state_preparation_after_wal_sha256
            $payload.state_preparation_after_shm_sha256 = [string]$activation.state_preparation_after_shm_sha256
            $payload.state_preparation_after_logical_snapshot_sha256 = [string]$activation.state_preparation_after_logical_snapshot_sha256
            $payload.state_preparation_inventory_sha256 = [string]$activation.state_preparation_inventory_sha256
            $payload.state_preparation_backup_id = [string]$activation.state_preparation_backup_id
            $payload.state_preparation_backup_bundle_path = [string]$activation.state_preparation_backup_bundle_path
            $payload.state_preparation_backup_db_sha256 = [string]$activation.state_preparation_backup_db_sha256
            $payload.state_preparation_backup_manifest_sha256 = [string]$activation.state_preparation_backup_manifest_sha256
            $payload.state_preparation_backup_manifest_file_sha256 = [string]$activation.state_preparation_backup_manifest_file_sha256
            $payload.state_backup_bundle_path = [string]$activation.state_backup_bundle_path
            $payload.state_backup_manifest_sha256 = [string]$activation.state_backup_manifest_sha256
            $payload.state_backup_logical_snapshot_sha256 = [string]$activation.state_backup_logical_snapshot_sha256
            $payload.state_backup_source_logical_snapshot_sha256 = [string]$activation.state_backup_source_logical_snapshot_sha256
            $payload.auxiliary_capture_present = [bool]$activation.auxiliary_capture_present
            $payload.auxiliary_capture_state_before = "Disabled"
            $payload.auxiliary_capture_state_after = [string]$activation.auxiliary_capture_state_before
            $payload.auxiliary_capture_action = "RESTORED_EXACT"
            $payload.auxiliary_capture_xml_sha256 = [string]$activation.auxiliary_capture_xml_sha256
            $payload.auxiliary_capture_xml_file_sha256 = [string]$activation.auxiliary_capture_xml_file_sha256
            $payload.auxiliary_capture_definition_contract_sha256 = [string]$activation.auxiliary_capture_definition_contract_sha256
            $payload.auxiliary_capture_action_contract_sha256 = [string]$activation.auxiliary_capture_action_contract_sha256
            $payload.auxiliary_capture_backup_name = [string]$activation.auxiliary_capture_backup_name
            $payload.auxiliary_capture_backup_manifest_sha256 = [string]$activation.auxiliary_capture_backup_manifest_sha256
            $payload.capture_hardening_receipt_relative_path = [string]$activation.capture_hardening_receipt_relative_path
            $payload.capture_hardening_receipt_raw_sha256 = [string]$activation.capture_hardening_receipt_raw_sha256
            $payload.capture_hardening_receipt_sha256 = [string]$activation.capture_hardening_receipt_sha256
            $payload.capture_hardening_xml_sha256 = [string]$activation.capture_hardening_xml_sha256
            $payload.capture_hardening_action_sha256 = [string]$activation.capture_hardening_action_sha256
            $payload.capture_hardening_principal_sha256 = [string]$activation.capture_hardening_principal_sha256
            $payload.capture_hardening_trigger_sha256 = [string]$activation.capture_hardening_trigger_sha256
            $payload.capture_hardening_settings_sha256 = [string]$activation.capture_hardening_settings_sha256
            $payload.capture_hardening_runner_before_sha256 = [string]$activation.capture_hardening_runner_before_sha256
            $payload.capture_hardening_runner_target_sha256 = [string]$activation.capture_hardening_runner_target_sha256
        }
        $input = Join-Path $rollbackReceiptRoot ".$activationId.input.json"
        Write-DawnstrikeActivationJson $payload $input
        try {
            $sealedRollback = Invoke-DawnstrikeContractCli $pythonPath $contract @("seal-receipt", "--input", $input, "--output", $rollbackReadyReceipt) "Rollback ready receipt sealing" $ProcessTimeoutSeconds
            if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_receipt") {
                if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
                Stop-Process -Id $PID -Force
            }
            # The temporary seal input is no longer recovery evidence.  Remove
            # it before the terminal journal transition so terminal cleanup
            # cannot turn a committed rollback into compensation.
            if (Test-Path -LiteralPath $input -PathType Leaf) {
                Remove-Item -LiteralPath $input -Force
            }
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase POST_SWAP_READY `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalReadyRelativePath -CompleteReceiptSha256 (Get-DawnstrikeSha256File $rollbackReadyReceipt) `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "POST_SWAP_READY"
        }
        finally {
            if (Test-Path -LiteralPath $input -PathType Leaf) {
                Remove-Item -LiteralPath $input -Force -ErrorAction SilentlyContinue
            }
        }
        }
        else {
            $sealedRollback = $existingRollbackReceipt
        }
        if ($journalPhase -eq "POST_SWAP") {
            $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                -Lock $activationLock -Operation runtime_rollback -Phase POST_SWAP_READY `
                -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                -CompleteReceiptRelativePath $journalReadyRelativePath -CompleteReceiptSha256 (Get-DawnstrikeSha256File $rollbackReadyReceipt) `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 $journalTaskContractSha256 `
                -RuntimeStageContractSha256 $stageContractSha256 `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
            $journalPhase = "POST_SWAP_READY"
        }
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_ready") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        Enable-DawnstrikeCanonicalTasks
        $taskAfter = Get-DawnstrikeTaskContract $runtime $state
        $null = Assert-DawnstrikeCanonicalTaskSemantics `
            -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $previousSha
        if (
            $taskAfter.enabled_count -ne 5 -or
            $taskAfter.disabled_count -ne 0 -or
            $taskAfter.task_contract_sha256 -ne [string]$payload.task_contract_sha256 -or
            $taskAfter.task_definition_contract_sha256 -ne [string]$payload.task_definition_contract_sha256 -or
            $taskAfter.task_action_contract_sha256 -ne [string]$payload.task_action_contract_sha256
        ) {
            throw "Task XML was not restored exactly to the previous-SHA Ready boundary."
        }
        $tasksDisabled = $false
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_enable") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        $payload.schema_version = "dawnstrike.runtime_rollback_receipt.v1"
        $payload.status = "ROLLED_BACK"
        $payload.task_count = [int]$taskAfter.task_count
        $payload.task_contract_sha256 = [string]$taskAfter.task_contract_sha256
        $payload.task_definition_contract_sha256 = [string]$taskAfter.task_definition_contract_sha256
        $payload.task_action_contract_sha256 = [string]$taskAfter.task_action_contract_sha256
        $payload.task_enablement_restored = $true
        $payload.completed_at_utc = [DateTime]::UtcNow.ToString("o")
        $input = Join-Path $rollbackReceiptRoot ".$activationId.complete.input.json"
        Write-DawnstrikeActivationJson $payload $input
        try {
            $sealedRollback = Invoke-DawnstrikeContractCli $pythonPath $contract `
                @("seal-receipt", "--input", $input, "--output", $rollbackReceipt) `
                "Rollback terminal receipt sealing" $ProcessTimeoutSeconds
        }
        finally {
            if (Test-Path -LiteralPath $input -PathType Leaf) {
                Remove-Item -LiteralPath $input -Force -ErrorAction SilentlyContinue
            }
        }
        $completeReceiptHash = Get-DawnstrikeSha256File $rollbackReceipt
        $operationJournal = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
            -Lock $activationLock -Operation runtime_rollback -Phase COMPLETE `
            -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
            -CurrentSha $previousSha -CurrentTree $previousTree -PreviousSha $previousSha -PreviousTree $previousTree `
            -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalPreparedRelativePath `
            -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
            -CompleteReceiptRelativePath $journalCompleteRelativePath -CompleteReceiptSha256 $completeReceiptHash `
            -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
            -TaskContractSha256 $journalTaskContractSha256 `
            -RuntimeStageContractSha256 $stageContractSha256 `
            -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256 | Out-Null
        $journalPhase = "COMPLETE"
        $null = Assert-DawnstrikeRollbackCompleteTerminal `
            -Journal $operationJournal -Activation $activation -ReceiptPath $rollbackReceipt `
            -CandidateRoot $contract -RuntimeRoot $runtime -StateRoot $state `
            -BackupRoot $safeBackupRoot -GitPath $gitPath -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $candidateSha `
            -CandidateTree ([string]$activation.candidate_tree) -PreviousSha $previousSha `
            -PreviousTree $previousTree -OriginIdentity $lockOrigin `
            -MarketDate $marketDate -StateDeclaration $stateDeclaration
        if ($env:DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_complete") {
            if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback crash injection is test-only." }
            Stop-Process -Id $PID -Force
        }
        return $sealedRollback
    }
    catch {
        $failure = $_
        # COMPLETE is an irreversible commit.  A cleanup/output fault can be
        # raised after the journal file was durably replaced, so reconcile the
        # exact terminal receipt before considering any compensation.  The
        # stale local POST_SWAP value must never authorize rollback of a
        # committed operation.
        $terminalJournal = $null
        try {
            $terminalJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
            $journalPhase = [string]$terminalJournal.payload.phase
        }
        catch {
            $preserveLocks = $true
            throw "Runtime rollback journal phase could not be reconciled; operator recovery is required."
        }
        if ($journalPhase -eq "COMPLETE") {
            try {
                $terminalRollbackReceipt = Assert-DawnstrikeRollbackCompleteTerminal `
                    -Journal $terminalJournal -Activation $activation -ReceiptPath $rollbackReceipt `
                    -CandidateRoot $contract -RuntimeRoot $runtime -StateRoot $state `
                    -BackupRoot $safeBackupRoot -GitPath $gitPath -PythonPath $pythonPath `
                    -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $candidateSha `
                    -CandidateTree ([string]$activation.candidate_tree) -PreviousSha $previousSha `
                    -PreviousTree $previousTree -OriginIdentity ([string]$terminalJournal.payload.origin_identity) `
                    -MarketDate $marketDate -StateDeclaration $stateDeclaration
                return $terminalRollbackReceipt
            }
            catch {
                $preserveLocks = $true
                throw "Complete rollback evidence could not be reconciled; operator recovery is required."
            }
        }
        if ($candidateMoved -or $previousInstalled -or $tasksDisabled) {
            try {
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                $tasksDisabled = $true
                if ($auxiliaryBefore.present) {
                    $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                    $auxiliaryDisabled = $true
                }
            }
            catch {
                $preserveLocks = $true
                throw "Runtime rollback failed and exact task quiescence could not be proven; runtime recovery was not attempted."
            }
        }
        try {
            if ($previousInstalled -and $candidateMoved -and (Test-Path -LiteralPath $runtime -PathType Container)) {
                $failedAttemptJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                    $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
                if ([string]$failedAttemptJournal.payload.phase -notin @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")) {
                    throw "Failed previous-runtime preservation lacks a nonterminal journal."
                }
                $failedAttemptKey = [string]$failedAttemptJournal.raw_file_sha256
                $failedPrevious = Join-Path $rollbackRoot "failed-previous-runtime-$failedAttemptKey"
                if (Test-Path -LiteralPath $failedPrevious) {
                    throw "Failed previous-runtime preservation path already exists."
                }
                [System.IO.Directory]::Move($runtime, $failedPrevious)
            }
            if (
                $candidateMoved -and
                -not (Test-Path -LiteralPath $runtime) -and
                (Test-Path -LiteralPath $deactivatedCandidate -PathType Container)
            ) {
                [System.IO.Directory]::Move($deactivatedCandidate, $runtime)
            }
            if ($tasksDisabled) {
                if ($env:DAWNSTRIKE_TEST_ROLLBACK_THROW_POINT -eq "during_compensation") {
                    if ($env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") { throw "Rollback failure injection is test-only." }
                    throw "Injected rollback compensation failure."
                }
                if ($null -eq $currentContract) {
                    throw "The pre-rollback runtime was missing, so automatic recovery is ambiguous."
                }
                $expectedRecoveryTree = if ($currentContract.head -eq $candidateSha) {
                    [string]$activation.candidate_tree
                }
                else {
                    $previousTree
                }
                $recoveredRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $currentContract.head
                if ($recoveredRuntime.tree -ne $expectedRecoveryTree) {
                    throw "Automatic rollback failure recovery did not restore the original tree."
                }
                $null = Assert-DawnstrikeTaskXmlBackup `
                    -StateRoot $state `
                    -BackupName $taskBackup.backup_name `
                    -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
                    -ExpectedTaskContractSha256 ([string]$activation.task_contract_sha256) `
                    -ExpectedTaskDefinitionContractSha256 ([string]$activation.task_definition_contract_sha256) `
                    -ExpectedTaskActionContractSha256 ([string]$activation.task_action_contract_sha256)
                $recoveredDisabledTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                if (
                    $recoveredDisabledTasks.disabled_count -ne 5 -or
                    $recoveredDisabledTasks.enabled_count -ne 0 -or
                    $recoveredDisabledTasks.task_definition_contract_sha256 -ne
                        [string]$activation.task_definition_contract_sha256
                ) {
                    throw "Automatic rollback recovery did not recover exact disabled task definitions."
                }
                if ($auxiliaryBefore.present) {
                    $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                        -Expected $auxiliaryBefore `
                        -RuntimeRoot $runtime `
                        -StateRoot $state `
                        -RunAsCredential $RunAsCredential
                    $auxiliaryDisabled = $false
                }
                # Compensation restores the original candidate boundary.  The
                # rollback transaction may have rebound actions to the
                # previous SHA, so restore the candidate binding while still
                # Disabled before re-enabling anything.
                Set-DawnstrikeCanonicalTaskExpectedSha `
                    -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $candidateSha
                $null = Assert-DawnstrikeCanonicalTaskSemantics `
                    -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $candidateSha -AllowDisabled
                Enable-DawnstrikeCanonicalTasks
                $recoveredTasks = Get-DawnstrikeTaskContract $runtime $state
                if ($recoveredTasks.task_contract_sha256 -ne [string]$activation.task_contract_sha256) {
                    throw "Automatic rollback failure recovery did not restore task XML."
                }
                $tasksDisabled = $false
            }
            # The original candidate and Ready task contract are now restored.
            # Seal this as terminal compensation so a retry cannot interpret
            # POST_SWAP/POST_SWAP_READY as proof that the previous runtime is installed.
            if ($journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY") -and -not $tasksDisabled) {
                $compensatedRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $candidateSha
                $compensatedTasks = Get-DawnstrikeTaskContract $runtime $state
                if ($compensatedRuntime.tree -ne [string]$activation.candidate_tree -or
                    $compensatedTasks.task_contract_sha256 -ne [string]$activation.task_contract_sha256) {
                    throw "Automatic rollback restore did not prove the exact original Ready boundary."
                }
                $journalBefore = Get-DawnstrikeStrictRuntimeOperationJournal `
                    $operationJournalPath $lockInterpreter.path $lockInterpreter.sha256
                $compensationAttemptKey = [string]$journalBefore.raw_file_sha256
                $compensationReceiptRelative = "receipts/runtime-rollback/runtime-rollback-$activationId.compensated-$compensationAttemptKey.json"
                $compensationReceipt = Join-Path $state ($compensationReceiptRelative.Replace('/', '\'))
                $failureReceipt = Join-Path $rollbackReceiptRoot "runtime-rollback-$activationId.failed-$compensationAttemptKey.json"
                Assert-DawnstrikeNoReparseComponents $compensationReceipt "Rollback compensation receipt"
                Assert-DawnstrikeNoReparseComponents $failureReceipt "Rollback failure receipt"
                $empty = Get-DawnstrikeSha256Text ""
                $statePrefix = [System.IO.Path]::GetFullPath($state).TrimEnd('\') + '\'
                $receiptFullPath = [System.IO.Path]::GetFullPath($receiptPath)
                if (-not $receiptFullPath.StartsWith($statePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Rollback activation receipt is outside StateRoot."
                }
                $priorReceiptRelative = $receiptFullPath.Substring($statePrefix.Length) -replace '\\','/'
                $priorReceiptHash = Get-DawnstrikeSha256File $receiptPath
                $failurePayload = [ordered]@{
                    schema_version = "dawnstrike.runtime_rollback_failure.v1"
                    status = "FAILED_RESTORED_EXACT_READY"
                    activation_id = $activationId
                    candidate_sha = $candidateSha
                    candidate_tree = [string]$activation.candidate_tree
                    restored_sha = $candidateSha
                    restored_tree = [string]$activation.candidate_tree
                    restored_task_contract_sha256 = [string]$compensatedTasks.task_contract_sha256
                    failure_phase = $journalPhase
                    failure_type = $failure.Exception.GetType().Name
                    recovery_evidence = "EXACT_CANDIDATE_RUNTIME_AND_READY_TASKS"
                    research_only = $true
                    broker_execution_enabled = $false
                }
                Write-DawnstrikeActivationJson $failurePayload $failureReceipt
                $compensationInput = "$compensationReceipt.$([guid]::NewGuid().ToString('N')).input.json"
                $compensationPayload = [ordered]@{
                    schema_version = "dawnstrike.runtime_compensation_receipt.v1"
                    status = "COMPENSATED"
                    operation = "runtime_rollback"
                    candidate_sha = $candidateSha
                    candidate_tree = [string]$activation.candidate_tree
                    prior_journal_file_sha256 = [string]$journalBefore.raw_file_sha256
                    task_contract_sha256 = [string]$compensatedTasks.task_contract_sha256
                    task_state = "Ready"
                    task_xml_sha256 = [string]$compensatedTasks.task_contract_sha256
                    task_action_contract_sha256 = [string]$compensatedTasks.task_action_contract_sha256
                    task_definition_contract_sha256 = [string]$compensatedTasks.task_definition_contract_sha256
                    prior_receipt_relative_path = $priorReceiptRelative
                    prior_receipt_sha256 = $priorReceiptHash
                    failure_type = $failure.Exception.GetType().Name
                    research_only = $true
                    broker_execution_enabled = $false
                }
                try {
                    Write-DawnstrikeActivationJson $compensationPayload $compensationInput
                    & $lockInterpreter.path -I -B -S (Join-Path $PSScriptRoot "runtime_operation_journal.py") seal-compensation `
                        --input $compensationInput --output $compensationReceipt --state-root $state --reuse-existing 2>$null | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw "Rollback compensation receipt strict sealing failed." }
                }
                finally { if (Test-Path -LiteralPath $compensationInput) { Remove-Item -LiteralPath $compensationInput -Force } }
                $compensationHash = Get-DawnstrikeSha256File $compensationReceipt
                $null = Set-DawnstrikeRuntimeOperationJournalPhase -StateRoot $state -JournalPath $operationJournalPath `
                    -Lock $activationLock -Operation runtime_rollback -Phase COMPENSATED `
                    -CandidateSha $candidateSha -CandidateTree ([string]$activation.candidate_tree) `
                    -CurrentSha $candidateSha -CurrentTree ([string]$activation.candidate_tree) `
                    -PreviousSha $previousSha -PreviousTree $previousTree -OriginIdentity $lockOrigin `
                    -PreparedReceiptRelativePath $journalPreparedRelativePath -PreparedReceiptSha256 (Get-DawnstrikeSha256File $receiptPath) `
                    -CompleteReceiptRelativePath $journalCompleteRelativePath -CompleteReceiptSha256 $empty `
                    -BackupContractSha256 ([string]$journalBefore.payload.backup_contract_sha256) `
                    -TaskContractSha256 ([string]$compensatedTasks.task_contract_sha256) -RuntimeStageContractSha256 $empty `
                    -CompensationReceiptRelativePath $compensationReceiptRelative -CompensationReceiptSha256 $compensationHash `
                    -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
                $journalPhase = "COMPENSATED"
            }
        }
        catch {
            try {
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
            }
            catch {
                $preserveLocks = $true
                throw "Runtime rollback and automatic candidate restore failed; exact task state is unverified and operator recovery is required."
            }
            # Compensation failed, but the best-effort Disabled boundary was
            # proven. Keep both exact locks while PRE_SWAP/POST_SWAP/POST_SWAP_READY evidence
            # remains so the next invocation can adopt and retry safely.
            if ($journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")) {
                if ($null -eq $activationLock -or $null -eq $dailyLock) {
                    $preserveLocks = $true
                    throw "Nonterminal rollback compensation lacks its adoptable lock pair."
                }
                $preserveLocks = $true
            }
            throw "Runtime rollback failed and automatic candidate restore could not be completed; canonical tasks are proven Disabled and all rollback artifacts must be preserved. Original failure: $($failure.Exception.Message)"
        }
        # A caught failure after a nonterminal journal transition must leave
        # both lock artifacts adoptable by the next invocation.  Releasing
        # them here would strand PRE_SWAP/POST_SWAP/POST_SWAP_READY evidence with no legal
        # owner and make recovery permanently fail closed.
        if ($journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")) {
            if ($null -eq $activationLock -or $null -eq $dailyLock) {
                throw "Nonterminal rollback recovery lacks its adoptable lock pair."
            }
            $preserveLocks = $true
        }
        throw $failure
    }
    finally {
        if (-not $preserveLocks) {
            if ($null -ne $dailyLock) { Exit-DawnstrikeDailyRunLock -Lock $dailyLock }
            Exit-DawnstrikeGovernedRuntimeLock $activationLock
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    if ([string]::IsNullOrWhiteSpace($ActivationReceipt)) {
        throw "ActivationReceipt is required."
    }
    if ([string]::IsNullOrWhiteSpace($ContractRoot)) {
        $ContractRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    }
    $result = Invoke-DawnstrikeRuntimeRollback `
        -ActivationReceipt $ActivationReceipt `
        -ContractRoot $ContractRoot `
        -RuntimeRoot $RuntimeRoot `
        -StateRoot $StateRoot `
        -BackupRoot $BackupRoot `
        -ProcessTimeoutSeconds $ProcessTimeoutSeconds `
        -RunAsCredential $RunAsCredential
    $result | ConvertTo-Json -Depth 12
}
