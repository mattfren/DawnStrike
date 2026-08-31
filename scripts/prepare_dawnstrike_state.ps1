[CmdletBinding()]
param(
    [string]$CandidateRoot = "",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "C:\r\dawnstrike-state-backups",
    [ValidatePattern('^[0-9a-f]{40}$')][string]$CandidateSha = "",
    [ValidateRange(1, 120)][int]$Retention = 5,
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($CandidateRoot)) {
    $CandidateRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}
$statePreparationCandidateRoot = $CandidateRoot
$statePreparationRuntimeRoot = $RuntimeRoot
$statePreparationStateRoot = $StateRoot
$statePreparationBackupRoot = $BackupRoot
$statePreparationTimeout = $ProcessTimeoutSeconds
. (Join-Path $CandidateRoot "scripts\activate_dawnstrike_runtime.ps1")
. (Join-Path $statePreparationCandidateRoot "scripts\dawnstrike_job_process.ps1")
$CandidateRoot = $statePreparationCandidateRoot
$RuntimeRoot = $statePreparationRuntimeRoot
$StateRoot = $statePreparationStateRoot
$BackupRoot = $statePreparationBackupRoot
$ProcessTimeoutSeconds = $statePreparationTimeout
$candidate = Resolve-DawnstrikeActivationRoot $CandidateRoot "CandidateRoot"
$runtime = Resolve-DawnstrikeActivationRoot $RuntimeRoot "RuntimeRoot"
$state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
Assert-DawnstrikeRootIsolation $BackupRoot @($candidate, $runtime, $state) "BackupRoot"
$git = @(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0].Source
$python = @(Get-Command py.exe -CommandType Application -ErrorAction Stop)[0].Source
$candidateContract = Get-DawnstrikeGitContract $git $candidate $ProcessTimeoutSeconds
if ([string]::IsNullOrWhiteSpace($CandidateSha)) { $CandidateSha = [string]$candidateContract.head }
if ($candidateContract.head -ne $CandidateSha) { throw "Candidate checkout is not the exact requested SHA." }
$origin = Get-DawnstrikeGitValue $git $candidate @("remote", "get-url", "origin") "State-preparation origin verification" $ProcessTimeoutSeconds
Assert-DawnstrikeSafeOrigin $origin
$remoteMain = Get-DawnstrikeGitValue $git $candidate @("rev-parse", "refs/remotes/origin/main") "State-preparation origin/main verification" $ProcessTimeoutSeconds
if ($remoteMain.ToLowerInvariant() -ne $CandidateSha) { throw "Candidate is not the exact clean origin/main SHA." }
$stateDeclaration = Get-DawnstrikeStatePreparationDeclaration `
    -CandidateRoot $candidate `
    -GitPath $git `
    -CandidateSha $candidateContract.head `
    -CandidateTree $candidateContract.tree `
    -PythonPath $python `
    -TimeoutSeconds $ProcessTimeoutSeconds
$null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
    -GitPath $git `
    -CandidateRoot $candidate `
    -CandidateSha $candidateContract.head `
    -CandidateTree $candidateContract.tree `
    -Declaration $stateDeclaration `
    -TimeoutSeconds $ProcessTimeoutSeconds
$canonical = Get-DawnstrikeTaskContract $runtime $state
$canonicalBeforePreparation = $canonical
$auxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
$auxiliaryBeforeStatePreparation = $auxiliary
$restoreAuxiliary = $auxiliary.present -and $auxiliary.state -eq "Ready"
$preparationLock = Enter-DawnstrikeRuntimeActivationLock $state
try {
    # Recheck both task inventory and all pre-existing locks after acquiring
    # the atomic preparation lock.  A normal daily stage must not enter after
    # the initial read and race the online backup/migration window.
    $canonical = Get-DawnstrikeTaskContract $runtime $state
    $auxiliaryLocked = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if (
        $canonical.task_contract_sha256 -ne $canonicalBeforePreparation.task_contract_sha256 -or
        $auxiliaryLocked.present -ne $auxiliaryBeforeStatePreparation.present -or
        ($auxiliaryLocked.present -and (
            $auxiliaryLocked.state -ne $auxiliaryBeforeStatePreparation.state -or
            $auxiliaryLocked.xml_sha256 -ne $auxiliaryBeforeStatePreparation.xml_sha256 -or
            $auxiliaryLocked.action_contract_sha256 -ne $auxiliaryBeforeStatePreparation.action_contract_sha256
        ))
    ) { throw "Task inventory changed while acquiring the state-preparation lock." }
    $auxiliary = $auxiliaryLocked
    if ($restoreAuxiliary) {
        # State preparation owns a short, receipt-bound quiescence window.  The
        # exact pre-procedure XML and enablement are restored before returning,
        # so activation can still capture the original Ready baseline.
        $auxiliary = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
    }
    if ($auxiliary.present -and $auxiliary.state -ne "Disabled") {
        throw "Auxiliary capture task must be Disabled before state preparation."
    }
$lockRoot = Join-Path $state "locks"
if (Test-Path -LiteralPath $lockRoot -PathType Container) {
    $locks = @(Get-ChildItem -LiteralPath $lockRoot -File -Force | Where-Object { $_.FullName -ne $preparationLock.path })
    if ($locks.Count -gt 0) { throw "State preparation requires no active locks." }
}
$proofRoot = Join-Path $state "receipts\state-preparation"
Assert-DawnstrikeNoReparseComponents $proofRoot "State-preparation receipt root"
New-Item -ItemType Directory -Path $proofRoot -Force | Out-Null
$proofRoot = Resolve-DawnstrikeActivationRoot $proofRoot "State-preparation receipt root"
$proofPath = Join-Path $proofRoot ("task-proof-" + $CandidateSha + ".json")
Assert-DawnstrikeNoReparseComponents $proofPath "State-preparation task proof"
$proof = [ordered]@{
    schema_version = "dawnstrike.state_preparation_task_proof.v1"
    candidate_sha = $CandidateSha
    candidate_tree = [string]$candidateContract.tree
    task_count = [int]$canonical.task_count
    canonical_running_count = 0
    canonical_enabled_count = [int]$canonical.enabled_count
    capture_present = [bool]$auxiliary.present
    capture_running = $false
    capture_state = if ($auxiliary.present) { [string]$auxiliary.state } else { "ABSENT" }
    capture_action = if ($auxiliary.present) { "DISABLED_UNTIL_EXACT_SHA_REBIND" } else { "ABSENT_ALLOWED" }
    capture_xml_sha256 = [string]$auxiliary.xml_sha256
    capture_action_contract_sha256 = [string]$auxiliary.action_contract_sha256
    research_only = $true
    broker_execution_enabled = $false
}
Write-DawnstrikeActivationJson $proof $proofPath
$tool = Join-Path $candidate "scripts\prepare_dawnstrike_state.py"
if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "State-preparation Python tool is missing." }
$db = Join-Path $state "shadow_real.sqlite"
$result = Invoke-DawnstrikeActivationProcess $python @(
    $tool, "--db-path", $db, "--state-root", $state, "--backup-root", $BackupRoot,
    "--candidate-sha", $CandidateSha, "--candidate-tree", $candidateContract.tree,
    "--task-proof", $proofPath, "--preparation-lock", $preparationLock.path, "--retention", $Retention
) $candidate "Governed state preparation" $ProcessTimeoutSeconds
    if ($restoreAuxiliary) {
        $restoredAuxiliary = Restore-DawnstrikeAuxiliaryCaptureTask `
            -Expected $auxiliaryBeforeStatePreparation `
            -RuntimeRoot $runtime -StateRoot $state
        if (
            $restoredAuxiliary.state -ne "Ready" -or
            $restoredAuxiliary.xml_sha256 -ne [string]$auxiliaryBeforeStatePreparation.xml_sha256 -or
            $restoredAuxiliary.action_contract_sha256 -ne [string]$auxiliaryBeforeStatePreparation.action_contract_sha256 -or
            $restoredAuxiliary.definition_contract_sha256 -ne [string]$auxiliaryBeforeStatePreparation.definition_contract_sha256
        ) {
            throw "Auxiliary capture task was not restored to its original Ready enablement."
        }
    }
Write-Output ([string]$result.Stdout).Trim()
}
catch {
    if ($restoreAuxiliary) {
        try {
            $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                -Expected $auxiliaryBeforeStatePreparation `
                -RuntimeRoot $runtime -StateRoot $state
        }
        catch {
            throw "State preparation failed and original auxiliary capture recovery could not be proven."
        }
    }
    throw
}
finally {
    Exit-DawnstrikeRuntimeActivationLock $preparationLock
    if (Test-Path -LiteralPath $preparationLock.path -PathType Leaf) {
        throw "State-preparation lock could not be released; operator recovery is required."
    }
}
