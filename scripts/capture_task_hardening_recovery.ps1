# Narrow, side-effect-free recovery proof used by the hardening entrypoint.
# The scheduler update and receipt seal remain outside this helper so a retry
# can only continue after the immutable PREPARED after-state is re-proven.

function Get-HardeningRecoverySha256Text {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash([System.Text.UTF8Encoding]::new($false).GetBytes($Text)))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-HardeningRecoverySha256File {
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Assert-HardeningRecoveryNoReparseComponents {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $cursor = [System.IO.FileInfo]::new($full)
    if (-not $cursor.Exists) { $cursor = [System.IO.DirectoryInfo]::new((Split-Path -Parent $full)) }
    while ($null -ne $cursor) {
        if ($cursor.Exists -and ($cursor.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Hardening recovery path contains a reparse-point component.'
        }
        if ([string]::Equals($cursor.FullName.TrimEnd('\\'), $cursor.Root.FullName.TrimEnd('\\'), [System.StringComparison]::OrdinalIgnoreCase)) { break }
        $cursor = $cursor.Parent
    }
}

function Assert-HardeningPreparedRecoveryState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Prepared,
        [Parameter(Mandatory = $true)][object]$CurrentTask,
        [Parameter(Mandatory = $true)][string]$PreparedPath,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskName,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskPath,
        [Parameter(Mandatory = $true)][string]$ExpectedCandidateSha,
        [Parameter(Mandatory = $true)][string]$ExpectedCandidateTree,
        [Parameter(Mandatory = $true)][object]$RuntimeIdentity,
        [Parameter(Mandatory = $true)][object]$InterpreterIdentity,
        [Parameter(Mandatory = $true)][string]$ExpectedReceiptPath
    )
    if ($Prepared.status -ne 'PREPARED' -or $Prepared.schema_version -ne 'dawnstrike.capture_task_hardening_prepared.v2') {
        throw 'PREPARED recovery record is not the governed immutable v2 state.'
    }
    if ($Prepared.task_name -cne $ExpectedTaskName -or $Prepared.task_path -cne $ExpectedTaskPath -or
        $Prepared.candidate_sha -cne $ExpectedCandidateSha -or $Prepared.candidate_tree -cne $ExpectedCandidateTree) {
        throw 'PREPARED recovery identity does not match this task and candidate.'
    }
    if ($Prepared.runtime_head -cne $RuntimeIdentity.head -or $Prepared.runtime_tree -cne $RuntimeIdentity.tree -or
        $Prepared.runtime_origin -cne $RuntimeIdentity.origin -or $Prepared.runner_before_sha256 -cne $RuntimeIdentity.runner_sha256) {
        throw 'PREPARED recovery runtime identity is stale or cross-origin.'
    }
    if ($Prepared.interpreter_path -cne $InterpreterIdentity.path -or $Prepared.interpreter_sha256 -cne $InterpreterIdentity.sha256 -or
        $Prepared.interpreter_version -cne $InterpreterIdentity.version -or
        $Prepared.interpreter_signer_subject -cne $InterpreterIdentity.signer_subject -or
        $Prepared.interpreter_signer_thumbprint -cne $InterpreterIdentity.signer_thumbprint) {
        throw 'PREPARED recovery interpreter identity is not exact.'
    }
    Assert-HardeningRecoveryNoReparseComponents $PreparedPath
    if ($Prepared.intended_receipt_path -cne $ExpectedReceiptPath) { throw 'PREPARED recovery receipt path is not exact.' }
    if ($CurrentTask.state -ne 'Disabled' -or $CurrentTask.task_path -cne $ExpectedTaskPath -or
        $CurrentTask.xml_sha256 -cne $Prepared.xml_after_sha256) {
        throw 'PREPARED recovery task is not the exact Disabled after-state.'
    }
    $backupPath = [string]$Prepared.backup_path
    Assert-HardeningRecoveryNoReparseComponents $backupPath
    if ([string]::IsNullOrWhiteSpace($backupPath) -or -not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        throw 'PREPARED recovery backup is missing.'
    }
    if ((Get-HardeningRecoverySha256File $backupPath) -cne $Prepared.backup_xml_file_sha256) {
        throw 'PREPARED recovery backup file hash changed.'
    }
    $backupText = [System.IO.File]::ReadAllText($backupPath, [System.Text.UTF8Encoding]::new($false))
    if ((Get-HardeningRecoverySha256Text $backupText) -cne $Prepared.backup_xml_sha256 -or
        (Get-HardeningRecoverySha256Text $backupText) -cne $Prepared.xml_before_sha256) {
        throw 'PREPARED recovery backup content changed.'
    }
    if (-not (Test-Path -LiteralPath $PreparedPath -PathType Leaf)) { throw 'PREPARED recovery record is missing.' }
    return $true
}
