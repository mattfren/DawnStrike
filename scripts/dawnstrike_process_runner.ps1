Set-StrictMode -Version Latest
$script:DawnstrikeExpectedReleaseSha = ""

# Every scheduled child is launched through the native Job Object runner.  A
# PowerShell process tree is not a sufficient ownership boundary on Windows:
# detached Python/Node descendants can outlive the wrapper after a timeout.
# Keep this import local so interactive callers get the same kill-on-close
# contract without needing to know about the implementation helper.
if (-not ("Dawnstrike.Native.JobProcessRunner" -as [type])) {
    . (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
}
if (-not (Get-Command Get-DawnstrikeApprovedLockInterpreter -ErrorAction SilentlyContinue)) {
    . (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")
}

function Assert-DawnstrikeProcessSourceBoundToHead {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ReleaseRoot,
        [string]$ExpectedSha = "",
        [string]$EntryScript = ""
    )

    $root = [System.IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')
    $gitDirectory = Join-Path $root ".git"
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container)) {
        throw "Scheduled Python release root is not a self-contained Git checkout."
    }
    $git = (Get-DawnstrikeApprovedGit).path
    # Windows checkouts may materialize committed LF blobs as CRLF.  Keep the
    # normal Git text normalization contract while disabling all external
    # filters/hooks; otherwise a clean, ordinary checkout is falsely rejected
    # as a byte-substituted release.
    $gitArgs = @('-c', 'core.autocrlf=true', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-C', $root)
    $savedGitEnvironment = @{}
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        $savedGitEnvironment[[string]$entry.Name] = [string]$entry.Value
        Remove-Item -LiteralPath ("Env:" + [string]$entry.Name) -ErrorAction SilentlyContinue
    }
    try {
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_CONFIG_GLOBAL = 'NUL'
        $env:GIT_TERMINAL_PROMPT = '0'
        $env:GIT_OPTIONAL_LOCKS = '0'
        $env:GIT_NO_REPLACE_OBJECTS = '1'
        $top = ((& $git @gitArgs rev-parse --show-toplevel 2>$null) -join '').Trim()
        if ($LASTEXITCODE -ne 0 -or [System.IO.Path]::GetFullPath($top).TrimEnd('\') -ine $root) {
            throw "Scheduled Python release root is not the exact Git root."
        }
        $releaseHead = ((& $git @gitArgs rev-parse HEAD 2>$null) -join '').Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $releaseHead -notmatch '^[0-9a-f]{40}$') {
            throw "Scheduled Python release HEAD is invalid."
        }
        if ($ExpectedSha -and $releaseHead -cne $ExpectedSha.ToLowerInvariant()) {
            throw "Scheduled release HEAD does not match the externally activated SHA."
        }
        $status = ((& $git @gitArgs status --porcelain=v1 --untracked-files=all 2>$null) -join '')
        if ($LASTEXITCODE -ne 0 -or $status) {
            throw "Scheduled Python release checkout is not clean."
        }
        $flags = ((& $git @gitArgs ls-files -v -z 2>$null) -join '')
        if ($LASTEXITCODE -ne 0 -or @($flags -split "`0" | Where-Object { $_ -and $_.Substring(0, 1) -cmatch '[hSs]' }).Count -gt 0) {
            throw "Scheduled Python release contains hidden Git index entries."
        }
        $ignored = ((& $git @gitArgs ls-files --others --ignored --exclude-standard -z 2>$null) -join '')
        if ($LASTEXITCODE -ne 0) { throw "Scheduled Python ignored-artifact inventory failed." }
        $forbiddenIgnored = @(
            $ignored -split "`0" | Where-Object {
                if (-not $_) { return $false }
                $name = [System.IO.Path]::GetFileName($_).ToLowerInvariant()
                $extension = [System.IO.Path]::GetExtension($_).ToLowerInvariant()
                $extension -in @(
                    '.ps1', '.psm1', '.py', '.pyc', '.pyd', '.dll', '.exe',
                    '.com', '.bat', '.cmd', '.sh', '.pth'
                ) -or $name -in @('sitecustomize.py', 'usercustomize.py')
            }
        )
        if ($forbiddenIgnored.Count -gt 0) {
            throw "Scheduled Python release contains ignored executable or startup artifacts."
        }
        $replacements = ((& $git @gitArgs replace -l 2>$null) -join '').Trim()
        if ($LASTEXITCODE -eq 0 -and $replacements) {
            throw "Scheduled Python release contains Git replace refs."
        }
        foreach ($configPattern in @('filter.*', 'core.attributesfile', 'core.hooksPath')) {
            $config = ((& $git @gitArgs config --local --get-regexp $configPattern 2>$null) -join '').Trim()
            if ($LASTEXITCODE -eq 0 -and $config) {
                throw "Scheduled Python release contains a Git execution/filter configuration."
            }
        }
        $null = & $git @gitArgs diff-index --quiet HEAD -- 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Scheduled Python release differs from exact HEAD."
        }
        foreach ($relative in @(
            "scripts/dawnstrike_process_runner.ps1",
            "scripts/dawnstrike_job_process.ps1",
            "scripts/runtime_activation_lock.ps1",
            "scripts/dawnstrike_python_bootstrap.py"
        )) {
            $relative = $relative.Replace('\', '/')
            $path = Join-Path $root ($relative.Replace('/', '\'))
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
                throw "Scheduled Python helper is missing: $relative"
            }
            $headBlob = ((& $git @gitArgs rev-parse ("HEAD:" + $relative) 2>$null) -join '').Trim().ToLowerInvariant()
            if ($LASTEXITCODE -ne 0 -or $headBlob -notmatch '^[0-9a-f]{40}$') {
                throw "Scheduled Python helper is not tracked by exact HEAD: $relative"
            }
            $worktree = ((& $git @gitArgs hash-object ("--path=" + $relative) -- $path 2>$null) -join '').Trim().ToLowerInvariant()
            if ($LASTEXITCODE -ne 0 -or $worktree -cne $headBlob) {
                throw "Scheduled Python helper bytes changed from exact HEAD: $relative"
            }
        }
        if ($EntryScript) {
            $entryPath = [System.IO.Path]::GetFullPath($EntryScript)
            $rootPrefix = $root.TrimEnd('\') + '\'
            if (-not $entryPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Scheduled entry script is outside the exact release root."
            }
            $entryRelative = $entryPath.Substring($rootPrefix.Length).Replace('\', '/')
            $entryHeadBlob = ((& $git @gitArgs rev-parse ("HEAD:" + $entryRelative) 2>$null) -join '').Trim().ToLowerInvariant()
            $entryWorktree = ((& $git @gitArgs hash-object ("--path=" + $entryRelative) -- $entryPath 2>$null) -join '').Trim().ToLowerInvariant()
            if ($LASTEXITCODE -ne 0 -or $entryHeadBlob -notmatch '^[0-9a-f]{40}$' -or $entryWorktree -cne $entryHeadBlob) {
                throw "Scheduled entry script bytes changed from exact HEAD."
            }
        }
        return [pscustomobject]@{ root = $root; head = $releaseHead }
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ("Env:" + [string]$entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $savedGitEnvironment.Keys) { Set-Item -LiteralPath ("Env:" + $name) -Value $savedGitEnvironment[$name] }
    }
}

function Get-DawnstrikeProcessBootstrapPreloader {
    [CmdletBinding()]
    param()
    return "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw(RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
}

function ConvertTo-DawnstrikeIsolatedPythonArguments {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string[]]$ArgumentList,
        [Parameter(Mandatory = $true)][string]$ReleaseRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSha
    )

    $source = @($ArgumentList)
    if ($source.Count -gt 0 -and [string]$source[0] -eq '-3.13') {
        $source = @($source | Select-Object -Skip 1)
    }
    $interpreterOptions = @()
    while ($source.Count -gt 0) {
        $token = [string]$source[0]
        if ($token -in @('-I', '-B', '-S')) {
            $source = @($source | Select-Object -Skip 1)
            continue
        }
        if ($token -eq '-u') {
            $interpreterOptions += $token
            $source = @($source | Select-Object -Skip 1)
            continue
        }
        if ($token -eq '-X') {
            if ($source.Count -lt 2) { throw "Scheduled Python -X option is incomplete." }
            $interpreterOptions += @($token, [string]$source[1])
            $source = @($source | Select-Object -Skip 2)
            continue
        }
        if ($token.StartsWith('-X', [System.StringComparison]::Ordinal)) {
            $interpreterOptions += $token
            $source = @($source | Select-Object -Skip 1)
            continue
        }
        break
    }
    $bootstrap = Join-Path $ReleaseRoot "scripts\dawnstrike_python_bootstrap.py"
    if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
        throw "Scheduled Python release bootstrap is missing."
    }
    $bootstrapSha256 = (Get-FileHash -LiteralPath $bootstrap -Algorithm SHA256).Hash.ToLowerInvariant()
    $bootstrapLaunch = @(
        '-c', (Get-DawnstrikeProcessBootstrapPreloader), $bootstrap, $bootstrapSha256,
        '--release-root', $ReleaseRoot, '--expected-sha', $ExpectedSha
    )
    if ($source.Count -gt 0 -and [string]$source[0] -eq '-m') {
        if ($source.Count -lt 2 -or [string]::IsNullOrWhiteSpace([string]$source[1])) {
            throw "Scheduled Python module target is incomplete."
        }
        $module = [string]$source[1]
        $tail = if ($source.Count -gt 2) { @($source | Select-Object -Skip 2) } else { @() }
        return @('-I', '-B', '-S') + $interpreterOptions + $bootstrapLaunch + @(
            '--module', $module, '--'
        ) + $tail
    }
    if ($source.Count -gt 0 -and [string]$source[0] -notin @('-c', '-')) {
        $script = [string]$source[0]
        if ($script.ToLowerInvariant().EndsWith('.py')) {
            $scriptPath = if ([System.IO.Path]::IsPathRooted($script)) {
                [System.IO.Path]::GetFullPath($script)
            }
            else {
                [System.IO.Path]::GetFullPath((Join-Path $ReleaseRoot $script))
            }
            $rootPrefix = $ReleaseRoot.TrimEnd('\') + '\'
            if (-not $scriptPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Scheduled Python script is outside the exact release root."
            }
            $tail = if ($source.Count -gt 1) { @($source | Select-Object -Skip 1) } else { @() }
            return @('-I', '-B', '-S') + $interpreterOptions + $bootstrapLaunch + @(
                '--script', $scriptPath, '--'
            ) + $tail
        }
    }
    # Inline snippets are not used by governed scheduled stages, but retain
    # their semantics while still forcing -S and clearing startup mappings.
    return @('-I', '-B', '-S') + $interpreterOptions + $source
}

function Invoke-DawnstrikeNativeProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$LogRoot,
        [Parameter(Mandatory = $true)][string]$LogName,
        [Parameter()][ValidateRange(0, 86400)][int]$TimeoutSeconds = 0,
        [Parameter()][ValidateRange(1, 60)][int]$OutputDrainTimeoutSeconds = 5,
        [Parameter()][string]$WorkingDirectory = (Get-Location).Path,
        [Parameter()][hashtable]$EnvironmentOverrides = @{},
        [Parameter()][switch]$NoSite
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
    $effectiveArguments = @($ArgumentList)
    $effectiveEnvironmentOverrides = @{}
    $resolved = $null
    $resolvedExecutableSha256 = $null
    $pythonIsolated = $false
    $pythonBootstrapPath = $null
    $pythonBootstrapSha256 = $null

    try {
        # Windows PowerShell promotes native stderr records to PowerShell error
        # records.  With the scheduled runners' ErrorActionPreference=Stop,
        # ordinary Python logging on stderr otherwise jumps into this catch and
        # is falsely recorded as a process-start failure with exit code 127.
        # Resolve the executable while errors still terminate, then allow the
        # native process to complete and trust its real exit code.
        $requestedLeaf = [System.IO.Path]::GetFileName($FilePath).ToLowerInvariant()
        if ($requestedLeaf -in @("py.exe", "python.exe")) {
            $approved = Get-DawnstrikeApprovedLockInterpreter
            $resolved = [string]$approved.path
            $resolvedExecutableSha256 = [string]$approved.sha256
            $sourceIdentity = Assert-DawnstrikeProcessSourceBoundToHead `
                (Join-Path $PSScriptRoot "..") `
                -ExpectedSha ([string]$script:DawnstrikeExpectedReleaseSha)
            $releaseRoot = [string]$sourceIdentity.root
            $effectiveArguments = ConvertTo-DawnstrikeIsolatedPythonArguments `
                -ArgumentList $effectiveArguments -ReleaseRoot $releaseRoot `
                -ExpectedSha ([string]$sourceIdentity.head)
            $pythonBootstrapPath = Join-Path $releaseRoot "scripts\dawnstrike_python_bootstrap.py"
            $pythonBootstrapSha256 = (Get-FileHash -LiteralPath $pythonBootstrapPath -Algorithm SHA256).Hash.ToLowerInvariant()
            $effectiveEnvironmentOverrides = @{
                PYTHONHOME = $null
                PYTHONPATH = $null
                PYTHONSTARTUP = $null
                PYTHONDONTWRITEBYTECODE = "1"
            }
            $pythonIsolated = $true
        }
        elseif ($requestedLeaf -in @("git", "git.exe")) {
            $approved = Get-DawnstrikeApprovedGit
            $resolved = [string]$approved.path
            $resolvedExecutableSha256 = [string]$approved.sha256
        }
        else {
            $resolved = (Get-Command $FilePath -ErrorAction Stop).Path
            Assert-DawnstrikeSharedLockNoReparse $resolved "Governed native executable"
            $resolvedExecutableSha256 = Get-DawnstrikeRuntimeLockHash $resolved
        }
        foreach ($key in $EnvironmentOverrides.Keys) {
            $effectiveEnvironmentOverrides[[string]$key] = $EnvironmentOverrides[$key]
        }
        $ErrorActionPreference = "Continue"
        # Native runner owns the complete process tree and enforces the
        # deadline.  Do not use PowerShell redirection/pipelines here: those
        # wrappers can outlive the child and obscure its real exit status.
        # The retired wrapper used ``$exitCode = if ($null -eq $LASTEXITCODE)``;
        # the Job Object result now supplies the authoritative native code.
        $result = Invoke-DawnstrikeJobProcess `
            -FilePath $resolved `
            -ArgumentList $effectiveArguments `
            -WorkingDirectory $WorkingDirectory `
            -Label $LogName `
            -TimeoutSeconds $TimeoutSeconds `
            -OutputDrainTimeoutSeconds $OutputDrainTimeoutSeconds `
            -EnvironmentOverrides $effectiveEnvironmentOverrides
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
        argument_count = @($effectiveArguments).Count
        resolved_executable_path = $resolved
        resolved_executable_sha256 = $resolvedExecutableSha256
        python_isolated = $pythonIsolated
        python_bootstrap_path = $pythonBootstrapPath
        python_bootstrap_sha256 = $pythonBootstrapSha256
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
        [Parameter(Mandatory = $true)][string]$LogRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha
    )

    $runtimePath = [System.IO.Path]::GetFullPath(
        (Resolve-Path -LiteralPath $RuntimeRoot).Path
    ).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $null = Assert-DawnstrikeProcessSourceBoundToHead -ReleaseRoot $runtimePath -ExpectedSha $ExpectedSha
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
    if ($shaBefore -cne $ExpectedSha) {
        throw "Runtime release SHA does not match the externally activated task SHA."
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
    $script:DawnstrikeExpectedReleaseSha = $ExpectedSha.ToLowerInvariant()
    return $script:DawnstrikeExpectedReleaseSha
}
