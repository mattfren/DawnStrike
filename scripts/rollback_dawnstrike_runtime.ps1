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
. (Join-Path $PSScriptRoot "activate_dawnstrike_runtime.ps1")

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
    $receiptPath = (Resolve-Path -LiteralPath $ActivationReceipt -ErrorAction Stop).Path
    Assert-DawnstrikeNoReparsePath $state "StateRoot"
    $activationReceiptRoot = Join-Path $state "receipts\runtime-activation"
    Assert-DawnstrikeNoReparsePath $activationReceiptRoot "Activation receipt directory"
    $approvedReceiptRoot = [System.IO.Path]::GetFullPath(
        (Join-Path $state "receipts\runtime-activation")
    ).TrimEnd('\') + '\'
    if (-not $receiptPath.StartsWith($approvedReceiptRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Activation receipt must be inside the durable activation receipt root."
    }
    Assert-DawnstrikeNoReparsePath $receiptPath "Activation receipt"
    $receiptItem = Get-Item -LiteralPath $receiptPath -Force
    if ($receiptItem.PSIsContainer -or ($receiptItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
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
        $receiptRaw = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8
        Assert-DawnstrikeJsonUniqueProperties $receiptRaw
        $receiptHint = $receiptRaw | ConvertFrom-Json
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
    if ([string]$activation.status -notin @("PREPARED", "COMPLETE")) {
        throw "Rollback requires a sealed PREPARED or COMPLETE activation receipt."
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
    Assert-DawnstrikeNoReparsePath $rollbackRoot "Rollback root"
    Assert-DawnstrikeNoReparsePath $rollbackCheckout "Rollback checkout" -AllowMissingLeaf
    Assert-DawnstrikeNoReparsePath $rollbackBundle "Rollback bundle" -AllowMissingLeaf
    Assert-DawnstrikeNoReparsePath $rollbackStage "Rollback stage" -AllowMissingLeaf
    Assert-DawnstrikeNoReparsePath $deactivatedCandidate "Deactivated candidate" -AllowMissingLeaf
    $schedulerBackupRoot = Join-Path $state "scheduler-backups"
    Assert-DawnstrikeNoReparsePath $schedulerBackupRoot "Scheduler backup directory"
    Assert-DawnstrikeNoReparsePath $rollbackSchedulerBackupPath "Rollback scheduler backup" -AllowMissingLeaf
    $rollbackReceiptRoot = Ensure-DawnstrikeActivationArtifactRoot $rollbackReceiptRoot "Rollback receipt directory"
    Assert-DawnstrikeNoReparsePath $rollbackReceipt "Rollback receipt" -AllowMissingLeaf
    $preparedReceiptPath = if ([string]$activation.status -eq "PREPARED") {
        $receiptPath
    } else {
        Join-Path $state "receipts\runtime-activation\runtime-activation-$activationId.prepared.json"
    }
    Assert-DawnstrikeNoReparsePath $preparedReceiptPath "Prepared activation receipt"
    $preparedReceipt = if ([string]$activation.status -eq "PREPARED") {
        $activation
    } else {
        Invoke-DawnstrikeContractCli `
            -PythonPath $pythonPath `
            -CandidateRoot $contract `
            -Arguments @("verify-receipt", "--receipt", $preparedReceiptPath, "--expected-status", "PREPARED") `
            -Label "Prepared activation receipt verification" `
            -TimeoutSeconds $ProcessTimeoutSeconds
    }
    if (
        [string]$preparedReceipt.activation_id -ne $activationId -or
        [string]$preparedReceipt.market_date -ne $marketDate
    ) {
        throw "Prepared activation receipt does not match the sealed activation receipt."
    }
    $preparedReceiptFileSha256 = Get-DawnstrikeSha256File $preparedReceiptPath
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
        $null = Archive-DawnstrikeReceiptBoundStaleLocks `
            -StateRoot $state `
            -ActivationReceiptPath $receiptPath `
            -Receipt $activation `
            -PreparedReceipt $preparedReceipt `
            -PreparedReceiptFileSha256 $preparedReceiptFileSha256
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
    $rollbackCheckoutExists = Test-Path -LiteralPath $rollbackCheckout -PathType Container
    if ($null -ne $currentContract -and $currentContract.head -eq $previousSha) {
        if ($currentContract.tree -ne $previousTree) {
            throw "Restored runtime tree does not match the activation receipt."
        }
        if ($rollbackCheckoutExists) {
            throw "Restored previous runtime cannot coexist with a rollback checkout."
        }
    }
    elseif (-not $rollbackCheckoutExists) {
        throw "Previous runtime checkout is missing; rollback cannot recover its approved origin."
    }

    $origin = ""
    if ($rollbackCheckoutExists) {
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
    $recoveryArtifactArguments = @{
        Receipt = $activation
        StateRoot = $state
        BackupRoot = $safeBackupRoot
        ToolRoot = $contract
        GitPath = $gitPath
        PythonPath = $pythonPath
        TimeoutSeconds = $ProcessTimeoutSeconds
    }
    if ($null -eq $currentContract -or $currentContract.head -eq $candidateSha) {
        $recoveryArtifactArguments.RequireRollbackCheckout = $true
    }
    $null = Assert-DawnstrikeReceiptRecoveryArtifacts @recoveryArtifactArguments
    $null = Archive-DawnstrikeReceiptBoundStaleLocks `
        -StateRoot $state `
        -ActivationReceiptPath $receiptPath `
        -Receipt $activation `
        -PreparedReceipt $preparedReceipt `
        -PreparedReceiptFileSha256 $preparedReceiptFileSha256

    if ($null -ne $currentContract -and $currentContract.head -eq $candidateSha) {
        if (Test-Path -LiteralPath $rollbackStage) {
            throw "Rollback stage already exists and requires review."
        }
        Assert-DawnstrikeNoReparsePath (Split-Path -Parent $rollbackStage) "Rollback staging parent"
        Assert-DawnstrikeNoReparsePath $rollbackStage "Rollback staging path" -AllowMissingLeaf
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("clone", "--no-checkout", "--quiet", $rollbackBundle, $rollbackStage) (Split-Path -Parent $runtime) "Previous runtime staging" $ProcessTimeoutSeconds
        Assert-DawnstrikeNoReparsePath $rollbackStage "Rollback staging path"
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
    $taskBackup = $null
    $preserveLocks = $false
    try {
        $lockBinding = @{}
        $lockBinding = @{
            ActivationId = $activationId
            PreparedReceiptName = Split-Path -Leaf $preparedReceiptPath
            PreparedReceiptSha256 = [string]$preparedReceipt.receipt_sha256
            PreparedReceiptFileSha256 = $preparedReceiptFileSha256
        }
        $activationLock = Enter-DawnstrikeRuntimeActivationLock `
            -StateRoot $state -Owner "runtime_rollback" @lockBinding
        Assert-DawnstrikeNoDailyLocks $state
        $dailyLock = Enter-DawnstrikeDailyRunLock `
            -StateRoot $state `
            -MarketDate $marketDate `
            -Owner "runtime_rollback" `
            @lockBinding
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
                -TaskContract $taskLocked
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
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName $taskBackup.backup_name `
            -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
            -ExpectedTaskContractSha256 ([string]$activation.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$activation.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$activation.task_action_contract_sha256)

        if ($null -eq $currentContract) {
            Assert-DawnstrikeNoReparsePath $rollbackCheckout "Rollback checkout"
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root" -AllowMissingLeaf
            [System.IO.Directory]::Move($rollbackCheckout, $runtime)
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
            $previousInstalled = $true
        }
        elseif ($currentContract.head -eq $candidateSha) {
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
            Assert-DawnstrikeNoReparsePath $deactivatedCandidate "Deactivated candidate" -AllowMissingLeaf
            Assert-DawnstrikeNoReparsePath $rollbackStage "Rollback stage"
            if (Test-Path -LiteralPath $deactivatedCandidate) {
                throw "Deactivated candidate preservation path already exists."
            }
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
            Assert-DawnstrikeNoReparsePath $deactivatedCandidate "Deactivated candidate" -AllowMissingLeaf
            [System.IO.Directory]::Move($runtime, $deactivatedCandidate)
            $candidateMoved = $true
            Assert-DawnstrikeNoReparsePath $deactivatedCandidate "Deactivated candidate"
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root" -AllowMissingLeaf
            Assert-DawnstrikeNoReparsePath $rollbackStage "Rollback stage"
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root" -AllowMissingLeaf
            [System.IO.Directory]::Move($rollbackStage, $runtime)
            Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
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
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $activation `
            -StateRoot $state `
            -BackupRoot $safeBackupRoot `
            -ToolRoot $contract `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
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
        $input = Join-Path $rollbackReceiptRoot ".$activationId.input.json"
        Write-DawnstrikeActivationJson `
            -Payload $payload `
            -Path $input `
            -ExpectedRoot $rollbackReceiptRoot
        try {
            Assert-DawnstrikeActivationJsonPath $input $rollbackReceiptRoot "Rollback input receipt"
            Assert-DawnstrikeActivationJsonPath $rollbackReceipt $rollbackReceiptRoot "Rollback receipt" -AllowMissingLeaf
            $result = Invoke-DawnstrikeContractCli $pythonPath $contract @("seal-receipt", "--input", $input, "--output", $rollbackReceipt) "Rollback receipt sealing" $ProcessTimeoutSeconds
            Assert-DawnstrikeActivationJsonPath $rollbackReceipt $rollbackReceiptRoot "Rollback receipt"
            return $result
        }
        finally {
            if (Test-Path -LiteralPath $input -PathType Leaf) {
                Assert-DawnstrikeActivationJsonPath $input $rollbackReceiptRoot "Rollback input receipt"
                Remove-Item -LiteralPath $input -Force
            }
        }
    }
    catch {
        $failure = $_
        if ($candidateMoved -or $previousInstalled -or $tasksDisabled) {
            try {
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                $tasksDisabled = $true
            }
            catch {
                $preserveLocks = $true
                throw "Runtime rollback failed and exact task quiescence could not be proven; runtime recovery was not attempted."
            }
        }
        try {
            if ($previousInstalled -and $candidateMoved -and (Test-Path -LiteralPath $runtime -PathType Container)) {
                $failedPrevious = Join-Path $rollbackRoot "failed-previous-runtime"
                Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
                Assert-DawnstrikeNoReparsePath $failedPrevious "Failed previous runtime" -AllowMissingLeaf
                if (Test-Path -LiteralPath $failedPrevious) {
                    throw "Failed previous-runtime preservation path already exists."
                }
                Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
                Assert-DawnstrikeNoReparsePath $failedPrevious "Failed previous runtime" -AllowMissingLeaf
                [System.IO.Directory]::Move($runtime, $failedPrevious)
                Assert-DawnstrikeNoReparsePath $failedPrevious "Failed previous runtime"
            }
            if (
                $candidateMoved -and
                -not (Test-Path -LiteralPath $runtime) -and
                (Test-Path -LiteralPath $deactivatedCandidate -PathType Container)
            ) {
                Assert-DawnstrikeNoReparsePath $deactivatedCandidate "Deactivated candidate"
                Assert-DawnstrikeNoReparsePath $runtime "Runtime root" -AllowMissingLeaf
                [System.IO.Directory]::Move($deactivatedCandidate, $runtime)
                Assert-DawnstrikeNoReparsePath $runtime "Runtime root"
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
            throw "Runtime rollback failed and automatic candidate restore could not be completed; canonical tasks are proven Disabled and all rollback artifacts must be preserved."
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
