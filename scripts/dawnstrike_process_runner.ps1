Set-StrictMode -Version Latest

# Every scheduled child is launched through the native Job Object runner.  A
# PowerShell process tree is not a sufficient ownership boundary on Windows:
# detached Python/Node descendants can outlive the wrapper after a timeout.
# Keep this import local so interactive callers get the same kill-on-close
# contract without needing to know about the implementation helper.
if (-not ("Dawnstrike.Native.JobProcessRunner" -as [type])) {
    . (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
}

function Invoke-DawnstrikeNativeProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$LogRoot,
        [Parameter(Mandatory = $true)][string]$LogName,
        [Parameter()][ValidateRange(0, 86400)][int]$TimeoutSeconds = 0,
        [Parameter()][ValidateRange(1, 60)][int]$OutputDrainTimeoutSeconds = 5
    )

    $startedAt = (Get-Date).ToUniversalTime()
    if ($TimeoutSeconds -eq 0) {
        # Match the child deadline to the scheduled stage while retaining one
        # native tree-kill contract for every invocation.
        $TimeoutSeconds = switch -Regex ($LogName) {
            "(?i)monitor|trade_watch|scenario" { 180; break }
            "(?i)weekly|training" { 10800; break }
            "(?i)finalize" { 10800; break }
            "(?i)eod|paperops" { 7200; break }
            "(?i)morning|universe" { 3600; break }
            default { 900 }
        }
    }
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    $safeName = $LogName -replace "[^A-Za-z0-9._-]", "_"
    $stdoutPath = Join-Path $LogRoot "$safeName.stdout.log"
    $stderrPath = Join-Path $LogRoot "$safeName.stderr.log"
    $receiptPath = Join-Path $LogRoot "$safeName.receipt.json"
    $exitCode = 127
    $startError = $null
    $timedOut = $false
    $activeJobMembersAfterCleanup = $null
    $previousErrorActionPreference = $ErrorActionPreference

    try {
        # Windows PowerShell promotes native stderr records to PowerShell error
        # records.  With the scheduled runners' ErrorActionPreference=Stop,
        # ordinary Python logging on stderr otherwise jumps into this catch and
        # is falsely recorded as a process-start failure with exit code 127.
        # Resolve the executable while errors still terminate, then allow the
        # native process to complete and trust its real exit code.
        $resolved = (Get-Command $FilePath -ErrorAction Stop).Path
        $ErrorActionPreference = "Continue"
        # Native runner owns the complete process tree and enforces the
        # deadline.  Do not use PowerShell redirection/pipelines here: those
        # wrappers can outlive the child and obscure its real exit status.
        # The retired wrapper used ``$exitCode = if ($null -eq $LASTEXITCODE)``;
        # the Job Object result now supplies the authoritative native code.
        $result = Invoke-DawnstrikeJobProcess `
            -FilePath $resolved `
            -ArgumentList $ArgumentList `
            -WorkingDirectory (Get-Location).Path `
            -Label $LogName `
            -TimeoutSeconds $TimeoutSeconds `
            -OutputDrainTimeoutSeconds $OutputDrainTimeoutSeconds
        $exitCode = [int]$result.ExitCode
        $activeJobMembersAfterCleanup = [int]$result.ActiveJobMembersAfterCleanup
        [System.IO.File]::WriteAllText($stdoutPath, [string]$result.Stdout, [System.Text.UTF8Encoding]::new($false))
        [System.IO.File]::WriteAllText($stderrPath, [string]$result.Stderr, [System.Text.UTF8Encoding]::new($false))
    }
    catch {
        $startError = $_.Exception.Message
        $timedOut = $startError -match "(?i)timed out after"
        if ($timedOut) {
            # 124 is reserved for deadline termination and cannot be confused
            # with a child application's non-zero exit code.
            $exitCode = 124
            $cleanupMatch = [regex]::Match(
                $startError,
                "(?i)active_job_members_after_cleanup=(\d+)"
            )
            if ($cleanupMatch.Success) {
                $activeJobMembersAfterCleanup = [int]$cleanupMatch.Groups[1].Value
            }
        }
        [System.IO.File]::WriteAllText($stderrPath, $startError, [System.Text.UTF8Encoding]::new($false))
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    $completedAt = (Get-Date).ToUniversalTime()
    $stdoutHash = if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
        (Get-FileHash -LiteralPath $stdoutPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { $null }
    $stderrHash = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
        (Get-FileHash -LiteralPath $stderrPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { $null }
    $receipt = [ordered]@{
        schema_version = "dawnstrike.native_process_receipt.v1"
        process_name = [IO.Path]::GetFileName($FilePath)
        argument_count = @($ArgumentList).Count
        started_at = $startedAt.ToString("o")
        completed_at = $completedAt.ToString("o")
        duration_ms = [math]::Round(($completedAt - $startedAt).TotalMilliseconds)
        exit_code = $exitCode
        timeout_seconds = $TimeoutSeconds
        timed_out = $timedOut
        active_job_members_after_cleanup = $activeJobMembersAfterCleanup
        timeout_cleanup_confirmed = ($timedOut -and $activeJobMembersAfterCleanup -eq 0)
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
        stdout_sha256 = $stdoutHash
        stderr_sha256 = $stderrHash
        start_error = $startError
        research_only = $true
        broker_execution_enabled = $false
    }
    $receipt | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $receiptPath -Encoding UTF8

    foreach ($path in @($stdoutPath, $stderrPath)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Get-Content -LiteralPath $path | ForEach-Object { [Console]::Out.WriteLine($_) }
        }
    }
    $receipt["receipt_path"] = $receiptPath
    return [pscustomobject]$receipt
}

function Resolve-DawnstrikeReleaseSha {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$LogRoot
    )

    $runtimePath = [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $RuntimeRoot).Path
    ).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $rootReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @("-C", $runtimePath, "rev-parse", "--show-toplevel") `
        -LogRoot $LogRoot `
        -LogName "resolve_release_root"
    if ($rootReceipt.exit_code -ne 0) {
        throw "Could not resolve the deployed runtime Git root."
    }
    $resolvedRoot = [System.IO.Path]::GetFullPath(
        (Get-Content -LiteralPath $rootReceipt.stdout_path -Raw).Trim()
    ).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    if (-not [string]::Equals(
        $runtimePath,
        $resolvedRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Runtime root must be the exact deployed Git worktree root."
    }

    $beforeReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @("-C", $runtimePath, "rev-parse", "HEAD") `
        -LogRoot $LogRoot `
        -LogName "resolve_release_sha_before_cleanliness"
    if ($beforeReceipt.exit_code -ne 0) {
        throw "Could not resolve the deployed runtime release SHA."
    }
    $shaBefore = (Get-Content -LiteralPath $beforeReceipt.stdout_path -Raw).Trim()
    if ($shaBefore -notmatch "^[0-9a-fA-F]{40}$") {
        throw "Runtime release SHA was not a full Git commit SHA."
    }

    # A commit identity is truthful only when every executable byte in the
    # deployed worktree is represented by that commit.  Include index,
    # worktree, submodule, and non-ignored untracked paths; ignored runtime
    # state remains outside the release identity by repository policy.
    $statusReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @(
            "-C", $runtimePath,
            "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"
        ) `
        -LogRoot $LogRoot `
        -LogName "resolve_release_cleanliness"
    if ($statusReceipt.exit_code -ne 0) {
        throw "Could not verify deployed runtime worktree cleanliness."
    }
    $status = Get-Content -LiteralPath $statusReceipt.stdout_path -Raw
    if (-not [string]::IsNullOrWhiteSpace($status)) {
        throw "Runtime release SHA is untrustworthy because the deployed worktree is dirty."
    }

    $afterReceipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @("-C", $runtimePath, "rev-parse", "HEAD") `
        -LogRoot $LogRoot `
        -LogName "resolve_release_sha_after_cleanliness"
    if ($afterReceipt.exit_code -ne 0) {
        throw "Could not confirm the deployed runtime release SHA."
    }
    $shaAfter = (Get-Content -LiteralPath $afterReceipt.stdout_path -Raw).Trim()
    if (
        $shaAfter -notmatch "^[0-9a-fA-F]{40}$" -or
        $shaBefore -ne $shaAfter
    ) {
        throw "Runtime release SHA changed while verifying deployed bytes."
    }
    return $shaAfter.ToLowerInvariant()
}
