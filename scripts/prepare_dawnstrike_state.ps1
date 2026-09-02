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
$statePreparationCandidateRoot = [System.IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
$statePreparationToolRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..")).TrimEnd('\')
$statePreparationRuntimeRoot = $RuntimeRoot
$statePreparationStateRoot = $StateRoot
$statePreparationBackupRoot = $BackupRoot
$statePreparationTimeout = $ProcessTimeoutSeconds

# This script can be handed a caller-supplied CandidateRoot.  Do not load any
# code from that path until the checkout itself has been authenticated.  The
# bootstrap is deliberately self-contained: it uses only the pinned absolute
# Git/Python identities and raw Git plumbing, so a hostile candidate cannot
# replace the validators that establish its own admission.
function Assert-DawnstrikeStatePreparationBootstrapSource {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Root)

    $bootstrapGit = 'C:\Program Files\Git\cmd\git.exe'
    $bootstrapGitSha256 = '37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9'
    $bootstrapGitSubject = 'CN=Johannes Schindelin, O=Johannes Schindelin, S=Nordrhein-Westfalen, C=DE'
    $bootstrapGitThumbprint = '3EB14A3AEF84B7153E139397F0A49E2FAC662B0E'
    $bootstrapPython = 'C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe'
    $bootstrapPythonSha256 = 'ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1'
    $bootstrapPythonSubject = 'CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US'
    $bootstrapPythonThumbprint = '9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48'

    foreach ($executable in @(
        [pscustomobject]@{ path = $bootstrapGit; hash = $bootstrapGitSha256; subject = $bootstrapGitSubject; thumbprint = $bootstrapGitThumbprint; label = 'Git' },
        [pscustomobject]@{ path = $bootstrapPython; hash = $bootstrapPythonSha256; subject = $bootstrapPythonSubject; thumbprint = $bootstrapPythonThumbprint; label = 'Python' }
    )) {
        $cursor = [System.IO.Path]::GetFullPath([string]$executable.path)
        while ($true) {
            $item = Get-Item -LiteralPath $cursor -Force -ErrorAction Stop
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "State-preparation bootstrap $($executable.label) path contains a reparse point."
            }
            $parent = Split-Path -Parent $cursor
            if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) { break }
            $cursor = $parent
        }
        if (-not (Test-Path -LiteralPath $executable.path -PathType Leaf) -or
            (Get-FileHash -LiteralPath $executable.path -Algorithm SHA256).Hash.ToLowerInvariant() -cne [string]$executable.hash) {
            throw "State-preparation bootstrap $($executable.label) identity changed."
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $executable.path -ErrorAction Stop
        if ([string]$signature.Status -cne 'Valid' -or $null -eq $signature.SignerCertificate -or
            [string]$signature.SignerCertificate.Subject -cne [string]$executable.subject -or
            [string]$signature.SignerCertificate.Thumbprint -cne [string]$executable.thumbprint) {
            throw "State-preparation bootstrap $($executable.label) signer changed."
        }
    }

    $expectedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $rootItem = Get-Item -LiteralPath $expectedRoot -Force -ErrorAction Stop
    if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "State-preparation bootstrap ToolRoot is not a safe directory."
    }
    $cursor = $rootItem
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "State-preparation bootstrap ToolRoot contains a reparse point."
        }
        $cursor = $cursor.Parent
    }
    $savedGitEnvironment = @{}
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        $savedGitEnvironment[$entry.Name] = $entry.Value
        Remove-Item -LiteralPath ("Env:" + $entry.Name) -ErrorAction Stop
    }
    function Invoke-StatePreparationBootstrapGit {
        param([Parameter(Mandatory = $true)][string[]]$Arguments)
        $safeConfiguration = @(
            '-c', 'core.fsmonitor=false',
            '-c', 'core.hooksPath=NUL',
            '-c', 'protocol.ext.allow=never',
            '-c', 'submodule.recurse=false'
        )
        $output = & $bootstrapGit @safeConfiguration -C $expectedRoot @Arguments 2>&1
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) { throw "State-preparation bootstrap Git command failed." }
        return ((@($output) | ForEach-Object { [string]$_ }) -join "`n").Trim()
    }
    try {
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_CONFIG_GLOBAL = 'NUL'
        $top = [System.IO.Path]::GetFullPath((Invoke-StatePreparationBootstrapGit @('rev-parse', '--show-toplevel'))).TrimEnd('\')
        if (-not [string]::Equals($top, $expectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "State-preparation bootstrap root is not the exact Git worktree root."
        }
        $origin = Invoke-StatePreparationBootstrapGit @('config', '--local', '--get', 'remote.origin.url')
        $governedOrigin = $origin -match '^(?:https://github\.com/mattfren/DawnStrike\.git|git@github\.com:mattfren/DawnStrike\.git|ssh://git@github\.com/mattfren/DawnStrike\.git)$'
        if (-not $governedOrigin) {
            throw "State-preparation bootstrap origin is not the governed repository."
        }
        $null = Invoke-StatePreparationBootstrapGit @(
            'fetch', '--quiet', '--prune', 'origin',
            '+refs/heads/main:refs/remotes/origin/main'
        )
        $headBefore = (Invoke-StatePreparationBootstrapGit @('rev-parse', 'HEAD')).ToLowerInvariant()
        $treeBefore = (Invoke-StatePreparationBootstrapGit @('rev-parse', 'HEAD^{tree}')).ToLowerInvariant()
        $originMainBefore = (Invoke-StatePreparationBootstrapGit @('rev-parse', 'refs/remotes/origin/main')).ToLowerInvariant()
        if ($headBefore -notmatch '^[0-9a-f]{40}$' -or $treeBefore -notmatch '^[0-9a-f]{40}$' -or
            $originMainBefore -notmatch '^[0-9a-f]{40}$' -or $headBefore -cne $originMainBefore) {
            throw "State-preparation bootstrap requires exact origin/main HEAD and tree identity."
        }
        $status = Invoke-StatePreparationBootstrapGit @('status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')
        if (-not [string]::IsNullOrWhiteSpace($status)) {
            throw "State-preparation bootstrap requires a clean ToolRoot worktree."
        }
        # The module bytes must be bound without allowing a checkout-selected
        # clean/smudge/process filter (or an attributes file outside the index)
        # to rewrite what is executed.  A normal CRLF checkout is harmless and
        # is handled explicitly below; arbitrary filters are not.
        $localFilterOutput = & $bootstrapGit -c core.fsmonitor=false -c core.hooksPath=NUL -c protocol.ext.allow=never -c submodule.recurse=false -C $expectedRoot config --local --get-regexp '^filter\.' 2>&1
        $localFilterExit = $LASTEXITCODE
        if ($localFilterExit -eq 0 -and -not [string]::IsNullOrWhiteSpace(((@($localFilterOutput) | ForEach-Object { [string]$_ }) -join "`n"))) {
            throw "State-preparation bootstrap refuses configured Git filters."
        }
        $trackedAttributesText = Invoke-StatePreparationBootstrapGit @(
            'ls-files', '--', '*.gitattributes', '.gitattributes'
        )
        $trackedAttributes = @(([string]$trackedAttributesText).Split(
            [char]10, [System.StringSplitOptions]::RemoveEmptyEntries
        ))
        if ($trackedAttributes.Count -ne 1 -or $trackedAttributes[0] -cne '.gitattributes') {
            throw "State-preparation bootstrap requires one governed root .gitattributes file."
        }
        $attributesPath = Join-Path $expectedRoot '.gitattributes'
        $attributesBlob = (Invoke-StatePreparationBootstrapGit @(
            'rev-parse', ($headBefore + ':.gitattributes')
        )).ToLowerInvariant()
        $attributesWorking = (Invoke-StatePreparationBootstrapGit @(
            '-c', 'core.autocrlf=true', 'hash-object', '--path=.gitattributes', '--', $attributesPath
        )).ToLowerInvariant()
        if ($attributesBlob -notmatch '^[0-9a-f]{40}$' -or $attributesWorking -cne $attributesBlob) {
            throw "State-preparation bootstrap .gitattributes is not blob-bound to ToolRoot HEAD."
        }
        $attributesText = [IO.File]::ReadAllText($attributesPath)
        if ($attributesText -match '(?im)(?:^|\s)(?:filter|working-tree-encoding)\s*=') {
            throw "State-preparation bootstrap refuses executable Git attribute transforms."
        }
        $infoAttributes = Join-Path $expectedRoot '.git\info\attributes'
        if (Test-Path -LiteralPath $infoAttributes -PathType Leaf) {
            throw "State-preparation bootstrap refuses repository-local Git attributes."
        }
        $ignored = Invoke-StatePreparationBootstrapGit @('ls-files', '--others', '--ignored', '--exclude-standard', '-z')
        $forbidden = @(
            ([string]$ignored).Split([char]0, [System.StringSplitOptions]::RemoveEmptyEntries) |
                Where-Object {
                    $name = [System.IO.Path]::GetFileName($_).ToLowerInvariant()
                    $extension = [System.IO.Path]::GetExtension($_).ToLowerInvariant()
                    $extension -in @('.ps1', '.psm1', '.py', '.pyc', '.pyd', '.dll', '.exe', '.com', '.bat', '.cmd', '.sh', '.pth') -or
                        $name -in @('sitecustomize.py', 'usercustomize.py')
                }
        )
        if ($forbidden.Count -gt 0) { throw "State-preparation bootstrap found ignored executable/startup artifacts." }

        $moduleBlobs = [ordered]@{}
        foreach ($relative in @(
            'scripts/activate_dawnstrike_runtime.ps1',
            'scripts/runtime_activation_lock.ps1',
            'scripts/runtime_operation_journal.py',
            'scripts/state_preparation.py',
            'scripts/state_disaster_recovery.py',
            'scripts/capture_task_safety.ps1',
            'scripts/dawnstrike_job_process.ps1',
            'scripts/invoke_dawnstrike_stage.ps1'
        )) {
            $blob = (Invoke-StatePreparationBootstrapGit @('rev-parse', ("{0}:{1}" -f $headBefore, $relative))).ToLowerInvariant()
            # The fixture and approved Windows checkout use ordinary CRLF
            # normalization.  Pin that normalization instead of inheriting a
            # machine/global setting; no external filter is permitted above.
            $working = (Invoke-StatePreparationBootstrapGit @('-c', 'core.autocrlf=true', 'hash-object', ("--path={0}" -f $relative), '--', (Join-Path $expectedRoot ($relative -replace '/', '\')))).ToLowerInvariant()
            if ($blob -notmatch '^[0-9a-f]{40}$' -or $working -cne $blob) {
                throw "State-preparation bootstrap recovery module is not blob-bound to ToolRoot HEAD: $relative"
            }
            $moduleBlobs[$relative] = $blob
        }
        $headAfter = (Invoke-StatePreparationBootstrapGit @('rev-parse', 'HEAD')).ToLowerInvariant()
        $treeAfter = (Invoke-StatePreparationBootstrapGit @('rev-parse', 'HEAD^{tree}')).ToLowerInvariant()
        $originMainAfter = (Invoke-StatePreparationBootstrapGit @('rev-parse', 'refs/remotes/origin/main')).ToLowerInvariant()
        if ($headAfter -cne $headBefore -or $treeAfter -cne $treeBefore -or $originMainAfter -cne $originMainBefore) {
            throw "State-preparation bootstrap ToolRoot changed during validation."
        }
        return [pscustomobject]@{
            head = $headAfter
            tree = $treeAfter
            origin = $originMainAfter
            module_blobs = $moduleBlobs
            git = [pscustomobject]@{ path = $bootstrapGit; sha256 = $bootstrapGitSha256 }
            python = [pscustomobject]@{ path = $bootstrapPython; sha256 = $bootstrapPythonSha256 }
        }
    }
    finally {
        Remove-Item Env:GIT_CONFIG_NOSYSTEM -ErrorAction SilentlyContinue
        Remove-Item Env:GIT_CONFIG_GLOBAL -ErrorAction SilentlyContinue
        foreach ($entry in $savedGitEnvironment.GetEnumerator()) {
            Set-Item -LiteralPath ("Env:" + $entry.Key) -Value $entry.Value
        }
    }
}

if (-not [string]::Equals(
    $statePreparationCandidateRoot,
    $statePreparationToolRoot,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw "State-preparation CandidateRoot must be the exact trusted ToolRoot."
}
$statePreparationBootstrap = Assert-DawnstrikeStatePreparationBootstrapSource -Root $statePreparationToolRoot
# Only the authenticated ToolRoot is now allowed to provide PowerShell/Python
# helpers.  In particular, CandidateRoot is never dot-sourced as an
# unauthenticated caller path during recovery.
. (Join-Path $statePreparationToolRoot "scripts\activate_dawnstrike_runtime.ps1")
$statePreparationLockModule = New-Module -ScriptBlock {
    param([string]$Path)
    . $Path
} -ArgumentList (Join-Path $statePreparationToolRoot "scripts\runtime_activation_lock.ps1")
Import-Module $statePreparationLockModule -Force -DisableNameChecking
. (Join-Path $statePreparationToolRoot "scripts\dawnstrike_job_process.ps1")
. (Join-Path $statePreparationToolRoot "scripts\invoke_dawnstrike_stage.ps1")
$CandidateRoot = $statePreparationToolRoot
$statePreparationCandidateRoot = $statePreparationToolRoot
$RuntimeRoot = $statePreparationRuntimeRoot
$StateRoot = $statePreparationStateRoot
$BackupRoot = $statePreparationBackupRoot
$ProcessTimeoutSeconds = $statePreparationTimeout

function Get-DawnstrikeStatePreparationTaskBaseline {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256
    )
    Assert-DawnstrikeNoReparseComponents $Path "State-preparation task baseline"
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "State-preparation task baseline is missing."
    }
    if ((Get-DawnstrikeSha256File $Path) -ne $ExpectedSha256) {
        throw "State-preparation task baseline hash changed."
    }
    try { $value = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "State-preparation task baseline is invalid JSON." }
    if (
        [string]$value.schema_version -ne "dawnstrike.state_preparation_task_baseline.v1" -or
        [string]$value.candidate_sha -notmatch '^[0-9a-f]{40}$' -or
        [string]$value.candidate_tree -notmatch '^[0-9a-f]{40}$' -or
        [string]$value.task_name -ne [string]$script:DawnstrikeAuxiliaryCaptureTaskName -or
        [string]$value.task_path -eq "" -or
        [string]$value.state_before -notin @("Ready", "Disabled", "ABSENT") -or
        [string]$value.xml_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$value.action_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$value.definition_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$value.runtime_origin_identity -eq "" -or
        [string]$value.runtime_origin_sha256 -notmatch '^[0-9a-f]{64}$' -or
        $value.research_only -ne $true -or
        $value.broker_execution_enabled -ne $false
    ) { throw "State-preparation task baseline identity is invalid." }
    if ([string]$value.state_before -eq "ABSENT") {
        if ([string]$value.xml -ne "" -or [string]$value.task_path -ne "NONE") {
            throw "Absent state-preparation task baseline carries task bytes."
        }
    }
    else {
        if ([string]::IsNullOrWhiteSpace([string]$value.xml)) { throw "State-preparation task baseline XML is missing." }
        if ((Get-DawnstrikeSha256Text ([string]$value.xml)) -ne [string]$value.xml_sha256) {
            throw "State-preparation task baseline XML hash is invalid."
        }
    }
    return $value
}

function Assert-DawnstrikeStatePreparationTaskExact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Current,
        [Parameter(Mandatory = $true)][object]$Baseline,
        [switch]$RequireOriginalEnablement
    )
    if ([string]$Baseline.state_before -eq "ABSENT") {
        if ($Current.present) { throw "State-preparation recovery found an unexpected auxiliary task." }
        return
    }
    if (
        -not $Current.present -or
        [string]$Current.task_name -ne [string]$Baseline.task_name -or
        [string]$Current.task_path -ne [string]$Baseline.task_path -or
        [string]$Current.xml_sha256 -ne [string]$Baseline.xml_sha256 -or
        [string]$Current.action_contract_sha256 -ne [string]$Baseline.action_contract_sha256 -or
        [string]$Current.definition_contract_sha256 -ne [string]$Baseline.definition_contract_sha256 -or
        ($RequireOriginalEnablement -and [string]$Current.state -ne [string]$Baseline.state_before)
    ) { throw "State-preparation auxiliary task is not the exact sealed original." }
}

function Assert-DawnstrikeStatePreparationRuntimeExact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$ExpectedHead,
        [Parameter(Mandatory = $true)][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][string]$ExpectedOriginIdentity,
        [Parameter(Mandatory = $true)][string]$ExpectedOriginSha256
    )
    $contract = Get-DawnstrikeGitContract $GitPath $RuntimeRoot $TimeoutSeconds
    if ($contract.head -cne $ExpectedHead -or $contract.tree -cne $ExpectedTree) {
        throw "State-preparation locked runtime HEAD/tree changed."
    }
    $origin = Get-DawnstrikeGitValue $GitPath $RuntimeRoot @("remote", "get-url", "origin") `
        "State-preparation locked runtime origin verification" $TimeoutSeconds
    Assert-DawnstrikeSafeOrigin $origin
    if ((Convert-DawnstrikeCanonicalOriginIdentity $origin) -cne $ExpectedOriginIdentity -or
        (Get-DawnstrikeSha256Text $origin) -cne $ExpectedOriginSha256) {
        throw "State-preparation locked runtime origin changed."
    }
    return [pscustomobject]@{ contract = $contract; origin = $origin }
}

function Assert-DawnstrikeStatePreparationTaskProofExact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][object]$Canonical,
        [Parameter(Mandatory = $true)][object]$Baseline
    )
    Assert-DawnstrikeNoReparseComponents $Path "State-preparation task proof"
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "State-preparation task proof is missing."
    }
    try { $actual = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "State-preparation task proof is invalid JSON." }
    $expected = [ordered]@{
        schema_version = "dawnstrike.state_preparation_task_proof.v1"
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        task_count = [int]$Canonical.task_count
        canonical_running_count = 0
        canonical_enabled_count = [int]$Canonical.enabled_count
        capture_present = ([string]$Baseline.state_before -ne "ABSENT")
        capture_running = $false
        capture_state = if ([string]$Baseline.state_before -eq "ABSENT") { "ABSENT" } else { "Disabled" }
        capture_action = if ([string]$Baseline.state_before -eq "ABSENT") { "ABSENT_ALLOWED" } else { "DISABLED_UNTIL_EXACT_SHA_REBIND" }
        capture_xml_sha256 = [string]$Baseline.xml_sha256
        capture_action_contract_sha256 = [string]$Baseline.action_contract_sha256
        research_only = $true
        broker_execution_enabled = $false
    }
    $expectedNames = @($expected.Keys | ForEach-Object { [string]$_ })
    $actualNames = @($actual.psobject.Properties.Name | ForEach-Object { [string]$_ })
    if ($actualNames.Count -ne $expectedNames.Count -or
        (@(Compare-Object -ReferenceObject $expectedNames -DifferenceObject $actualNames).Count -ne 0)) {
        throw "State-preparation task proof fields are not exact."
    }
    foreach ($field in $expectedNames) {
        if ([string]$actual.$field -ne [string]$expected[$field]) {
            throw "State-preparation task proof is not bound to the exact task baseline."
        }
    }
    return $actual
}

function Assert-DawnstrikeStatePreparationCompleteTerminal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
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
        [Parameter(Mandatory = $true)][object]$Baseline,
        [Parameter(Mandatory = $true)][object]$Canonical,
        [switch]$AllowPrepared
    )

    if ([string]$Journal.payload.operation -ne "state_preparation" -or
        ([string]$Journal.payload.phase -ne "COMPLETE" -and -not $AllowPrepared) -or
        [string]$Journal.payload.candidate_sha -ne $CandidateSha -or
        [string]$Journal.payload.candidate_tree -ne $CandidateTree) {
        throw "State-preparation COMPLETE journal identity is not exact."
    }
    Assert-DawnstrikeNoReparseComponents $ReceiptPath "State-preparation COMPLETE receipt"
    $receiptFileHash = if (Test-Path -LiteralPath $ReceiptPath -PathType Leaf) { Get-DawnstrikeSha256File $ReceiptPath } else { "" }
    if (-not $receiptFileHash -or
        (-not $AllowPrepared -and (
            [string]$Journal.payload.complete_receipt_relative_path -eq "NONE" -or
            [string]$Journal.payload.complete_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
            $receiptFileHash -ne [string]$Journal.payload.complete_receipt_sha256
        ))) {
        throw "State-preparation COMPLETE receipt bytes are not journal-bound."
    }
    $expectedHead = [string]$Journal.payload.current_sha
    $expectedTree = [string]$Journal.payload.current_tree
    $expectedOrigin = [string]$Journal.payload.origin_identity
    if ($expectedHead -notmatch '^[0-9a-f]{40}$' -or $expectedTree -notmatch '^[0-9a-f]{40}$' -or [string]::IsNullOrWhiteSpace($expectedOrigin)) {
        throw "State-preparation COMPLETE runtime identity is incomplete."
    }
    $originSha = [string]$Baseline.runtime_origin_sha256
    if ($originSha -notmatch '^[0-9a-f]{64}$' -or [string]$Baseline.runtime_origin_identity -cne $expectedOrigin) {
        throw "State-preparation COMPLETE baseline origin identity is not sealed."
    }
    $null = Assert-DawnstrikeStatePreparationRuntimeExact `
        -GitPath $GitPath -RuntimeRoot $RuntimeRoot -TimeoutSeconds $TimeoutSeconds `
        -ExpectedHead $expectedHead -ExpectedTree $expectedTree `
        -ExpectedOriginIdentity $expectedOrigin -ExpectedOriginSha256 $originSha
    $liveCanonical = Get-DawnstrikeTaskContract $RuntimeRoot $StateRoot
    if ([string]$liveCanonical.task_contract_sha256 -cne [string]$Journal.payload.task_contract_sha256 -or
        [string]$liveCanonical.task_contract_sha256 -cne [string]$Canonical.task_contract_sha256) {
        throw "State-preparation COMPLETE canonical task contract changed."
    }
    $liveAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    Assert-DawnstrikeStatePreparationTaskExact -Current $liveAuxiliary -Baseline $Baseline -RequireOriginalEnablement
    $proof = Get-DawnstrikeStatePreparationProof `
        -CandidateRoot $CandidateRoot -StateRoot $StateRoot -BackupRoot $BackupRoot `
        -CandidateSha $CandidateSha -CandidateTree $CandidateTree `
        -PythonPath $PythonPath -TimeoutSeconds $TimeoutSeconds
    $emptyJournalHash = Get-DawnstrikeSha256Text ""
    $journalBackupHash = [string]$Journal.payload.backup_contract_sha256
    $receiptBackupHash = [string]$proof.receipt.backup_manifest_sha256
    if ([string]$proof.receipt_file_sha256 -cne $receiptFileHash -or
        [string]$proof.receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$proof.receipt.candidate_sha -cne $CandidateSha -or
        [string]$proof.receipt.candidate_tree -cne $CandidateTree -or
        $receiptBackupHash -notmatch '^[0-9a-f]{64}$' -or
        $receiptBackupHash -cne [string]$proof.backup_manifest_sha256 -or
        (($AllowPrepared -and $journalBackupHash -cne $emptyJournalHash) -or
         (-not $AllowPrepared -and $journalBackupHash -cne [string]$proof.backup_manifest_sha256))) {
        throw "State-preparation COMPLETE receipt/backup proof is not exact."
    }
    return $proof
}

function Move-DawnstrikeStatePreparationAttemptArtifact {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        if (Test-Path -LiteralPath $Destination) {
            Assert-DawnstrikeNoReparseComponents $Destination "$Label archive"
            return $true
        }
        return $false
    }
    Assert-DawnstrikeNoReparseComponents $Path $Label
    Assert-DawnstrikeNoReparseComponents $Destination "$Label archive"
    if (Test-Path -LiteralPath $Destination) { throw "$Label archive destination already exists." }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    Assert-DawnstrikeNoReparseComponents $Path $Label
    Assert-DawnstrikeNoReparseComponents $Destination "$Label archive"
    Move-Item -LiteralPath $Path -Destination $Destination -Force
    if (Test-Path -LiteralPath $Path) { throw "$Label archive was not proven." }
    if (-not (Test-Path -LiteralPath $Destination)) { throw "$Label archive is missing." }
    return $true
}

function Archive-DawnstrikeStatePreparationAttempt {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$JournalHash,
        [Parameter(Mandatory = $true)][string]$ProofPath,
        [Parameter(Mandatory = $true)][string]$BaselinePath,
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$BackupBundlePath
    )
    if ($JournalHash -notmatch '^[0-9a-f]{64}$') { throw "State-preparation journal hash is invalid." }
    $archiveRoot = Join-Path $StateRoot ("receipts\state-preparation\archive\attempt-" + $CandidateSha + "-" + $JournalHash)
    Assert-DawnstrikeNoReparseComponents $archiveRoot "State-preparation attempt archive"
    New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
    Assert-DawnstrikeNoReparseComponents $archiveRoot "State-preparation attempt archive"
    $null = Move-DawnstrikeStatePreparationAttemptArtifact $ProofPath (Join-Path $archiveRoot "task-proof.json") "State-preparation task proof"
    $null = Move-DawnstrikeStatePreparationAttemptArtifact $BaselinePath (Join-Path $archiveRoot "task-baseline.json") "State-preparation task baseline"
    $null = Move-DawnstrikeStatePreparationAttemptArtifact $ReceiptPath (Join-Path $archiveRoot "receipt.json") "State-preparation receipt"
    if (-not [string]::IsNullOrWhiteSpace($BackupBundlePath)) {
        $backupArchiveRoot = Join-Path $BackupRoot ("archive\attempt-" + $CandidateSha + "-" + $JournalHash)
        $null = Move-DawnstrikeStatePreparationAttemptArtifact $BackupBundlePath $backupArchiveRoot "State-preparation backup bundle"
    }
    return $archiveRoot
}

function Archive-DawnstrikeStatePreparationJournal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$JournalPath,
        [Parameter(Mandatory = $true)][string]$JournalHash
    )
    if ($JournalHash -notmatch '^[0-9a-f]{64}$') { throw "State-preparation journal hash is invalid." }
    Assert-DawnstrikeNoReparseComponents $JournalPath "State-preparation operation journal"
    $archiveRoot = Join-Path $StateRoot "receipts\runtime-operation\archive"
    $archive = Join-Path $archiveRoot ("state-preparation-" + $JournalHash + ".json")
    Assert-DawnstrikeNoReparseComponents $archiveRoot "State-preparation journal archive root"
    Assert-DawnstrikeNoReparseComponents $archive "State-preparation journal archive"
    if (Test-Path -LiteralPath $JournalPath -PathType Leaf) {
        if (Test-Path -LiteralPath $archive) { throw "State-preparation journal archive already exists." }
        New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
        Assert-DawnstrikeNoReparseComponents $JournalPath "State-preparation operation journal"
        Assert-DawnstrikeNoReparseComponents $archiveRoot "State-preparation journal archive root"
        Assert-DawnstrikeNoReparseComponents $archive "State-preparation journal archive"
        [IO.File]::Move($JournalPath, $archive)
        if (Test-Path -LiteralPath $JournalPath) { throw "State-preparation journal archive was not proven." }
    }
    if (-not (Test-Path -LiteralPath $archive -PathType Leaf)) { throw "State-preparation journal archive is missing." }
    if ((Get-DawnstrikeSha256File $archive) -ne $JournalHash) { throw "State-preparation journal archive hash was not proven." }
    return $archive
}

function Write-DawnstrikeStatePreparationCompensation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][string]$JournalPath,
        [Parameter(Mandatory = $true)][object]$Lock,
        [Parameter(Mandatory = $true)][object]$CurrentTask,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$OriginIdentity,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$PythonSha256,
        [Parameter(Mandatory = $true)][string]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$FailureType
    )
    $empty = Get-DawnstrikeSha256Text ""
    $attemptHash = [string]$Journal.raw_file_sha256
    $relative = "receipts/state-preparation/state-preparation-$CandidateSha.compensated-$attemptHash.json"
    $path = Join-Path $StateRoot ($relative.Replace('/', '\'))
    Assert-DawnstrikeNoReparseComponents $path "State-preparation compensation receipt"
    $input = "$path.$([guid]::NewGuid().ToString('N')).input.json"
    $payload = [ordered]@{
        schema_version = "dawnstrike.runtime_compensation_receipt.v1"
        status = "COMPENSATED"
        operation = "state_preparation"
        candidate_sha = $CandidateSha
        candidate_tree = $CandidateTree
        prior_journal_file_sha256 = $attemptHash
        task_contract_sha256 = [string]$Journal.payload.task_contract_sha256
        task_state = if ($CurrentTask.present) { [string]$CurrentTask.state } else { "ABSENT" }
        task_xml_sha256 = [string]$CurrentTask.xml_sha256
        task_action_contract_sha256 = [string]$CurrentTask.action_contract_sha256
        task_definition_contract_sha256 = [string]$CurrentTask.definition_contract_sha256
        prior_receipt_relative_path = "NONE"
        prior_receipt_sha256 = $empty
        failure_type = $FailureType
        research_only = $true
        broker_execution_enabled = $false
    }
    Write-DawnstrikeActivationJson $payload $input
    try {
        & $PythonPath -I -B -S (Join-Path $PSScriptRoot "runtime_operation_journal.py") seal-compensation `
            --input $input --output $path --state-root $StateRoot --reuse-existing 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "State-preparation compensation receipt strict sealing failed." }
    }
    finally { if (Test-Path -LiteralPath $input) { Remove-Item -LiteralPath $input -Force } }
    $hash = Get-DawnstrikeSha256File $path
    $transition = @{
        StateRoot = $StateRoot; JournalPath = $JournalPath; Lock = $Lock
        Operation = "state_preparation"; Phase = "COMPENSATED"
        CandidateSha = $CandidateSha; CandidateTree = $CandidateTree
        CurrentSha = [string]$Journal.payload.current_sha; CurrentTree = [string]$Journal.payload.current_tree
        PreviousSha = [string]$Journal.payload.previous_sha; PreviousTree = [string]$Journal.payload.previous_tree
        OriginIdentity = $OriginIdentity
        PreparedReceiptRelativePath = [string]$Journal.payload.prepared_receipt_relative_path
        PreparedReceiptSha256 = [string]$Journal.payload.prepared_receipt_sha256
        CompleteReceiptRelativePath = [string]$Journal.payload.complete_receipt_relative_path
        CompleteReceiptSha256 = $empty
        BackupContractSha256 = [string]$Journal.payload.backup_contract_sha256
        TaskContractSha256 = [string]$Journal.payload.task_contract_sha256
        RuntimeStageContractSha256 = $empty
        CompensationReceiptRelativePath = $relative
        CompensationReceiptSha256 = $hash
        PythonPath = $PythonPath; PythonSha256 = $PythonSha256
    }
    $terminal = Set-DawnstrikeRuntimeOperationJournalPhase @transition
    return [pscustomobject]@{ journal = $terminal; path = $path; relative_path = $relative }
}

$candidate = Resolve-DawnstrikeActivationRoot $CandidateRoot "CandidateRoot"
$runtime = Resolve-DawnstrikeActivationRoot $RuntimeRoot "RuntimeRoot"
$state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
Assert-DawnstrikeRootIsolation $BackupRoot @($candidate, $runtime, $state) "BackupRoot"
$approvedGit = $statePreparationBootstrap.git
$approvedPython = $statePreparationBootstrap.python
$git = [string]$approvedGit.path
$python = [string]$approvedPython.path
$toolRootContract = Get-DawnstrikeGitContract $git $candidate $ProcessTimeoutSeconds
$candidateContract = $toolRootContract
$origin = Get-DawnstrikeGitValue $git $candidate @("remote", "get-url", "origin") "State-preparation origin verification" $ProcessTimeoutSeconds
Assert-DawnstrikeSafeOrigin $origin
$null = Invoke-DawnstrikeActivationProcess $git @(
    "-C", $candidate, "fetch", "--quiet", "--prune", "origin",
    "+refs/heads/main:refs/remotes/origin/main"
) $candidate "State-preparation origin/main refresh" $ProcessTimeoutSeconds
$remoteMain = Get-DawnstrikeGitValue $git $candidate @("rev-parse", "refs/remotes/origin/main") "State-preparation origin/main verification" $ProcessTimeoutSeconds
$remoteMain = $remoteMain.ToLowerInvariant()
$advancedOriginRecovery = $null
$requestedStatePreparationSha = $CandidateSha
# Recovery discovery is deliberately attempted before admitting a requested
# candidate.  With no caller identity, the global lock plus its one canonical
# journal select the stale transaction; the stale SHA/tree/origin/phase and
# receipt paths then come only from that sealed record.  A fresh invocation has
# no lock and continues through the ordinary origin/main gate below.
$discoveredStatePreparation = Get-DawnstrikeAdvancedOriginRecoveryAdmission `
    -StateRoot $state -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
if ($null -ne $discoveredStatePreparation) {
    if ([string]$discoveredStatePreparation.operation -ne "state_preparation") {
        throw "A different runtime operation owns the governed recovery lock; state preparation cannot admit a candidate."
    }
    if (-not [string]::IsNullOrWhiteSpace($requestedStatePreparationSha) -and
        [string]$discoveredStatePreparation.candidate_sha -cne $requestedStatePreparationSha) {
        throw "The requested state-preparation SHA does not match the sealed lock-bound recovery journal."
    }
    $CandidateSha = [string]$discoveredStatePreparation.candidate_sha
    $candidateContract = [pscustomobject]@{
        head = [string]$discoveredStatePreparation.candidate_sha
        tree = [string]$discoveredStatePreparation.candidate_tree
    }
    $staleTree = [string]$discoveredStatePreparation.candidate_tree
    $null = Get-DawnstrikeGitValue $git $candidate @(
        "rev-parse", ($CandidateSha + "^{tree}")
    ) "State-preparation stale candidate tree proof" $ProcessTimeoutSeconds
    $null = Invoke-DawnstrikeActivationProcess $git @(
        "-C", $candidate, "merge-base", "--is-ancestor", $CandidateSha, $remoteMain
    ) $candidate "State-preparation stale candidate ancestry proof" $ProcessTimeoutSeconds
    $advancedOriginRecovery = $discoveredStatePreparation
    $stateDeclaration = [pscustomobject]@{ required = $false; declaration_present = $false; declaration_blob_sha = ""; sidecar_contract = "" }
}
elseif ([string]::IsNullOrWhiteSpace($CandidateSha)) {
    $CandidateSha = $remoteMain
    if ($toolRootContract.head -ne $CandidateSha) {
        throw "State-preparation ToolRoot is not the exact requested origin/main SHA."
    }
    $stateDeclaration = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $candidate `
        -GitPath $git `
        -CandidateSha $toolRootContract.head `
        -CandidateTree $toolRootContract.tree `
        -PythonPath $python `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $git `
        -CandidateRoot $candidate `
        -CandidateSha $toolRootContract.head `
        -CandidateTree $toolRootContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds
}
elseif ($CandidateSha -eq $remoteMain) {
    if ($toolRootContract.head -ne $CandidateSha) {
        throw "State-preparation ToolRoot is not the exact requested origin/main SHA."
    }
    $stateDeclaration = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $candidate `
        -GitPath $git `
        -CandidateSha $toolRootContract.head `
        -CandidateTree $toolRootContract.tree `
        -PythonPath $python `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $git `
        -CandidateRoot $candidate `
        -CandidateSha $toolRootContract.head `
        -CandidateTree $toolRootContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds
}
else {
    # The requested SHA is an older transaction.  It is data only: recover it
    # with the already authenticated Y ToolRoot and never dot-source/execute
    # the stale X checkout.  Derive X's tree from its sealed canonical journal,
    # then prove the object exists and is an ancestor of current origin/main.
    if ($CandidateSha -notmatch '^[0-9a-f]{40}$') { throw "State-preparation recovery candidate SHA is invalid." }
    $recoveryJournalPath = Join-Path $state ("receipts\runtime-operation\state-preparation-" + $CandidateSha + ".json")
    Assert-DawnstrikeNoReparseComponents $recoveryJournalPath "State-preparation recovery journal"
    if (-not (Test-Path -LiteralPath $recoveryJournalPath -PathType Leaf)) {
        throw "Requested state-preparation SHA is not current origin/main and has no exact recovery journal."
    }
    $recoveryJournalProbe = Get-DawnstrikeStrictRuntimeOperationJournal `
        $recoveryJournalPath $approvedPython.path $approvedPython.sha256
    if ([string]$recoveryJournalProbe.payload.operation -ne "state_preparation" -or
        [string]$recoveryJournalProbe.payload.candidate_sha -ne $CandidateSha -or
        [string]$recoveryJournalProbe.payload.candidate_tree -notmatch '^[0-9a-f]{40}$') {
        throw "State-preparation recovery journal is not bound to the requested stale transaction."
    }
    $staleTree = (Get-DawnstrikeGitValue $git $candidate @(
        "rev-parse", ($CandidateSha + "^{tree}")
    ) "State-preparation stale candidate tree proof" $ProcessTimeoutSeconds).ToLowerInvariant()
    if ($staleTree -ne [string]$recoveryJournalProbe.payload.candidate_tree) {
        throw "State-preparation stale candidate tree does not match the sealed journal."
    }
    $null = Invoke-DawnstrikeActivationProcess $git @(
        "-C", $candidate, "merge-base", "--is-ancestor", $CandidateSha, $remoteMain
    ) $candidate "State-preparation stale candidate ancestry proof" $ProcessTimeoutSeconds
    $advancedOriginRecovery = Get-DawnstrikeAdvancedOriginRecoveryAdmission `
        -StateRoot $state -Operation state_preparation -CandidateSha $CandidateSha `
        -CandidateTree $staleTree -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) `
        -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
    if ($null -eq $advancedOriginRecovery) {
        throw "State-preparation stale transaction has no exact lock-bound recovery admission."
    }
    $candidateContract = [pscustomobject]@{ head = $CandidateSha; tree = $staleTree }
    # Recovery uses sealed journal identity rather than X's declaration.  A
    # fresh Y declaration is admitted only on the separate branch above.
    $stateDeclaration = [pscustomobject]@{ required = $false; declaration_present = $false; declaration_blob_sha = ""; sidecar_contract = "" }
}
$runtimeContract = Get-DawnstrikeGitContract $git $runtime $ProcessTimeoutSeconds
$lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
$runtimeOrigin = Get-DawnstrikeGitValue $git $runtime @("remote", "get-url", "origin") "State-preparation runtime origin verification" $ProcessTimeoutSeconds
Assert-DawnstrikeSafeOrigin $runtimeOrigin
$runtimeOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $runtimeOrigin
if ($runtimeOriginIdentity -ne $lockOrigin) { throw "Runtime origin identity is not the candidate-bound origin." }
$runtimeOriginSha256 = Get-DawnstrikeSha256Text $runtimeOrigin
$canonical = Get-DawnstrikeTaskContract $runtime $state
$canonicalBeforePreparation = $canonical
$auxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
$auxiliaryBeforeStatePreparation = $auxiliary
$restoreAuxiliary = $auxiliary.present -and $auxiliary.state -eq "Ready"
$proofRoot = Join-Path $state "receipts\state-preparation"
Assert-DawnstrikeNoReparseComponents $proofRoot "State-preparation receipt root"
New-Item -ItemType Directory -Path $proofRoot -Force | Out-Null
$proofRoot = Resolve-DawnstrikeActivationRoot $proofRoot "State-preparation receipt root"
$proofPath = Join-Path $proofRoot ("task-proof-" + $CandidateSha + ".json")
$baselinePath = Join-Path $proofRoot ("task-baseline-" + $CandidateSha + ".json")
$statePreparationReceiptPath = Join-Path $proofRoot ("state-preparation-" + $CandidateSha + ".json")
$canonicalProofPath = $proofPath
$canonicalBaselinePath = $baselinePath
$canonicalStatePreparationReceiptPath = $statePreparationReceiptPath
$operationJournalPath = Join-Path $state "receipts\runtime-operation\state-preparation-$CandidateSha.json"
$journalBaselineRelative = "receipts/state-preparation/task-baseline-$CandidateSha.json"
$journalReceiptRelative = "receipts/state-preparation/state-preparation-$CandidateSha.json"
$journalTaskContractSha256 = [string]$canonical.task_contract_sha256
$emptyJournalHash = Get-DawnstrikeSha256Text ""
Assert-DawnstrikeNoReparseComponents $operationJournalPath "State-preparation operation journal"
Assert-DawnstrikeNoReparseComponents $baselinePath "State-preparation task baseline"
Assert-DawnstrikeNoReparseComponents $statePreparationReceiptPath "State-preparation receipt"
$preparationLock = $null
$journalPhase = "INIT"
$preservePreparationLock = $false

# A dead-owner journal is the only admissible retry entry point.  It is
# handled before a fresh lock/journal attempt so a child kill after disabling
# the auxiliary task cannot strand the global mutex or poison the next proof.
if (Test-Path -LiteralPath $operationJournalPath -PathType Leaf) {
$existingPreparationJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
        $operationJournalPath $approvedPython.path $approvedPython.sha256
    if (
        [string]$existingPreparationJournal.payload.operation -ne "state_preparation" -or
        [string]$existingPreparationJournal.payload.candidate_sha -ne $CandidateSha -or
        [string]$existingPreparationJournal.payload.candidate_tree -ne [string]$candidateContract.tree -or
        [string]$existingPreparationJournal.payload.origin_identity -ne $lockOrigin
    ) { throw "Existing state-preparation journal identity is invalid." }
    if (
        [string]$existingPreparationJournal.payload.current_sha -ne [string]$runtimeContract.head -or
        [string]$existingPreparationJournal.payload.current_tree -ne [string]$runtimeContract.tree -or
        [string]$existingPreparationJournal.payload.previous_sha -ne [string]$runtimeContract.head -or
        [string]$existingPreparationJournal.payload.previous_tree -ne [string]$runtimeContract.tree -or
        [string]$existingPreparationJournal.payload.task_contract_sha256 -ne [string]$canonical.task_contract_sha256 -or
        [string]$existingPreparationJournal.payload.prepared_receipt_relative_path -ne $journalBaselineRelative -or
        [string]$existingPreparationJournal.payload.complete_receipt_relative_path -ne $journalReceiptRelative
    ) { throw "Existing state-preparation journal is not bound to the exact live runtime/task baseline." }
    $journalPhase = [string]$existingPreparationJournal.payload.phase
    $attemptJournalHash = [string]$existingPreparationJournal.raw_file_sha256
    $attemptArchiveRoot = Join-Path $proofRoot ("archive\attempt-" + $CandidateSha + "-" + $attemptJournalHash)
    $recoveryBaselinePath = $canonicalBaselinePath
    $recoveryProofPath = $canonicalProofPath
    $recoveryStatePreparationReceiptPath = $canonicalStatePreparationReceiptPath
    if ($journalPhase -eq "COMPENSATED") {
        $compensationMarkerRelative = [string]$existingPreparationJournal.payload.compensation_receipt_relative_path
        $compensationMarkerHash = [string]$existingPreparationJournal.payload.compensation_receipt_sha256
        $compensationMarkerPath = Join-Path $state $compensationMarkerRelative.Replace('/', '\')
        if ($compensationMarkerRelative -eq "NONE" -or
            -not (Test-Path -LiteralPath $compensationMarkerPath -PathType Leaf) -or
            (Get-DawnstrikeSha256File $compensationMarkerPath) -ne $compensationMarkerHash) {
            throw "State-preparation COMPENSATED journal has no exact compensation marker."
        }
        try { $compensationMarkerPayload = Get-Content -LiteralPath $compensationMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw "State-preparation compensation marker is invalid JSON." }
        $attemptJournalHash = [string]$compensationMarkerPayload.prior_journal_file_sha256
        if ($attemptJournalHash -notmatch '^[0-9a-f]{64}$') {
            throw "State-preparation compensation marker has no valid prior journal hash."
        }
        $attemptArchiveRoot = Join-Path $proofRoot ("archive\attempt-" + $CandidateSha + "-" + $attemptJournalHash)
        # A crash after attempt-artifact archival but before tombstone clearing
        # leaves the journal as the only canonical marker.  Reuse only the
        # exact hash-bound archive, never a same-name replacement.
        if (-not (Test-Path -LiteralPath $recoveryBaselinePath -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $attemptArchiveRoot "task-baseline.json") -PathType Leaf)) {
            $recoveryBaselinePath = Join-Path $attemptArchiveRoot "task-baseline.json"
        }
        if (-not (Test-Path -LiteralPath $recoveryProofPath -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $attemptArchiveRoot "task-proof.json") -PathType Leaf)) {
            $recoveryProofPath = Join-Path $attemptArchiveRoot "task-proof.json"
        }
        if (-not (Test-Path -LiteralPath $recoveryStatePreparationReceiptPath -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $attemptArchiveRoot "receipt.json") -PathType Leaf)) {
            $recoveryStatePreparationReceiptPath = Join-Path $attemptArchiveRoot "receipt.json"
        }
    }
    $existingLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
    if (Test-Path -LiteralPath $existingLockPath -PathType Leaf) {
        $existingLockSnapshot = Get-DawnstrikeStrictRuntimeLock $existingLockPath $approvedPython.path $approvedPython.sha256
        if (-not (Test-DawnstrikeRuntimeLockOwnerDead $existingLockSnapshot.payload)) {
            throw "State-preparation recovery lock owner is still active."
        }
        $preparationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
            -StateRoot $state -JournalPath $operationJournalPath `
            -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
            -OriginIdentity $lockOrigin -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
    }
    elseif ($journalPhase -notin @("COMPLETE", "COMPENSATED")) {
        throw "State-preparation journal exists without its exact governed lock."
    }
    # The initial admission snapshot predates lock adoption. Re-read the
    # checkout and raw origin after the exact lock/journal pair is held (or for
    # a terminal retry with the journal as the sealed authority) before any
    # baseline, receipt, or cleanup decision.
    $lockedRecoveryRuntime = Assert-DawnstrikeStatePreparationRuntimeExact `
        -GitPath $git -RuntimeRoot $runtime -TimeoutSeconds $ProcessTimeoutSeconds `
        -ExpectedHead ([string]$existingPreparationJournal.payload.current_sha) `
        -ExpectedTree ([string]$existingPreparationJournal.payload.current_tree) `
        -ExpectedOriginIdentity $lockOrigin -ExpectedOriginSha256 $runtimeOriginSha256
    $runtimeContract = $lockedRecoveryRuntime.contract
    $runtimeOrigin = [string]$lockedRecoveryRuntime.origin
    $runtimeOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $runtimeOrigin
    $runtimeOriginSha256 = Get-DawnstrikeSha256Text $runtimeOrigin
    $baseline = $null
    if ($journalPhase -eq "INIT" -and
        [string]$existingPreparationJournal.payload.prepared_receipt_sha256 -eq $emptyJournalHash) {
        # The baseline is intentionally written before INIT->PREPARE.  If a
        # child dies in that tiny window, bind the already-proven baseline to
        # the adopted INIT journal before doing any recovery mutation.
        if ($null -eq $preparationLock) { throw "State-preparation INIT journal has no adoptable lock." }
        if (-not (Test-Path -LiteralPath $canonicalBaselinePath -PathType Leaf)) {
            throw "State-preparation INIT journal has no pre-mutation task baseline."
        }
        $baseline = Get-DawnstrikeStatePreparationTaskBaseline $canonicalBaselinePath (Get-DawnstrikeSha256File $canonicalBaselinePath)
        if (
            [string]$baseline.candidate_sha -ne $CandidateSha -or
            [string]$baseline.candidate_tree -ne [string]$candidateContract.tree -or
            [string]$baseline.runtime_origin_identity -ne $runtimeOriginIdentity -or
            [string]$baseline.runtime_origin_sha256 -ne $runtimeOriginSha256
        ) { throw "State-preparation INIT baseline identity is invalid." }
        Assert-DawnstrikeStatePreparationTaskExact -Current $auxiliaryBeforeStatePreparation -Baseline $baseline -RequireOriginalEnablement
        $null = Set-DawnstrikeRuntimeOperationJournalPhase `
            -StateRoot $state -JournalPath $operationJournalPath -Lock $preparationLock `
            -Operation state_preparation -Phase PREPARE `
            -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
            -CurrentSha ([string]$existingPreparationJournal.payload.current_sha) -CurrentTree ([string]$existingPreparationJournal.payload.current_tree) `
            -PreviousSha ([string]$existingPreparationJournal.payload.previous_sha) -PreviousTree ([string]$existingPreparationJournal.payload.previous_tree) `
            -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalBaselineRelative `
            -PreparedReceiptSha256 (Get-DawnstrikeSha256File $canonicalBaselinePath) `
            -CompleteReceiptRelativePath $journalReceiptRelative -CompleteReceiptSha256 $emptyJournalHash `
            -BackupContractSha256 ([string]$existingPreparationJournal.payload.backup_contract_sha256) `
            -TaskContractSha256 ([string]$existingPreparationJournal.payload.task_contract_sha256) `
            -RuntimeStageContractSha256 ([string]$existingPreparationJournal.payload.runtime_stage_contract_sha256) `
            -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
        $existingPreparationJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
            $operationJournalPath $approvedPython.path $approvedPython.sha256
        $journalPhase = "PREPARE"
    }
    if ([string]$existingPreparationJournal.payload.prepared_receipt_relative_path -ne "NONE" -and
        [string]$existingPreparationJournal.payload.prepared_receipt_sha256 -ne $emptyJournalHash) {
        if ($null -eq $baseline) {
            $baseline = Get-DawnstrikeStatePreparationTaskBaseline $recoveryBaselinePath ([string]$existingPreparationJournal.payload.prepared_receipt_sha256)
        }
        if (
            [string]$baseline.runtime_origin_identity -ne $runtimeOriginIdentity -or
            [string]$baseline.runtime_origin_sha256 -ne $runtimeOriginSha256
        ) { throw "State-preparation baseline is not bound to the exact live runtime origin." }
    }
    $currentRecoveryAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state -AllowDisabled
    if ($null -ne $baseline) {
        if ([string]$baseline.state_before -eq "Ready" -and $currentRecoveryAuxiliary.present -and $currentRecoveryAuxiliary.state -eq "Disabled") {
            $null = Restore-DawnstrikeAuxiliaryCaptureTask -Expected ([pscustomobject]@{
                present = $true; task_name = [string]$baseline.task_name; task_path = [string]$baseline.task_path
                state = "Ready"; enabled = $true; xml = [string]$baseline.xml; xml_sha256 = [string]$baseline.xml_sha256
                xml_file_sha256 = [string]$baseline.xml_sha256; definition_contract_sha256 = [string]$baseline.definition_contract_sha256
                action_contract_sha256 = [string]$baseline.action_contract_sha256
            }) -RuntimeRoot $runtime -StateRoot $state
            $currentRecoveryAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state -AllowDisabled
        }
        Assert-DawnstrikeStatePreparationTaskExact -Current $currentRecoveryAuxiliary -Baseline $baseline -RequireOriginalEnablement
    }
    $completeReceiptPath = Join-Path $state ([string]$existingPreparationJournal.payload.complete_receipt_relative_path).Replace('/', '\')
    if ([string]$existingPreparationJournal.payload.complete_receipt_relative_path -ne $journalReceiptRelative) {
        throw "State-preparation complete receipt path is not canonical."
    }
    if ($journalPhase -eq "COMPENSATED") {
        $compensationRelative = [string]$existingPreparationJournal.payload.compensation_receipt_relative_path
        $compensationHash = [string]$existingPreparationJournal.payload.compensation_receipt_sha256
        if ($compensationRelative -eq "NONE" -or $compensationHash -notmatch '^[0-9a-f]{64}$') {
            throw "State-preparation COMPENSATED journal has no exact compensation receipt."
        }
        $compensationPath = Join-Path $state $compensationRelative.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $compensationPath -PathType Leaf) -or
            (Get-DawnstrikeSha256File $compensationPath) -ne $compensationHash) {
            throw "State-preparation compensation receipt changed or is missing."
        }
        $null = Invoke-DawnstrikeActivationProcess $python @(
            "-S", (Join-Path $candidate "scripts\runtime_operation_journal.py"), "verify-compensation",
            "--receipt", $compensationPath, "--state-root", $state
        ) $candidate "State-preparation compensation recovery verification" $ProcessTimeoutSeconds
        try { $compensationPayload = Get-Content -LiteralPath $compensationPath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { throw "State-preparation compensation receipt is invalid JSON." }
        if (
            [string]$compensationPayload.prior_journal_file_sha256 -ne $attemptJournalHash -or
            [string]$compensationPayload.task_contract_sha256 -ne [string]$canonical.task_contract_sha256 -or
            ($null -ne $baseline -and [string]$compensationPayload.task_state -ne [string]$baseline.state_before) -or
            ($null -ne $baseline -and [string]$compensationPayload.task_xml_sha256 -ne [string]$baseline.xml_sha256) -or
            ($null -ne $baseline -and [string]$compensationPayload.task_action_contract_sha256 -ne [string]$baseline.action_contract_sha256) -or
            ($null -ne $baseline -and [string]$compensationPayload.task_definition_contract_sha256 -ne [string]$baseline.definition_contract_sha256)
        ) { throw "State-preparation compensation receipt is not bound to the exact restored task." }
        if (Test-Path -LiteralPath $recoveryProofPath -PathType Leaf) {
            $null = Assert-DawnstrikeStatePreparationTaskProofExact `
                -Path $recoveryProofPath -CandidateSha $CandidateSha `
                -CandidateTree ([string]$candidateContract.tree) -Canonical $canonical -Baseline $baseline
        }
        # Re-read the locked runtime immediately before terminal compensation
        # cleanup.  Task-only evidence must never clear a COMPENSATED journal
        # after a concurrent runtime checkout/origin substitution.
        $compensationOriginSha = if ($null -ne $baseline) { [string]$baseline.runtime_origin_sha256 } else { $runtimeOriginSha256 }
        $null = Assert-DawnstrikeStatePreparationRuntimeExact `
            -GitPath $git -RuntimeRoot $runtime -TimeoutSeconds $ProcessTimeoutSeconds `
            -ExpectedHead ([string]$existingPreparationJournal.payload.current_sha) `
            -ExpectedTree ([string]$existingPreparationJournal.payload.current_tree) `
            -ExpectedOriginIdentity $lockOrigin -ExpectedOriginSha256 $compensationOriginSha
        # The sealed COMPENSATED journal is itself an admissible retry marker.
        # Archive/release/clear are each independently retryable so a kill at
        # any terminal boundary cannot leave an unowned poison operation.
        $null = Archive-DawnstrikeStatePreparationAttempt `
            -StateRoot $state -BackupRoot $BackupRoot -CandidateSha $CandidateSha `
            -JournalHash $attemptJournalHash -ProofPath $canonicalProofPath `
            -BaselinePath $canonicalBaselinePath -ReceiptPath $canonicalStatePreparationReceiptPath `
            -BackupBundlePath ""
        if ($null -ne $preparationLock) {
            Exit-DawnstrikeGovernedRuntimeLock $preparationLock
            $preparationLock = $null
        }
        $null = Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournalPath `
            -Operation state_preparation -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
            -OriginIdentity $lockOrigin -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
        $journalPhase = "INIT"
        if ($null -ne $advancedOriginRecovery) {
            Write-Output (([ordered]@{
                status = "RECOVERED_SUPERSEDED_TRANSACTION"
                operation = "state_preparation"
                recovered_candidate_sha = $CandidateSha
                recovered_candidate_tree = [string]$candidateContract.tree
                current_origin_main_sha = [string]$statePreparationBootstrap.head
                research_only = $true
                broker_execution_enabled = $false
            } | ConvertTo-Json -Compress))
            return
        }
        # Continue into a new INIT journal only after the prior attempt has
        # been hash-bound, terminally archived, and cleared.
    }
    if ($journalPhase -eq "COMPLETE") {
        if ([string]$existingPreparationJournal.payload.complete_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$existingPreparationJournal.payload.complete_receipt_sha256 -eq $emptyJournalHash -or
            -not (Test-Path -LiteralPath $completeReceiptPath -PathType Leaf) -or
            (Get-DawnstrikeSha256File $completeReceiptPath) -ne [string]$existingPreparationJournal.payload.complete_receipt_sha256) {
            throw "Complete state-preparation journal has no exact receipt."
        }
        if ($null -eq $baseline) { throw "Complete state-preparation journal has no exact task baseline." }
        $null = Assert-DawnstrikeStatePreparationTaskProofExact `
            -Path $recoveryProofPath -CandidateSha $CandidateSha `
            -CandidateTree ([string]$candidateContract.tree) -Canonical $canonical -Baseline $baseline
        $completeProof = Assert-DawnstrikeStatePreparationCompleteTerminal `
            -Journal $existingPreparationJournal -ReceiptPath $completeReceiptPath `
            -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state `
            -BackupRoot $BackupRoot -GitPath $git -PythonPath $python `
            -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $CandidateSha `
            -CandidateTree ([string]$candidateContract.tree) -Baseline $baseline -Canonical $canonical
        $completeResult = Invoke-DawnstrikeActivationProcess $python @(
            (Join-Path $candidate "scripts\state_preparation.py"), "--db-path", (Join-Path $state "shadow_real.sqlite"),
            "--verify-receipt", $completeReceiptPath,
            "--candidate-sha", $CandidateSha, "--candidate-tree", [string]$candidateContract.tree
        ) $candidate "State-preparation COMPLETE recovery verification" $ProcessTimeoutSeconds
        if ($null -ne $preparationLock) {
            Exit-DawnstrikeGovernedRuntimeLock $preparationLock
            $preparationLock = $null
        }
        $null = Archive-DawnstrikeStatePreparationJournal -StateRoot $state -JournalPath $operationJournalPath -JournalHash ([string]$existingPreparationJournal.raw_file_sha256)
        if ($null -ne $advancedOriginRecovery) {
            Write-Output (([ordered]@{
                status = "RECOVERED_SUPERSEDED_TRANSACTION"
                operation = "state_preparation"
                recovered_candidate_sha = $CandidateSha
                recovered_candidate_tree = [string]$candidateContract.tree
                current_origin_main_sha = [string]$statePreparationBootstrap.head
                research_only = $true
                broker_execution_enabled = $false
            } | ConvertTo-Json -Compress))
            return
        }
        Write-Output ([string]$completeResult.Stdout).Trim()
        return
    }
    if ($journalPhase -notin @("INIT", "PREPARE")) { throw "State-preparation journal phase is not recoverable." }
    $receiptValid = $false
    if (Test-Path -LiteralPath $completeReceiptPath -PathType Leaf) {
        try {
            $null = Invoke-DawnstrikeActivationProcess $python @(
                (Join-Path $candidate "scripts\state_preparation.py"), "--db-path", (Join-Path $state "shadow_real.sqlite"),
                "--verify-receipt", $completeReceiptPath,
                "--candidate-sha", $CandidateSha, "--candidate-tree", [string]$candidateContract.tree
            ) $candidate "State-preparation partial receipt verification" $ProcessTimeoutSeconds
            $receiptValid = $true
        }
        catch { $receiptValid = $false }
    }
    if ($receiptValid) {
        # The worker committed the sidecar, but the wrapper may have died
        # before its terminal journal write. Complete the journal only after
        # exact original task restoration and sealed receipt proof.
        if ($null -eq $baseline) { throw "State-preparation receipt has no exact task baseline." }
        $null = Assert-DawnstrikeStatePreparationTaskProofExact `
            -Path $recoveryProofPath -CandidateSha $CandidateSha `
            -CandidateTree ([string]$candidateContract.tree) -Canonical $canonical -Baseline $baseline
        $preparedReceiptProof = Assert-DawnstrikeStatePreparationCompleteTerminal `
            -Journal $existingPreparationJournal -ReceiptPath $completeReceiptPath `
            -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state `
            -BackupRoot $BackupRoot -GitPath $git -PythonPath $python `
            -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $CandidateSha `
            -CandidateTree ([string]$candidateContract.tree) -Baseline $baseline -Canonical $canonical `
            -AllowPrepared
        $receiptHash = Get-DawnstrikeSha256File $completeReceiptPath
        $receiptPayload = $preparedReceiptProof.receipt
        $backupHash = [string]$receiptPayload.backup_manifest_sha256
        if ($null -eq $preparationLock) { throw "State-preparation COMPLETE recovery lost its governed lock." }
        $terminal = Set-DawnstrikeRuntimeOperationJournalPhase `
            -StateRoot $state -JournalPath $operationJournalPath -Lock $preparationLock `
            -Operation state_preparation -Phase COMPLETE `
            -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
            -CurrentSha ([string]$existingPreparationJournal.payload.current_sha) -CurrentTree ([string]$existingPreparationJournal.payload.current_tree) `
            -PreviousSha ([string]$existingPreparationJournal.payload.previous_sha) -PreviousTree ([string]$existingPreparationJournal.payload.previous_tree) `
            -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalBaselineRelative `
            -PreparedReceiptSha256 (Get-DawnstrikeSha256File $recoveryBaselinePath) `
            -CompleteReceiptRelativePath $journalReceiptRelative -CompleteReceiptSha256 $receiptHash `
            -BackupContractSha256 $backupHash -TaskContractSha256 $journalTaskContractSha256 `
            -RuntimeStageContractSha256 $emptyJournalHash -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
        $null = Assert-DawnstrikeStatePreparationCompleteTerminal `
            -Journal $terminal -ReceiptPath $completeReceiptPath `
            -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state `
            -BackupRoot $BackupRoot -GitPath $git -PythonPath $python `
            -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $CandidateSha `
            -CandidateTree ([string]$candidateContract.tree) -Baseline $baseline -Canonical $canonical
        Exit-DawnstrikeGovernedRuntimeLock $preparationLock
        $preparationLock = $null
        $null = Archive-DawnstrikeStatePreparationJournal -StateRoot $state -JournalPath $operationJournalPath -JournalHash ([string]$terminal.raw_file_sha256)
        if ($null -ne $advancedOriginRecovery) {
            Write-Output (([ordered]@{
                status = "RECOVERED_SUPERSEDED_TRANSACTION"
                operation = "state_preparation"
                recovered_candidate_sha = $CandidateSha
                recovered_candidate_tree = [string]$candidateContract.tree
                current_origin_main_sha = [string]$statePreparationBootstrap.head
                research_only = $true
                broker_execution_enabled = $false
            } | ConvertTo-Json -Compress))
            return
        }
        Write-Output ([string]$receiptPayload | ConvertTo-Json -Depth 12 -Compress)
        return
    }
    # Partial database work is never guessed. Restore only the exact original
    # auxiliary baseline, seal terminal compensation, archive all attempt
    # files by the prior journal hash, then allow a fresh governed attempt.
    if ($null -eq $baseline) { throw "State-preparation partial journal has no exact task baseline." }
    if ($currentRecoveryAuxiliary.present -and [string]$baseline.state_before -eq "Ready" -and $currentRecoveryAuxiliary.state -eq "Disabled") {
        $null = Restore-DawnstrikeAuxiliaryCaptureTask -Expected ([pscustomobject]@{
            present = $true; task_name = [string]$baseline.task_name; task_path = [string]$baseline.task_path
            state = "Ready"; enabled = $true; xml = [string]$baseline.xml; xml_sha256 = [string]$baseline.xml_sha256
            xml_file_sha256 = [string]$baseline.xml_sha256; definition_contract_sha256 = [string]$baseline.definition_contract_sha256
            action_contract_sha256 = [string]$baseline.action_contract_sha256
        }) -RuntimeRoot $runtime -StateRoot $state
    }
    $restoredAfterCompensation = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state -AllowDisabled
    Assert-DawnstrikeStatePreparationTaskExact -Current $restoredAfterCompensation -Baseline $baseline -RequireOriginalEnablement
    $compensation = Write-DawnstrikeStatePreparationCompensation `
        -StateRoot $state -Journal $existingPreparationJournal -JournalPath $operationJournalPath `
        -Lock $preparationLock -CurrentTask $restoredAfterCompensation -CandidateSha $CandidateSha `
        -CandidateTree ([string]$candidateContract.tree) -OriginIdentity $lockOrigin `
        -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256 `
        -TimeoutSeconds ([string]$ProcessTimeoutSeconds) -FailureType "state_preparation_partial_recovery"
    $terminalJournal = $compensation.journal
    $null = Archive-DawnstrikeStatePreparationAttempt `
        -StateRoot $state -BackupRoot $BackupRoot -CandidateSha $CandidateSha `
        -JournalHash ([string]$existingPreparationJournal.raw_file_sha256) -ProofPath $proofPath `
        -BaselinePath $baselinePath -ReceiptPath $statePreparationReceiptPath `
        -BackupBundlePath ""
    Exit-DawnstrikeGovernedRuntimeLock $preparationLock
    $preparationLock = $null
    $null = Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournalPath `
        -Operation state_preparation -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
        -OriginIdentity $lockOrigin -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
}

$preparationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal `
    -StateRoot $state -JournalPath $operationJournalPath -Operation state_preparation `
    -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
    -CurrentSha ([string]$runtimeContract.head) -CurrentTree ([string]$runtimeContract.tree) `
    -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
    -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalBaselineRelative `
    -CompleteReceiptRelativePath $journalReceiptRelative -TaskContractSha256 $journalTaskContractSha256 `
    -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
try {
    # Recheck both task inventory and all pre-existing locks after acquiring
    # the atomic preparation lock.  A normal daily stage must not enter after
    # the initial read and race the online backup/migration window.
    $lockedFreshRuntime = Assert-DawnstrikeStatePreparationRuntimeExact `
        -GitPath $git -RuntimeRoot $runtime -TimeoutSeconds $ProcessTimeoutSeconds `
        -ExpectedHead ([string]$runtimeContract.head) -ExpectedTree ([string]$runtimeContract.tree) `
        -ExpectedOriginIdentity $lockOrigin -ExpectedOriginSha256 $runtimeOriginSha256
    $runtimeContract = $lockedFreshRuntime.contract
    $runtimeOrigin = [string]$lockedFreshRuntime.origin
    $runtimeOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $runtimeOrigin
    $runtimeOriginSha256 = Get-DawnstrikeSha256Text $runtimeOrigin
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

    # Seal the exact task baseline and move the operation journal to PREPARE
    # before the first external mutation.  A child kill after Disable-ScheduledTask
    # must leave enough evidence for the next invocation to restore the original
    # task; an INIT journal with a baseline created later is not recoverable.
    $baselinePayload = [ordered]@{
        schema_version = "dawnstrike.state_preparation_task_baseline.v1"
        candidate_sha = $CandidateSha
        candidate_tree = [string]$candidateContract.tree
        task_name = if ($auxiliaryBeforeStatePreparation.present) { [string]$auxiliaryBeforeStatePreparation.task_name } else { [string]$script:DawnstrikeAuxiliaryCaptureTaskName }
        task_path = if ($auxiliaryBeforeStatePreparation.present) { [string]$auxiliaryBeforeStatePreparation.task_path } else { "NONE" }
        state_before = if ($auxiliaryBeforeStatePreparation.present) { [string]$auxiliaryBeforeStatePreparation.state } else { "ABSENT" }
        xml = if ($auxiliaryBeforeStatePreparation.present) { [string]$auxiliaryBeforeStatePreparation.xml } else { "" }
        xml_sha256 = [string]$auxiliaryBeforeStatePreparation.xml_sha256
        action_contract_sha256 = [string]$auxiliaryBeforeStatePreparation.action_contract_sha256
        definition_contract_sha256 = [string]$auxiliaryBeforeStatePreparation.definition_contract_sha256
        runtime_origin_identity = $runtimeOriginIdentity
        runtime_origin_sha256 = $runtimeOriginSha256
        research_only = $true
        broker_execution_enabled = $false
    }
    if (Test-Path -LiteralPath $baselinePath -PathType Leaf) {
        $baseline = Get-DawnstrikeStatePreparationTaskBaseline $baselinePath (Get-DawnstrikeSha256File $baselinePath)
        if (
            [string]$baseline.candidate_sha -ne $CandidateSha -or
            [string]$baseline.candidate_tree -ne [string]$candidateContract.tree -or
            [string]$baseline.xml_sha256 -ne [string]$baselinePayload.xml_sha256 -or
            [string]$baseline.action_contract_sha256 -ne [string]$baselinePayload.action_contract_sha256 -or
            [string]$baseline.definition_contract_sha256 -ne [string]$baselinePayload.definition_contract_sha256 -or
            [string]$baseline.runtime_origin_identity -ne [string]$baselinePayload.runtime_origin_identity -or
            [string]$baseline.runtime_origin_sha256 -ne [string]$baselinePayload.runtime_origin_sha256
        ) { throw "Existing state-preparation task baseline is not the exact current baseline." }
    }
    else {
        Write-DawnstrikeActivationJson $baselinePayload $baselinePath
        $baseline = Get-DawnstrikeStatePreparationTaskBaseline $baselinePath (Get-DawnstrikeSha256File $baselinePath)
    }
    $baselineHash = Get-DawnstrikeSha256File $baselinePath
    $journalBeforePrepare = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $approvedPython.path $approvedPython.sha256
    if ([string]$journalBeforePrepare.payload.phase -eq "INIT") {
        $null = Set-DawnstrikeRuntimeOperationJournalPhase `
            -StateRoot $state -JournalPath $operationJournalPath -Lock $preparationLock `
            -Operation state_preparation -Phase PREPARE `
            -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
            -CurrentSha ([string]$runtimeContract.head) -CurrentTree ([string]$runtimeContract.tree) `
            -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
            -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalBaselineRelative `
            -PreparedReceiptSha256 $baselineHash -CompleteReceiptRelativePath $journalReceiptRelative `
            -CompleteReceiptSha256 $emptyJournalHash -BackupContractSha256 $emptyJournalHash `
            -TaskContractSha256 $journalTaskContractSha256 -RuntimeStageContractSha256 $emptyJournalHash `
            -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
        $journalPhase = "PREPARE"
    }
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
    $locks = @()
    foreach ($lockItem in @(Get-ChildItem -LiteralPath $lockRoot -File -Force)) {
        if ($lockItem.FullName -eq $preparationLock.path) { continue }
        Assert-DawnstrikeNoReparseComponents $lockItem.FullName "State-preparation lock-root evidence"
        if ([string]$lockItem.Name -match '^recovered-stale-([0-9a-f]{64})\.lock$') {
            $archiveHash = [string]$Matches[1]
            $archive = Get-DawnstrikeStrictRuntimeLock `
                $lockItem.FullName $approvedPython.path $approvedPython.sha256
            if ([string]$archive.raw_file_sha256 -cne $archiveHash) {
                throw "Recovered runtime-lock archive hash does not match its canonical filename."
            }
            continue
        }
        if ([string]$lockItem.Name -match '^dawnstrike-daily-(\d{4}-\d{2}-\d{2})\.lock\.stale-dead-([0-9a-f]{64})$') {
            $archiveDate = [string]$Matches[1]
            $archiveHash = [string]$Matches[2]
            $archive = Get-DawnstrikeLockSnapshot `
                -Path $lockItem.FullName -Label "Archived daily run lock"
            if (
                [string]$archive.bytes_sha256 -cne $archiveHash -or
                [string]$archive.payload.market_date -cne $archiveDate -or
                (Test-DawnstrikeLockOwnerActive -LockPath $lockItem.FullName)
            ) {
                throw "Archived daily run lock is not an exact dead-owner hash-bound archive."
            }
            continue
        }
        $locks += $lockItem
    }
    if ($locks.Count -gt 0) { throw "State preparation requires no active locks." }
}
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
if (Test-Path -LiteralPath $proofPath -PathType Leaf) {
    # The proof is immutable attempt evidence. Reuse it only after the
    # candidate-bound bytes are proven; rewriting it would poison a retry after
    # a transient Python failure.
    Assert-DawnstrikeNoReparseComponents $proofPath "State-preparation task proof"
    # The proof is intentionally not self-hashed; parse and bind every field
    # instead of accepting arbitrary existing bytes or rewriting an immutable
    # proof after a transient worker failure.
    try { $existingProof = Get-Content -LiteralPath $proofPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw "Existing state-preparation task proof is invalid JSON." }
    foreach ($field in @(
        "schema_version", "candidate_sha", "candidate_tree", "task_count",
        "canonical_running_count", "canonical_enabled_count", "capture_present",
        "capture_running", "capture_state", "capture_action", "capture_xml_sha256",
        "capture_action_contract_sha256", "research_only", "broker_execution_enabled"
    )) {
        if ([string]$existingProof.$field -ne [string]$proof.$field) {
            throw "Existing state-preparation task proof is not the exact current proof."
        }
    }
}
else {
    Write-DawnstrikeActivationJson $proof $proofPath
}
$tool = Join-Path $candidate "scripts\prepare_dawnstrike_state.py"
if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) { throw "State-preparation Python tool is missing." }
$db = Join-Path $state "shadow_real.sqlite"
$result = Invoke-DawnstrikeActivationProcess $python @(
    $tool, "--db-path", $db, "--state-root", $state, "--backup-root", $BackupRoot,
    "--candidate-sha", $CandidateSha, "--candidate-tree", $candidateContract.tree,
    "--task-proof", $proofPath, "--preparation-lock", $preparationLock.path, "--retention", $Retention
) $candidate "Governed state preparation" $ProcessTimeoutSeconds
try { $resultPayload = [string]$result.Stdout | ConvertFrom-Json }
catch { throw "State-preparation worker did not return a valid COMPLETE receipt." }
if ([string]$resultPayload.status -ne "COMPLETE" -or [string]$resultPayload.candidate_sha -ne $CandidateSha -or [string]$resultPayload.candidate_tree -ne [string]$candidateContract.tree) {
    throw "State-preparation worker returned an unbound receipt."
}
if ([string]$resultPayload.backup_manifest_sha256 -notmatch '^[0-9a-f]{64}$') {
    throw "State-preparation worker did not return an exact backup manifest hash."
}
if (-not (Test-Path -LiteralPath $statePreparationReceiptPath -PathType Leaf)) {
    throw "State-preparation COMPLETE receipt is missing after worker success."
}
if ([string]$resultPayload.receipt_sha256 -eq "" -or (Get-DawnstrikeSha256File $statePreparationReceiptPath) -eq $emptyJournalHash) {
    throw "State-preparation COMPLETE receipt hash is invalid."
}
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
    $restoredCanonical = Get-DawnstrikeTaskContract $runtime $state
    if ($restoredCanonical.task_contract_sha256 -ne $journalTaskContractSha256) {
        throw "State-preparation changed canonical task XML across its recovery boundary."
    }
    $journalBeforeComplete = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournalPath $approvedPython.path $approvedPython.sha256
    $completeJournal = Set-DawnstrikeRuntimeOperationJournalPhase `
        -StateRoot $state -JournalPath $operationJournalPath -Lock $preparationLock `
        -Operation state_preparation -Phase COMPLETE `
        -CandidateSha $CandidateSha -CandidateTree ([string]$candidateContract.tree) `
        -CurrentSha ([string]$runtimeContract.head) -CurrentTree ([string]$runtimeContract.tree) `
        -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
        -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $journalBaselineRelative `
        -PreparedReceiptSha256 $baselineHash -CompleteReceiptRelativePath $journalReceiptRelative `
        -CompleteReceiptSha256 (Get-DawnstrikeSha256File $statePreparationReceiptPath) `
        -BackupContractSha256 ([string]$resultPayload.backup_manifest_sha256) `
        -TaskContractSha256 $journalTaskContractSha256 -RuntimeStageContractSha256 $emptyJournalHash `
        -PythonPath $approvedPython.path -PythonSha256 $approvedPython.sha256
    $null = Assert-DawnstrikeStatePreparationCompleteTerminal `
        -Journal $completeJournal -ReceiptPath $statePreparationReceiptPath `
        -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state `
        -BackupRoot $BackupRoot -GitPath $git -PythonPath $python `
        -TimeoutSeconds $ProcessTimeoutSeconds -CandidateSha $CandidateSha `
        -CandidateTree ([string]$candidateContract.tree) -Baseline $baseline -Canonical $canonical
    Exit-DawnstrikeGovernedRuntimeLock $preparationLock
    $preparationLock = $null
    $null = Archive-DawnstrikeStatePreparationJournal -StateRoot $state -JournalPath $operationJournalPath -JournalHash ([string]$completeJournal.raw_file_sha256)
if ($null -ne $advancedOriginRecovery) {
    Write-Output (([ordered]@{
        status = "RECOVERED_SUPERSEDED_TRANSACTION"
        operation = "state_preparation"
        recovered_candidate_sha = $CandidateSha
        recovered_candidate_tree = [string]$candidateContract.tree
        current_origin_main_sha = [string]$statePreparationBootstrap.head
        research_only = $true
        broker_execution_enabled = $false
    } | ConvertTo-Json -Compress))
}
else {
    Write-Output ([string]$result.Stdout).Trim()
}
}
catch {
    $restoreSucceeded = $true
    if ($restoreAuxiliary) {
        try {
            $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                -Expected $auxiliaryBeforeStatePreparation `
                -RuntimeRoot $runtime -StateRoot $state
        }
        catch {
            $restoreSucceeded = $false
        }
    }
    if ($null -ne $preparationLock -and $journalPhase -in @("INIT", "PREPARE")) {
        # Leave the exact journal, baseline, proof, and any online backup in
        # place for dead-owner recovery. Releasing this lock would strand the
        # partial database/task boundary without an admissible owner.
        $preservePreparationLock = $true
    }
    if (-not $restoreSucceeded) {
        throw "State preparation failed and original auxiliary capture recovery could not be proven; governed lock retained."
    }
    throw
}
finally {
    if (-not $preservePreparationLock -and $null -ne $preparationLock) {
        Exit-DawnstrikeGovernedRuntimeLock $preparationLock
        if (Test-Path -LiteralPath $preparationLock.path -PathType Leaf) {
            throw "State-preparation lock could not be released; operator recovery is required."
        }
    }
}
