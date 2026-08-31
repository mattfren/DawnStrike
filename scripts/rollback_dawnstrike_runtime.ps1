[CmdletBinding()]
param(
    [string]$ActivationReceipt = "",
    [string]$ContractRoot = "",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "C:\r\dawnstrike-state-backups",
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
$rollbackRuntimeRoot = $RuntimeRoot
$rollbackStateRoot = $StateRoot
$rollbackBackupRoot = $BackupRoot
$rollbackTimeout = $ProcessTimeoutSeconds
. (Join-Path $PSScriptRoot "activate_dawnstrike_runtime.ps1")
$RuntimeRoot = $rollbackRuntimeRoot
$StateRoot = $rollbackStateRoot
$BackupRoot = $rollbackBackupRoot
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
        [Parameter(Mandatory = $true)][string]$CandidateTree
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
    try {
        $prepared = Get-Content -LiteralPath $preparedPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Capture-task PREPARED recovery record is invalid JSON."
    }
    $expectedFields = @(
        "schema_version", "status", "task_name", "candidate_sha", "candidate_tree",
        "activation_id", "activation_receipt_name", "activation_receipt_sha256",
        "previous_candidate_sha", "xml_before_sha256", "action_before_sha256",
        "definition_before_sha256", "principal_sha256", "trigger_sha256", "settings_sha256",
        "enablement_before", "compensation", "research_only", "broker_execution_enabled"
    )
    $expectedFieldText = (@($expectedFields | Sort-Object) -join "|")
    $actualFieldText = (@($prepared.PSObject.Properties.Name | Sort-Object) -join "|")
    if ($expectedFieldText -ne $actualFieldText) {
        throw "Capture-task PREPARED recovery record fields are not exact."
    }
    $activationPath = Join-Path $StateRoot ("receipts\runtime-activation\runtime-activation-" + [string]$Activation.activation_id + ".json")
    Assert-DawnstrikeNoReparseComponents $activationPath "Prepared recovery activation receipt"
    if (-not (Test-Path -LiteralPath $activationPath -PathType Leaf)) {
        throw "Capture-task PREPARED recovery activation receipt is missing."
    }
    $activationItem = Get-Item -LiteralPath $activationPath -Force -ErrorAction Stop
    $hashFields = @(
        "activation_receipt_sha256", "xml_before_sha256", "action_before_sha256",
        "definition_before_sha256", "principal_sha256", "trigger_sha256", "settings_sha256"
    )
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

function Invoke-DawnstrikeRuntimeRollback {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ActivationReceipt,
        [Parameter(Mandatory = $true)][string]$ContractRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][int]$ProcessTimeoutSeconds
    )

    $contract = Resolve-DawnstrikeActivationRoot $ContractRoot "ContractRoot"
    $state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
    $safeBackupRoot = Resolve-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    $runtime = Get-DawnstrikeFutureActivationRoot $RuntimeRoot "RuntimeRoot"
    Assert-DawnstrikeRootIsolation $safeBackupRoot @($contract, $runtime, $state) "BackupRoot"
    Assert-DawnstrikeNoReparseComponents $ActivationReceipt "Activation receipt"
    $receiptPath = (Resolve-Path -LiteralPath $ActivationReceipt -ErrorAction Stop).Path
    $approvedReceiptRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $state "receipts\runtime-activation")
    ).TrimEnd('\') + '\'
    if (-not $receiptPath.StartsWith($approvedReceiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Activation receipt must be inside the durable activation receipt root."
    }
    Assert-DawnstrikeNoReparseComponents $receiptPath "Activation receipt"
    $receiptItem = Get-Item -LiteralPath $receiptPath -Force
    if (($receiptItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Activation receipt cannot be a reparse point."
    }

    $gitCommand = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0]
    $pythonCommand = @(Get-Command py.exe -CommandType Application -ErrorAction Stop)[0]
    $gitPath = $gitCommand.Source
    $pythonPath = $pythonCommand.Source
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
        [string]$receiptHint.schema_version -ne "dawnstrike.runtime_activation_receipt.v1" -or
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
    . (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")

    $activation = Invoke-DawnstrikeContractCli `
        -PythonPath $pythonPath `
        -CandidateRoot $contract `
        -Arguments @("verify-receipt", "--receipt", $receiptPath) `
        -Label "Activation receipt verification" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    if ($activation.schema_version -ne "dawnstrike.runtime_activation_receipt.v1") {
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
    $rollbackSchedulerBackupName = "runtime-rollback-$activationId"
    $rollbackSchedulerBackupPath = Join-Path $state "scheduler-backups\$rollbackSchedulerBackupName"
    Assert-DawnstrikeSameVolume @($runtime, $rollbackStage, $rollbackRoot)

    if (Test-Path -LiteralPath $rollbackReceipt -PathType Leaf) {
        $existing = Invoke-DawnstrikeContractCli $pythonPath $contract @("verify-receipt", "--receipt", $rollbackReceipt, "--expected-status", "ROLLED_BACK") "Existing rollback receipt verification" $ProcessTimeoutSeconds
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
        if ((Get-DawnstrikeSha256Text $currentOrigin) -ne [string]$existing.runtime_origin_sha256) {
            throw "Rollback receipt exists but the runtime origin does not match."
        }
        $existingTasks = Get-DawnstrikeTaskContract $runtime $state
        if (
            $existingTasks.task_contract_sha256 -ne [string]$existing.task_contract_sha256 -or
            $existingTasks.task_definition_contract_sha256 -ne
                [string]$existing.task_definition_contract_sha256 -or
            $existingTasks.task_action_contract_sha256 -ne
                [string]$existing.task_action_contract_sha256
        ) {
            throw "Rollback receipt exists but exact Ready task XML does not match."
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName ([string]$existing.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$existing.scheduler_backup_manifest_sha256) `
            -ExpectedTaskContractSha256 ([string]$existing.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$existing.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$existing.task_action_contract_sha256)
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $existing `
            -StateRoot $state `
            -BackupRoot $safeBackupRoot `
            -ToolRoot $contract `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
        return $existing
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

    Assert-DawnstrikeNoDailyLocks $state
    $taskBefore = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
    $stateDeclaration = Get-DawnstrikeStatePreparationDeclaration $contract
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
                        -CandidateTree $activation.candidate_tree
                }
            }
            else { throw "Rollback auxiliary capture task is in an ambiguous state." }
        }
    }
    if ($taskBefore.task_action_contract_sha256 -ne [string]$activation.task_action_contract_sha256) {
        throw "Task actions do not match the activation receipt."
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

    if ($null -ne $currentContract -and $currentContract.head -eq $candidateSha) {
        if (Test-Path -LiteralPath $rollbackStage) {
            throw "Rollback stage already exists and requires review."
        }
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("clone", "--no-checkout", "--quiet", $rollbackBundle, $rollbackStage) (Split-Path -Parent $runtime) "Previous runtime staging" $ProcessTimeoutSeconds
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $rollbackStage, "checkout", "--detach", "--quiet", $previousSha) $rollbackStage "Previous runtime checkout" $ProcessTimeoutSeconds
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $rollbackStage, "remote", "set-url", "origin", $origin) $rollbackStage "Previous origin binding" $ProcessTimeoutSeconds
        $staged = Get-DawnstrikeGitContract $gitPath $rollbackStage $ProcessTimeoutSeconds $previousSha
        if ($staged.tree -ne $previousTree) {
            throw "Rollback stage tree does not match the activation receipt."
        }
    }

    $activationLock = $null
    $dailyLock = $null
    $candidateMoved = $false
    $previousInstalled = $false
    $tasksDisabled = $tasksInitiallyDisabled
    $auxiliaryDisabled = $false
    $taskBackup = $null
    $preserveLocks = $false
    try {
        $activationLock = Enter-DawnstrikeRuntimeActivationLock $state
        Assert-DawnstrikeNoDailyLocks $state
        $dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $marketDate -Owner "runtime_rollback"
        if (-not $dailyLock.acquired) {
            throw "Runtime rollback could not acquire the daily run lock."
        }
        $taskLocked = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        if ($taskLocked.task_action_contract_sha256 -ne $taskBefore.task_action_contract_sha256) {
            throw "Task definitions changed during rollback preflight."
        }
        if ($tasksInitiallyEnabled) {
            if ($taskLocked.task_contract_sha256 -ne $taskBefore.task_contract_sha256) {
                throw "Task XML changed during rollback preflight."
            }
            $taskBackup = New-DawnstrikeTaskXmlBackup `
                -StateRoot $state `
                -BackupName $rollbackSchedulerBackupName `
                -ActivationId $activationId `
                -TaskContract $taskLocked `
                -AuxiliaryCapture $auxiliaryBefore
            $tasksDisabled = $true
            Disable-DawnstrikeCanonicalTasks
        }
        else {
            $taskBackup = [pscustomobject]@{
                backup_name = [string]$activation.scheduler_backup_name
                manifest_sha256 = [string]$activation.scheduler_backup_manifest_sha256
            }
        }
        $taskSwapBoundary = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        if (
            $taskSwapBoundary.disabled_count -ne 5 -or
            $taskSwapBoundary.enabled_count -ne 0 -or
            $taskSwapBoundary.task_definition_contract_sha256 -ne
                [string]$activation.task_definition_contract_sha256 -or
            $taskSwapBoundary.task_action_contract_sha256 -ne
                [string]$activation.task_action_contract_sha256
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

        if ($null -eq $currentContract) {
            [System.IO.Directory]::Move($rollbackCheckout, $runtime)
            $previousInstalled = $true
        }
        elseif ($currentContract.head -eq $candidateSha) {
            if (Test-Path -LiteralPath $deactivatedCandidate) {
                throw "Deactivated candidate preservation path already exists."
            }
            [System.IO.Directory]::Move($runtime, $deactivatedCandidate)
            $candidateMoved = $true
            [System.IO.Directory]::Move($rollbackStage, $runtime)
            $previousInstalled = $true
        }

        $restored = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $previousSha
        if ($restored.tree -ne $previousTree) {
            throw "Restored runtime tree does not match the activation receipt."
        }
        $restoredOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Restored origin verification" $ProcessTimeoutSeconds
        if ((Get-DawnstrikeSha256Text $restoredOrigin) -ne [string]$activation.runtime_origin_sha256) {
            throw "Restored runtime origin does not match the activation receipt."
        }
        $taskAfterDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
        if (
            $taskAfterDisabled.disabled_count -ne 5 -or
            $taskAfterDisabled.enabled_count -ne 0 -or
            $taskAfterDisabled.task_definition_contract_sha256 -ne
                [string]$activation.task_definition_contract_sha256 -or
            $taskAfterDisabled.task_action_contract_sha256 -ne
                [string]$activation.task_action_contract_sha256
        ) {
            throw "Task definitions changed across the rollback swap."
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
                    -StateRoot $state
            }
            $auxiliaryDisabled = $false
        }
        Enable-DawnstrikeCanonicalTasks
        $taskAfter = Get-DawnstrikeTaskContract $runtime $state
        if ($taskAfter.task_contract_sha256 -ne [string]$activation.task_contract_sha256) {
            throw "Task XML was not restored exactly after rollback."
        }
        $tasksDisabled = $false

        $payload = [ordered]@{
            schema_version = "dawnstrike.runtime_rollback_receipt.v1"
            status = "ROLLED_BACK"
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
            task_enablement_restored = $true
            scheduler_backup_name = [string]$taskBackup.backup_name
            scheduler_backup_manifest_sha256 = [string]$taskBackup.manifest_sha256
            runtime_origin_sha256 = [string]$activation.runtime_origin_sha256
            swap_contract = "same_volume_two_rename_with_immediate_restore"
            prepared_at_utc = [string]$activation.prepared_at_utc
            completed_at_utc = [DateTime]::UtcNow.ToString("o")
            research_only = $true
            broker_execution_enabled = $false
        }
        if ($stateDeclaration.required -and $activation.PSObject.Properties.Name -contains "auxiliary_capture_present") {
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
        }
        $input = Join-Path $rollbackReceiptRoot ".$activationId.input.json"
        Write-DawnstrikeActivationJson $payload $input
        try {
            return Invoke-DawnstrikeContractCli $pythonPath $contract @("seal-receipt", "--input", $input, "--output", $rollbackReceipt) "Rollback receipt sealing" $ProcessTimeoutSeconds
        }
        finally {
            if (Test-Path -LiteralPath $input -PathType Leaf) { Remove-Item -LiteralPath $input -Force }
        }
    }
    catch {
        $failure = $_
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
                $failedPrevious = Join-Path $rollbackRoot "failed-previous-runtime"
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
                        -StateRoot $state
                    $auxiliaryDisabled = $false
                }
                Enable-DawnstrikeCanonicalTasks
                $recoveredTasks = Get-DawnstrikeTaskContract $runtime $state
                if ($recoveredTasks.task_contract_sha256 -ne [string]$activation.task_contract_sha256) {
                    throw "Automatic rollback failure recovery did not restore task XML."
                }
                $tasksDisabled = $false
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
            throw "Runtime rollback failed and automatic candidate restore could not be completed; canonical tasks are proven Disabled and all rollback artifacts must be preserved. Original failure: $($failure.Exception.Message)"
        }
        throw $failure
    }
    finally {
        if (-not $preserveLocks) {
            if ($null -ne $dailyLock) { Exit-DawnstrikeDailyRunLock -Lock $dailyLock }
            Exit-DawnstrikeRuntimeActivationLock $activationLock
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
        -ProcessTimeoutSeconds $ProcessTimeoutSeconds
    $result | ConvertTo-Json -Depth 12
}
