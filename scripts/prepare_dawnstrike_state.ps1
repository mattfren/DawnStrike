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
. (Join-Path $CandidateRoot "scripts\activate_dawnstrike_runtime.ps1")
. (Join-Path $CandidateRoot "scripts\dawnstrike_job_process.ps1")
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
$null = Get-DawnstrikeStatePreparationDeclaration $candidate
$canonical = Get-DawnstrikeTaskContract $runtime $state
$auxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
if ($auxiliary.present -and $auxiliary.state -ne "Disabled") {
    throw "Auxiliary capture task must be Disabled before state preparation."
}
$lockRoot = Join-Path $state "locks"
if (Test-Path -LiteralPath $lockRoot -PathType Container) {
    $locks = @(Get-ChildItem -LiteralPath $lockRoot -File -Force)
    if ($locks.Count -gt 0) { throw "State preparation requires no active locks." }
}
$proofRoot = Join-Path $state "receipts\state-preparation"
New-Item -ItemType Directory -Path $proofRoot -Force | Out-Null
$proofPath = Join-Path $proofRoot ("task-proof-" + $CandidateSha + ".json")
$proof = [ordered]@{
    schema_version = "dawnstrike.state_preparation_task_proof.v1"
    task_count = [int]$canonical.task_count
    canonical_running_count = 0
    canonical_enabled_count = [int]$canonical.enabled_count
    capture_present = [bool]$auxiliary.present
    capture_running = $false
    capture_state = if ($auxiliary.present) { [string]$auxiliary.state } else { "ABSENT" }
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
    "--task-proof", $proofPath, "--retention", $Retention
) $candidate "Governed state preparation" $ProcessTimeoutSeconds
Write-Output ([string]$result.Stdout).Trim()
