[CmdletBinding()]
param(
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$ExpectedSha = "",
    [ValidatePattern('^$|^\d{4}-\d{2}-\d{2}$')][string]$MarketDate = "",
    [string]$CiEvidencePath = "",
    [string]$SolEvidencePath = "",
    [string]$CandidateRoot = "",
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [string]$BackupRoot = "C:\r\dawnstrike-state-backups",
    [ValidateRange(1, 120)][int]$BackupRetention = 30,
    [ValidateRange(30, 1800)][int]$ProcessTimeoutSeconds = 300,
    [pscredential]$RunAsCredential,
    [switch]$PreflightOnly,
    [switch]$InjectCrashBetweenRuntimeRenames,
    [ValidateSet(
        "",
        "after_stage_directory",
        "after_stage_checkout",
        "after_init_recovery_lock_release",
        "after_pre_quiesce_recovery_lock_release",
        "after_candidate_runtime_rename",
        "after_ready_journal",
        "after_enable_before_complete",
        "after_complete_journal"
    )][string]$TestStageCrashPoint = "",
    [string]$TestNowUtc = ""
)

$ErrorActionPreference = "Stop"
$script:DawnstrikePowerShellExecutable = "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
if ($InjectCrashBetweenRuntimeRenames -and $env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") {
    throw "Activation runtime-rename crash injection is test-only."
}
if (
    [int]$PSVersionTable.PSVersion.Major -lt 5 -or
    [string]$PSVersionTable.PSEdition -ne "Desktop"
) {
    throw "Dawnstrike activation requires Windows PowerShell 5.1 or later (Desktop edition)."
}

$script:DawnstrikeCanonicalTaskNames = @(
    "Dawnstrike AlphaOps Morning",
    "Dawnstrike AlphaOps Monitor 5m",
    "Dawnstrike AlphaOps EOD Full Report",
    "Dawnstrike AlphaOps V6 Weekly Training",
    "Dawnstrike 10of10 Daily Finalize"
)
$script:DawnstrikeAuxiliaryCaptureTaskName = "Dawnstrike Delayed SIP Capture"
$script:DawnstrikeStatePreparationContractFile = "config\state_preparation_contract.json"

function Get-DawnstrikeSha256Text {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
}

function Get-DawnstrikeSha256File {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read
        )
        return ([System.BitConverter]::ToString($sha.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        if ($null -ne $stream) { $stream.Dispose() }
        $sha.Dispose()
    }
}

function Resolve-DawnstrikeActivationRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    Assert-DawnstrikeNoReparseComponents $Path $Label
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "$Label must be an existing directory."
    }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label cannot be a reparse point."
    }
    return $item.FullName.TrimEnd('\')
}

function Assert-DawnstrikeNoReparseComponents {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [System.IO.Path]::GetFullPath($Path)
    $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
    if ($null -ne $item -and ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "$Label contains a reparse-point path component."
    }
    $current = if ($null -ne $item -and $item.PSIsContainer) {
        [System.IO.DirectoryInfo]::new($item.FullName)
    }
    else {
        [System.IO.DirectoryInfo]::new((Split-Path -Parent $full))
    }
    while ($null -ne $current) {
        if ($current.Exists -and ($current.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point path component."
        }
        if ([string]::Equals($current.FullName.TrimEnd('\'), $current.Root.FullName.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $current = $current.Parent
    }
}

function Get-DawnstrikeFutureActivationRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Path -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be an absolute drive-qualified directory."
    }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $pathRoot = [System.IO.Path]::GetPathRoot($fullPath)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) {
        throw "$Label does not have a valid filesystem root."
    }
    if ($fullPath.Length -gt $pathRoot.Length) {
        $fullPath = $fullPath.TrimEnd('\')
    }
    $missing = New-Object System.Collections.Generic.List[string]
    $cursor = $fullPath
    while (-not (Test-Path -LiteralPath $cursor -PathType Container)) {
        $missing.Add($cursor)
        $parent = Split-Path -Parent $cursor
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $cursor) {
            throw "$Label parent directory does not exist."
        }
        $cursor = $parent.TrimEnd('\')
    }
    $null = Resolve-DawnstrikeActivationRoot $cursor $Label
    return $fullPath
}

function Ensure-DawnstrikeActivationRoot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $fullPath = Get-DawnstrikeFutureActivationRoot $Path $Label
    if (-not (Test-Path -LiteralPath $fullPath -PathType Container)) {
        New-Item -ItemType Directory -Path $fullPath -ErrorAction Stop | Out-Null
    }
    return Resolve-DawnstrikeActivationRoot $fullPath $Label
}

function Assert-DawnstrikeRootIsolation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$OtherPaths,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if ($Path -notmatch '^[A-Za-z]:\\') {
        throw "$Label must be an absolute drive-qualified directory."
    }
    Assert-DawnstrikeNoReparseComponents $Path $Label
    $candidate = [System.IO.Path]::GetFullPath($Path).TrimEnd('\') + '\'
    foreach ($otherPath in $OtherPaths) {
        Assert-DawnstrikeNoReparseComponents $otherPath "$Label comparison root"
        $other = [System.IO.Path]::GetFullPath($otherPath).TrimEnd('\') + '\'
        if (
            [string]::Equals($candidate, $other, [System.StringComparison]::OrdinalIgnoreCase) -or
            $candidate.StartsWith($other, [System.StringComparison]::OrdinalIgnoreCase) -or
            $other.StartsWith($candidate, [System.StringComparison]::OrdinalIgnoreCase)
        ) {
            throw "$Label must be separate from candidate, runtime, and state roots."
        }
    }
}

function Assert-DawnstrikeSafeOrigin {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Origin)

    if ([string]::IsNullOrWhiteSpace($Origin)) {
        throw "Candidate origin is missing."
    }
    $lower = $Origin.ToLowerInvariant()
    if (
        $lower -match '(gh[pousr]_|oauth|password|access[_-]?token|private[_-]?key)' -or
        $Origin.Contains("?") -or
        $Origin.Contains("#")
    ) {
        throw "Candidate origin contains forbidden credential-like material."
    }
    if ($lower -match '^https?://[^/]*@') {
        throw "Candidate HTTPS origin cannot contain user information."
    }
}

function Get-DawnstrikeActivationBootstrapPreloader {
    [CmdletBinding()]
    param()
    return "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw(RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
}

function Invoke-DawnstrikeActivationProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $effectiveArguments = @($ArgumentList)
    $approvedPythonPath = 'C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe'
    if ([string]::Equals(
            [System.IO.Path]::GetFullPath($FilePath),
            $approvedPythonPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {
        if ($effectiveArguments.Count -lt 2 -or $effectiveArguments[0] -ne '-I' -or $effectiveArguments[1] -ne '-B') {
            $effectiveArguments = @('-I', '-B') + $effectiveArguments
        }
        if ($effectiveArguments.Count -lt 3 -or $effectiveArguments[2] -ne '-S') {
            $effectiveArguments = @($effectiveArguments[0..1]) + @('-S') + @($effectiveArguments | Select-Object -Skip 2)
        }
        # Every candidate Python script imports intraday_scanner.  With -S,
        # Python intentionally skips site.py, so invoke it through the
        # materialized release bootstrap which adds only the approved
        # interpreter dependency directories and exact release root.
        $scriptIndex = -1
        for ($index = 0; $index -lt $effectiveArguments.Count; $index++) {
            if ([string]$effectiveArguments[$index] -match '(?i)\.py$') {
                $scriptIndex = $index
                break
            }
        }
        if ($scriptIndex -ge 0) {
            $pythonScript = [System.IO.Path]::GetFullPath([string]$effectiveArguments[$scriptIndex])
            if (-not $pythonScript.EndsWith("dawnstrike_python_bootstrap.py", [System.StringComparison]::OrdinalIgnoreCase)) {
                $scriptsDirectory = Split-Path -Parent $pythonScript
                $releaseRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptsDirectory ".."))
                $bootstrap = Join-Path $scriptsDirectory "dawnstrike_python_bootstrap.py"
                Assert-DawnstrikeNoReparseComponents $pythonScript "Python release script"
                Assert-DawnstrikeNoReparseComponents $releaseRoot "Python release root"
                if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
                    throw "Python release bootstrap is missing."
                }
                $bootstrapSha256 = Get-DawnstrikeSha256File $bootstrap
                $releaseRootSha = Get-DawnstrikeGitValue `
                    -GitPath (Get-DawnstrikeApprovedGit).path `
                    -Root $releaseRoot `
                    -Arguments @("rev-parse", "HEAD") `
                    -Label "Python release HEAD binding" `
                    -TimeoutSeconds $TimeoutSeconds
                if ($releaseRootSha -notmatch '^[0-9a-f]{40}$') {
                    throw "Python release HEAD is invalid."
                }
                $tail = if ($effectiveArguments.Count -gt ($scriptIndex + 1)) {
                    @($effectiveArguments | Select-Object -Skip ($scriptIndex + 1))
                } else { @() }
                $effectiveArguments = @(
                    '-I', '-B', '-S', '-c',
                    (Get-DawnstrikeActivationBootstrapPreloader),
                    $bootstrap, $bootstrapSha256,
                    '--release-root', $releaseRoot, '--expected-sha', $releaseRootSha,
                    '--script', $pythonScript,
                    '--'
                ) + $tail
            }
        }
    }
    $savedGitEnvironment = @{}
    $isGitProcess = [System.IO.Path]::GetFileName($FilePath).Equals("git.exe", [System.StringComparison]::OrdinalIgnoreCase)
    if ($isGitProcess) {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            $savedGitEnvironment[[string]$entry.Name] = [string]$entry.Value
            Remove-Item -LiteralPath ("Env:" + [string]$entry.Name) -ErrorAction SilentlyContinue
        }
    }
    try {
        $environment = @{ PYTHONDONTWRITEBYTECODE = "1" }
        if ($isGitProcess) {
            $environment.GIT_CONFIG_NOSYSTEM = "1"
            $environment.GIT_CONFIG_GLOBAL = "NUL"
            $environment.GIT_TERMINAL_PROMPT = "0"
            $environment.GIT_OPTIONAL_LOCKS = "0"
            $environment.GIT_NO_REPLACE_OBJECTS = "1"
        }
        $result = Invoke-DawnstrikeJobProcess `
            -FilePath $FilePath `
            -ArgumentList $effectiveArguments `
            -WorkingDirectory $WorkingDirectory `
            -Label $Label `
            -TimeoutSeconds $TimeoutSeconds `
            -OutputDrainTimeoutSeconds 5 `
            -EnvironmentOverrides $environment
    }
    finally {
        if ($isGitProcess) {
            foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
                Remove-Item -LiteralPath ("Env:" + [string]$entry.Name) -ErrorAction SilentlyContinue
            }
            foreach ($name in $savedGitEnvironment.Keys) {
                Set-Item -LiteralPath ("Env:" + $name) -Value $savedGitEnvironment[$name]
            }
        }
    }
    if ($result.ExitCode -ne 0) {
        # Do not echo native stderr. Remote helpers and environment-specific
        # tooling may include authentication material in their diagnostics.
        throw "$Label failed with exit code $($result.ExitCode)."
    }
    return $result
}

function Get-DawnstrikeActivationNowUtc {
    [CmdletBinding()]
    param([string]$TestNowUtc = "")

    if (-not [string]::IsNullOrWhiteSpace($TestNowUtc)) {
        if ($env:DAWNSTRIKE_TEST_ACTIVATION_CLOCK -ne "1") {
            throw "Activation clock override is test-only."
        }
        try {
            $parsed = [DateTimeOffset]::Parse(
                $TestNowUtc,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            )
        }
        catch {
            throw "Activation clock override is invalid."
        }
        if ($parsed.Offset -ne [TimeSpan]::Zero) {
            throw "Activation clock override must be UTC."
        }
        return $parsed.ToUniversalTime()
    }
    return [DateTimeOffset]::UtcNow
}

function Invoke-DawnstrikeActivationBoundary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][DateTimeOffset]$NowUtc,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $contract = Join-Path $CandidateRoot "scripts\runtime_activation_contract.py"
    if (-not (Test-Path -LiteralPath $contract -PathType Leaf)) {
        throw "Activation boundary contract is missing."
    }
    $result = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @(
            $contract,
            "validate-activation-boundary",
            "--market-date", $MarketDate,
            "--now-utc", $NowUtc.ToUniversalTime().ToString("o"),
            "--state-root", $StateRoot,
            "--runtime-root", $RuntimeRoot
        ) `
        -WorkingDirectory $CandidateRoot `
        -Label "Runtime activation market boundary" `
        -TimeoutSeconds $TimeoutSeconds
    try {
        $payload = $result.Stdout | ConvertFrom-Json
    }
    catch {
        throw "Runtime activation market boundary returned invalid output."
    }
    if ($payload.status -ne "PASS" -or $payload.ready -ne $true) {
        $reasons = @($payload.errors | ForEach-Object { [string]$_ }) -join ","
        throw "Runtime activation market boundary is blocked: $reasons"
    }
    return $payload
}

function Get-DawnstrikeGitValue {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $result = Invoke-DawnstrikeActivationProcess `
        -FilePath $GitPath `
        -ArgumentList (@("-c", "core.autocrlf=true", "-C", $Root) + $Arguments) `
        -WorkingDirectory $Root `
        -Label $Label `
        -TimeoutSeconds $TimeoutSeconds
    return ([string]$result.Stdout).Trim()
}

function Get-DawnstrikeGitContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [string]$ExpectedCommit = ""
    )

    $gitDirectory = Join-Path $Root ".git"
    if (-not (Test-Path -LiteralPath $gitDirectory -PathType Container)) {
        throw "Runtime activation accepts only a self-contained Git checkout."
    }
    $top = Get-DawnstrikeGitValue $GitPath $Root @("rev-parse", "--show-toplevel") "Git root verification" $TimeoutSeconds
    if (-not [string]::Equals(
        [System.IO.Path]::GetFullPath($top).TrimEnd('\'),
        [System.IO.Path]::GetFullPath($Root).TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Git checkout root does not match the requested activation root."
    }
    $head = (Get-DawnstrikeGitValue $GitPath $Root @("rev-parse", "HEAD") "Git HEAD verification" $TimeoutSeconds).ToLowerInvariant()
    $tree = (Get-DawnstrikeGitValue $GitPath $Root @("rev-parse", "HEAD^{tree}") "Git tree verification" $TimeoutSeconds).ToLowerInvariant()
    if ($head -notmatch '^[0-9a-f]{40}$' -or $tree -notmatch '^[0-9a-f]{40}$') {
        throw "Git checkout identity is invalid."
    }
    if ($ExpectedCommit -and $head -ne $ExpectedCommit) {
        throw "Git checkout HEAD does not equal the expected release SHA."
    }
    $status = Get-DawnstrikeGitValue $GitPath $Root @("status", "--porcelain=v1", "--untracked-files=all") "Git cleanliness verification" $TimeoutSeconds
    if ($status) {
        throw "Git checkout is not clean."
    }
    # ``status`` and the normal diff machinery intentionally trust the index.
    # An attacker can hide modified tracked bytes with assume-unchanged or
    # skip-worktree, so those index bits are themselves a hard failure.  This
    # check must happen before any release helper is trusted for mutation.
    $indexEntries = Get-DawnstrikeGitValue $GitPath $Root @("ls-files", "-v", "-z") "Git index flag verification" $TimeoutSeconds
    $hiddenIndexEntries = @(
        ([string]$indexEntries).Split([char]0, [System.StringSplitOptions]::RemoveEmptyEntries) |
            Where-Object {
                # ``git ls-files -v`` uses lowercase ``h`` for
                # assume-unchanged and uppercase ``S`` for skip-worktree;
                # normal cached entries are uppercase ``H``.  Check both
                # hidden-index spellings explicitly instead of relying on
                # porcelain status, which intentionally trusts the index.
                $_.Length -gt 2 -and $_.Substring(0, 1) -cmatch "[hSs]"
            }
    )
    if ($hiddenIndexEntries.Count -gt 0) {
        throw "Git checkout contains assume-unchanged or skip-worktree entries."
    }
    $replacements = Get-DawnstrikeGitValue $GitPath $Root @("replace", "-l") "Git replace-ref verification" $TimeoutSeconds
    if ($replacements) {
        throw "Git checkout contains replace refs; activation cannot trust the object identity."
    }
    $localConfigPath = Join-Path $gitDirectory "config"
    if (-not (Test-Path -LiteralPath $localConfigPath -PathType Leaf)) {
        throw "Git checkout local configuration is missing."
    }
    $localConfig = Get-Content -Raw -LiteralPath $localConfigPath
    if ($localConfig -match "(?im)^\s*\[\s*filter(?:\s|\])|^\s*(?:attributesfile|hookspath|path)\s*=") {
        throw "Git checkout contains a local execution/filter configuration."
    }
    $ignored = Get-DawnstrikeGitValue $GitPath $Root @("ls-files", "--others", "--ignored", "--exclude-standard", "-z") "Ignored runtime artifact verification" $TimeoutSeconds
    $forbiddenIgnored = @(
        ([string]$ignored).Split([char]0, [System.StringSplitOptions]::RemoveEmptyEntries) |
            Where-Object {
                $name = [System.IO.Path]::GetFileName($_).ToLowerInvariant()
                $extension = [System.IO.Path]::GetExtension($_).ToLowerInvariant()
                $extension -in @(
                    ".ps1", ".psm1", ".py", ".pyc", ".pyd", ".dll", ".exe",
                    ".com", ".bat", ".cmd", ".sh", ".pth"
                ) -or $name -in @("sitecustomize.py", "usercustomize.py")
            }
    )
    if ($forbiddenIgnored.Count -gt 0) {
        throw "Git checkout contains ignored executable or Python-startup artifacts."
    }
    return [pscustomobject]@{ head = $head; tree = $tree }
}

function Assert-DawnstrikeHelpersBoundToHead {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    # These files are sourced or executed before/around the activation swap.
    # Compare their worktree Git blobs with the exact HEAD blobs rather than
    # trusting status (which is deliberately checked separately for hidden
    # index flags above).  A missing helper is also a fail-closed result.
    $helpers = @(
        "scripts/activate_dawnstrike_runtime.ps1",
        "scripts/rollback_dawnstrike_runtime.ps1",
        "scripts/capture_task_safety.ps1",
        "scripts/runtime_activation_lock.ps1",
        "scripts/runtime_activation_lock_contract.py",
        "scripts/runtime_operation_journal.py",
        "scripts/dawnstrike_job_process.ps1",
        "scripts/dawnstrike_process_runner.ps1",
        "scripts/invoke_dawnstrike_stage.ps1",
        "scripts/runtime_activation_contract.py",
        "scripts/state_disaster_recovery.py",
        "scripts/dawnstrike_python_bootstrap.py"
    )
    foreach ($relative in $helpers) {
        $relative = $relative.Replace("\", "/")
        $path = Join-Path $Root ($relative.Replace("/", "\"))
        Assert-DawnstrikeNoReparseComponents $path "Release helper"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Release helper is missing: $relative"
        }
        $headBlob = Get-DawnstrikeGitValue $GitPath $Root @("rev-parse", ("HEAD:" + $relative)) "Release helper HEAD binding" $TimeoutSeconds
        if ($headBlob -notmatch "^[0-9a-f]{40}$") {
            throw "Release helper is not tracked by the exact HEAD: $relative"
        }
        $worktreeBlob = Get-DawnstrikeGitValue $GitPath $Root @("hash-object", ("--path=" + $relative), "--", $path) "Release helper worktree binding" $TimeoutSeconds
        if ($worktreeBlob -cne $headBlob) {
            throw "Release helper bytes do not match exact HEAD: $relative"
        }
    }
}

function Get-DawnstrikeTaskProperty {
    [CmdletBinding()]
    param(
        [AllowNull()][object]$Object,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-DawnstrikeCanonicalTaskPolicy {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha = ""
    )

    $runners = @{
        "Dawnstrike AlphaOps Morning" = "run_alphaops_morning.ps1"
        "Dawnstrike AlphaOps Monitor 5m" = "run_alphaops_monitor.ps1"
        "Dawnstrike AlphaOps EOD Full Report" = "run_alphaops_eod.ps1"
        "Dawnstrike AlphaOps V6 Weekly Training" = "run_alphaops_weekly_training.ps1"
        "Dawnstrike 10of10 Daily Finalize" = "run_daily_finalize.ps1"
    }
    if (-not $runners.ContainsKey($TaskName)) {
        throw "Unknown canonical Dawnstrike task: $TaskName"
    }
    $runtime = [System.IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $state = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\')
    $runner = Join-Path $runtime ("scripts\" + [string]$runners[$TaskName])
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -RuntimeRoot `"$runtime`" -StateRoot `"$state`""
    if ($ExpectedSha) { $arguments += " -ExpectedSha `"$ExpectedSha`"" }
    if ($TaskName -eq "Dawnstrike 10of10 Daily Finalize") {
        $arguments += " -PublicationMode Production -VercelProjectId `"prj_5pef3EZF1u5YadebEz3dFjnkWOXy`""
    }
    $weekly = $TaskName -ne "Dawnstrike 10of10 Daily Finalize"
    $days = if ($TaskName -eq "Dawnstrike AlphaOps V6 Weekly Training") { 2 } elseif ($weekly) { 62 } else { $null }
    $start = switch ($TaskName) {
        "Dawnstrike AlphaOps Morning" { "08:00"; break }
        "Dawnstrike AlphaOps Monitor 5m" { "08:35"; break }
        "Dawnstrike AlphaOps EOD Full Report" { "15:15"; break }
        "Dawnstrike AlphaOps V6 Weekly Training" { "21:00"; break }
        default { "17:30" }
    }
    $executionLimit = switch ($TaskName) {
        "Dawnstrike AlphaOps Morning" { "PT1H"; break }
        "Dawnstrike AlphaOps Monitor 5m" { "PT4M"; break }
        "Dawnstrike AlphaOps EOD Full Report" { "PT2H"; break }
        default { "PT3H" }
    }
    $restartCount = switch ($TaskName) {
        "Dawnstrike AlphaOps V6 Weekly Training" { 4; break }
        "Dawnstrike 10of10 Daily Finalize" { 2; break }
        default { 3 }
    }
    $restartInterval = if ($TaskName -in @("Dawnstrike AlphaOps V6 Weekly Training", "Dawnstrike 10of10 Daily Finalize")) { "PT15M" } else { "PT5M" }
    return [pscustomobject]@{
        arguments = $arguments
        runner = $runner
        weekly = $weekly
        days = $days
        start = $start
        execution_limit = $executionLimit
        restart_count = $restartCount
        restart_interval = $restartInterval
        monitor = $TaskName -eq "Dawnstrike AlphaOps Monitor 5m"
    }
}

function Assert-DawnstrikeCanonicalTaskSemantics {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha = "",
        [switch]$AllowDisabled
    )

    $results = @()
    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $policy = if ($ExpectedSha) {
            Get-DawnstrikeCanonicalTaskPolicy $taskName $RuntimeRoot $StateRoot $ExpectedSha
        }
        else {
            Get-DawnstrikeCanonicalTaskPolicy $taskName $RuntimeRoot $StateRoot
        }
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) { throw "Canonical task semantic check requires exactly one task: $taskName" }
        $task = $matches[0]
        $taskPath = [string](Get-DawnstrikeTaskProperty $task "TaskPath")
        if ([string]::IsNullOrWhiteSpace($taskPath)) {
            throw "Canonical task path is missing: $taskName"
        }
        if ($taskPath -cne "\") { throw "Canonical task path drifted: $taskName" }
        $state = [string](Get-DawnstrikeTaskProperty $task "State")
        if ($state -eq "Ready") { }
        elseif ($AllowDisabled -and $state -eq "Disabled") { }
        else { throw "Canonical task state is not an approved boundary: $taskName state=$state" }

        $actions = @((Get-DawnstrikeTaskProperty $task "Actions"))
        if ($actions.Count -ne 1) { throw "Canonical task must have exactly one action: $taskName" }
        $action = $actions[0]
        if ([string](Get-DawnstrikeTaskProperty $action "Execute") -cne $script:DawnstrikePowerShellExecutable) {
            throw "Canonical task executable is not the pinned Windows PowerShell binary: $taskName"
        }
        $actualArguments = [string](Get-DawnstrikeTaskProperty $action "Arguments")
        $argumentsMatch = $actualArguments -ceq [string]$policy.arguments
        if (-not $ExpectedSha) {
            # During migration/recovery the task may still carry a previously
            # activated SHA.  Accept that bounded suffix only at the
            # pre-rebind boundary; the caller must immediately replace it and
            # perform a second exact-SHA assertion before enabling the task.
            $argumentsMatch = $argumentsMatch -or ($actualArguments -cmatch ([regex]::Escape([string]$policy.arguments) + ' -ExpectedSha "[0-9a-f]{40}"$'))
        }
        if (-not $argumentsMatch) {
            throw "Canonical task arguments are not the exact governed action: $taskName"
        }
        if ([string](Get-DawnstrikeTaskProperty $action "WorkingDirectory") -cne [string]$RuntimeRoot) {
            throw "Canonical task working directory is not the exact runtime root: $taskName"
        }

        $triggers = @((Get-DawnstrikeTaskProperty $task "Triggers"))
        if ($triggers.Count -ne 1) { throw "Canonical task must have exactly one trigger: $taskName" }
        $trigger = $triggers[0]
        $cimClass = Get-DawnstrikeTaskProperty $trigger "CimClass"
        $triggerType = [string](Get-DawnstrikeTaskProperty $cimClass "CimClassName")
        $expectedType = if ($policy.weekly) { "MSFT_TaskWeeklyTrigger" } else { "MSFT_TaskDailyTrigger" }
        if ($triggerType -cne $expectedType) { throw "Canonical task trigger type drifted: $taskName" }
        if ((Get-DawnstrikeTaskProperty $trigger "Enabled") -ne $true) { throw "Canonical task trigger is disabled: $taskName" }
        try {
            $boundary = [DateTimeOffset]::Parse(
                [string](Get-DawnstrikeTaskProperty $trigger "StartBoundary"),
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind
            )
        }
        catch { throw "Canonical task trigger start boundary is invalid: $taskName" }
        if ($boundary.Hour -ne [int]$policy.start.Split(':')[0] -or $boundary.Minute -ne [int]$policy.start.Split(':')[1] -or $boundary.Second -ne 0) {
            throw "Canonical task trigger start time drifted: $taskName"
        }
        if (-not [string]::IsNullOrWhiteSpace([string](Get-DawnstrikeTaskProperty $trigger "EndBoundary")) -or -not [string]::IsNullOrWhiteSpace([string](Get-DawnstrikeTaskProperty $trigger "RandomDelay"))) {
            throw "Canonical task trigger has an unexpected end boundary or random delay: $taskName"
        }
        if ($policy.weekly) {
            if ([int](Get-DawnstrikeTaskProperty $trigger "DaysOfWeek") -ne [int]$policy.days -or [int](Get-DawnstrikeTaskProperty $trigger "WeeksInterval") -ne 1) {
                throw "Canonical weekly task trigger calendar drifted: $taskName"
            }
            if ($null -ne (Get-DawnstrikeTaskProperty $trigger "DaysInterval")) { throw "Weekly task has a daily interval: $taskName" }
        }
        else {
            if ([int](Get-DawnstrikeTaskProperty $trigger "DaysInterval") -ne 1) { throw "Canonical daily task interval drifted: $taskName" }
            if ($null -ne (Get-DawnstrikeTaskProperty $trigger "DaysOfWeek")) { throw "Daily task has a weekly calendar: $taskName" }
        }
        $repetition = Get-DawnstrikeTaskProperty $trigger "Repetition"
        if ($policy.monitor) {
            if ($null -eq $repetition -or [string](Get-DawnstrikeTaskProperty $repetition "Interval") -cne "PT5M" -or [string](Get-DawnstrikeTaskProperty $repetition "Duration") -cne "PT6H35M" -or (Get-DawnstrikeTaskProperty $repetition "StopAtDurationEnd") -ne $true) {
                throw "Canonical monitor repetition contract drifted."
            }
        }
        elseif (
            $null -eq $repetition -or
            -not [string]::IsNullOrWhiteSpace([string](Get-DawnstrikeTaskProperty $repetition "Interval")) -or
            -not [string]::IsNullOrWhiteSpace([string](Get-DawnstrikeTaskProperty $repetition "Duration")) -or
            (Get-DawnstrikeTaskProperty $repetition "StopAtDurationEnd") -ne $false
        ) { throw "Non-monitor task has an unexpected repetition: $taskName" }

        $principal = Get-DawnstrikeTaskProperty $task "Principal"
        $logonType = [string](Get-DawnstrikeTaskProperty $principal "LogonType")
        if ($logonType -notin @("Password", "ServiceAccount") -or [string]::IsNullOrWhiteSpace([string](Get-DawnstrikeTaskProperty $principal "UserId")) -or [string](Get-DawnstrikeTaskProperty $principal "RunLevel") -cne "Limited") {
            throw "Canonical task principal semantics drifted: $taskName"
        }
        $settings = Get-DawnstrikeTaskProperty $task "Settings"
        $enabled = Get-DawnstrikeTaskProperty $settings "Enabled"
        $expectedEnabled = $state -eq "Ready"
        if ($enabled -ne $expectedEnabled -or (Get-DawnstrikeTaskProperty $settings "StartWhenAvailable") -ne $true -or (Get-DawnstrikeTaskProperty $settings "WakeToRun") -ne $true -or (Get-DawnstrikeTaskProperty $settings "StopIfGoingOnBatteries") -ne $false -or (Get-DawnstrikeTaskProperty $settings "DisallowStartIfOnBatteries") -ne $false -or [string](Get-DawnstrikeTaskProperty $settings "MultipleInstances") -cne "IgnoreNew" -or [string](Get-DawnstrikeTaskProperty $settings "ExecutionTimeLimit") -cne [string]$policy.execution_limit -or [int](Get-DawnstrikeTaskProperty $settings "RestartCount") -ne [int]$policy.restart_count -or [string](Get-DawnstrikeTaskProperty $settings "RestartInterval") -cne [string]$policy.restart_interval -or (Get-DawnstrikeTaskProperty $settings "Hidden") -ne $false -or (Get-DawnstrikeTaskProperty $settings "RunOnlyIfIdle") -ne $false -or (Get-DawnstrikeTaskProperty $settings "RunOnlyIfNetworkAvailable") -ne $false -or (Get-DawnstrikeTaskProperty $settings "UseUnifiedSchedulingEngine") -ne $true) {
            throw "Canonical task settings semantics drifted: $taskName"
        }
        $results += [pscustomobject]@{ name = $taskName; state = $state; action = [string]$policy.arguments }
    }
    return $results
}

function Get-DawnstrikeTaskDefinitionText {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Xml)

    try {
        $document = [System.Xml.XmlDocument]::new()
        # Task Scheduler omits Settings/Enabled when the task is Ready because
        # the schema default is true, but emits it after Disable-ScheduledTask.
        # Parse structurally and remove that one state-only element so the
        # definition contract remains stable across the enable/disable window.
        $document.PreserveWhitespace = $false
        $document.LoadXml($Xml)
        $namespace = [string]$document.DocumentElement.NamespaceURI
        if ([string]::IsNullOrWhiteSpace($namespace)) {
            $enabledNodes = @($document.SelectNodes("/Task/Settings/Enabled"))
        }
        else {
            $manager = [System.Xml.XmlNamespaceManager]::new($document.NameTable)
            $manager.AddNamespace("task", $namespace)
            $enabledNodes = @($document.SelectNodes("/task:Task/task:Settings/task:Enabled", $manager))
        }
        if ($enabledNodes.Count -gt 1) {
            throw "Task XML must contain at most one Settings/Enabled element."
        }
        if ($enabledNodes.Count -eq 1) {
            $null = $enabledNodes[0].ParentNode.RemoveChild($enabledNodes[0])
        }
        return [string]$document.OuterXml
    }
    catch {
        throw "Canonical task XML cannot produce an enablement-independent definition contract."
    }
}

function Get-DawnstrikeStatePreparationDeclaration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [string]$GitPath = "",
        [string]$CandidateSha = "",
        [string]$CandidateTree = "",
        [string]$PythonPath = "",
        [ValidateRange(30, 1800)][int]$TimeoutSeconds = 300
    )

    if ([string]::IsNullOrWhiteSpace($GitPath)) {
        $GitPath = (Get-DawnstrikeApprovedGit).path
    }
    if ([string]::IsNullOrWhiteSpace($CandidateSha) -or [string]::IsNullOrWhiteSpace($CandidateTree)) {
        $identity = Get-DawnstrikeGitContract $GitPath $CandidateRoot $TimeoutSeconds
        if ([string]::IsNullOrWhiteSpace($CandidateSha)) { $CandidateSha = [string]$identity.head }
        if ([string]::IsNullOrWhiteSpace($CandidateTree)) { $CandidateTree = [string]$identity.tree }
    }
    if ($CandidateSha -notmatch '^[0-9a-f]{40}$' -or $CandidateTree -notmatch '^[0-9a-f]{40}$') {
        throw "State-preparation declaration requires an exact candidate commit and tree."
    }
    $declaredTree = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "rev-parse", ($CandidateSha + "^{tree}")
    ) "State-preparation declaration tree identity" $TimeoutSeconds).ToLowerInvariant()
    if ($declaredTree -ne $CandidateTree) {
        throw "State-preparation declaration candidate tree identity is invalid."
    }
    $path = Join-Path $CandidateRoot $script:DawnstrikeStatePreparationContractFile
    Assert-DawnstrikeNoReparseComponents $path "State-preparation declaration"

    # The working tree is not the authority for declaration presence.  A
    # delete/restore between the clean-check and this read must not turn a
    # sidecar-bearing commit into an implicit legacy activation (or substitute
    # a hostile declaration).  Bind both presence and the raw file bytes to
    # the exact commit object recorded by the exact tree.
    $relativePath = $script:DawnstrikeStatePreparationContractFile.Replace('\', '/')
    $treePaths = @(@(Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "ls-tree", "-r", "--full-tree", "--name-only", $CandidateSha, "--", $relativePath
    ) "State-preparation declaration tree binding" $TimeoutSeconds) |
        ForEach-Object { ([string]$_).Split("`n", [System.StringSplitOptions]::RemoveEmptyEntries) } |
        Where-Object { $_ -ne "" })
    if ($treePaths.Count -eq 0) {
        if (Test-Path -LiteralPath $path) {
            throw "Candidate declaration exists but the exact candidate commit does not track it."
        }
        # Older runtimes predate the account/capture/trial sidecar.  Legacy
        # compatibility is valid only when the exact candidate commit truly
        # lacks the declaration.
        return [pscustomobject]@{
            required = $false
            path = $path
            declaration_present = $false
            declaration_blob_sha = ""
        }
    }
    if ($treePaths.Count -ne 1 -or $treePaths[0] -ne $relativePath) {
        throw "State-preparation declaration tree binding is not unique."
    }
    $declarationBlobSha = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "rev-parse", ("{0}:{1}" -f $CandidateSha, $relativePath)
    ) "State-preparation declaration blob binding" $TimeoutSeconds).ToLowerInvariant()
    if ($declarationBlobSha -notmatch '^[0-9a-f]{40}$') {
        throw "State-preparation declaration blob identity is invalid."
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "State-preparation declaration is missing from the exact candidate checkout."
    }
    $workingBlobSha = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "hash-object", ("--path={0}" -f $relativePath), "--", $path
    ) "State-preparation declaration working-tree binding" $TimeoutSeconds).ToLowerInvariant()
    if ($workingBlobSha -ne $declarationBlobSha) {
        throw "State-preparation declaration bytes do not match the exact candidate commit."
    }
    if ([string]::IsNullOrWhiteSpace($PythonPath)) {
        $PythonPath = (Get-DawnstrikeApprovedLockInterpreter).path
    }
    # Validate the raw bytes through the strict Python contract loader before
    # PowerShell parses them. ConvertFrom-Json silently accepts duplicate
    # properties (last value wins), which would let a hostile sidecar replace
    # an otherwise valid declaration at this activation boundary.
    $validated = Invoke-DawnstrikeContractCli `
        -PythonPath $PythonPath `
        -CandidateRoot $CandidateRoot `
        -Arguments @("validate-state-preparation-declaration", "--input", $path) `
        -Label "State-preparation declaration validation" `
        -TimeoutSeconds $TimeoutSeconds
    # Use the validated object returned by the same strict read.  Do not
    # reread the path with ConvertFrom-Json: a concurrent replacement between
    # reads would otherwise create a time-of-check/time-of-use gap.
    $declaration = $validated
    $workingBlobAfterValidation = (Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
        "hash-object", ("--path={0}" -f $relativePath), "--", $path
    ) "State-preparation declaration post-validation binding" $TimeoutSeconds).ToLowerInvariant()
    if ($workingBlobAfterValidation -ne $declarationBlobSha) {
        throw "State-preparation declaration changed during strict validation."
    }
    return [pscustomobject]@{
        required = $true
        path = $path
        declaration_present = $true
        declaration_blob_sha = $declarationBlobSha
        sidecar_contract = [string]$declaration.sidecar_contract
    }
}

function Assert-DawnstrikeCandidateIdentityAndDeclaration {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][object]$Declaration,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $identity = Get-DawnstrikeGitContract $GitPath $CandidateRoot $TimeoutSeconds $CandidateSha
    if ($identity.tree -ne $CandidateTree) {
        throw "Candidate checkout tree changed during activation."
    }
    $current = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $CandidateRoot `
        -GitPath $GitPath `
        -CandidateSha $CandidateSha `
        -CandidateTree $CandidateTree `
        -PythonPath ((Get-DawnstrikeApprovedLockInterpreter).path) `
        -TimeoutSeconds $TimeoutSeconds
    if (
        [bool]$current.required -ne [bool]$Declaration.required -or
        [bool]$current.declaration_present -ne [bool]$Declaration.declaration_present -or
        [string]$current.declaration_blob_sha -ne [string]$Declaration.declaration_blob_sha
    ) {
        throw "Candidate declaration identity changed during activation."
    }
    return $identity
}

function Get-DawnstrikeAuxiliaryCaptureTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [switch]$AllowDisabled
    )

    $matches = @(Get-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -ErrorAction SilentlyContinue)
    if ($matches.Count -eq 0) {
        return [pscustomobject]@{
            present = $false
            task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
            state = "ABSENT"
            enabled = $false
            xml = ""
            xml_sha256 = Get-DawnstrikeSha256Text ""
            xml_file_sha256 = Get-DawnstrikeSha256Text ""
            definition_contract_sha256 = Get-DawnstrikeSha256Text ""
            action_contract_sha256 = Get-DawnstrikeSha256Text ""
            task_path = "NONE"
        }
    }
    if ($matches.Count -ne 1) {
        throw "Auxiliary capture task name is not unique."
    }
    $task = $matches[0]
    $state = [string]$task.State
    if ($state -notin @("Ready", "Disabled")) {
        throw "Auxiliary capture task is not quiesceable: state=$state"
    }
    $actions = @($task.Actions)
    if ($actions.Count -lt 1) {
        throw "Auxiliary capture task has no action."
    }
    $actionText = ($actions | ForEach-Object {
        "{0}|{1}|{2}" -f $_.Execute, $_.Arguments, $_.WorkingDirectory
    }) -join "`n"
    if (
        -not $actionText.ToLowerInvariant().Contains($RuntimeRoot.ToLowerInvariant()) -or
        -not $actionText.ToLowerInvariant().Contains($StateRoot.ToLowerInvariant())
    ) {
        throw "Auxiliary capture task does not retain the fixed runtime/state roots."
    }
    $taskPath = [string]$task.TaskPath
    if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
    $xml = [string](Export-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $taskPath -ErrorAction Stop)
    if ([string]::IsNullOrWhiteSpace($xml)) {
        throw "Auxiliary capture task export is empty."
    }
    return [pscustomobject]@{
        present = $true
        task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
        state = $state
        enabled = ($state -eq "Ready")
        task_path = $taskPath
        xml = $xml
        xml_sha256 = Get-DawnstrikeSha256Text $xml
        xml_file_sha256 = Get-DawnstrikeSha256Text $xml
        definition_contract_sha256 = Get-DawnstrikeSha256Text (Get-DawnstrikeTaskDefinitionText $xml)
        action_contract_sha256 = Get-DawnstrikeSha256Text $actionText
    }
}

function Get-DawnstrikeAuxiliarySectionHash {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][ValidateSet("Principal", "Triggers", "Settings", "Actions")][string]$Name,
        [ValidateSet("", "true", "false")][string]$NormalizeEnabledTo = ""
    )
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $nodes = @($document.SelectNodes("//*[local-name()='$Name']"))
        if ($nodes.Count -ne 1) { throw "expected exactly one $Name section" }
        if ($Name -eq "Settings" -and $NormalizeEnabledTo) {
            $enabled = @($nodes[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
            if ($enabled.Count -gt 1) { throw "expected at most one Settings/Enabled node" }
            if ($enabled.Count -eq 1) { $enabled[0].InnerText = $NormalizeEnabledTo }
        }
        return Get-DawnstrikeSha256Text ([string]$nodes[0].OuterXml)
    }
    catch {
        throw "Auxiliary capture XML has an invalid $Name section."
    }
}

function Disable-DawnstrikeAuxiliaryCaptureTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )
    $task = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (-not $task.present) { return $task }
    if ($task.state -eq "Ready") {
        Disable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $task.task_path -ErrorAction Stop | Out-Null
    }
    $after = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (
        $after.state -ne "Disabled" -or
        $after.definition_contract_sha256 -ne $task.definition_contract_sha256 -or
        $after.action_contract_sha256 -ne $task.action_contract_sha256
    ) {
        throw "Auxiliary capture task did not enter the exact Disabled boundary."
    }
    return $after
}

function Restore-DawnstrikeAuxiliaryCaptureTask {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Expected,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [pscredential]$RunAsCredential
    )
    $current = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (-not $Expected.present) {
        if ($current.present) { throw "An auxiliary capture task appeared despite the absent-task policy." }
        return $current
    }
    if (-not $current.present) { throw "The receipt-bound auxiliary capture task is missing." }
    # Password-logon task XML cannot safely be re-registered without threading
    # a password through recovery.  Preserve the existing principal/triggers/
    # settings and restore only the action and enablement fields that governed
    # activation/rebind are allowed to change.
    $expectedDocument = [System.Xml.XmlDocument]::new()
    $expectedDocument.PreserveWhitespace = $true
    $expectedDocument.LoadXml([string]$Expected.xml)
    $currentDocument = [System.Xml.XmlDocument]::new()
    $currentDocument.PreserveWhitespace = $true
    $currentDocument.LoadXml([string]$current.xml)
    foreach ($sectionName in @("Principal", "Triggers", "Settings")) {
        $expectedNodes = @($expectedDocument.SelectNodes("//*[local-name()='$sectionName']"))
        $currentNodes = @($currentDocument.SelectNodes("//*[local-name()='$sectionName']"))
        if ($expectedNodes.Count -ne 1 -or $currentNodes.Count -ne 1) {
            throw "Auxiliary capture $sectionName policy is ambiguous during compensation."
        }
        if ($sectionName -eq "Settings") {
            $expectedEnabled = @($expectedNodes[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
            $currentEnabled = @($currentNodes[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
            if ($expectedEnabled.Count -gt 1 -or $currentEnabled.Count -gt 1) {
                throw "Auxiliary capture enablement policy is ambiguous during compensation."
            }
            if ($expectedEnabled.Count -eq 1 -and $currentEnabled.Count -eq 1) {
                $currentEnabled[0].InnerText = [string]$expectedEnabled[0].InnerText
            }
        }
        if ([string]$expectedNodes[0].OuterXml -ne [string]$currentNodes[0].OuterXml) {
            throw "Auxiliary capture principal, trigger, or settings policy drifted during compensation."
        }
    }
    $expectedActions = @($expectedDocument.SelectNodes("//*[local-name()='Actions']"))
    $currentActions = @($currentDocument.SelectNodes("//*[local-name()='Actions']"))
    if ($expectedActions.Count -ne 1 -or $currentActions.Count -ne 1) {
        throw "Auxiliary capture action policy is ambiguous during compensation."
    }
    if ([string]$expectedActions[0].OuterXml -ne [string]$currentActions[0].OuterXml) {
        if ($null -eq $RunAsCredential -or [string]::IsNullOrWhiteSpace($RunAsCredential.UserName)) {
            throw "Auxiliary action drift requires the locally prompted RunAsCredential for Password-task compensation."
        }
        $restorePassword = $RunAsCredential.GetNetworkCredential().Password
        if ([string]::IsNullOrWhiteSpace($restorePassword)) { throw "Auxiliary compensation credential is incomplete." }
        $expectedExec = @($expectedActions[0].ChildNodes | Where-Object { $_.LocalName -eq "Exec" })
        if ($expectedExec.Count -ne 1) { throw "Auxiliary capture action policy is invalid during compensation." }
        $command = @($expectedExec[0].ChildNodes | Where-Object { $_.LocalName -eq "Command" })
        $arguments = @($expectedExec[0].ChildNodes | Where-Object { $_.LocalName -eq "Arguments" })
        $working = @($expectedExec[0].ChildNodes | Where-Object { $_.LocalName -eq "WorkingDirectory" })
        if ($command.Count -ne 1 -or $arguments.Count -ne 1 -or $working.Count -ne 1) {
            throw "Auxiliary capture action contract is incomplete during compensation."
        }
        $restoreAction = New-ScheduledTaskAction `
            -Execute ([string]$command[0].InnerText) `
            -Argument ([string]$arguments[0].InnerText) `
            -WorkingDirectory ([string]$working[0].InnerText)
        Set-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName `
            -TaskPath ([string]$Expected.task_path) -Action @($restoreAction) `
            -User $RunAsCredential.UserName -Password $restorePassword -ErrorAction Stop | Out-Null
    }
    $restored = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    $restoredDocument = [System.Xml.XmlDocument]::new()
    $restoredDocument.PreserveWhitespace = $true
    $restoredDocument.LoadXml([string]$restored.xml)
    foreach ($sectionName in @("Principal", "Triggers", "Actions")) {
        $expectedNodes = @($expectedDocument.SelectNodes("//*[local-name()='$sectionName']"))
        $restoredNodes = @($restoredDocument.SelectNodes("//*[local-name()='$sectionName']"))
        if ($expectedNodes.Count -ne 1 -or $restoredNodes.Count -ne 1 -or [string]$expectedNodes[0].OuterXml -ne [string]$restoredNodes[0].OuterXml) {
            throw "Auxiliary capture $sectionName was not restored exactly."
        }
    }
    $expectedSettings = @($expectedDocument.SelectNodes("//*[local-name()='Settings']"))
    $restoredSettings = @($restoredDocument.SelectNodes("//*[local-name()='Settings']"))
    if ($expectedSettings.Count -ne 1 -or $restoredSettings.Count -ne 1) { throw "Auxiliary capture settings are ambiguous after compensation." }
    $expectedEnabled = @($expectedSettings[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
    $restoredEnabled = @($restoredSettings[0].ChildNodes | Where-Object { $_.LocalName -eq "Enabled" })
    if ($expectedEnabled.Count -eq 1 -and $restoredEnabled.Count -eq 1) { $restoredEnabled[0].InnerText = [string]$expectedEnabled[0].InnerText }
    if ($expectedEnabled.Count -ne $restoredEnabled.Count -or ($expectedEnabled.Count -eq 1 -and [string]$expectedSettings[0].OuterXml -ne [string]$restoredSettings[0].OuterXml)) {
        throw "Auxiliary capture settings were not restored exactly."
    }
    if ($Expected.enabled) {
        if ($restored.state -eq "Disabled") {
            Enable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $restored.task_path -ErrorAction Stop | Out-Null
        }
    }
    else {
        if ($restored.state -eq "Ready") {
            Disable-ScheduledTask -TaskName $script:DawnstrikeAuxiliaryCaptureTaskName -TaskPath $restored.task_path -ErrorAction Stop | Out-Null
        }
    }
    $final = Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    if (
        $final.task_path -ne [string]$Expected.task_path -or
        $final.definition_contract_sha256 -ne [string]$Expected.definition_contract_sha256 -or
        $final.enabled -ne [bool]$Expected.enabled -or
        $final.action_contract_sha256 -ne [string]$Expected.action_contract_sha256
    ) {
        throw "Auxiliary capture task action or enablement did not restore exactly."
    }
    return $final
}

function Get-DawnstrikeTaskContract {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [switch]$AllowDisabled
    )

    $records = @()
    $definitionRecords = @()
    $actionRecords = @()
    $enabledCount = 0
    $disabledCount = 0
    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Canonical Dawnstrike task name is not unique: $taskName"
        }
        $task = $matches[0]
        $state = [string]$task.State
        if ($state -eq "Ready") {
            $enabledCount += 1
        }
        elseif ($AllowDisabled -and $state -eq "Disabled") {
            $disabledCount += 1
        }
        else {
            throw "Canonical Dawnstrike task is not in an approved exact state: $taskName state=$state"
        }
        $actions = @($task.Actions)
        if ($actions.Count -lt 1) {
            throw "Canonical Dawnstrike task has no action: $taskName"
        }
        $actionText = ($actions | ForEach-Object {
            "{0}|{1}|{2}" -f $_.Execute, $_.Arguments, $_.WorkingDirectory
        }) -join "`n"
        $lower = $actionText.ToLowerInvariant()
        if (
            -not $lower.Contains($RuntimeRoot.ToLowerInvariant()) -or
            -not $lower.Contains($StateRoot.ToLowerInvariant())
        ) {
            throw "Canonical Dawnstrike task does not retain the fixed runtime/state roots: $taskName"
        }
        $taskPath = [string]$task.TaskPath
        if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
        $xml = [string](Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop)
        if ([string]::IsNullOrWhiteSpace($xml)) {
            throw "Canonical Dawnstrike task export is empty: $taskName"
        }
        $records += "$taskName`0$(Get-DawnstrikeSha256Text $xml)`n"
        $definition = Get-DawnstrikeTaskDefinitionText $xml
        $definitionRecords += "$taskName`0$(Get-DawnstrikeSha256Text $definition)`n"
        $actionRecords += "$taskName`0$taskPath`0$actionText`n"
    }
    return [pscustomobject]@{
        task_count = $script:DawnstrikeCanonicalTaskNames.Count
        task_contract_sha256 = Get-DawnstrikeSha256Text ($records -join "")
        task_definition_contract_sha256 = Get-DawnstrikeSha256Text ($definitionRecords -join "")
        task_action_contract_sha256 = Get-DawnstrikeSha256Text ($actionRecords -join "")
        enabled_count = $enabledCount
        disabled_count = $disabledCount
    }
}

function Write-DawnstrikeTaskXmlFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Xml,
        [Parameter(Mandatory = $true)][string]$Path
    )

    Assert-DawnstrikeNoReparseComponents $Path "Task XML backup file"
    try {
        $document = [System.Xml.XmlDocument]::new()
        $document.PreserveWhitespace = $true
        $document.LoadXml($Xml)
        $declaration = @(
            $document.ChildNodes |
                Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::XmlDeclaration }
        )
        if ($declaration.Count -gt 1) {
            throw "Task XML has multiple declarations."
        }
        $declaredEncoding = if ($declaration.Count -eq 1) {
            ([string]$declaration[0].Encoding).ToLowerInvariant()
        }
        else {
            ""
        }
        if ($declaredEncoding -in @("utf-16", "unicode", "utf-16le")) {
            $encoding = [System.Text.Encoding]::Unicode
            $encodingLabel = "utf-16le-bom"
        }
        elseif ($declaredEncoding -in @("", "utf-8")) {
            $encoding = [System.Text.UTF8Encoding]::new($false)
            $encodingLabel = "utf-8"
        }
        else {
            throw "Task XML declares an unsupported encoding."
        }
        Assert-DawnstrikeNoReparseComponents $Path "Task XML backup file"
        $parent = Split-Path -Parent ([System.IO.Path]::GetFullPath($Path))
        Assert-DawnstrikeNoReparseComponents $parent "Task XML backup root"
        if (Test-Path -LiteralPath $Path) {
            $existing = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
            if ($existing.PSIsContainer -or ($existing.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Task XML backup destination is not a regular file."
            }
        }
        [System.IO.File]::WriteAllText($Path, $Xml, $encoding)
        Assert-DawnstrikeNoReparseComponents $Path "Task XML backup file"
        $written = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($written.PSIsContainer -or ($written.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Task XML backup destination is not a regular file after write."
        }
        return $encodingLabel
    }
    catch {
        throw "Canonical task XML cannot be persisted with its declared encoding."
    }
}

function New-DawnstrikeTaskXmlBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupName,
        [Parameter(Mandatory = $true)][string]$ActivationId,
        [Parameter(Mandatory = $true)][object]$TaskContract,
        [AllowNull()][object]$AuxiliaryCapture
    )

    if ($BackupName -notmatch '^runtime-(activation|rollback)-[0-9a-f]{24}$') {
        throw "Scheduler backup name is invalid."
    }
    $root = Join-Path $StateRoot "scheduler-backups"
    Assert-DawnstrikeNoReparseComponents $root "Scheduler backup root"
    if (Test-Path -LiteralPath $root) {
        $rootItem = Get-Item -LiteralPath $root -Force -ErrorAction Stop
        if (
            -not $rootItem.PSIsContainer -or
            ($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
        ) {
            throw "Scheduler backup root is not a safe directory."
        }
    }
    else {
        New-Item -ItemType Directory -Path $root -ErrorAction Stop | Out-Null
    }
    Assert-DawnstrikeNoReparseComponents $root "Scheduler backup root"
    $final = Join-Path $root $BackupName
    Assert-DawnstrikeNoReparseComponents $final "Scheduler backup bundle"
    if (Test-Path -LiteralPath $final) {
        throw "Scheduler XML backup already exists and requires review."
    }
    $temporary = Join-Path $root (".incomplete-$BackupName-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporary -ErrorAction Stop | Out-Null
    Assert-DawnstrikeNoReparseComponents $temporary "Temporary scheduler backup bundle"
    try {
        $entries = @()
        foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
            if ([string]$task.State -ne "Ready") {
                throw "Scheduler XML backup requires every canonical task to be exactly Ready."
            }
            $taskPath = [string]$task.TaskPath
            if ([string]::IsNullOrWhiteSpace($taskPath)) { $taskPath = "\" }
            $xml = [string](Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop)
            $safeName = ($taskName -replace '[^A-Za-z0-9_.-]', '_') + ".xml"
            $xmlPath = Join-Path $temporary $safeName
            Assert-DawnstrikeNoReparseComponents $xmlPath "Task XML backup file"
            $xmlEncoding = Write-DawnstrikeTaskXmlFile $xml $xmlPath
            $entries += [ordered]@{
                task_name = $taskName
                task_path = $taskPath
                enabled = $true
                file_name = $safeName
                xml_encoding = $xmlEncoding
                xml_sha256 = Get-DawnstrikeSha256Text $xml
                xml_file_sha256 = Get-DawnstrikeSha256File $xmlPath
            }
        }
        if ($null -eq $AuxiliaryCapture) {
            $AuxiliaryCapture = Get-DawnstrikeAuxiliaryCaptureTask `
                -RuntimeRoot (Get-Location).Path -StateRoot $StateRoot
        }
        $auxiliaryEntry = [ordered]@{
            present = [bool]$AuxiliaryCapture.present
            task_name = $script:DawnstrikeAuxiliaryCaptureTaskName
            state_before = if ($AuxiliaryCapture.present) { [string]$AuxiliaryCapture.state } else { "ABSENT" }
            enabled_before = if ($AuxiliaryCapture.present) { [bool]$AuxiliaryCapture.enabled } else { $false }
            action = if ($AuxiliaryCapture.present) { "DISABLED_UNTIL_EXACT_SHA_REBIND" } else { "ABSENT_ALLOWED" }
        }
        if ($AuxiliaryCapture.present) {
            $auxiliaryFileName = "Dawnstrike_Delayed_SIP_Capture.xml"
            $auxiliaryPath = Join-Path $temporary $auxiliaryFileName
            $null = Write-DawnstrikeTaskXmlFile `
                -Xml ([string]$AuxiliaryCapture.xml) -Path $auxiliaryPath
            $auxiliaryEntry.file_name = $auxiliaryFileName
            $auxiliaryEntry.xml_sha256 = [string]$AuxiliaryCapture.xml_sha256
            $auxiliaryEntry.xml_file_sha256 = Get-DawnstrikeSha256File $auxiliaryPath
            $auxiliaryEntry.definition_contract_sha256 = [string]$AuxiliaryCapture.definition_contract_sha256
            $auxiliaryEntry.action_contract_sha256 = [string]$AuxiliaryCapture.action_contract_sha256
            $auxiliaryEntry.task_path = [string]$AuxiliaryCapture.task_path
        }
        $manifest = [ordered]@{
            schema_version = "dawnstrike.scheduler_xml_backup.v1"
            activation_id = $ActivationId
            created_at_utc = [DateTime]::UtcNow.ToString("o")
            task_count = [int]$TaskContract.task_count
            task_contract_sha256 = [string]$TaskContract.task_contract_sha256
            task_definition_contract_sha256 = [string]$TaskContract.task_definition_contract_sha256
            task_action_contract_sha256 = [string]$TaskContract.task_action_contract_sha256
            tasks = $entries
            auxiliary_capture = $auxiliaryEntry
            research_only = $true
            broker_execution_enabled = $false
        }
        $manifestPath = Join-Path $temporary "manifest.json"
        Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
        Write-DawnstrikeActivationJson $manifest $manifestPath
        Assert-DawnstrikeNoReparseComponents $temporary "Temporary scheduler backup bundle"
        Assert-DawnstrikeNoReparseComponents $final "Scheduler backup bundle"
        [System.IO.Directory]::Move($temporary, $final)
        Assert-DawnstrikeNoReparseComponents $final "Scheduler backup bundle"
        $finalManifest = Join-Path $final "manifest.json"
        $result = [pscustomobject]@{
            backup_name = $BackupName
            backup_path = $final
            manifest_sha256 = Get-DawnstrikeSha256File $finalManifest
        }
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $StateRoot `
            -BackupName $result.backup_name `
            -ExpectedManifestSha256 $result.manifest_sha256 `
            -ExpectedTaskContractSha256 ([string]$TaskContract.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$TaskContract.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$TaskContract.task_action_contract_sha256)
        return $result
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Container) {
            Remove-Item -LiteralPath $temporary -Recurse -Force
        }
    }
}

function Get-DawnstrikeTaskXmlBackupManifest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupName,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256
    )

    if ($BackupName -notmatch '^runtime-(activation|rollback)-[0-9a-f]{24}$' -or
        $ExpectedManifestSha256 -notmatch '^[0-9a-f]{64}$') {
        throw "Scheduler backup manifest identity is invalid."
    }
    $manifestPath = Join-Path $StateRoot "scheduler-backups\$BackupName\manifest.json"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
    $manifestItem = Get-Item -LiteralPath $manifestPath -Force -ErrorAction Stop
    if ($manifestItem.PSIsContainer -or
        ($manifestItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        (Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256) {
        throw "Scheduler backup manifest does not match its receipt-bound identity."
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Scheduler backup manifest is invalid JSON."
    }
    Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
    if ((Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256) {
        throw "Scheduler backup manifest changed during read."
    }
    foreach ($field in @(
        "task_contract_sha256",
        "task_definition_contract_sha256",
        "task_action_contract_sha256"
    )) {
        if ([string]$manifest.$field -notmatch '^[0-9a-f]{64}$') {
            throw "Scheduler backup manifest task identity is invalid."
        }
    }
    return $manifest
}

function Assert-DawnstrikeTaskXmlBackup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupName,
        [Parameter(Mandatory = $true)][string]$ExpectedManifestSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskContractSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskDefinitionContractSha256,
        [Parameter(Mandatory = $true)][string]$ExpectedTaskActionContractSha256,
        [string]$BackupPath = ""
    )

    if ($BackupName -notmatch '^runtime-(activation|rollback)-[0-9a-f]{24}$') {
        throw "Scheduler backup name is invalid."
    }
    foreach ($hash in @(
        $ExpectedManifestSha256,
        $ExpectedTaskContractSha256,
        $ExpectedTaskDefinitionContractSha256,
        $ExpectedTaskActionContractSha256
    )) {
        if ($hash -notmatch '^[0-9a-f]{64}$') {
            throw "Scheduler backup expected hash is invalid."
        }
    }
    $backupPath = if ([string]::IsNullOrWhiteSpace($BackupPath)) {
        Join-Path $StateRoot "scheduler-backups\$BackupName"
    }
    else {
        [IO.Path]::GetFullPath($BackupPath)
    }
    Assert-DawnstrikeNoReparseComponents $backupPath "Scheduler backup bundle"
    $backupItem = Get-Item -LiteralPath $backupPath -Force -ErrorAction Stop
    if (
        -not $backupItem.PSIsContainer -or
        ($backupItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Scheduler XML backup is not a safe directory."
    }
    $manifestPath = Join-Path $backupPath "manifest.json"
    Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
    $manifestItem = Get-Item -LiteralPath $manifestPath -Force -ErrorAction Stop
    if (
        $manifestItem.PSIsContainer -or
        ($manifestItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        (Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256
    ) {
        throw "Scheduler XML backup manifest does not match its receipt-bound hash."
    }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        Assert-DawnstrikeNoReparseComponents $manifestPath "Scheduler backup manifest"
        if ((Get-DawnstrikeSha256File $manifestPath) -ne $ExpectedManifestSha256) {
            throw "Scheduler backup manifest changed during read."
        }
    }
    catch {
        throw "Scheduler XML backup manifest is invalid JSON."
    }
    if (
        [string]$manifest.schema_version -ne "dawnstrike.scheduler_xml_backup.v1" -or
        [string]$manifest.activation_id -ne $BackupName.Substring($BackupName.Length - 24) -or
        [int]$manifest.task_count -ne 5 -or
        [string]$manifest.task_contract_sha256 -ne $ExpectedTaskContractSha256 -or
        [string]$manifest.task_definition_contract_sha256 -ne
            $ExpectedTaskDefinitionContractSha256 -or
        [string]$manifest.task_action_contract_sha256 -ne $ExpectedTaskActionContractSha256 -or
        $manifest.research_only -ne $true -or
        $manifest.broker_execution_enabled -ne $false
    ) {
        throw "Scheduler XML backup manifest violates the exact safety contract."
    }
    $entries = @($manifest.tasks)
    if ($entries.Count -ne $script:DawnstrikeCanonicalTaskNames.Count) {
        throw "Scheduler XML backup does not contain exactly five tasks."
    }
    $auxiliary = $manifest.auxiliary_capture
    if ($null -eq $auxiliary) {
        throw "Scheduler XML backup does not attest auxiliary capture task policy."
    }
    $expectedChildren = @("manifest.json") + @($entries | ForEach-Object { [string]$_.file_name })
    if ($auxiliary.present -eq $true) {
        if ([string]$auxiliary.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName) {
            throw "Scheduler XML backup auxiliary task name is invalid."
        }
        $expectedChildren += [string]$auxiliary.file_name
        if (
            [string]$auxiliary.state_before -notin @("Ready", "Disabled") -or
            [string]$auxiliary.action -ne "DISABLED_UNTIL_EXACT_SHA_REBIND" -or
            [string]$auxiliary.xml_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$auxiliary.xml_file_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$auxiliary.definition_contract_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$auxiliary.action_contract_sha256 -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "Scheduler XML backup auxiliary task entry violates the exact contract."
        }
        $auxiliaryXmlPath = Join-Path $backupPath ([string]$auxiliary.file_name)
        Assert-DawnstrikeNoReparseComponents $auxiliaryXmlPath "Auxiliary scheduler XML backup"
        $auxiliaryXmlItem = Get-Item -LiteralPath $auxiliaryXmlPath -Force -ErrorAction Stop
        if (
            $auxiliaryXmlItem.PSIsContainer -or
            ($auxiliaryXmlItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            (Get-DawnstrikeSha256File $auxiliaryXmlPath) -ne [string]$auxiliary.xml_file_sha256 -or
            (Get-DawnstrikeSha256Text ([System.IO.File]::ReadAllText($auxiliaryXmlPath))) -ne [string]$auxiliary.xml_sha256
        ) {
            throw "Scheduler XML backup auxiliary task file does not match its manifest."
        }
        Assert-DawnstrikeNoReparseComponents $auxiliaryXmlPath "Auxiliary scheduler XML backup"
        if ((Get-DawnstrikeSha256File $auxiliaryXmlPath) -ne [string]$auxiliary.xml_file_sha256) {
            throw "Scheduler XML backup auxiliary task file changed during read."
        }
    }
    elseif (
        $auxiliary.present -ne $false -or
        [string]$auxiliary.task_name -ne $script:DawnstrikeAuxiliaryCaptureTaskName -or
        [string]$auxiliary.state_before -ne "ABSENT" -or
        [string]$auxiliary.action -ne "ABSENT_ALLOWED"
    ) {
        throw "Scheduler XML backup absent auxiliary policy is invalid."
    }
    $actualChildren = @(Get-ChildItem -LiteralPath $backupPath -Force)
    if (
        $actualChildren.Count -ne $expectedChildren.Count -or
        @($actualChildren | Where-Object { $_.PSIsContainer }).Count -ne 0 -or
        @(Compare-Object `
            ($expectedChildren | Sort-Object) `
            (@($actualChildren.Name) | Sort-Object)
        ).Count -ne 0
    ) {
        throw "Scheduler XML backup contains unexpected files or directories."
    }
    $records = @()
    $definitionRecords = @()
    for ($index = 0; $index -lt $entries.Count; $index += 1) {
        $entry = $entries[$index]
        $expectedName = $script:DawnstrikeCanonicalTaskNames[$index]
        $expectedFileName = ($expectedName -replace '[^A-Za-z0-9_.-]', '_') + ".xml"
        if (
            [string]$entry.task_name -ne $expectedName -or
            $entry.enabled -ne $true -or
            [string]$entry.file_name -ne $expectedFileName -or
            [string]$entry.xml_encoding -notin @("utf-8", "utf-16le-bom") -or
            [string]$entry.xml_sha256 -notmatch '^[0-9a-f]{64}$' -or
            [string]$entry.xml_file_sha256 -notmatch '^[0-9a-f]{64}$'
        ) {
            throw "Scheduler XML backup task entry violates the exact contract."
        }
        $xmlPath = Join-Path $backupPath $expectedFileName
        Assert-DawnstrikeNoReparseComponents $xmlPath "Scheduler task XML backup"
        $xmlItem = Get-Item -LiteralPath $xmlPath -Force -ErrorAction Stop
        if (
            $xmlItem.PSIsContainer -or
            ($xmlItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
            (Get-DawnstrikeSha256File $xmlPath) -ne [string]$entry.xml_file_sha256
        ) {
            throw "Scheduler XML backup task file does not match its manifest."
        }
        $xml = [System.IO.File]::ReadAllText($xmlPath)
        Assert-DawnstrikeNoReparseComponents $xmlPath "Scheduler task XML backup"
        if ((Get-DawnstrikeSha256File $xmlPath) -ne [string]$entry.xml_file_sha256) {
            throw "Scheduler task XML backup changed during read."
        }
        if ((Get-DawnstrikeSha256Text $xml) -ne [string]$entry.xml_sha256) {
            throw "Scheduler XML backup task text does not match its manifest."
        }
        $records += "$expectedName`0$([string]$entry.xml_sha256)`n"
        $definition = Get-DawnstrikeTaskDefinitionText $xml
        $definitionRecords += "$expectedName`0$(Get-DawnstrikeSha256Text $definition)`n"
    }
    if ((Get-DawnstrikeSha256Text ($records -join "")) -ne $ExpectedTaskContractSha256) {
        throw "Scheduler XML backup files do not reproduce the task contract hash."
    }
    if (
        (Get-DawnstrikeSha256Text ($definitionRecords -join "")) -ne
            $ExpectedTaskDefinitionContractSha256
    ) {
        throw "Scheduler XML backup files do not reproduce the task definition hash."
    }
}

function Assert-DawnstrikeReceiptRecoveryArtifacts {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$ToolRoot,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [switch]$RequireRollbackCheckout
    )

    $activationId = [string]$Receipt.activation_id
    if ($activationId -notmatch '^[0-9a-f]{24}$') {
        throw "Recovery artifact activation id is invalid."
    }
    $backup = Resolve-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    $rollbackRoot = Join-Path $StateRoot "runtime-rollbacks\$activationId"
    Assert-DawnstrikeNoReparseComponents $rollbackRoot "Receipt-bound rollback root"
    $rollbackRootItem = Get-Item -LiteralPath $rollbackRoot -Force -ErrorAction Stop
    if (
        -not $rollbackRootItem.PSIsContainer -or
        ($rollbackRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Receipt-bound rollback root is missing or unsafe."
    }
    $bundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    Assert-DawnstrikeNoReparseComponents $bundle "Receipt-bound rollback bundle"
    $bundleItem = Get-Item -LiteralPath $bundle -Force -ErrorAction Stop
    if (
        $bundleItem.PSIsContainer -or
        ($bundleItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or
        (Get-DawnstrikeSha256File $bundle) -ne [string]$Receipt.rollback_bundle_sha256
    ) {
        throw "Receipt-bound rollback bundle is missing or invalid."
    }
    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $GitPath `
        -ArgumentList @("bundle", "verify", $bundle) `
        -WorkingDirectory $ToolRoot `
        -Label "Receipt-bound rollback bundle verification" `
        -TimeoutSeconds $TimeoutSeconds

    $stateBundle = Join-Path $backup ([string]$Receipt.state_backup_id)
    Assert-DawnstrikeNoReparseComponents $stateBundle "Receipt-bound durable-state backup"
    $stateBundleItem = Get-Item -LiteralPath $stateBundle -Force -ErrorAction Stop
    if (
        -not $stateBundleItem.PSIsContainer -or
        ($stateBundleItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    ) {
        throw "Receipt-bound durable-state backup is missing or unsafe."
    }
    $stateVerification = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @(
            (Join-Path $ToolRoot "scripts\state_disaster_recovery.py"),
            "restore-verify", "--bundle", $stateBundle,
            "--target-db", (Join-Path $StateRoot "shadow_real.sqlite"),
            "--backup-root", $backup, "--state-root", $StateRoot
        ) `
        -WorkingDirectory $ToolRoot `
        -Label "Receipt-bound durable-state backup verification" `
        -TimeoutSeconds $TimeoutSeconds
    try {
        $stateResult = [string]$stateVerification.Stdout | ConvertFrom-Json
    }
    catch {
        throw "Durable-state backup verification did not return valid JSON."
    }
    if (
        [string]$stateResult.status -ne "VERIFY" -or
        [string]$stateResult.backup_id -ne [string]$Receipt.state_backup_id -or
        [string]$stateResult.backup_db_sha256 -ne
            [string]$Receipt.state_backup_db_sha256 -or
        [string]$stateResult.source_release_sha -ne [string]$Receipt.previous_sha -or
        [int]$stateResult.schema_version -ne [int]$Receipt.state_schema_version -or
        [string]$stateResult.quick_check -ne "ok" -or
        $stateResult.write_performed -ne $false -or
        $stateResult.automatic_overwrite -ne $false
    ) {
        throw "Receipt-bound durable-state backup does not match the activation receipt."
    }
    if ($Receipt.PSObject.Properties.Name -contains "state_backup_bundle_path") {
        Assert-DawnstrikeNoReparseComponents ([string]$Receipt.state_backup_bundle_path) `
            "Receipt-bound durable-state backup path"
        $receiptStateBundle = Resolve-DawnstrikeActivationRoot `
            ([string]$Receipt.state_backup_bundle_path) `
            "Receipt-bound durable-state backup path"
        if (
            -not [string]::Equals(
                $receiptStateBundle,
                $stateBundle,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$stateResult.manifest_sha256 -ne [string]$Receipt.state_backup_manifest_sha256 -or
            [string]$stateResult.backup_logical_snapshot_sha256 -ne
                [string]$Receipt.state_backup_logical_snapshot_sha256 -or
            [string]$stateResult.source_logical_snapshot_sha256 -ne
                [string]$Receipt.state_backup_source_logical_snapshot_sha256
        ) {
            throw "Receipt-bound durable-state backup manifest or logical lineage does not match the activation receipt."
        }
    }

    if ($RequireRollbackCheckout) {
        $checkout = Resolve-DawnstrikeActivationRoot `
            (Join-Path $rollbackRoot "previous-runtime") `
            "RollbackCheckout"
        $checkoutContract = Get-DawnstrikeGitContract `
            $GitPath `
            $checkout `
            $TimeoutSeconds `
            ([string]$Receipt.previous_sha)
        if ($checkoutContract.tree -ne [string]$Receipt.previous_tree) {
            throw "Receipt-bound rollback checkout tree is invalid."
        }
        $checkoutOrigin = Get-DawnstrikeGitValue `
            $GitPath `
            $checkout `
            @("remote", "get-url", "origin") `
            "Receipt-bound rollback checkout origin verification" `
            $TimeoutSeconds
        if ((Get-DawnstrikeSha256Text $checkoutOrigin) -ne [string]$Receipt.runtime_origin_sha256) {
            throw "Receipt-bound rollback checkout origin is invalid."
        }
    }
    return $stateResult
}

function Assert-DawnstrikeActivationCompleteTerminal {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Journal,
        [Parameter(Mandatory = $true)][object]$Receipt,
        [Parameter(Mandatory = $true)][string]$ReceiptPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][string]$ExpectedTree,
        [Parameter(Mandatory = $true)][string]$OriginIdentity,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][object]$StateDeclaration,
        [Parameter(Mandatory = $true)][object]$ExpectedTask
    )

    # This is the one terminal admission used by both the ordinary COMPLETE
    # return and the catch/reconciliation path.  Every value below is bound to
    # the sealed journal/receipt; no stale local phase or pre-lock snapshot may
    # authorize release of a terminal lock.
    if ([string]$Journal.payload.operation -ne "runtime_activation" -or
        [string]$Journal.payload.phase -ne "COMPLETE" -or
        [string]$Journal.payload.candidate_sha -ne $ExpectedSha -or
        [string]$Journal.payload.candidate_tree -ne $ExpectedTree -or
        [string]$Journal.payload.origin_identity -ne $OriginIdentity -or
        [string]$Journal.payload.complete_receipt_relative_path -eq "NONE") {
        throw "Complete activation journal identity is not exact."
    }
    Assert-DawnstrikeNoReparseComponents $ReceiptPath "Complete activation receipt"
    if (-not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
        throw "Complete activation journal has no exact complete receipt."
    }
    $stateFull = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'
    $receiptFull = [System.IO.Path]::GetFullPath($ReceiptPath)
    $receiptRelative = ($receiptFull.Substring($stateFull.Length) -replace '\\','/')
    if ([string]$Journal.payload.complete_receipt_relative_path -cne $receiptRelative -or
        [string]$Journal.payload.complete_receipt_sha256 -notmatch '^[0-9a-f]{64}$' -or
        [string]$Journal.payload.complete_receipt_sha256 -cne (Get-DawnstrikeSha256File $ReceiptPath)) {
        throw "Complete activation journal is not bound to the exact receipt bytes."
    }
    $receiptOriginHash = [string]$Receipt.runtime_origin_sha256
    if ($receiptOriginHash -notmatch '^[0-9a-f]{64}$') { throw "Complete activation receipt origin hash is invalid." }
    if ([string]$Receipt.candidate_sha -ne $ExpectedSha -or
        [string]$Receipt.candidate_tree -ne $ExpectedTree -or
        [string]$Receipt.market_date -ne $MarketDate -or
        [string]$Receipt.status -ne "COMPLETE") {
        throw "Complete activation receipt is not bound to the requested terminal identity."
    }
    $verified = Invoke-DawnstrikeContractCli $PythonPath $CandidateRoot `
        -Arguments @("verify-receipt", "--receipt", $ReceiptPath, "--expected-status", "COMPLETE") `
        -Label "Complete activation receipt terminal validation" -TimeoutSeconds $TimeoutSeconds
    if ([string]$verified.candidate_sha -ne $ExpectedSha -or
        [string]$verified.candidate_tree -ne $ExpectedTree) {
        throw "Complete activation receipt terminal validation returned a foreign candidate."
    }
    $live = Get-DawnstrikeGitContract $GitPath $RuntimeRoot $TimeoutSeconds $ExpectedSha
    if ($live.tree -ne $ExpectedTree -or
        [string]$Journal.payload.current_sha -ne $live.head -or
        [string]$Journal.payload.current_tree -ne $live.tree) {
        throw "Complete activation runtime HEAD/tree is not journal-bound."
    }
    $liveOrigin = Get-DawnstrikeGitValue $GitPath $RuntimeRoot @("remote", "get-url", "origin") `
        "Complete activation runtime origin validation" $TimeoutSeconds
    Assert-DawnstrikeSafeOrigin $liveOrigin
    if ((Convert-DawnstrikeCanonicalOriginIdentity $liveOrigin) -ne $OriginIdentity -or
        (Get-DawnstrikeSha256Text $liveOrigin) -ne $receiptOriginHash) {
        throw "Complete activation runtime origin is not receipt/journal-bound."
    }
    $tasks = Get-DawnstrikeTaskContract $RuntimeRoot $StateRoot
    if ($tasks.task_contract_sha256 -ne [string]$Receipt.task_contract_sha256 -or
        $tasks.task_definition_contract_sha256 -ne [string]$Receipt.task_definition_contract_sha256 -or
        $tasks.task_action_contract_sha256 -ne [string]$Receipt.task_action_contract_sha256 -or
        $tasks.task_contract_sha256 -ne [string]$ExpectedTask.task_contract_sha256) {
        throw "Complete activation canonical task contract is not exact."
    }
    $auxiliary = if ($StateDeclaration.required) {
        Get-DawnstrikeAuxiliaryCaptureTask $RuntimeRoot $StateRoot -AllowDisabled
    }
    else { $null }
    if ($StateDeclaration.required) {
        if ([bool]$Receipt.auxiliary_capture_present -ne [bool]$auxiliary.present) {
            throw "Complete activation auxiliary-task presence is not exact."
        }
        if ($auxiliary.present) {
            if ($auxiliary.state -ne "Disabled" -or
                [string]$auxiliary.xml_sha256 -ne [string]$Receipt.auxiliary_capture_xml_sha256 -or
                [string]$auxiliary.definition_contract_sha256 -ne [string]$Receipt.auxiliary_capture_definition_contract_sha256 -or
                [string]$auxiliary.action_contract_sha256 -ne [string]$Receipt.auxiliary_capture_action_contract_sha256) {
                throw "Complete activation auxiliary-task contract is not exact."
            }
        }
    }
    # The scheduler backup records the pre-swap task actions, while the
    # COMPLETE receipt records the newly SHA-bound live actions.  Bind each
    # side independently through the receipt-hashed backup manifest instead
    # of incorrectly requiring the two intentionally different contracts to
    # be equal.
    $terminalBackupManifest = Get-DawnstrikeTaskXmlBackupManifest `
        -StateRoot $StateRoot -BackupName ([string]$Receipt.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$Receipt.scheduler_backup_manifest_sha256)
    $null = Assert-DawnstrikeTaskXmlBackup `
        -StateRoot $StateRoot -BackupName ([string]$Receipt.scheduler_backup_name) `
        -ExpectedManifestSha256 ([string]$Receipt.scheduler_backup_manifest_sha256) `
        -ExpectedTaskContractSha256 ([string]$terminalBackupManifest.task_contract_sha256) `
        -ExpectedTaskDefinitionContractSha256 ([string]$terminalBackupManifest.task_definition_contract_sha256) `
        -ExpectedTaskActionContractSha256 ([string]$terminalBackupManifest.task_action_contract_sha256)
    $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
        -Receipt $Receipt -StateRoot $StateRoot -BackupRoot $BackupRoot `
        -ToolRoot $CandidateRoot -GitPath $GitPath -PythonPath $PythonPath `
        -TimeoutSeconds $TimeoutSeconds -RequireRollbackCheckout
    if ($StateDeclaration.required) {
        $proof = Get-DawnstrikeStatePreparationProof `
            -CandidateRoot $CandidateRoot -StateRoot $StateRoot -BackupRoot $BackupRoot `
            -CandidateSha $ExpectedSha -CandidateTree $ExpectedTree `
            -PythonPath $PythonPath -TimeoutSeconds $TimeoutSeconds
        if ([string]$Receipt.state_preparation_receipt_sha256 -ne [string]$proof.receipt_sha256 -or
            [string]$Receipt.state_preparation_after_db_sha256 -ne [string]$proof.after_db_sha256 -or
            [string]$Receipt.state_preparation_inventory_sha256 -ne [string]$proof.inventory_sha256) {
            throw "Complete activation state-preparation lineage is not exact."
        }
    }
    return [pscustomobject]@{ receipt = $verified; runtime = $live; tasks = $tasks; auxiliary = $auxiliary }
}

function Disable-DawnstrikeCanonicalTasks {
    [CmdletBinding()]
    param()

    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Canonical Dawnstrike task name is not unique before disable: $taskName"
        }
        $task = $matches[0]
        if ([string]$task.State -ne "Ready") {
            throw "Canonical task is not exactly Ready before disable: $taskName"
        }
        Disable-ScheduledTask -TaskName $taskName -TaskPath ([string]$task.TaskPath) -ErrorAction Stop | Out-Null
    }
}

function Enable-DawnstrikeCanonicalTasks {
    [CmdletBinding()]
    param()

    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1) {
            throw "Canonical Dawnstrike task name is not unique before enable: $taskName"
        }
        $task = $matches[0]
        if ([string]$task.State -ne "Disabled") {
            throw "Canonical task is not exactly Disabled before enable: $taskName"
        }
        Enable-ScheduledTask -TaskName $taskName -TaskPath ([string]$task.TaskPath) -ErrorAction Stop | Out-Null
    }
}

function Set-DawnstrikeCanonicalTaskExpectedSha {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha
    )
    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
        if ($matches.Count -ne 1 -or [string]$matches[0].State -ne "Disabled") {
            throw "Canonical task must be uniquely Disabled before SHA rebind: $taskName"
        }
        $policy = Get-DawnstrikeCanonicalTaskPolicy $taskName $RuntimeRoot $StateRoot $ExpectedSha
        $action = New-ScheduledTaskAction -Execute $script:DawnstrikePowerShellExecutable -Argument ([string]$policy.arguments) -WorkingDirectory $RuntimeRoot
        Set-ScheduledTask -TaskName $taskName -TaskPath ([string]$matches[0].TaskPath) -Action $action -ErrorAction Stop | Out-Null
    }
}

function Set-DawnstrikeTasksFailClosedDisabled {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot
    )

    foreach ($taskName in $script:DawnstrikeCanonicalTaskNames) {
        try {
            $matches = @(Get-ScheduledTask -TaskName $taskName -ErrorAction Stop)
            if ($matches.Count -ne 1) { continue }
            $task = $matches[0]
            if ([string]$task.State -ne "Disabled") {
                Disable-ScheduledTask -TaskName $taskName -TaskPath ([string]$task.TaskPath) -ErrorAction Stop | Out-Null
            }
        }
        catch {
            # Best effort only: the caller still throws the ambiguous-state hard stop.
        }
    }
    $proof = Get-DawnstrikeTaskContract $RuntimeRoot $StateRoot -AllowDisabled
    if ($proof.disabled_count -ne 5 -or $proof.enabled_count -ne 0) {
        throw "Unable to prove that all canonical tasks are exactly Disabled."
    }
    return $proof
}

function Write-DawnstrikeActivationJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Payload,
        [Parameter(Mandatory = $true)][string]$Path
    )

    Assert-DawnstrikeNoReparseComponents $Path "Receipt output"
    $parent = Split-Path -Parent $Path
    Assert-DawnstrikeNoReparseComponents $parent "Receipt output root"
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    Assert-DawnstrikeNoReparseComponents $parent "Receipt output root"
    $temporary = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    Assert-DawnstrikeNoReparseComponents $temporary "Temporary receipt output"
    try {
        $json = $Payload | ConvertTo-Json -Depth 12
        $bytes = [System.Text.UTF8Encoding]::new($false).GetBytes($json)
        $stream = [System.IO.FileStream]::new(
            $temporary,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None,
            4096,
            [System.IO.FileOptions]::WriteThrough
        )
        try {
            $stream.Write($bytes, 0, $bytes.Length)
            $stream.Flush($true)
        }
        finally {
            $stream.Dispose()
        }
        Assert-DawnstrikeNoReparseComponents $temporary "Temporary receipt output"
        $temporaryItem = Get-Item -LiteralPath $temporary -Force -ErrorAction Stop
        if ($temporaryItem.PSIsContainer -or ($temporaryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Temporary receipt output is not a regular file."
        }
        Assert-DawnstrikeNoReparseComponents $Path "Receipt output"
        if (Test-Path -LiteralPath $Path) {
            throw "Receipt output already exists."
        }
        [System.IO.File]::Move($temporary, $Path)
        Assert-DawnstrikeNoReparseComponents $Path "Receipt output"
        $writtenItem = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($writtenItem.PSIsContainer -or ($writtenItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Receipt output is not a regular file after move."
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
}

function Invoke-DawnstrikeContractCli {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $contract = Join-Path $CandidateRoot "scripts\runtime_activation_contract.py"
    $result = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList (@($contract) + $Arguments) `
        -WorkingDirectory $CandidateRoot `
        -Label $Label `
        -TimeoutSeconds $TimeoutSeconds
    try {
        return ([string]$result.Stdout | ConvertFrom-Json)
    }
    catch {
        throw "$Label did not return valid JSON."
    }
}

function Get-DawnstrikePriorRuntimeAuthorization {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$PreviousSha,
        [Parameter(Mandatory = $true)][string]$PreviousTree,
        [Parameter(Mandatory = $true)][string]$OriginIdentity,
        [Parameter(Mandatory = $true)][string]$OriginSha256,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $empty = Get-DawnstrikeSha256Text ""
    $notAuthorized = [pscustomobject]@{
        authorized = $false
        disposition = "QUARANTINED_UNAUTHORIZED"
        receipt_sha256 = $empty
        journal_sha256 = $empty
        receipt_path = ""
        journal_path = ""
    }
    $receiptRoot = Join-Path $StateRoot "receipts\runtime-activation"
    if (-not (Test-Path -LiteralPath $receiptRoot -PathType Container)) { return $notAuthorized }
    Assert-DawnstrikeNoReparseComponents $receiptRoot "Prior activation receipt root"
    $items = @(
        Get-ChildItem -LiteralPath $receiptRoot -Filter "runtime-activation-*.json" -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notlike "*.prepared.json" } |
            Sort-Object Name
    )
    foreach ($item in $items) {
        try {
            Assert-DawnstrikeNoReparseComponents $item.FullName "Prior activation receipt"
            $prior = Invoke-DawnstrikeContractCli $PythonPath $CandidateRoot @(
                "verify-receipt", "--receipt", $item.FullName, "--expected-status", "COMPLETE"
            ) "Prior activation authorization receipt verification" $TimeoutSeconds
            if (
                [string]$prior.candidate_sha -ne $PreviousSha -or
                [string]$prior.candidate_tree -ne $PreviousTree -or
                [string]$prior.runtime_origin_sha256 -ne $OriginSha256
            ) { continue }
            $priorId = [string]$prior.activation_id
            $journalPath = Join-Path $StateRoot "receipts\runtime-operation\runtime-activation-$priorId.json"
            if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) { continue }
            Assert-DawnstrikeNoReparseComponents $journalPath "Prior activation authorization journal"
            $journalInterpreter = Get-DawnstrikeApprovedLockInterpreter
            $journal = Get-DawnstrikeStrictRuntimeOperationJournal `
                $journalPath $journalInterpreter.path $journalInterpreter.sha256
            $relative = ([System.IO.Path]::GetFullPath($item.FullName).Substring(([System.IO.Path]::GetFullPath($StateRoot)).TrimEnd('\').Length + 1) -replace '\\','/')
            if (
                [string]$journal.payload.operation -ne "runtime_activation" -or
                [string]$journal.payload.phase -ne "COMPLETE" -or
                [string]$journal.payload.candidate_sha -ne $PreviousSha -or
                [string]$journal.payload.candidate_tree -ne $PreviousTree -or
                [string]$journal.payload.current_sha -ne $PreviousSha -or
                [string]$journal.payload.current_tree -ne $PreviousTree -or
                [string]$journal.payload.origin_identity -ne $OriginIdentity -or
                [string]$journal.payload.complete_receipt_relative_path -ne $relative -or
                [string]$journal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $item.FullName)
            ) { continue }
            return [pscustomobject]@{
                authorized = $true
                disposition = "AUTHORIZED_COMPLETE_CHAIN"
                receipt_sha256 = Get-DawnstrikeSha256File $item.FullName
                journal_sha256 = Get-DawnstrikeSha256File $journalPath
                receipt_path = $item.FullName
                journal_path = $journalPath
            }
        }
        catch {
            # A malformed, foreign, or stale prior receipt is not an
            # authorization. Continue scanning so one poisoned artifact cannot
            # select a different runtime by accident.
            continue
        }
    }
    return $notAuthorized
}

function Get-DawnstrikeStatePreparationProof {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [string]$GitPath = "",
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $tool = Join-Path $CandidateRoot "scripts\state_preparation.py"
    if (-not (Test-Path -LiteralPath $tool -PathType Leaf)) {
        throw "Candidate declares the sidecar contract but state-preparation tool is missing."
    }
    $receiptPath = Join-Path $StateRoot ("receipts\state-preparation\state-preparation-" + $CandidateSha + ".json")
    Assert-DawnstrikeNoReparseComponents $receiptPath "State-preparation receipt"
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        throw "Matching COMPLETE state-preparation receipt is required before activation."
    }
    $receipt = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @(
            $tool, "--db-path", (Join-Path $StateRoot "shadow_real.sqlite"),
            "--state-root", $StateRoot, "--backup-root", $BackupRoot,
            "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree,
            "--verify-receipt", $receiptPath
        ) `
        -WorkingDirectory $CandidateRoot `
        -Label "State-preparation receipt verification" `
        -TimeoutSeconds $TimeoutSeconds
    try { $parsed = [string]$receipt.Stdout | ConvertFrom-Json }
    catch { throw "State-preparation receipt verification did not return valid JSON." }
    if ($parsed.status -ne "COMPLETE") { throw "State-preparation receipt is not COMPLETE." }
    $liveProcess = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @($tool, "--db-path", (Join-Path $StateRoot "shadow_real.sqlite"), "--inspect-live") `
        -WorkingDirectory $CandidateRoot `
        -Label "Live state-preparation inventory verification" `
        -TimeoutSeconds $TimeoutSeconds
    try { $live = [string]$liveProcess.Stdout | ConvertFrom-Json }
    catch { throw "Live state-preparation inventory verification did not return valid JSON." }
    if (
        [string]$live.db_sha256 -ne [string]$parsed.after_db_sha256 -or
        [string]$live.wal_sha256 -ne [string]$parsed.after_wal_sha256 -or
        [string]$live.shm_sha256 -ne [string]$parsed.after_shm_sha256 -or
        [string]$live.inventory_sha256 -ne [string]$parsed.inventory_sha256 -or
        [string]$live.logical_snapshot_sha256 -ne [string]$parsed.after_logical_snapshot_sha256 -or
        [int]$live.schema_marker -ne 30 -or
        [string]$live.quick_check -ne "ok"
    ) {
        throw "Live state does not match the COMPLETE state-preparation receipt."
    }
    $bundle = Resolve-DawnstrikeActivationRoot ([string]$parsed.backup_bundle_path) "State-preparation backup bundle"
    $backupRootResolved = Resolve-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    if (-not $bundle.StartsWith($backupRootResolved.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "State-preparation backup bundle is outside the supplied backup root."
    }
    $manifestPath = Join-Path $bundle "manifest.json"
    Assert-DawnstrikeNoReparseComponents $manifestPath "State-preparation backup manifest"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "State-preparation backup manifest is missing."
    }
    if ((Get-DawnstrikeSha256File $manifestPath) -ne [string]$parsed.backup_manifest_file_sha256) {
        throw "State-preparation backup manifest file hash does not match the receipt."
    }
    $stateVerification = Invoke-DawnstrikeActivationProcess `
        -FilePath $PythonPath `
        -ArgumentList @(
            (Join-Path $CandidateRoot "scripts\state_disaster_recovery.py"),
            "restore-verify", "--bundle", $bundle,
            "--target-db", (Join-Path $StateRoot "shadow_real.sqlite"),
            "--backup-root", $backupRootResolved, "--state-root", $StateRoot
        ) `
        -WorkingDirectory $CandidateRoot `
        -Label "State-preparation backup verification" `
        -TimeoutSeconds $TimeoutSeconds
    try { $verifiedBackup = [string]$stateVerification.Stdout | ConvertFrom-Json }
    catch { throw "State-preparation backup verification did not return valid JSON." }
    if (
        $verifiedBackup.status -ne "VERIFY" -or
        [string]$verifiedBackup.backup_id -ne [string]$parsed.backup_id -or
        [string]$verifiedBackup.bundle_path -ne $bundle -or
        [string]$verifiedBackup.manifest_sha256 -ne [string]$parsed.backup_manifest_sha256 -or
        [string]$verifiedBackup.backup_db_sha256 -ne [string]$parsed.backup_db_sha256 -or
        [string]$verifiedBackup.source_release_sha -ne $CandidateSha -or
        [int]$verifiedBackup.schema_version -ne 30 -or
        [string]$verifiedBackup.quick_check -ne "ok" -or
        $verifiedBackup.write_performed -ne $false -or
        $verifiedBackup.automatic_overwrite -ne $false
    ) { throw "State-preparation backup bundle does not match its COMPLETE receipt." }
    return [pscustomobject]@{
        receipt = $parsed
        receipt_sha256 = [string]$parsed.receipt_sha256
        receipt_file_sha256 = Get-DawnstrikeSha256File $receiptPath
        after_db_sha256 = [string]$parsed.after_db_sha256
        after_wal_sha256 = [string]$parsed.after_wal_sha256
        after_shm_sha256 = [string]$parsed.after_shm_sha256
        after_logical_snapshot_sha256 = [string]$parsed.after_logical_snapshot_sha256
        inventory_sha256 = [string]$parsed.inventory_sha256
        backup_id = [string]$parsed.backup_id
        backup_bundle_path = $bundle
        backup_db_sha256 = [string]$parsed.backup_db_sha256
        backup_manifest_sha256 = [string]$parsed.backup_manifest_sha256
        backup_manifest_file_sha256 = [string]$parsed.backup_manifest_file_sha256
    }
}

function Assert-DawnstrikeCaptureHardeningAttestation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$Auxiliary,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [ValidateSet("PRE_SWAP", "POST_SWAP")][string]$Stage = "PRE_SWAP"
    )
    if (-not $Auxiliary.present -or $Auxiliary.state -ne "Disabled") {
        throw "Activation requires the auxiliary capture task to be exactly Disabled."
    }
    $root = Join-Path $StateRoot "receipts\capture-task"
    Assert-DawnstrikeNoReparseComponents $root "Capture-task hardening receipt root"
    $expectedPath = Join-Path $root ("capture-task-hardening-" + $CandidateSha + ".json")
    Assert-DawnstrikeNoReparseComponents $expectedPath "Candidate hardening receipt"
    if (-not (Test-Path -LiteralPath $expectedPath -PathType Leaf)) { throw "Activation requires the exact current-candidate hardening receipt." }
    $paths = @(Get-Item -LiteralPath $expectedPath -Force -ErrorAction Stop)
    $contract = Join-Path $CandidateRoot "scripts\capture_task_hardening_contract.py"
    $result = Invoke-DawnstrikeActivationProcess $PythonPath @(
        $contract, "verify-hardening", "--receipt", $paths[0].FullName,
        "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
    ) $CandidateRoot "Activation hardening receipt verification" $TimeoutSeconds
    try { $receipt = [string]$result.Stdout | ConvertFrom-Json } catch { throw "Activation hardening receipt verification did not return valid JSON." }
    $stateFull = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'
    $receiptRelative = ([System.IO.Path]::GetFullPath($paths[0].FullName).Substring($stateFull.Length) -replace '\\','/')
    if ([string]$receipt.status -ne "COMPLETE" -or [string]$receipt.schema_version -ne "dawnstrike.capture_task_hardening_receipt.v2" -or [string]$receipt.candidate_sha -ne $CandidateSha -or [string]$receipt.candidate_tree -ne $CandidateTree -or [string]$receipt.final_state -ne "Disabled" -or [string]$receipt.receipt_relative_path -ne $receiptRelative) { throw "Activation hardening receipt is not bound to the exact disabled candidate task." }
    $principalHash = Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Principal"
    $triggerHash = Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Triggers"
    $settingsHash = Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Settings"
    $actionHash = Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Actions"
    if ([string]$receipt.xml_after_sha256 -ne [string]$Auxiliary.xml_sha256 -or [string]$receipt.principal_after_sha256 -ne $principalHash -or [string]$receipt.trigger_sha256 -ne $triggerHash -or [string]$receipt.settings_after_sha256 -ne $settingsHash -or [string]$receipt.action_after_sha256 -ne $actionHash) { throw "Activation hardening receipt does not match the current auxiliary task." }
    $safetyArguments = @{
        Xml = [string]$Auxiliary.xml
        RuntimeRoot = $RuntimeRoot
        StateRoot = $StateRoot
        ExpectedCandidateSha = $CandidateSha
        ExpectedInterpreterPath = [string]$receipt.interpreter_path
        ExpectedInterpreterSha256 = [string]$receipt.interpreter_sha256
        ExpectedInterpreterSignerThumbprint = [string]$receipt.interpreter_signer_thumbprint
        ExpectedEnabled = "false"
        RequirePasswordPrincipal = $true
        RequireRunner = $true
    }
    if ($Stage -eq "PRE_SWAP") { $safetyArguments.AllowMissingBootstrap = $true }
    $null = Assert-DawnstrikeCaptureTaskSafety @safetyArguments
    $receiptBindings = $receipt.action_bindings
    if ([string]$receipt.runner_path -ne [string]$receiptBindings.runner_path -or [string]$receipt.runner_sha256 -ne [string]$receiptBindings.runner_sha256 -or [string]$receiptBindings.candidate_sha -ne $CandidateSha) { throw "Activation hardening input bindings are not exact." }
    $taskDocument = [System.Xml.XmlDocument]::new()
    $taskDocument.LoadXml([string]$Auxiliary.xml)
    $execNode = @($taskDocument.SelectNodes("//*[local-name()='Exec']"))
    if ($execNode.Count -ne 1) { throw "Activation hardening action is ambiguous." }
    $argumentNode = @($execNode[0].ChildNodes | Where-Object { $_.LocalName -eq "Arguments" })
    if ($argumentNode.Count -ne 1) { throw "Activation hardening action arguments are missing." }
    $tokens = @(Get-DawnstrikeCaptureQuotedTokens ([string]$argumentNode[0].InnerText))
    $actionValues = @{}
    $expectedBytecodePrefix = [System.IO.Path]::GetFullPath((Join-Path $StateRoot ("capture-bytecode\" + $CandidateSha)))
    $expectedBootstrap = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot "scripts\dawnstrike_python_bootstrap.py"))
    $expectedRunner = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot "scripts\run_daily_intraday_capture.py"))
    $candidateBootstrap = [System.IO.Path]::GetFullPath((Join-Path $CandidateRoot "scripts\dawnstrike_python_bootstrap.py"))
    Assert-DawnstrikeNoReparseComponents $candidateBootstrap "Candidate hardening bootstrap"
    if (-not (Test-Path -LiteralPath $candidateBootstrap -PathType Leaf)) {
        throw "Candidate hardening bootstrap is missing."
    }
    if (-not [string]::IsNullOrWhiteSpace($GitPath)) {
        $bootstrapRelative = "scripts/dawnstrike_python_bootstrap.py"
        $headBootstrap = Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
            "rev-parse", ("{0}:{1}" -f $CandidateSha, $bootstrapRelative)
        ) "Candidate hardening bootstrap HEAD binding" $TimeoutSeconds
        $workingBootstrap = Get-DawnstrikeGitValue $GitPath $CandidateRoot @(
            "hash-object", ("--path={0}" -f $bootstrapRelative), "--", $candidateBootstrap
        ) "Candidate hardening bootstrap working-tree binding" $TimeoutSeconds
        if ($headBootstrap -notmatch '^[0-9a-f]{40}$' -or $workingBootstrap -cne $headBootstrap) {
            throw "Candidate hardening bootstrap does not match the exact candidate HEAD."
        }
    }
    # The delayed-SIP action is a scheduled Python boundary.  It must use the
    # exact isolated interpreter prefix and the materialized release bootstrap;
    # accepting a legacy direct runner here would re-enable global .pth or
    # editable-install resolution before the release root is selected.
    if (
        $tokens.Count -lt 19 -or
        $tokens[0] -cne "-I" -or
        $tokens[1] -cne "-B" -or
        $tokens[2] -cne "-S" -or
        $tokens[3] -cne "-X" -or
        $tokens[4] -cne ("pycache_prefix=" + $expectedBytecodePrefix) -or
        $tokens[5] -cne "-u" -or
        $tokens[6] -cne "-c" -or
        $tokens[7] -cne (Get-DawnstrikeCaptureBootstrapPreloader) -or
        [System.IO.Path]::GetFullPath([string]$tokens[8]) -cne $expectedBootstrap -or
        $tokens[9] -notmatch '^[0-9a-f]{64}$' -or
        $tokens[9] -cne (Get-DawnstrikeSha256File $candidateBootstrap) -or
        $tokens[10] -cne "--release-root" -or
        [System.IO.Path]::GetFullPath([string]$tokens[11]) -cne [System.IO.Path]::GetFullPath($RuntimeRoot) -or
        $tokens[12] -cne "--expected-sha" -or
        $tokens[13] -cne $CandidateSha -or
        $tokens[14] -cne "--script" -or
        [System.IO.Path]::GetFullPath([string]$tokens[15]) -cne $expectedRunner -or
        $tokens[16] -cne "--"
    ) { throw "Activation hardening action bootstrap or isolation prefix is not exact." }
    $optionStart = 17
    if ($tokens[$tokens.Count - 1] -cne "--execute" -or (($tokens.Count - $optionStart - 1) % 2) -ne 0) {
        throw "Activation hardening action option contract is malformed."
    }
    for ($index = $optionStart; $index -lt ($tokens.Count - 1); $index += 2) {
        if (-not ([string]$tokens[$index]).StartsWith("--", [System.StringComparison]::Ordinal) -or [string]::IsNullOrWhiteSpace([string]$tokens[$index + 1])) {
            throw "Activation hardening action option contract is malformed."
        }
        $actionValues[[string]$tokens[$index]] = [string]$tokens[$index + 1]
    }
    foreach ($binding in @(
        @("--candidate-sha", "candidate_sha"), @("--symbols-manifest", "symbols_manifest_path"),
        @("--symbols-manifest-sha256", "symbols_manifest_sha256"), @("--entitlement-receipt", "entitlement_receipt_path"),
        @("--entitlement-receipt-sha256", "entitlement_receipt_sha256"), @("--source-config", "source_config_path"),
        @("--source-config-sha256", "source_config_sha256")
    )) {
        if ([string]$actionValues[$binding[0]] -ne [string]$receiptBindings.($binding[1])) { throw "Activation hardening receipt input binding does not match the disabled task." }
    }
    $liveRunner = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot "scripts\run_daily_intraday_capture.py"))
    $candidateRunner = [System.IO.Path]::GetFullPath((Join-Path $CandidateRoot "scripts\run_daily_intraday_capture.py"))
    if ((Get-DawnstrikeSha256File $candidateRunner) -ne [string]$receipt.runner_sha256) {
        throw "Hardening receipt candidate runner identity is invalid."
    }
    $runtimeRunnerSha = Get-DawnstrikeSha256File $liveRunner
    if ($Stage -eq "PRE_SWAP" -and $runtimeRunnerSha -ne [string]$receipt.runner_before_sha256) {
        throw "Pre-swap runtime runner does not match the hardening migration identity."
    }
    if ($Stage -eq "POST_SWAP" -and $runtimeRunnerSha -ne [string]$receipt.runner_sha256) {
        throw "Post-swap runtime runner does not match the attested candidate identity."
    }
    if ($Stage -eq "POST_SWAP") {
        $liveBootstrap = [System.IO.Path]::GetFullPath((Join-Path $RuntimeRoot "scripts\dawnstrike_python_bootstrap.py"))
        Assert-DawnstrikeNoReparseComponents $liveBootstrap "Post-swap hardening bootstrap"
        if (-not (Test-Path -LiteralPath $liveBootstrap -PathType Leaf)) {
            throw "Post-swap runtime hardening bootstrap is missing."
        }
        if ((Get-DawnstrikeSha256File $liveBootstrap) -ne (Get-DawnstrikeSha256File $candidateBootstrap)) {
            throw "Post-swap runtime hardening bootstrap does not match the candidate."
        }
    }
    return [pscustomobject]@{ path = $paths[0].FullName; payload = $receipt; raw_sha256 = Get-DawnstrikeSha256File $paths[0].FullName }
}

function Assert-DawnstrikeCaptureRebindChain {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][object]$ActivationReceipt,
        [Parameter(Mandatory = $true)][object]$Auxiliary,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$CandidateSha,
        [Parameter(Mandatory = $true)][string]$CandidateTree,
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    if (-not $Auxiliary.present -or $Auxiliary.state -ne "Ready") {
        throw "A post-rebind capture chain requires the exact Ready auxiliary task."
    }
    $captureReceiptRoot = Join-Path $StateRoot "receipts\capture-task"
    Assert-DawnstrikeNoReparseComponents $captureReceiptRoot "Capture-task receipt root"
    $paths = @(Get-ChildItem -LiteralPath $captureReceiptRoot -Filter ("capture-task-rebind-" + $CandidateSha + ".json") -File -ErrorAction SilentlyContinue)
    foreach ($path in $paths) {
        Assert-DawnstrikeNoReparseComponents $path.FullName "Capture-task receipt"
    }
    if ($paths.Count -ne 1) { throw "Exactly one COMPLETE capture rebind receipt is required for a Ready auxiliary task." }
    $captureContract = Join-Path $CandidateRoot "scripts\capture_task_contract.py"
    $result = Invoke-DawnstrikeActivationProcess $PythonPath @(
        $captureContract, "verify-receipt", "--receipt", $paths[0].FullName,
        "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
    ) $CandidateRoot "Capture rebind chain verification" $TimeoutSeconds
    try { $capture = [string]$result.Stdout | ConvertFrom-Json }
    catch { throw "Capture rebind chain verification did not return valid JSON." }
    $activationPath = Join-Path $StateRoot ("receipts\runtime-activation\runtime-activation-" + [string]$ActivationReceipt.activation_id + ".json")
    Assert-DawnstrikeNoReparseComponents $activationPath "Capture rebind activation receipt"
    if (-not (Test-Path -LiteralPath $activationPath -PathType Leaf)) {
        throw "Capture rebind activation receipt is missing."
    }
    $activationItem = Get-Item -LiteralPath $activationPath -Force -ErrorAction Stop
    if (
        [string]$capture.activation_id -ne [string]$ActivationReceipt.activation_id -or
        [string]$capture.activation_receipt_name -ne $activationItem.Name -or
        [string]$capture.activation_receipt_sha256 -ne (Get-DawnstrikeSha256File $activationItem) -or
        [string]$capture.xml_after_sha256 -ne [string]$Auxiliary.xml_sha256 -or
        [string]$capture.action_after_sha256 -ne [string]$Auxiliary.action_contract_sha256 -or
        [string]$capture.definition_after_sha256 -ne [string]$Auxiliary.definition_contract_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Principal") -ne [string]$capture.principal_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Triggers") -ne [string]$capture.trigger_sha256 -or
        (Get-DawnstrikeAuxiliarySectionHash ([string]$Auxiliary.xml) "Settings" "false") -ne [string]$capture.settings_sha256
    ) { throw "Ready auxiliary task is not bound to the exact activation receipt chain." }
    $hardeningRelative = [string]$ActivationReceipt.capture_hardening_receipt_relative_path
    if ([string]::IsNullOrWhiteSpace($hardeningRelative) -or $hardeningRelative -eq "NONE" -or $hardeningRelative -match '(^|[\\/])\.\.?([\\/]|$)') {
        throw "Ready auxiliary task is missing its activation-bound hardening receipt path."
    }
    $hardeningPath = [System.IO.Path]::GetFullPath((Join-Path $StateRoot ($hardeningRelative -replace '/', '\')))
    $statePrefix = [System.IO.Path]::GetFullPath($StateRoot).TrimEnd('\') + '\'
    if (-not $hardeningPath.StartsWith($statePrefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Activation-bound hardening receipt escaped StateRoot." }
    Assert-DawnstrikeNoReparseComponents $hardeningPath "Activation-bound hardening receipt"
    if (-not (Test-Path -LiteralPath $hardeningPath -PathType Leaf) -or (Get-DawnstrikeSha256File $hardeningPath) -ne [string]$ActivationReceipt.capture_hardening_receipt_raw_sha256) {
        throw "Activation-bound hardening receipt raw identity is invalid."
    }
    $hardeningContract = Join-Path $CandidateRoot "scripts\capture_task_hardening_contract.py"
    $hardeningResult = Invoke-DawnstrikeActivationProcess $PythonPath @(
        $hardeningContract, "verify-hardening", "--receipt", $hardeningPath,
        "--candidate-sha", $CandidateSha, "--candidate-tree", $CandidateTree
    ) $CandidateRoot "Ready auxiliary hardening-chain verification" $TimeoutSeconds
    try { $hardening = [string]$hardeningResult.Stdout | ConvertFrom-Json } catch { throw "Ready auxiliary hardening-chain verification did not return valid JSON." }
    if (
        [string]$hardening.schema_version -ne "dawnstrike.capture_task_hardening_receipt.v2" -or
        [string]$hardening.receipt_sha256 -ne [string]$ActivationReceipt.capture_hardening_receipt_sha256 -or
        [string]$hardening.action_after_sha256 -ne [string]$ActivationReceipt.capture_hardening_action_sha256 -or
        [string]$hardening.principal_after_sha256 -ne [string]$ActivationReceipt.capture_hardening_principal_sha256 -or
        [string]$hardening.trigger_sha256 -ne [string]$ActivationReceipt.capture_hardening_trigger_sha256 -or
        [string]$hardening.settings_after_sha256 -ne [string]$ActivationReceipt.capture_hardening_settings_sha256
    ) { throw "Ready auxiliary task hardening attestation chain is not exact." }
    if ([string]$capture.changed_field -ne "candidate_sha_and_input_bindings") {
        throw "Ready auxiliary task receipt does not attest the permitted input-binding transformation."
    }
    foreach ($binding in @(
        @("symbols-manifest-sha256", [string]$capture.symbols_manifest_sha256),
        @("entitlement-receipt-sha256", [string]$capture.entitlement_receipt_sha256),
        @("source-config-sha256", [string]$capture.source_config_sha256)
    )) {
        $escapedName = [regex]::Escape($binding[0])
        $bindingOption = "(?:`"--$escapedName`"|'--$escapedName'|--$escapedName)"
        $bindingPattern = '(?i)(?<![A-Za-z0-9_-])' + $bindingOption + '(?:=|\s+)(?:"' + [regex]::Escape($binding[1]) + '"|' + [regex]::Escape($binding[1]) + ')(?![A-Za-z0-9])'
        if (@([regex]::Matches([string]$Auxiliary.xml, $bindingPattern)).Count -ne 1) {
            throw "Ready auxiliary task does not bind the supplied $($binding[0]) receipt hash."
        }
    }
    return $capture
}

function Enter-DawnstrikeRuntimeActivationLockCore {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $lockRoot = Join-Path $StateRoot "locks"
    Assert-DawnstrikeNoReparseComponents $lockRoot "Runtime activation lock root"
    New-Item -ItemType Directory -Path $lockRoot -Force | Out-Null
    Assert-DawnstrikeNoReparseComponents $lockRoot "Runtime activation lock root"
    $path = Join-Path $lockRoot "dawnstrike-runtime-activation.lock"
    Assert-DawnstrikeNoReparseComponents $path "Runtime activation lock"
    if (Test-Path -LiteralPath $path) {
        throw "A runtime activation lock already exists and requires review."
    }
    if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
        $dailyBefore = @(
            Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
                ForEach-Object {
                    $null = Get-DawnstrikeLockSnapshot -Path $_.FullName -Label "Counterpart daily run lock"
                    $_
                }
        )
        if ($dailyBefore.Count -gt 0) {
            throw "A daily run lock exists; runtime activation is not permitted."
        }
    }
    $token = [guid]::NewGuid().ToString("N")
    $payload = [ordered]@{
        schema_version = "dawnstrike.runtime_activation_lock.v1"
        process_id = $PID
        process_started_at_utc = (Get-Process -Id $PID).StartTime.ToUniversalTime().ToString("o")
        acquired_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        lock_token = $token
        research_only = $true
        broker_execution_enabled = $false
    } | ConvertTo-Json -Depth 4
    $handle = $null
    try {
        $handle = [System.IO.File]::Open(
            $path,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $handle.Write($bytes, 0, $bytes.Length)
        $handle.Flush($true)
    }
    finally {
        if ($null -ne $handle) { $handle.Dispose() }
    }
    Assert-DawnstrikeNoReparseComponents $path "Runtime activation lock"
    $snapshot = if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
        Get-DawnstrikeLockSnapshot -Path $path -Label "Runtime activation lock"
    }
    else { $null }
    if (
        $null -ne $snapshot -and
        (-not $snapshot.present -or $snapshot.lock_token -ne $token)
    ) { throw "Runtime activation lock could not be read back with its own token." }
    if ($null -ne $snapshot) {
        $dailyAfter = @(
            Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
                ForEach-Object {
                    $null = Get-DawnstrikeLockSnapshot -Path $_.FullName -Label "Counterpart daily run lock"
                    $_
                }
        )
        if ($dailyAfter.Count -gt 0) {
            $owned = [pscustomobject]@{
                acquired = $true
                path = $path
                token = $token
                bytes_sha256 = $snapshot.bytes_sha256
            }
            $current = Get-DawnstrikeLockSnapshot -Path $path -Label "Runtime activation lock"
            if (
                $current.present -and
                $current.lock_token -eq $token -and
                $current.bytes_sha256 -eq $snapshot.bytes_sha256
            ) {
                Remove-Item -LiteralPath $path -Force -ErrorAction Stop
                $removed = Get-DawnstrikeLockSnapshot -Path $path -Label "Runtime activation lock" -AllowMissing
                if ($removed.present) { throw "Runtime activation lock could not be relinquished after a conflict." }
            }
            throw "A daily run lock appeared during runtime activation lock acquisition."
        }
    }
    return [pscustomobject]@{
        path = $path
        token = $token
        bytes_sha256 = if ($null -ne $snapshot) { [string]$snapshot.bytes_sha256 } else { $null }
        acquired = $true
    }
}

function Enter-DawnstrikeRuntimeActivationLock {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    if (-not (Get-Command Enter-DawnstrikeLockOperationMutex -ErrorAction SilentlyContinue)) {
        return Enter-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        return Enter-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Exit-DawnstrikeRuntimeActivationLockCore {
    [CmdletBinding()]
    param([AllowNull()][object]$Lock)

    if ($null -eq $Lock -or -not (Test-Path -LiteralPath $Lock.path -PathType Leaf)) {
        return
    }
    try { Assert-DawnstrikeNoReparseComponents $Lock.path "Runtime activation lock" }
    catch { return }
    try {
        if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
            $snapshot = Get-DawnstrikeLockSnapshot -Path $Lock.path -Label "Runtime activation lock"
            if (
                $snapshot.present -and
                [string]$snapshot.lock_token -eq [string]$Lock.token -and
                (
                    [string]::IsNullOrWhiteSpace([string]$Lock.bytes_sha256) -or
                    [string]$snapshot.bytes_sha256 -eq [string]$Lock.bytes_sha256
                )
            ) {
                Remove-Item -LiteralPath $Lock.path -Force
                $after = Get-DawnstrikeLockSnapshot -Path $Lock.path -Label "Runtime activation lock" -AllowMissing
                if ($after.present) { return }
            }
        }
        else {
            $payload = Get-Content -LiteralPath $Lock.path -Raw | ConvertFrom-Json
            if ([string]$payload.lock_token -eq [string]$Lock.token) {
                Remove-Item -LiteralPath $Lock.path -Force
            }
        }
    }
    catch {
        # Never delete a lock whose ownership cannot be proven.
    }
}

function Exit-DawnstrikeRuntimeActivationLock {
    [CmdletBinding()]
    param([AllowNull()][object]$Lock)

    if (-not (Get-Command Enter-DawnstrikeLockOperationMutex -ErrorAction SilentlyContinue)) {
        return Exit-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    $mutex = Enter-DawnstrikeLockOperationMutex
    try {
        Exit-DawnstrikeRuntimeActivationLockCore @PSBoundParameters
    }
    finally {
        Exit-DawnstrikeLockOperationMutex $mutex
    }
}

function Assert-DawnstrikeNoDailyLocks {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$StateRoot)

    $lockRoot = Join-Path $StateRoot "locks"
    if (-not (Test-Path -LiteralPath $lockRoot -PathType Container)) { return }
    Assert-DawnstrikeNoReparseComponents $lockRoot "Daily lock root"
    $dailyLocks = @(
        Get-ChildItem -LiteralPath $lockRoot -Filter "dawnstrike-daily-*.lock" -File -Force |
            ForEach-Object {
                if (Get-Command Get-DawnstrikeLockSnapshot -ErrorAction SilentlyContinue) {
                    $null = Get-DawnstrikeLockSnapshot -Path $_.FullName -Label "Daily run lock"
                }
                $_
            }
    )
    if ($dailyLocks.Count -gt 0) {
        throw "A daily run lock exists; runtime activation is not permitted."
    }
}

function Assert-DawnstrikeSameVolume {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    $roots = @($Paths | ForEach-Object {
        [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath($_)).ToLowerInvariant()
    } | Select-Object -Unique)
    if ($roots.Count -ne 1) {
        throw "Runtime, stage, and rollback paths must share one volume for recoverable rename."
    }
}

# Override the legacy per-script implementation before any operation runs.
. (Join-Path $PSScriptRoot "runtime_activation_lock.ps1")

function Invoke-DawnstrikeRuntimeActivation {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedSha,
        [Parameter(Mandatory = $true)][string]$MarketDate,
        [Parameter(Mandatory = $true)][string]$CiEvidencePath,
        [Parameter(Mandatory = $true)][string]$SolEvidencePath,
        [Parameter(Mandatory = $true)][string]$CandidateRoot,
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$StateRoot,
        [Parameter(Mandatory = $true)][string]$BackupRoot,
        [Parameter(Mandatory = $true)][int]$BackupRetention,
        [Parameter(Mandatory = $true)][int]$ProcessTimeoutSeconds,
        [pscredential]$RunAsCredential,
        [switch]$PreflightOnly,
        [switch]$InjectCrashBetweenRuntimeRenames,
        [ValidateSet("", "after_stage_directory", "after_stage_checkout", "after_init_recovery_lock_release", "after_pre_quiesce_recovery_lock_release", "after_candidate_runtime_rename", "after_ready_journal", "after_enable_before_complete", "after_complete_journal")][string]$TestStageCrashPoint = "",
        [string]$TestNowUtc = ""
    )

    if ($TestStageCrashPoint -ne "" -and $env:DAWNSTRIKE_TEST_ACTIVATION_STAGE_CRASH -ne "1") {
        throw "Activation stage crash injection is test-only."
    }
    if ($InjectCrashBetweenRuntimeRenames -and $env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1") {
        throw "Activation runtime-rename crash injection is test-only."
    }

    $candidate = Resolve-DawnstrikeActivationRoot $CandidateRoot "CandidateRoot"
    $runtime = Resolve-DawnstrikeActivationRoot $RuntimeRoot "RuntimeRoot"
    $state = Resolve-DawnstrikeActivationRoot $StateRoot "StateRoot"
    Assert-DawnstrikeRootIsolation $BackupRoot @($candidate, $runtime, $state) "BackupRoot"
    $backupRoot = if ($PreflightOnly) {
        Get-DawnstrikeFutureActivationRoot $BackupRoot "BackupRoot"
    }
    else {
        Ensure-DawnstrikeActivationRoot $BackupRoot "BackupRoot"
    }
    $toolRoot = Resolve-DawnstrikeActivationRoot (Join-Path $PSScriptRoot "..") "ToolRoot"
    if (-not [string]::Equals(
        $candidate,
        $toolRoot,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "CandidateRoot must be the exact checkout containing the activation tool."
    }
    foreach ($pair in @(@($candidate, $runtime), @($candidate, $state), @($runtime, $state))) {
        if ([string]::Equals($pair[0], $pair[1], [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Candidate, runtime, and state roots must be distinct."
        }
    }
    $gitPath = (Get-DawnstrikeApprovedGit).path
    $pythonPath = (Get-DawnstrikeApprovedLockInterpreter).path
    . (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")
    $activationNowUtc = Get-DawnstrikeActivationNowUtc -TestNowUtc $TestNowUtc

    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $gitPath `
        -ArgumentList @("-C", $candidate, "fetch", "--quiet", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main") `
        -WorkingDirectory $candidate `
        -Label "Candidate origin/main refresh" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $candidateContract = Get-DawnstrikeGitContract $gitPath $candidate $ProcessTimeoutSeconds $ExpectedSha
    $null = Assert-DawnstrikeHelpersBoundToHead `
        -GitPath $gitPath -Root $candidate -TimeoutSeconds $ProcessTimeoutSeconds
    . (Join-Path $PSScriptRoot "capture_task_safety.ps1")
    $stateDeclaration = Get-DawnstrikeStatePreparationDeclaration `
        -CandidateRoot $candidate `
        -GitPath $gitPath `
        -CandidateSha $candidateContract.head `
        -CandidateTree $candidateContract.tree `
        -PythonPath $pythonPath `
        -TimeoutSeconds $ProcessTimeoutSeconds
    . (Join-Path $PSScriptRoot "invoke_dawnstrike_stage.ps1")
    $remoteMain = (Get-DawnstrikeGitValue $gitPath $candidate @("rev-parse", "refs/remotes/origin/main") "origin/main verification" $ProcessTimeoutSeconds).ToLowerInvariant()
    $origin = Get-DawnstrikeGitValue $gitPath $candidate @("remote", "get-url", "origin") "Candidate origin verification" $ProcessTimeoutSeconds
    Assert-DawnstrikeSafeOrigin $origin
    $originIdentity = Convert-DawnstrikeCanonicalOriginIdentity $origin
    $advancedOriginRecovery = $null
    if ($remoteMain -ne $ExpectedSha) {
        $recoveryInterpreter = Get-DawnstrikeApprovedLockInterpreter
        $advancedOriginRecovery = Get-DawnstrikeAdvancedOriginRecoveryAdmission `
            -StateRoot $state -Operation runtime_activation -CandidateSha $ExpectedSha `
            -CandidateTree ([string]$candidateContract.tree) -OriginIdentity $originIdentity `
            -PythonPath $recoveryInterpreter.path -PythonSha256 $recoveryInterpreter.sha256
        if ($null -eq $advancedOriginRecovery) {
            throw "Expected release SHA is not current origin/main and has no exact active recovery journal."
        }
    }
    $null = Invoke-DawnstrikeActivationProcess `
        -FilePath $gitPath `
        -ArgumentList @("-C", $candidate, "merge-base", "--is-ancestor", $ExpectedSha, "refs/remotes/origin/main") `
        -WorkingDirectory $candidate `
        -Label "Candidate remote ancestry verification" `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $originHash = Get-DawnstrikeSha256Text $origin

    $ci = (Resolve-Path -LiteralPath $CiEvidencePath -ErrorAction Stop).Path
    $sol = (Resolve-Path -LiteralPath $SolEvidencePath -ErrorAction Stop).Path
    $evidence = Invoke-DawnstrikeContractCli `
        -PythonPath $pythonPath `
        -CandidateRoot $candidate `
        -Arguments @("validate-evidence", "--ci", $ci, "--sol", $sol, "--candidate-sha", $ExpectedSha, "--candidate-tree", $candidateContract.tree, "--require-live-github-ci", "--require-live-github-owner-authorization") `
        -Label "Runtime activation evidence validation" `
        -TimeoutSeconds $ProcessTimeoutSeconds

    $statePreparation = $null
    if ($stateDeclaration.required) {
        $statePreparation = Get-DawnstrikeStatePreparationProof `
            -CandidateRoot $candidate `
            -StateRoot $state `
            -BackupRoot $backupRoot `
            -CandidateSha $ExpectedSha `
            -CandidateTree $candidateContract.tree `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds
    }
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $candidate `
        -CandidateSha $ExpectedSha `
        -CandidateTree $candidateContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds

    $runtimeContract = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds
    # Capture the exact pre-swap origin before any runtime rename.  A matching
    # HEAD/tree alone is not enough: a hostile .git/config in restored P must
    # be rejected during COMPENSATED recovery.
    $previousRuntimeOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Previous runtime origin verification" $ProcessTimeoutSeconds
    Assert-DawnstrikeSafeOrigin $previousRuntimeOrigin
    $previousRuntimeOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity $previousRuntimeOrigin
    $previousRuntimeOriginSha256 = Get-DawnstrikeSha256Text $previousRuntimeOrigin
    if ($previousRuntimeOriginIdentity -ne $originIdentity -or $previousRuntimeOriginSha256 -ne (Get-DawnstrikeSha256Text $origin)) {
        throw "Previous runtime origin is not the exact accepted origin binding."
    }
    # Replace the candidate-only hash with the raw pre-swap runtime binding;
    # the PREPARED receipt and all later recovery proof now carry P's origin.
    $originHash = $previousRuntimeOriginSha256
    $previousRuntimeAuthorization = Get-DawnstrikePriorRuntimeAuthorization `
        -StateRoot $state -CandidateRoot $candidate `
        -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
        -OriginIdentity $previousRuntimeOriginIdentity -OriginSha256 $originHash `
        -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds
    if ($runtimeContract.head -eq $ExpectedSha) {
        $receiptRoot = Join-Path $state "receipts\runtime-activation"
        Assert-DawnstrikeNoReparseComponents $receiptRoot "Activation receipt root"
        $existing = @(
            Get-ChildItem -LiteralPath $receiptRoot -Filter "runtime-activation-*.json" -File -ErrorAction SilentlyContinue |
                ForEach-Object {
                    Assert-DawnstrikeNoReparseComponents $_.FullName "Activation receipt"
                    $_
                } | Sort-Object LastWriteTimeUtc -Descending
        )
        foreach ($item in $existing) {
            try {
                $receipt = Invoke-DawnstrikeContractCli $pythonPath $candidate @("verify-receipt", "--receipt", $item.FullName, "--expected-status", "COMPLETE") "Existing activation receipt verification" $ProcessTimeoutSeconds
                if ($receipt.candidate_sha -eq $ExpectedSha) {
                    if ($runtimeContract.tree -ne [string]$receipt.candidate_tree) {
                        throw "Existing activation receipt does not match the runtime tree."
                    }
                    $runtimeOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Installed origin verification" $ProcessTimeoutSeconds
                    if ((Get-DawnstrikeSha256Text $runtimeOrigin) -ne [string]$receipt.runtime_origin_sha256) {
                        throw "Existing activation receipt does not match the runtime origin."
                    }
                    $existingTasks = Get-DawnstrikeTaskContract $runtime $state
                    if (
                        $existingTasks.task_contract_sha256 -ne
                            [string]$receipt.task_contract_sha256 -or
                        $existingTasks.task_definition_contract_sha256 -ne
                            [string]$receipt.task_definition_contract_sha256 -or
                        $existingTasks.task_action_contract_sha256 -ne
                            [string]$receipt.task_action_contract_sha256
                    ) {
                        throw "Existing activation receipt does not match exact Ready task XML."
                    }
                    if ($stateDeclaration.required) {
                        $existingAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
                        if ([bool]$receipt.auxiliary_capture_present -ne [bool]$existingAuxiliary.present) {
                            throw "Existing activation receipt does not match auxiliary capture presence."
                        }
                        if ($existingAuxiliary.present) {
                            if ($existingAuxiliary.state -eq "Disabled") {
                                if (
                                    $existingAuxiliary.definition_contract_sha256 -ne [string]$receipt.auxiliary_capture_definition_contract_sha256 -or
                                    $existingAuxiliary.action_contract_sha256 -ne [string]$receipt.auxiliary_capture_action_contract_sha256
                                ) { throw "Existing activation receipt does not match the disabled auxiliary task." }
                                $existingHardening = Assert-DawnstrikeCaptureHardeningAttestation `
                                    -Auxiliary $existingAuxiliary -CandidateRoot $candidate -StateRoot $state `
                                    -RuntimeRoot $runtime -CandidateSha $ExpectedSha -CandidateTree $candidateContract.tree `
                                    -GitPath $gitPath `
                                    -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds -Stage POST_SWAP
                                if ([string]$receipt.capture_hardening_receipt_raw_sha256 -ne [string]$existingHardening.raw_sha256 -or [string]$receipt.capture_hardening_receipt_sha256 -ne [string]$existingHardening.payload.receipt_sha256) {
                                    throw "Existing activation receipt does not match the hardening attestation chain."
                                }
                            }
                            elseif ($existingAuxiliary.state -eq "Ready") {
                                $null = Assert-DawnstrikeCaptureRebindChain `
                                    -ActivationReceipt $receipt -Auxiliary $existingAuxiliary `
                                    -CandidateRoot $candidate -StateRoot $state -CandidateSha $ExpectedSha `
                                    -CandidateTree $candidateContract.tree -PythonPath $pythonPath `
                                    -TimeoutSeconds $ProcessTimeoutSeconds
                            }
                            else { throw "Existing auxiliary capture task is in an ambiguous state." }
                        }
                        if (
                            [string]$receipt.state_preparation_receipt_sha256 -ne [string]$statePreparation.receipt_sha256 -or
                            [string]$receipt.state_preparation_after_db_sha256 -ne [string]$statePreparation.after_db_sha256 -or
                            [string]$receipt.state_preparation_inventory_sha256 -ne [string]$statePreparation.inventory_sha256
                        ) { throw "Existing activation receipt does not match live state preparation." }
                    }
                     $backupManifestPath = Join-Path $state ("scheduler-backups\" + [string]$receipt.scheduler_backup_name + "\manifest.json")
                     $backupManifest = Get-Content -LiteralPath $backupManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
                     $null = Assert-DawnstrikeTaskXmlBackup `
                         -StateRoot $state `
                         -BackupName ([string]$receipt.scheduler_backup_name) `
                         -ExpectedManifestSha256 ([string]$receipt.scheduler_backup_manifest_sha256) `
                         -ExpectedTaskContractSha256 ([string]$backupManifest.task_contract_sha256) `
                         -ExpectedTaskDefinitionContractSha256 ([string]$backupManifest.task_definition_contract_sha256) `
                         -ExpectedTaskActionContractSha256 ([string]$backupManifest.task_action_contract_sha256)
                    $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
                        -Receipt $receipt `
                        -StateRoot $state `
                        -BackupRoot $backupRoot `
                        -ToolRoot $candidate `
                        -GitPath $gitPath `
                        -PythonPath $pythonPath `
                        -TimeoutSeconds $ProcessTimeoutSeconds `
                        -RequireRollbackCheckout
                    if ([string]$receipt.market_date -ne $MarketDate) {
                        throw "Existing activation receipt market date does not match the requested recovery date."
                    }
                    $earlyActivationId = [string]$receipt.activation_id
                    $earlyJournalPath = Join-Path $state "receipts\runtime-operation\runtime-activation-$earlyActivationId.json"
                    $earlyReceiptRelative = "receipts/runtime-activation/runtime-activation-$earlyActivationId.json"
                    $earlyInterpreter = Get-DawnstrikeApprovedLockInterpreter
                    if (-not (Test-Path -LiteralPath $earlyJournalPath -PathType Leaf)) {
                        throw "Existing activation receipt is missing its durable operation journal."
                    }
                    $earlyJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                        $earlyJournalPath $earlyInterpreter.path $earlyInterpreter.sha256
                    if (
                        [string]$earlyJournal.payload.operation -ne "runtime_activation" -or
                        [string]$earlyJournal.payload.phase -ne "COMPLETE" -or
                        [string]$earlyJournal.payload.candidate_sha -ne $ExpectedSha -or
                        [string]$earlyJournal.payload.candidate_tree -ne [string]$candidateContract.tree -or
                        [string]$earlyJournal.payload.complete_receipt_relative_path -ne $earlyReceiptRelative -or
                        [string]$earlyJournal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $item.FullName)
                    ) { throw "Existing activation receipt is not bound to the exact COMPLETE journal." }
                    $earlyLockRoot = Join-Path $state "locks"
                    $earlyRuntimeLockPath = Join-Path $earlyLockRoot "dawnstrike-runtime-activation.lock"
                    $earlyExpectedDailyPath = Join-Path $earlyLockRoot "dawnstrike-daily-$MarketDate.lock"
                    $earlyDailyPaths = @(
                        Get-ChildItem -LiteralPath $earlyLockRoot -Filter "dawnstrike-daily-*.lock" `
                            -File -Force -ErrorAction SilentlyContinue |
                            ForEach-Object { [System.IO.Path]::GetFullPath($_.FullName) }
                    )
                    $earlyForeignDaily = @($earlyDailyPaths | Where-Object {
                        $_ -ne [System.IO.Path]::GetFullPath($earlyExpectedDailyPath)
                    })
                    if ($earlyForeignDaily.Count -gt 0 -or $earlyDailyPaths.Count -gt 1) {
                        throw "Existing COMPLETE activation has a foreign or multiple daily lock set."
                    }
                    if (Test-Path -LiteralPath $earlyRuntimeLockPath -PathType Leaf) {
                        $earlyLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                            -StateRoot $state -JournalPath $earlyJournalPath -CandidateSha $ExpectedSha `
                            -CandidateTree ([string]$candidateContract.tree) `
                            -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) `
                            -PythonPath $earlyInterpreter.path -PythonSha256 $earlyInterpreter.sha256
                        if (Test-Path -LiteralPath $earlyExpectedDailyPath -PathType Leaf) {
                            $earlyDaily = Enter-DawnstrikeDailyRunLock `
                                -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
                            if (-not $earlyDaily.acquired) {
                                throw "Existing COMPLETE activation could not reacquire its exact daily lock."
                            }
                            Confirm-DawnstrikeActivationDailyLockHandshake `
                                -StateRoot $state -ActivationLock $earlyLock -DailyLock $earlyDaily | Out-Null
                            Exit-DawnstrikeDailyRunLock $earlyDaily
                            if (Test-Path -LiteralPath $earlyExpectedDailyPath) {
                                throw "Existing COMPLETE activation did not release its exact daily lock."
                            }
                        }
                        Exit-DawnstrikeGovernedRuntimeLock $earlyLock
                        if (Test-Path -LiteralPath $earlyRuntimeLockPath) {
                            throw "Existing COMPLETE activation did not release its exact runtime lock."
                        }
                    }
                    elseif ($earlyDailyPaths.Count -ne 0) {
                        throw "Existing COMPLETE activation has a daily lock without its exact runtime lock."
                    }
                    return $receipt
                }
            }
            catch {
                continue
            }
        }
        throw "Runtime already has the candidate SHA but no valid COMPLETE activation receipt exists."
    }

    $dbPath = Join-Path $state "shadow_real.sqlite"
    $stateInfo = Invoke-DawnstrikeContractCli $pythonPath $candidate @("inspect-state", "--db-path", $dbPath) "Durable state validation" $ProcessTimeoutSeconds
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $candidate `
        -CandidateSha $ExpectedSha `
        -CandidateTree $candidateContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $taskBefore = Get-DawnstrikeTaskContract $runtime $state
    $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state
    # Inventory the auxiliary independently of the candidate declaration.  A
    # present task without an explicit sidecar contract is an ungoverned task,
    # never an implicit legacy-compatible absence.
    $auxiliaryBefore = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
    if ($auxiliaryBefore.present -and -not $stateDeclaration.required) {
        throw "Auxiliary capture task is present but the candidate does not declare its governed sidecar contract."
    }
    $hardeningAttestation = $null
    if ($stateDeclaration.required -and $auxiliaryBefore.present) {
        $hardeningAttestation = Assert-DawnstrikeCaptureHardeningAttestation `
            -Auxiliary $auxiliaryBefore -CandidateRoot $candidate -StateRoot $state `
            -RuntimeRoot $runtime `
            -CandidateSha $ExpectedSha -CandidateTree $candidateContract.tree `
            -GitPath $gitPath `
            -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds
    }

    # Activation is only a pre-Morning boundary for the governed current or
    # next open session.  Existing transaction artifacts are handled by the
    # recovery branch below; recovery itself restores an already-started
    # transaction and must not be blocked by a newly frozen public artifact.
    $partialActivationPresent = (
        (Test-Path -LiteralPath (Join-Path $state "receipts\runtime-operation") -PathType Container) -and
        @(
            Get-ChildItem -LiteralPath (Join-Path $state "receipts\runtime-operation") `
                -Filter "runtime-activation-*.json" -File -ErrorAction SilentlyContinue
        ).Count -gt 0
    )
    if (-not $partialActivationPresent) {
        $null = Invoke-DawnstrikeActivationBoundary `
            -PythonPath $pythonPath `
            -CandidateRoot $candidate `
            -MarketDate $MarketDate `
            -RuntimeRoot $runtime `
            -StateRoot $state `
            -NowUtc $activationNowUtc `
            -TimeoutSeconds $ProcessTimeoutSeconds
    }

    if ($PreflightOnly) {
        Assert-DawnstrikeNoDailyLocks $state
        return [pscustomobject]@{
            schema_version = "dawnstrike.runtime_activation_preflight.v1"
            status = "PASS"
            candidate_sha = $ExpectedSha
            candidate_tree = $candidateContract.tree
            previous_sha = $runtimeContract.head
            previous_tree = $runtimeContract.tree
            state_schema_version = $stateInfo.schema_version
            state_quick_check = $stateInfo.quick_check
            task_count = $taskBefore.task_count
            task_contract_sha256 = $taskBefore.task_contract_sha256
            task_definition_contract_sha256 = $taskBefore.task_definition_contract_sha256
            ci_evidence_sha256 = $evidence.ci_evidence_sha256
            sol_evidence_sha256 = $evidence.sol_evidence_sha256
            auxiliary_capture_present = [bool]$auxiliaryBefore.present
            auxiliary_capture_state_before = if ($auxiliaryBefore.present) { [string]$auxiliaryBefore.state } else { "ABSENT" }
            auxiliary_capture_state_after = if ($auxiliaryBefore.present) { [string]$auxiliaryBefore.state } else { "ABSENT" }
            auxiliary_capture_action = if ($auxiliaryBefore.present) { "PREPARED_FOR_QUIESCE" } else { "ABSENT_ALLOWED" }
            hardening_receipt_path = if ($null -ne $hardeningAttestation) { $hardeningAttestation.path } else { "NONE" }
            hardening_receipt_sha256 = if ($null -ne $hardeningAttestation) { $hardeningAttestation.raw_sha256 } else { Get-DawnstrikeSha256Text "" }
            research_only = $true
            broker_execution_enabled = $false
        }
    }

    $activationSeed = "$ExpectedSha`:$($runtimeContract.head)`:$MarketDate`:$($evidence.ci_evidence_sha256)`:$($evidence.sol_evidence_sha256)"
    $activationId = (Get-DawnstrikeSha256Text $activationSeed).Substring(0, 24)
    $stage = "$runtime.stage-$activationId"
    $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
        -GitPath $gitPath `
        -CandidateRoot $candidate `
        -CandidateSha $ExpectedSha `
        -CandidateTree $candidateContract.tree `
        -Declaration $stateDeclaration `
        -TimeoutSeconds $ProcessTimeoutSeconds
    $rollbackRoot = Join-Path $state "runtime-rollbacks\$activationId"
    $rollbackCheckout = Join-Path $rollbackRoot "previous-runtime"
    $rollbackBundle = Join-Path $rollbackRoot "previous-runtime.bundle"
    $receiptRoot = Join-Path $state "receipts\runtime-activation"
    $schedulerBackupName = "runtime-activation-$activationId"
    $backupId = $schedulerBackupName
    $schedulerBackupPath = Join-Path $state "scheduler-backups\$schedulerBackupName"
    $preparedReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.prepared.json"
    $readyReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.ready.json"
    $completeReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.json"
    $operationJournal = Join-Path $state "receipts\runtime-operation\runtime-activation-$activationId.json"
    $preparedReceiptRelative = "receipts/runtime-activation/runtime-activation-$activationId.prepared.json"
    $readyReceiptRelative = "receipts/runtime-activation/runtime-activation-$activationId.ready.json"
    $completeReceiptRelative = "receipts/runtime-activation/runtime-activation-$activationId.json"
    Assert-DawnstrikeNoReparseComponents $receiptRoot "Activation receipt root"
    Assert-DawnstrikeNoReparseComponents $preparedReceipt "Prepared activation receipt"
    Assert-DawnstrikeNoReparseComponents $readyReceipt "Ready-to-enable activation receipt"
    Assert-DawnstrikeNoReparseComponents $completeReceipt "Complete activation receipt"
    Assert-DawnstrikeSameVolume @($runtime, $stage, $rollbackCheckout)
    $lockInterpreter = Get-DawnstrikeApprovedLockInterpreter

    # Recovery cleanup releases the governed locks before deleting its durable
    # journal.  A hard kill in that narrow interval therefore leaves a strict,
    # owner-bound PRE_QUIESCE tombstone rather than an unrecoverable bare lock.
    # Accept that tombstone only after proving the owner dead, every candidate
    # artifact absent, and the exact previous runtime/task contracts unchanged.
    $activationLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
    $pendingCompleteReceipt = $false
    if (
        (Test-Path -LiteralPath $completeReceipt -PathType Leaf) -and
        (Test-Path -LiteralPath $operationJournal -PathType Leaf)
    ) {
        try {
            $pendingJournalProbe = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
            $pendingCompleteReceipt = [string]$pendingJournalProbe.payload.phase -eq "POST_SWAP_READY"
        }
        catch { $pendingCompleteReceipt = $false }
    }
    if (
        (Test-Path -LiteralPath $operationJournal -PathType Leaf) -and
        -not (Test-Path -LiteralPath $preparedReceipt) -and
        -not (Test-Path -LiteralPath $readyReceipt) -and
        -not (Test-Path -LiteralPath $stage) -and
        -not (Test-Path -LiteralPath $rollbackRoot) -and
        -not (Test-Path -LiteralPath $schedulerBackupPath)
    ) {
        $restartJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
        if ([string]$restartJournal.payload.phase -eq "PRE_QUIESCE") {
            $restartLock = $null
            if (Test-Path -LiteralPath $activationLockPath -PathType Leaf) {
                $restartLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                    -StateRoot $state -JournalPath $operationJournal -CandidateSha $ExpectedSha `
                    -CandidateTree ([string]$candidateContract.tree) `
                    -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) `
                    -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
                $restartJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
            }
            else {
                $restartOwner = [pscustomobject]@{
                    process_id = [int]$restartJournal.payload.init_owner_process_id
                    process_started_at_utc = [string]$restartJournal.payload.init_owner_started_at_utc
                }
                if (-not (Test-DawnstrikeRuntimeLockOwnerDead $restartOwner)) {
                    throw "Recovery tombstone owner is still active."
                }
            }
            if (
                [string]$restartJournal.payload.operation -ne "runtime_activation" -or
                [string]$restartJournal.payload.candidate_sha -ne $ExpectedSha -or
                [string]$restartJournal.payload.candidate_tree -ne [string]$candidateContract.tree -or
                [string]$restartJournal.payload.current_sha -ne [string]$runtimeContract.head -or
                [string]$restartJournal.payload.current_tree -ne [string]$runtimeContract.tree -or
                [string]$restartJournal.payload.previous_sha -ne [string]$runtimeContract.head -or
                [string]$restartJournal.payload.previous_tree -ne [string]$runtimeContract.tree -or
                [string]$restartJournal.payload.task_contract_sha256 -ne [string]$taskBefore.task_contract_sha256
            ) { throw "Recovery tombstone identity is invalid." }
            $restartJournalHash = [string]$restartJournal.raw_file_sha256
            if ((Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256).raw_file_sha256 -ne $restartJournalHash) {
                throw "Recovery tombstone changed during validation."
            }
            if ($null -ne $restartLock) {
                Exit-DawnstrikeGovernedRuntimeLock $restartLock
                if ($TestStageCrashPoint -eq "after_pre_quiesce_recovery_lock_release") { Stop-Process -Id $PID -Force }
            }
            Remove-Item -LiteralPath $operationJournal -Force
            if (Test-Path -LiteralPath $operationJournal) { throw "Recovery tombstone cleanup failed." }
        }
    }

    # A failed mutation may have restored the exact previous runtime and Ready
    # task boundary.  Such a transaction is terminally compensated, not a
    # PRE_SWAP/POST_SWAP retry: accepting it as nonterminal would make the
    # journal demand Disabled tasks and candidate/rollback filesystem shape
    # that no longer exists.  Validate the immutable compensation receipt,
    # release only adopted locks, archive the prepared input, quarantine
    # candidate artifacts, then clear the tombstone before retrying.
    if (Test-Path -LiteralPath $operationJournal -PathType Leaf) {
        $compensatedJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
            $operationJournal $lockInterpreter.path $lockInterpreter.sha256
        if ([string]$compensatedJournal.payload.phase -eq "COMPENSATED") {
            $compensationAttemptKey = [string]$compensatedJournal.payload.prior_journal_file_sha256
            $compensatedRelative = [string]$compensatedJournal.payload.compensation_receipt_relative_path
            $compensatedPath = Join-Path $state ($compensatedRelative.Replace('/', '\'))
            $compensationCheck = & $lockInterpreter.path -I -B -S (Join-Path $PSScriptRoot "runtime_operation_journal.py") verify-compensation `
                --receipt $compensatedPath --state-root $state 2>$null
            if ($LASTEXITCODE -ne 0) { throw "Compensated activation receipt failed strict validation." }
            $compensationPayload = (($compensationCheck -join "") | ConvertFrom-Json).payload
            # Bind the restored P checkout to the raw origin sealed before the
            # swap.  HEAD/tree proof without this check accepts a tampered
            # .git/config and would make a COMPENSATED journal unsafe to clear.
            if (-not (Test-Path -LiteralPath $preparedReceipt -PathType Leaf)) {
                throw "Compensated activation is missing its PREPARED origin evidence."
            }
            try {
                $preparedOriginEvidence = Get-Content -LiteralPath $preparedReceipt -Raw -Encoding UTF8 | ConvertFrom-Json
            }
            catch { throw "Compensated activation PREPARED origin evidence is invalid JSON." }
            if (
                [string]$preparedOriginEvidence.runtime_origin_sha256 -notmatch '^[0-9a-f]{64}$' -or
                [string]$compensatedJournal.payload.prepared_receipt_sha256 -ne (Get-DawnstrikeSha256File $preparedReceipt)
            ) { throw "Compensated activation PREPARED origin evidence is not journal-bound." }
            $restoredOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Compensated previous runtime origin verification" $ProcessTimeoutSeconds
            Assert-DawnstrikeSafeOrigin $restoredOrigin
            if (
                (Get-DawnstrikeSha256Text $restoredOrigin) -ne [string]$preparedOriginEvidence.runtime_origin_sha256 -or
                (Convert-DawnstrikeCanonicalOriginIdentity $restoredOrigin) -ne [string]$compensatedJournal.payload.origin_identity
            ) { throw "Compensated activation restored runtime origin is not the exact pre-swap binding." }
            $compensatedRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $runtimeContract.head
            $compensatedTasks = Get-DawnstrikeTaskContract $runtime $state
            if (
                $compensationPayload.operation -ne "runtime_activation" -or
                $compensationPayload.candidate_sha -ne $ExpectedSha -or
                $compensationPayload.candidate_tree -ne [string]$candidateContract.tree -or
                $compensationPayload.prior_journal_file_sha256 -ne [string]$compensatedJournal.payload.prior_journal_file_sha256 -or
                $compensationPayload.task_state -ne "Ready" -or
                $compensatedRuntime.tree -ne [string]$runtimeContract.tree -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensatedJournal.payload.task_contract_sha256 -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensationPayload.task_contract_sha256 -or
                $compensatedTasks.task_contract_sha256 -ne [string]$compensationPayload.task_xml_sha256 -or
                $compensatedTasks.task_action_contract_sha256 -ne [string]$compensationPayload.task_action_contract_sha256 -or
                $compensatedTasks.task_definition_contract_sha256 -ne [string]$compensationPayload.task_definition_contract_sha256 -or
                $compensatedJournal.payload.compensation_receipt_sha256 -ne (Get-DawnstrikeSha256File $compensatedPath)
            ) { throw "Compensated activation tombstone does not attest the exact restored boundary." }
            $compensationLock = $null
            if (Test-Path -LiteralPath $activationLockPath -PathType Leaf) {
                $compensationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                    -StateRoot $state -JournalPath $operationJournal -CandidateSha $ExpectedSha `
                    -CandidateTree ([string]$candidateContract.tree) -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) `
                    -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            }
            $compensationDaily = $null
            if (@(Get-ChildItem -LiteralPath (Join-Path $state "locks") -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue).Count -gt 0) {
                if ($null -eq $compensationLock) { throw "Compensated activation has a daily lock without its runtime lock." }
                $compensationDaily = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
                if (-not $compensationDaily.acquired) { throw "Compensated activation could not recover its daily lock." }
                Exit-DawnstrikeDailyRunLock $compensationDaily
            }
            if ($null -ne $compensationLock) { Exit-DawnstrikeGovernedRuntimeLock $compensationLock }
            if (Test-Path -LiteralPath $preparedReceipt -PathType Leaf) {
                $archiveRoot = Join-Path $state "receipts\runtime-activation\archive"
                New-Item -ItemType Directory -Path $archiveRoot -Force | Out-Null
                $preparedHash = Get-DawnstrikeSha256File $preparedReceipt
                $preparedArchive = Join-Path $archiveRoot "compensated-$activationId-$compensationAttemptKey-$preparedHash.prepared.json"
                if (Test-Path -LiteralPath $preparedArchive) { throw "Compensated activation prepared receipt archive already exists." }
                [IO.File]::Move($preparedReceipt, $preparedArchive)
                if ((Test-Path -LiteralPath $preparedReceipt) -or (Get-DawnstrikeSha256File $preparedArchive) -ne $preparedHash) { throw "Compensated activation prepared receipt archive was not proven." }
            }
            if (Test-Path -LiteralPath $readyReceipt -PathType Leaf) {
                $readyHash = Get-DawnstrikeSha256File $readyReceipt
                $readyArchive = Join-Path $archiveRoot "compensated-$activationId-$compensationAttemptKey-$readyHash.ready.json"
                if (Test-Path -LiteralPath $readyArchive) { throw "Compensated activation ready receipt archive already exists." }
                [IO.File]::Move($readyReceipt, $readyArchive)
                if ((Test-Path -LiteralPath $readyReceipt) -or (Get-DawnstrikeSha256File $readyArchive) -ne $readyHash) { throw "Compensated activation ready receipt archive was not proven." }
            }
            if (Test-Path -LiteralPath $completeReceipt -PathType Leaf) {
                $completeHash = Get-DawnstrikeSha256File $completeReceipt
                $completeArchive = Join-Path $archiveRoot "compensated-$activationId-$compensationAttemptKey-$completeHash.complete.json"
                if (Test-Path -LiteralPath $completeArchive) { throw "Compensated activation complete receipt archive already exists." }
                [IO.File]::Move($completeReceipt, $completeArchive)
                if ((Test-Path -LiteralPath $completeReceipt) -or (Get-DawnstrikeSha256File $completeArchive) -ne $completeHash) { throw "Compensated activation complete receipt archive was not proven." }
            }
            foreach ($candidateArtifact in @(
                [pscustomobject]@{ path = $rollbackRoot; name = "rollback" },
                [pscustomobject]@{ path = $schedulerBackupPath; name = "scheduler-backup" },
                [pscustomobject]@{ path = $stage; name = "stage" }
            )) {
                if (Test-Path -LiteralPath $candidateArtifact.path) {
                    $quarantineRoot = Join-Path $state "recovery-quarantine\compensated-$activationId-$compensationAttemptKey"
                    New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
                    $destination = Join-Path $quarantineRoot ([string]$candidateArtifact.name)
                    if (Test-Path -LiteralPath $destination) { throw "Compensated activation quarantine destination already exists." }
                    Move-Item -LiteralPath $candidateArtifact.path -Destination $destination -Force
                    if (Test-Path -LiteralPath $candidateArtifact.path) { throw "Compensated activation artifact quarantine failed." }
                }
            }
            Clear-DawnstrikeCompensatedJournalTombstone -StateRoot $state -JournalPath $operationJournal `
                -Operation runtime_activation -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) `
                -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            if ($null -ne $advancedOriginRecovery) {
                return [pscustomobject]@{
                    schema_version = "dawnstrike.runtime_activation_recovery.v1"
                    status = "RECOVERED_SUPERSEDED_TRANSACTION"
                    recovered_phase = "COMPENSATED"
                    candidate_sha = $ExpectedSha
                    candidate_tree = [string]$candidateContract.tree
                    current_origin_main_sha = $remoteMain
                    research_only = $true
                    broker_execution_enabled = $false
                }
            }
            return Invoke-DawnstrikeRuntimeActivation @PSBoundParameters
        }
    }

    if ((Test-Path -LiteralPath $completeReceipt -PathType Leaf) -and -not $pendingCompleteReceipt) {
        $existing = Invoke-DawnstrikeContractCli $pythonPath $candidate @("verify-receipt", "--receipt", $completeReceipt, "--expected-status", "COMPLETE") "Existing activation receipt verification" $ProcessTimeoutSeconds
        if (-not (Test-Path -LiteralPath $operationJournal -PathType Leaf)) {
            throw "Complete activation receipt is missing its durable operation journal."
        }
        $completeJournal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
        if (
            [string]$completeJournal.payload.operation -ne "runtime_activation" -or
            [string]$completeJournal.payload.phase -ne "COMPLETE" -or
            [string]$completeJournal.payload.candidate_sha -ne $ExpectedSha -or
            [string]$completeJournal.payload.candidate_tree -ne [string]$candidateContract.tree -or
            [string]$completeJournal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $completeReceipt) -or
            [string]$completeJournal.payload.complete_receipt_relative_path -ne $completeReceiptRelative
        ) { throw "Complete activation journal does not bind the exact receipt." }
        $current = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
        if (
            $existing.candidate_sha -ne $current.head -or
            [string]$existing.candidate_tree -ne $current.tree
        ) {
            throw "Existing activation receipt does not match the runtime."
        }
        $currentTasks = Get-DawnstrikeTaskContract $runtime $state
        if (
            $currentTasks.task_contract_sha256 -ne [string]$existing.task_contract_sha256 -or
            $currentTasks.task_definition_contract_sha256 -ne
                [string]$existing.task_definition_contract_sha256 -or
            $currentTasks.task_action_contract_sha256 -ne
                [string]$existing.task_action_contract_sha256
        ) {
            throw "Existing activation receipt does not match exact Ready task XML."
        }
        if ($stateDeclaration.required) {
            $currentAuxiliary = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            if ([bool]$existing.auxiliary_capture_present -ne [bool]$currentAuxiliary.present) {
                throw "Existing activation receipt does not match auxiliary capture presence."
            }
            if ($currentAuxiliary.present) {
                if ($currentAuxiliary.state -eq "Disabled") {
                    if (
                        $currentAuxiliary.definition_contract_sha256 -ne [string]$existing.auxiliary_capture_definition_contract_sha256 -or
                        $currentAuxiliary.action_contract_sha256 -ne [string]$existing.auxiliary_capture_action_contract_sha256
                    ) { throw "Existing activation receipt does not match the disabled auxiliary task." }
                    $existingHardening = Assert-DawnstrikeCaptureHardeningAttestation `
                        -Auxiliary $currentAuxiliary -CandidateRoot $candidate -StateRoot $state `
                        -RuntimeRoot $runtime -CandidateSha $ExpectedSha -CandidateTree $candidateContract.tree `
                        -GitPath $gitPath `
                        -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds -Stage POST_SWAP
                    if ([string]$existing.capture_hardening_receipt_raw_sha256 -ne [string]$existingHardening.raw_sha256 -or [string]$existing.capture_hardening_receipt_sha256 -ne [string]$existingHardening.payload.receipt_sha256) {
                        throw "Existing activation receipt does not match the hardening attestation chain."
                    }
                }
                elseif ($currentAuxiliary.state -eq "Ready") {
                    $null = Assert-DawnstrikeCaptureRebindChain `
                        -ActivationReceipt $existing -Auxiliary $currentAuxiliary `
                        -CandidateRoot $candidate -StateRoot $state -CandidateSha $ExpectedSha `
                        -CandidateTree $current.tree -PythonPath $pythonPath `
                        -TimeoutSeconds $ProcessTimeoutSeconds
                }
                else { throw "Existing auxiliary capture task is in an ambiguous state." }
            }
        }
        $existingBackupManifest = Get-DawnstrikeTaskXmlBackupManifest `
            -StateRoot $state -BackupName ([string]$existing.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$existing.scheduler_backup_manifest_sha256)
        $null = Assert-DawnstrikeTaskXmlBackup `
            -StateRoot $state `
            -BackupName ([string]$existing.scheduler_backup_name) `
            -ExpectedManifestSha256 ([string]$existing.scheduler_backup_manifest_sha256) `
            -ExpectedTaskContractSha256 ([string]$existingBackupManifest.task_contract_sha256) `
            -ExpectedTaskDefinitionContractSha256 ([string]$existingBackupManifest.task_definition_contract_sha256) `
            -ExpectedTaskActionContractSha256 ([string]$existingBackupManifest.task_action_contract_sha256)
        $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
            -Receipt $existing `
            -StateRoot $state `
            -BackupRoot $backupRoot `
            -ToolRoot $candidate `
            -GitPath $gitPath `
            -PythonPath $pythonPath `
            -TimeoutSeconds $ProcessTimeoutSeconds `
            -RequireRollbackCheckout
        $completeRuntimeLockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
        $completeDailyLocks = @(
            Get-ChildItem -LiteralPath (Join-Path $state "locks") `
                -Filter "dawnstrike-daily-*.lock" -File -Force -ErrorAction SilentlyContinue
        )
        if (Test-Path -LiteralPath $completeRuntimeLockPath -PathType Leaf) {
            $completeLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
                -StateRoot $state -JournalPath $operationJournal -CandidateSha $ExpectedSha `
                -CandidateTree ([string]$candidateContract.tree) `
                -OriginIdentity (Convert-DawnstrikeCanonicalOriginIdentity $origin) `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $completeDailyLock = Enter-DawnstrikeDailyRunLock `
                -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
            if (-not $completeDailyLock.acquired) {
                throw "Complete activation retry could not acquire its exact daily lock."
            }
            Confirm-DawnstrikeActivationDailyLockHandshake `
                -StateRoot $state -ActivationLock $completeLock -DailyLock $completeDailyLock | Out-Null
            Exit-DawnstrikeDailyRunLock $completeDailyLock
            if (Test-Path -LiteralPath $completeDailyLock.lock_path) {
                throw "Complete activation retry did not release its exact daily lock."
            }
            Exit-DawnstrikeGovernedRuntimeLock $completeLock
            if (Test-Path -LiteralPath $completeRuntimeLockPath) {
                throw "Complete activation retry did not release its exact runtime lock."
            }
        }
        elseif ($completeDailyLocks.Count -ne 0) {
            throw "Complete activation retry found a daily lock without its exact runtime lock."
        }
        return $existing
    }
    if (
        (Test-Path -LiteralPath $preparedReceipt) -or
        (Test-Path -LiteralPath $readyReceipt) -or
        (Test-Path -LiteralPath $stage) -or
        (Test-Path -LiteralPath $rollbackRoot) -or
        (Test-Path -LiteralPath $schedulerBackupPath)
    ) {
        $lockPath = Join-Path $state "locks\dawnstrike-runtime-activation.lock"
        if (
            -not (Test-Path -LiteralPath $operationJournal -PathType Leaf) -or
            -not (Test-Path -LiteralPath $lockPath -PathType Leaf)
        ) { throw "A partial activation exists without an exact operation journal recovery chain." }
        $lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
        $lockInterpreter = Get-DawnstrikeApprovedLockInterpreter
        $journal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
        if (
            [string]$journal.payload.operation -ne "runtime_activation" -or
            [string]$journal.payload.candidate_sha -ne $ExpectedSha -or
            [string]$journal.payload.candidate_tree -ne [string]$candidateContract.tree -or
            [string]$journal.payload.origin_identity -ne $lockOrigin
        ) { throw "Partial activation journal identity is invalid." }
        $activationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
            -StateRoot $state -JournalPath $operationJournal -CandidateSha $ExpectedSha `
            -CandidateTree ([string]$candidateContract.tree) -OriginIdentity $lockOrigin `
            -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
        try {
            $journal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
            if ([string]$journal.payload.phase -eq "INIT") {
                # INIT is sealed before scheduler mutation.  A crash here may
                # leave only the staged checkout and/or a stale daily lock;
                # prove the old runtime and exact Ready tasks, recover the
                # governed daily lock, and restart from a clean operation.
                if (
                    (Test-Path -LiteralPath $preparedReceipt) -or
                    (Test-Path -LiteralPath $readyReceipt) -or
                    (Test-Path -LiteralPath $schedulerBackupPath)
                ) { throw "INIT journal has unexpected prepared artifacts." }
                $initTasks = Get-DawnstrikeTaskContract $runtime $state
                if ($initTasks.task_contract_sha256 -ne [string]$journal.payload.task_contract_sha256) {
                    throw "INIT recovery found task drift before activation mutation."
                }
                $initRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds ([string]$journal.payload.previous_sha)
                if ($initRuntime.tree -ne [string]$journal.payload.previous_tree) {
                    throw "INIT recovery found runtime drift before activation mutation."
                }
                if (Test-Path -LiteralPath $stage) {
                    Assert-DawnstrikeNoReparseComponents $stage "INIT recovery stage"
                    $initStageItem = Get-Item -LiteralPath $stage -Force -ErrorAction Stop
                    if (-not $initStageItem.PSIsContainer -or ($initStageItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                        throw "INIT recovery stage is not a safe directory."
                    }
                    # Clone may have died before a readable Git contract
                    # existed.  The deterministic stage path is nevertheless
                    # bound by the exact dead-owner INIT journal and unchanged
                    # runtime/task contracts.  Preserve its bytes in quarantine
                    # rather than accepting or deleting an incomplete checkout.
                    $initQuarantineRoot = Join-Path $state "recovery-quarantine"
                    Assert-DawnstrikeNoReparseComponents $initQuarantineRoot "INIT recovery quarantine"
                    New-Item -ItemType Directory -Path $initQuarantineRoot -Force | Out-Null
                    $initQuarantine = Join-Path $initQuarantineRoot ("runtime-activation-$activationId-init-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))")
                    Assert-DawnstrikeNoReparseComponents $initQuarantine "INIT recovery quarantine destination"
                    Move-Item -LiteralPath $stage -Destination $initQuarantine -ErrorAction Stop
                    if (Test-Path -LiteralPath $stage) { throw "INIT recovery could not quarantine the exact staged path." }
                }
                $recoveredDaily = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
                if ($recoveredDaily.acquired) { Exit-DawnstrikeDailyRunLock $recoveredDaily }
                Exit-DawnstrikeGovernedRuntimeLock $activationLock
                $activationLock = $null
                if ($TestStageCrashPoint -eq "after_init_recovery_lock_release") { Stop-Process -Id $PID -Force }
                Remove-Item -LiteralPath $operationJournal -Force
                if (Test-Path -LiteralPath $operationJournal) { throw "INIT recovery journal cleanup failed." }
                if ($null -ne $advancedOriginRecovery) {
                    return [pscustomobject]@{
                        schema_version = "dawnstrike.runtime_activation_recovery.v1"
                        status = "RECOVERED_SUPERSEDED_TRANSACTION"
                        recovered_phase = "INIT"
                        candidate_sha = $ExpectedSha
                        candidate_tree = [string]$candidateContract.tree
                        current_origin_main_sha = $remoteMain
                        research_only = $true
                        broker_execution_enabled = $false
                    }
                }
                return Invoke-DawnstrikeRuntimeActivation @PSBoundParameters
            }
            if ([string]$journal.payload.phase -eq "PRE_QUIESCE") {
                # The durable quiesce intent is the recovery boundary for a
                # crash during task disablement.  Validate the exact backup
                # and stage, force all canonical tasks Disabled, restore their
                # exact Ready state, then discard only these proven ephemeral
                # artifacts and restart the transaction.
                if (-not (Test-Path -LiteralPath $schedulerBackupPath -PathType Container)) {
                    throw "PRE_QUIESCE recovery is missing its exact scheduler backup."
                }
                $recoveryDaily = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
                if (-not $recoveryDaily.acquired) { throw "PRE_QUIESCE recovery could not acquire the exact daily lock." }
                Confirm-DawnstrikeActivationDailyLockHandshake `
                    -StateRoot $state -ActivationLock $activationLock -DailyLock $recoveryDaily | Out-Null
                $quiesceManifestPath = Join-Path $schedulerBackupPath "manifest.json"
                Assert-DawnstrikeNoReparseComponents $quiesceManifestPath "PRE_QUIESCE scheduler backup manifest"
                try { $quiesceManifest = Get-Content -LiteralPath $quiesceManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json }
                catch { throw "PRE_QUIESCE scheduler backup manifest is invalid." }
                if ([string]$quiesceManifest.task_contract_sha256 -ne [string]$journal.payload.task_contract_sha256) {
                    throw "PRE_QUIESCE scheduler backup task identity is invalid."
                }
                $null = Assert-DawnstrikeTaskXmlBackup -StateRoot $state -BackupName $schedulerBackupName `
                    -ExpectedManifestSha256 ([string]$journal.payload.backup_contract_sha256) `
                    -ExpectedTaskContractSha256 ([string]$quiesceManifest.task_contract_sha256) `
                    -ExpectedTaskDefinitionContractSha256 ([string]$quiesceManifest.task_definition_contract_sha256) `
                    -ExpectedTaskActionContractSha256 ([string]$quiesceManifest.task_action_contract_sha256)
                $quiesceStage = Get-DawnstrikeGitContract $gitPath $stage $ProcessTimeoutSeconds $ExpectedSha
                $expectedStageHash = Get-DawnstrikeSha256Text ("$ExpectedSha`:$($candidateContract.tree)`:$stage")
                if ($quiesceStage.tree -ne [string]$candidateContract.tree -or $expectedStageHash -ne [string]$journal.payload.runtime_stage_contract_sha256) {
                    throw "PRE_QUIESCE recovery stage identity is invalid."
                }
                $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                $quiesced = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                if (
                    $quiesced.disabled_count -ne 5 -or
                    $quiesced.enabled_count -ne 0 -or
                    $quiesced.task_definition_contract_sha256 -ne [string]$quiesceManifest.task_definition_contract_sha256 -or
                    $quiesced.task_action_contract_sha256 -ne [string]$quiesceManifest.task_action_contract_sha256
                ) {
                    throw "PRE_QUIESCE recovery could not prove exact canonical disablement."
                }
                $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -AllowDisabled
                Set-DawnstrikeCanonicalTaskExpectedSha -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
                Enable-DawnstrikeCanonicalTasks
                $restored = Get-DawnstrikeTaskContract $runtime $state
                $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
                Remove-Item -LiteralPath $stage -Recurse -Force
                if (Test-Path -LiteralPath $stage) { throw "PRE_QUIESCE recovery could not remove the exact staged checkout." }
                # A crash after quiescence can leave a partially written
                # durable-state backup or rollback bundle even though PRE_SWAP
                # was never sealed.  Do not silently reuse or delete those
                # candidate-bound artifacts: move each exact path to a guarded
                # quarantine, preserving forensic bytes while making retry
                # unambiguous.
                $quarantineRoot = Join-Path $state "recovery-quarantine"
                Assert-DawnstrikeNoReparseComponents $quarantineRoot "Activation recovery quarantine"
                New-Item -ItemType Directory -Path $quarantineRoot -Force | Out-Null
                $quarantineName = "runtime-activation-$activationId-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))"
                $quarantinePath = Join-Path $quarantineRoot $quarantineName
                Assert-DawnstrikeNoReparseComponents $quarantinePath "Activation recovery quarantine bundle"
                New-Item -ItemType Directory -Path $quarantinePath -Force | Out-Null
                foreach ($candidateArtifact in @(
                    [pscustomobject]@{ path = $schedulerBackupPath; name = "scheduler-backup" },
                    [pscustomobject]@{ path = (Join-Path $backupRoot $backupId); name = "state-backup" },
                    [pscustomobject]@{ path = $rollbackRoot; name = "rollback" }
                )) {
                    if (Test-Path -LiteralPath $candidateArtifact.path) {
                        Assert-DawnstrikeNoReparseComponents $candidateArtifact.path "PRE_QUIESCE candidate artifact"
                        $destination = Join-Path $quarantinePath ([string]$candidateArtifact.name)
                        Assert-DawnstrikeNoReparseComponents $destination "PRE_QUIESCE quarantine destination"
                        Move-Item -LiteralPath $candidateArtifact.path -Destination $destination -Force
                        if (Test-Path -LiteralPath $candidateArtifact.path) { throw "PRE_QUIESCE candidate artifact quarantine failed." }
                    }
                }
                Exit-DawnstrikeDailyRunLock $recoveryDaily
                Exit-DawnstrikeGovernedRuntimeLock $activationLock
                $activationLock = $null
                if ($TestStageCrashPoint -eq "after_pre_quiesce_recovery_lock_release") { Stop-Process -Id $PID -Force }
                Remove-Item -LiteralPath $operationJournal -Force
                if (Test-Path -LiteralPath $operationJournal) { throw "PRE_QUIESCE recovery journal cleanup failed." }
                if ($null -ne $advancedOriginRecovery) {
                    return [pscustomobject]@{
                        schema_version = "dawnstrike.runtime_activation_recovery.v1"
                        status = "RECOVERED_SUPERSEDED_TRANSACTION"
                        recovered_phase = "PRE_QUIESCE"
                        candidate_sha = $ExpectedSha
                        candidate_tree = [string]$candidateContract.tree
                        current_origin_main_sha = $remoteMain
                        research_only = $true
                        broker_execution_enabled = $false
                    }
                }
                return Invoke-DawnstrikeRuntimeActivation @PSBoundParameters
            }
            $prepared = Invoke-DawnstrikeContractCli $pythonPath $candidate @("verify-receipt", "--receipt", $preparedReceipt, "--expected-status", "PREPARED") "Prepared activation recovery receipt" $ProcessTimeoutSeconds
            $taskRecovery = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if ([string]$journal.payload.phase -in @("PRE_SWAP", "POST_SWAP") -and ($taskRecovery.disabled_count -ne 5 -or $taskRecovery.enabled_count -ne 0 -or $taskRecovery.task_contract_sha256 -ne [string]$journal.payload.task_contract_sha256)) {
                throw "Activation recovery tasks are not the exact journaled Disabled contract."
            }
            if ([string]$journal.payload.phase -eq "PRE_SWAP") {
                $runtimePresent = Test-Path -LiteralPath $runtime -PathType Container
                $rollbackPresent = Test-Path -LiteralPath $rollbackCheckout -PathType Container
                $stagePresent = Test-Path -LiteralPath $stage -PathType Container
                if ($runtimePresent -and -not $rollbackPresent -and $stagePresent) {
                    $old = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds ([string]$journal.payload.previous_sha)
                    if ($old.tree -ne [string]$journal.payload.previous_tree) { throw "Recovery old runtime tree is invalid." }
                    [IO.Directory]::Move($runtime,$rollbackCheckout)
                    $runtimePresent=$false;$rollbackPresent=$true
                }
                if (-not $runtimePresent -and $rollbackPresent -and $stagePresent) {
                    $staged = Get-DawnstrikeGitContract $gitPath $stage $ProcessTimeoutSeconds $ExpectedSha
                    if ($staged.tree -ne [string]$candidateContract.tree) { throw "Recovery stage tree is invalid." }
                    [IO.Directory]::Move($stage,$runtime)
                    if ($TestStageCrashPoint -eq "after_candidate_runtime_rename") { Stop-Process -Id $PID -Force }
                    $runtimePresent=$true;$stagePresent=$false
                } elseif ($runtimePresent -and $rollbackPresent -and -not $stagePresent) {
                    # Deterministic hard-crash state after the second atomic
                    # rename but before PRE_SWAP was advanced to POST_SWAP.
                    $candidateRecovery = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
                    $previousRecovery = Get-DawnstrikeGitContract $gitPath $rollbackCheckout $ProcessTimeoutSeconds ([string]$journal.payload.previous_sha)
                    $candidateRecoveryOrigin = Convert-DawnstrikeCanonicalOriginIdentity (Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "PRE_SWAP installed origin verification" $ProcessTimeoutSeconds)
                    $previousRecoveryOrigin = Convert-DawnstrikeCanonicalOriginIdentity (Get-DawnstrikeGitValue $gitPath $rollbackCheckout @("remote", "get-url", "origin") "PRE_SWAP previous origin verification" $ProcessTimeoutSeconds)
                    if (
                        $candidateRecovery.tree -ne [string]$candidateContract.tree -or
                        $previousRecovery.tree -ne [string]$journal.payload.previous_tree -or
                        $candidateRecoveryOrigin -ne [string]$journal.payload.origin_identity -or
                        $previousRecoveryOrigin -ne [string]$journal.payload.origin_identity
                    ) { throw "PRE_SWAP installed/previous runtime identity is invalid." }
                } else { throw "PRE_SWAP recovery filesystem state is ambiguous." }
                $installed = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
                $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                    -StateRoot $state -JournalPath $operationJournal -Lock $activationLock -Operation runtime_activation -Phase POST_SWAP `
                    -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) -CurrentSha $ExpectedSha -CurrentTree ([string]$installed.tree) `
                    -PreviousSha ([string]$journal.payload.previous_sha) -PreviousTree ([string]$journal.payload.previous_tree) -OriginIdentity $lockOrigin `
                    -PreparedReceiptRelativePath $preparedReceiptRelative -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                    -CompleteReceiptRelativePath $readyReceiptRelative -CompleteReceiptSha256 (Get-DawnstrikeSha256Text "") `
                    -BackupContractSha256 ([string]$journal.payload.backup_contract_sha256) -TaskContractSha256 ([string]$taskRecovery.task_contract_sha256) `
                    -RuntimeStageContractSha256 ([string]$journal.payload.runtime_stage_contract_sha256) `
                    -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            } elseif ([string]$journal.payload.phase -eq "POST_SWAP_READY") {
                # This phase binds a PREPARED ready-to-enable receipt. It never
                # claims terminal enablement. Recovery either finishes a
                # partially enabled set or validates an already Ready set,
                # then seals the first and only COMPLETE receipt.
                if (-not (Test-Path -LiteralPath $readyReceipt -PathType Leaf)) { throw "POST_SWAP_READY recovery has no exact ready-to-enable receipt." }
                if ([string]$journal.payload.complete_receipt_relative_path -ne $readyReceiptRelative -or
                    [string]$journal.payload.complete_receipt_sha256 -ne (Get-DawnstrikeSha256File $readyReceipt)) {
                    throw "POST_SWAP_READY journal does not bind the exact ready-to-enable receipt."
                }
                $ready = Invoke-DawnstrikeContractCli $pythonPath $runtime @("verify-receipt", "--receipt", $readyReceipt, "--expected-status", "PREPARED") "POST_SWAP_READY receipt verification" $ProcessTimeoutSeconds
                $readyRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
                if ([string]$ready.candidate_sha -ne $ExpectedSha -or [string]$ready.candidate_tree -ne [string]$candidateContract.tree -or $readyRuntime.tree -ne [string]$candidateContract.tree -or $ready.task_enablement_restored -ne $false -or -not (Test-Path -LiteralPath $rollbackCheckout -PathType Container) -or (Test-Path -LiteralPath $stage)) { throw "POST_SWAP_READY receipt or runtime identity is invalid." }
                $null = Assert-DawnstrikeReceiptRecoveryArtifacts -Receipt $ready -StateRoot $state -BackupRoot $backupRoot -ToolRoot $candidate -GitPath $gitPath -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds -RequireRollbackCheckout
                $readyTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                if ($readyTasks.enabled_count -ne 5) {
                    $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                    $readyTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                    if ($readyTasks.disabled_count -ne 5 -or
                        [string]$readyTasks.task_contract_sha256 -ne [string]$ready.task_contract_sha256 -or
                        [string]$readyTasks.task_definition_contract_sha256 -ne [string]$ready.task_definition_contract_sha256 -or
                        [string]$readyTasks.task_action_contract_sha256 -ne [string]$ready.task_action_contract_sha256) {
                        throw "POST_SWAP_READY recovery could not prove the exact disabled SHA-bound task contract."
                    }
                    $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha -AllowDisabled
                    Enable-DawnstrikeCanonicalTasks
                }
                $readyTasks = Get-DawnstrikeTaskContract $runtime $state
                $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
                if ($readyTasks.enabled_count -ne 5 -or
                    [string]$readyTasks.task_definition_contract_sha256 -ne [string]$ready.task_definition_contract_sha256 -or
                    [string]$readyTasks.task_action_contract_sha256 -ne [string]$ready.task_action_contract_sha256) { throw "POST_SWAP_READY recovery could not prove exact Ready tasks." }
                if (Test-Path -LiteralPath $completeReceipt -PathType Leaf) {
                    $complete = Invoke-DawnstrikeContractCli $pythonPath $runtime @("verify-receipt", "--receipt", $completeReceipt, "--expected-status", "COMPLETE") "Recovered activation complete receipt verification" $ProcessTimeoutSeconds
                    if ([string]$complete.task_contract_sha256 -ne [string]$readyTasks.task_contract_sha256 -or
                        [string]$complete.task_definition_contract_sha256 -ne [string]$readyTasks.task_definition_contract_sha256 -or
                        [string]$complete.task_action_contract_sha256 -ne [string]$readyTasks.task_action_contract_sha256) {
                        throw "Recovered activation COMPLETE receipt does not match the exact Ready task contract."
                    }
                }
                else {
                    $completePayload = [ordered]@{}
                    foreach ($property in $ready.PSObject.Properties) {
                        if ($property.Name -ne "receipt_sha256") { $completePayload[$property.Name] = $property.Value }
                    }
                    $completePayload.status = "COMPLETE"
                    $completePayload.task_count = [int]$readyTasks.task_count
                    $completePayload.task_contract_sha256 = [string]$readyTasks.task_contract_sha256
                    $completePayload.task_definition_contract_sha256 = [string]$readyTasks.task_definition_contract_sha256
                    $completePayload.task_action_contract_sha256 = [string]$readyTasks.task_action_contract_sha256
                    $completePayload.task_enablement_restored = $true
                    $completePayload.completed_at_utc = [DateTime]::UtcNow.ToString("o")
                    $recoveryInput = Join-Path $receiptRoot ".$activationId.ready-recovery.input.json"
                    Write-DawnstrikeActivationJson $completePayload $recoveryInput
                    try {
                        $complete = Invoke-DawnstrikeContractCli $pythonPath $runtime @("seal-receipt", "--input", $recoveryInput, "--output", $completeReceipt) "Recovered activation complete receipt sealing" $ProcessTimeoutSeconds
                    }
                    finally { if (Test-Path -LiteralPath $recoveryInput -PathType Leaf) { Remove-Item -LiteralPath $recoveryInput -Force } }
                }
                $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                    -StateRoot $state -JournalPath $operationJournal -Lock $activationLock -Operation runtime_activation -Phase COMPLETE `
                    -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) -CurrentSha $ExpectedSha -CurrentTree ([string]$candidateContract.tree) `
                    -PreviousSha ([string]$journal.payload.previous_sha) -PreviousTree ([string]$journal.payload.previous_tree) -OriginIdentity $lockOrigin `
                    -PreparedReceiptRelativePath $preparedReceiptRelative -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                    -CompleteReceiptRelativePath $completeReceiptRelative -CompleteReceiptSha256 (Get-DawnstrikeSha256File $completeReceipt) `
                    -BackupContractSha256 ([string]$journal.payload.backup_contract_sha256) -TaskContractSha256 ([string]$readyTasks.task_contract_sha256) `
                    -RuntimeStageContractSha256 ([string]$journal.payload.runtime_stage_contract_sha256) -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
                $journal = Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
                $null = Assert-DawnstrikeActivationCompleteTerminal `
                    -Journal $journal -Receipt $complete -ReceiptPath $completeReceipt -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state -BackupRoot $backupRoot `
                    -GitPath $gitPath -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds -ExpectedSha $ExpectedSha -ExpectedTree ([string]$candidateContract.tree) `
                    -OriginIdentity $lockOrigin -MarketDate $MarketDate -StateDeclaration $stateDeclaration -ExpectedTask $readyTasks
                return $complete
            } elseif ([string]$journal.payload.phase -ne "POST_SWAP") { throw "Activation journal phase is not recoverable." }
            $installed = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
            if ($installed.tree -ne [string]$candidateContract.tree -or -not (Test-Path -LiteralPath $rollbackCheckout -PathType Container) -or (Test-Path -LiteralPath $stage)) { throw "POST_SWAP recovery filesystem state is invalid." }
            $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -AllowDisabled
            Set-DawnstrikeCanonicalTaskExpectedSha -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
            $taskAfterDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha -AllowDisabled
            $readyPayload = [ordered]@{}
            foreach ($property in $prepared.PSObject.Properties) {
                if ($property.Name -ne "receipt_sha256") { $readyPayload[$property.Name] = $property.Value }
            }
            $readyPayload.status = "PREPARED"
            $readyPayload.task_count = [int]$taskAfterDisabled.task_count
            $readyPayload.task_contract_sha256 = [string]$taskAfterDisabled.task_contract_sha256
            $readyPayload.task_definition_contract_sha256 = [string]$taskAfterDisabled.task_definition_contract_sha256
            $readyPayload.task_action_contract_sha256 = [string]$taskAfterDisabled.task_action_contract_sha256
            $readyPayload.task_enablement_restored = $false
            $readyPayload.completed_at_utc = $null
            $readyInput = Join-Path $receiptRoot ".$activationId.post-swap-ready.input.json"
            Write-DawnstrikeActivationJson $readyPayload $readyInput
            try {
                $ready = Invoke-DawnstrikeContractCli $pythonPath $runtime @("seal-receipt", "--input", $readyInput, "--output", $readyReceipt) "Recovered ready-to-enable receipt sealing" $ProcessTimeoutSeconds
            }
            finally { if (Test-Path -LiteralPath $readyInput -PathType Leaf) { Remove-Item -LiteralPath $readyInput -Force } }
            $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                -StateRoot $state -JournalPath $operationJournal -Lock $activationLock -Operation runtime_activation -Phase POST_SWAP_READY `
                -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) -CurrentSha $ExpectedSha -CurrentTree ([string]$candidateContract.tree) `
                -PreviousSha ([string]$journal.payload.previous_sha) -PreviousTree ([string]$journal.payload.previous_tree) -OriginIdentity $lockOrigin `
                -PreparedReceiptRelativePath $preparedReceiptRelative -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                -CompleteReceiptRelativePath $readyReceiptRelative -CompleteReceiptSha256 (Get-DawnstrikeSha256File $readyReceipt) `
                -BackupContractSha256 ([string]$journal.payload.backup_contract_sha256) -TaskContractSha256 ([string]$taskAfterDisabled.task_contract_sha256) `
                -RuntimeStageContractSha256 ([string]$journal.payload.runtime_stage_contract_sha256) -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "POST_SWAP_READY"
            if ($TestStageCrashPoint -eq "after_ready_journal") { Stop-Process -Id $PID -Force }
            Enable-DawnstrikeCanonicalTasks
            $taskAfter = Get-DawnstrikeTaskContract $runtime $state
            $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
            if ($TestStageCrashPoint -eq "after_enable_before_complete") { Stop-Process -Id $PID -Force }
            $payload=[ordered]@{}
            foreach($property in $ready.PSObject.Properties){if($property.Name-ne'receipt_sha256'){$payload[$property.Name]=$property.Value}}
            $payload.status='COMPLETE';$payload.task_count=[int]$taskAfter.task_count;$payload.task_contract_sha256=[string]$taskAfter.task_contract_sha256;$payload.task_definition_contract_sha256=[string]$taskAfter.task_definition_contract_sha256;$payload.task_action_contract_sha256=[string]$taskAfter.task_action_contract_sha256;$payload.task_enablement_restored=$true;$payload.completed_at_utc=[DateTime]::UtcNow.ToString('o')
            $inputReceipt=Join-Path $receiptRoot ".$activationId.recovery.input.json"
            Write-DawnstrikeActivationJson $payload $inputReceipt
            try{$complete=Invoke-DawnstrikeContractCli $pythonPath $runtime @('seal-receipt','--input',$inputReceipt,'--output',$completeReceipt) 'Recovered activation receipt sealing' $ProcessTimeoutSeconds}finally{if(Test-Path $inputReceipt){Remove-Item $inputReceipt -Force}}
            $journal=Get-DawnstrikeStrictRuntimeOperationJournal $operationJournal $lockInterpreter.path $lockInterpreter.sha256
             $null=Set-DawnstrikeRuntimeOperationJournalPhase `
                 -StateRoot $state -JournalPath $operationJournal -Lock $activationLock -Operation runtime_activation -Phase COMPLETE `
                 -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) -CurrentSha $ExpectedSha -CurrentTree ([string]$candidateContract.tree) `
                 -PreviousSha ([string]$journal.payload.previous_sha) -PreviousTree ([string]$journal.payload.previous_tree) -OriginIdentity $lockOrigin `
                 -PreparedReceiptRelativePath $preparedReceiptRelative -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                 -CompleteReceiptRelativePath $completeReceiptRelative -CompleteReceiptSha256 (Get-DawnstrikeSha256File $completeReceipt) `
                 -BackupContractSha256 ([string]$journal.payload.backup_contract_sha256) -TaskContractSha256 ([string]$taskAfter.task_contract_sha256) `
                 -RuntimeStageContractSha256 ([string]$journal.payload.runtime_stage_contract_sha256) -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
             $completedJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                 $operationJournal $lockInterpreter.path $lockInterpreter.sha256
             $null = Assert-DawnstrikeActivationCompleteTerminal `
                 -Journal $completedJournal -Receipt $complete -ReceiptPath $completeReceipt `
                 -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state -BackupRoot $backupRoot `
                 -GitPath $gitPath -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds `
                 -ExpectedSha $ExpectedSha -ExpectedTree ([string]$candidateContract.tree) `
                 -OriginIdentity $lockOrigin -MarketDate $MarketDate -StateDeclaration $stateDeclaration `
                 -ExpectedTask $taskAfter
             if ($TestStageCrashPoint -eq "after_complete_journal") { Stop-Process -Id $PID -Force }
            $completeRecoveryDaily = Enter-DawnstrikeDailyRunLock `
                -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
            if (-not $completeRecoveryDaily.acquired) {
                throw "Recovered COMPLETE activation could not reacquire its exact daily lock."
            }
            Confirm-DawnstrikeActivationDailyLockHandshake `
                -StateRoot $state -ActivationLock $activationLock -DailyLock $completeRecoveryDaily | Out-Null
            Exit-DawnstrikeDailyRunLock $completeRecoveryDaily
            if (Test-Path -LiteralPath $completeRecoveryDaily.lock_path) {
                throw "Recovered COMPLETE activation did not release its exact daily lock."
            }
            Exit-DawnstrikeGovernedRuntimeLock $activationLock
            $activationLock = $null
            return $complete
        }
        catch {
            # Any strict recovery failure must retain the adopted runtime lock
            # and journal. Releasing either would orphan the only admissible
            # PRE_* recovery chain; the next exact invocation adopts it.
            throw
        }
    }

    $activationLock = $null
    $dailyLock = $null
    $swapStarted = $false
    $candidateInstalled = $false
    $tasksDisabled = $false
    $auxiliaryDisabled = $false
    $preserveLocks = $false
    $activationBodyStarted = $false
    $journalPhase = "INIT"
    $lockOrigin = Convert-DawnstrikeCanonicalOriginIdentity $origin
    $lockInterpreter = Get-DawnstrikeApprovedLockInterpreter
    $emptyJournalHash = Get-DawnstrikeSha256Text ""
    # INIT and its exact runtime lock must exist before clone can create even
    # the stage directory.  Daily stages observe the activation lock and fail
    # closed throughout clone/checkout, eliminating the no-journal stage gap.
    $activationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal `
        -StateRoot $state -JournalPath $operationJournal -Operation runtime_activation `
        -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) `
        -CurrentSha ([string]$runtimeContract.head) -CurrentTree ([string]$runtimeContract.tree) `
        -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
        -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $preparedReceiptRelative `
        -CompleteReceiptRelativePath $readyReceiptRelative `
        -TaskContractSha256 ([string]$taskBefore.task_contract_sha256) `
        -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
    try {
        if ($TestStageCrashPoint -eq "after_stage_directory") {
            New-Item -ItemType Directory -Path $stage -ErrorAction Stop | Out-Null
            Stop-Process -Id $PID -Force
        }
        $null = Invoke-DawnstrikeActivationProcess `
            -FilePath $gitPath `
            -ArgumentList @("clone", "--no-local", "--no-hardlinks", "--no-checkout", "--quiet", $candidate, $stage) `
            -WorkingDirectory (Split-Path -Parent $runtime) `
            -Label "Candidate runtime staging" `
            -TimeoutSeconds $ProcessTimeoutSeconds
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $stage, "checkout", "--detach", "--quiet", $ExpectedSha) $stage "Candidate checkout staging" $ProcessTimeoutSeconds
        if ($TestStageCrashPoint -eq "after_stage_checkout") { Stop-Process -Id $PID -Force }
        $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $stage, "remote", "set-url", "origin", $origin) $stage "Candidate origin binding" $ProcessTimeoutSeconds
        $stagedContract = Get-DawnstrikeGitContract $gitPath $stage $ProcessTimeoutSeconds $ExpectedSha
        if ($stagedContract.tree -ne $candidateContract.tree) {
            throw "Staged runtime tree does not match the accepted candidate tree."
        }
        $stagedOrigin = Get-DawnstrikeGitValue $gitPath $stage @("remote", "get-url", "origin") "Staged origin verification" $ProcessTimeoutSeconds
        if ((Get-DawnstrikeSha256Text $stagedOrigin) -ne $originHash) {
            throw "Staged runtime origin does not match the accepted candidate origin."
        }

        try {
            Assert-DawnstrikeNoDailyLocks $state
            $dailyLock = Enter-DawnstrikeDailyRunLock -StateRoot $state -MarketDate $MarketDate -Owner "runtime_activation"
            if (-not $dailyLock.acquired) {
                throw "Runtime activation could not acquire the daily run lock."
            }
            Confirm-DawnstrikeActivationDailyLockHandshake `
                -StateRoot $state -ActivationLock $activationLock -DailyLock $dailyLock | Out-Null
            $otherDailyLocks = @(Get-ChildItem -LiteralPath (Join-Path $state "locks") -Filter "dawnstrike-daily-*.lock" -File -Force | Where-Object { $_.FullName -ne $dailyLock.lock_path })
            if ($otherDailyLocks.Count -gt 0) {
                throw "Another daily run lock appeared during runtime activation."
            }
            # Re-read authoritative finalizer/public evidence after both
            # operation locks are held, closing the final race before the
            # first task disablement or runtime rename.
            $null = Invoke-DawnstrikeActivationBoundary `
                -PythonPath $pythonPath `
                -CandidateRoot $candidate `
                -MarketDate $MarketDate `
                -RuntimeRoot $runtime `
                -StateRoot $state `
                -NowUtc $activationNowUtc `
                -TimeoutSeconds $ProcessTimeoutSeconds
            $taskLocked = Get-DawnstrikeTaskContract $runtime $state
            $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state
            if ($taskLocked.task_contract_sha256 -ne $taskBefore.task_contract_sha256) {
                throw "Task definitions changed during activation preflight."
            }
            $taskBackup = New-DawnstrikeTaskXmlBackup `
                -StateRoot $state `
                -BackupName $schedulerBackupName `
                -ActivationId $activationId `
                -TaskContract $taskLocked `
                -AuxiliaryCapture $auxiliaryBefore
            $stageJournalHash = Get-DawnstrikeSha256Text ("$ExpectedSha`:$($candidateContract.tree)`:$stage")
            # This intent is the durable boundary immediately before the first
            # scheduler mutation.  Recovery can therefore safely force the
            # exact task set Disabled even if the process dies in the disable
            # loop, instead of guessing whether activation had started.
            $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                -StateRoot $state -JournalPath $operationJournal -Lock $activationLock `
                -Operation runtime_activation -Phase PRE_QUIESCE -CandidateSha $ExpectedSha `
                -CandidateTree ([string]$candidateContract.tree) `
                -CurrentSha ([string]$runtimeContract.head) -CurrentTree ([string]$runtimeContract.tree) `
                -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $preparedReceiptRelative `
                -PreparedReceiptSha256 $emptyJournalHash -CompleteReceiptRelativePath $readyReceiptRelative `
                -CompleteReceiptSha256 $emptyJournalHash -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 ([string]$taskLocked.task_contract_sha256) `
                -RuntimeStageContractSha256 $stageJournalHash `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "PRE_QUIESCE"
            # From this durable boundary onward, ordinary failures are part of
            # the journaled activation transaction.  Leave the exact lock pair
            # in place for the next process to adopt after compensation.
            $activationBodyStarted = $true
            $tasksDisabled = $true
            Disable-DawnstrikeCanonicalTasks
            $taskDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -AllowDisabled
            if (
                $taskDisabled.disabled_count -ne 5 -or
                $taskDisabled.enabled_count -ne 0 -or
                $taskDisabled.task_definition_contract_sha256 -ne
                    $taskLocked.task_definition_contract_sha256 -or
                $taskDisabled.task_action_contract_sha256 -ne
                    $taskLocked.task_action_contract_sha256
            ) {
                throw "Canonical tasks did not enter the exact disabled swap boundary."
            }
            if ($auxiliaryBefore.present) {
                $auxiliaryDisabled = $true
                $auxiliaryBoundary = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                if ($auxiliaryBoundary.xml_sha256 -ne $auxiliaryBefore.xml_sha256) {
                    throw "Auxiliary capture task XML changed while entering the disabled boundary."
                }
                # Re-read the task and the exact candidate-named attestation
                # under the shared activation lock immediately before any
                # backup/swap boundary.  The pre-lock observation is only a
                # hint and cannot authorize a checkout change.
                $auxiliaryLocked = Get-DawnstrikeAuxiliaryCaptureTask $runtime $state -AllowDisabled
                if ($auxiliaryLocked.state -ne "Disabled" -or $auxiliaryLocked.xml_sha256 -ne $auxiliaryBefore.xml_sha256) {
                    throw "Auxiliary capture task changed during the locked activation boundary."
                }
                $lockedHardeningAttestation = Assert-DawnstrikeCaptureHardeningAttestation `
                    -Auxiliary $auxiliaryLocked -CandidateRoot $candidate -StateRoot $state `
                    -RuntimeRoot $runtime -CandidateSha $ExpectedSha -CandidateTree $candidateContract.tree `
                    -GitPath $gitPath `
                    -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds
                if ($lockedHardeningAttestation.raw_sha256 -ne $hardeningAttestation.raw_sha256 -or [string]$lockedHardeningAttestation.payload.receipt_sha256 -ne [string]$hardeningAttestation.payload.receipt_sha256) {
                    throw "Hardening attestation changed during the locked activation boundary."
                }
                $hardeningAttestation = $lockedHardeningAttestation
            }
            if ($stateDeclaration.required) {
                # Re-read every database sidecar hash only after both locks and
                # all six task quiescence are proven.  This closes the WAL
                # and online-snapshot TOCTOU between preflight and swap.
                $statePreparationLocked = Get-DawnstrikeStatePreparationProof `
                    -CandidateRoot $candidate `
                    -StateRoot $state `
                    -BackupRoot $backupRoot `
                    -CandidateSha $ExpectedSha `
                    -CandidateTree $candidateContract.tree `
                    -PythonPath $pythonPath `
                    -TimeoutSeconds $ProcessTimeoutSeconds
                if (
                    $statePreparationLocked.receipt_sha256 -ne $statePreparation.receipt_sha256 -or
                    $statePreparationLocked.after_db_sha256 -ne $statePreparation.after_db_sha256 -or
                    $statePreparationLocked.after_wal_sha256 -ne $statePreparation.after_wal_sha256 -or
                    $statePreparationLocked.after_shm_sha256 -ne $statePreparation.after_shm_sha256 -or
                    $statePreparationLocked.after_logical_snapshot_sha256 -ne $statePreparation.after_logical_snapshot_sha256 -or
                    $statePreparationLocked.inventory_sha256 -ne $statePreparation.inventory_sha256
                ) { throw "Durable state changed after task quiescence; WAL or inventory drift is untrusted." }
            }
            $stateLocked = Invoke-DawnstrikeContractCli $pythonPath $candidate @("inspect-state", "--db-path", $dbPath) "Locked durable state validation" $ProcessTimeoutSeconds
            if ($stateLocked.main_file_sha256 -ne $stateInfo.main_file_sha256) {
                throw "Durable state changed during activation preflight."
            }

            $backupId = "runtime-activation-$activationId"
            $backupTool = Join-Path $candidate "scripts\state_disaster_recovery.py"
            Assert-DawnstrikeNoReparseComponents $backupTool "Durable-state backup tool"
            if (-not (Test-Path -LiteralPath $backupTool -PathType Leaf)) {
                throw "Durable-state backup tool is missing."
            }
            $backupArguments = @(
                $backupTool,
                "backup", "--source-db", $dbPath, "--backup-root", $backupRoot,
                "--state-root", $state, "--retention", [string]$BackupRetention,
                "--source-sha", $runtimeContract.head, "--backup-id", $backupId
            )
            if ($stateDeclaration.required) {
                if ($null -eq $statePreparationLocked) {
                    throw "Locked state-preparation proof is required before the activation backup."
                }
                foreach ($lockedHash in @(
                    $statePreparationLocked.after_db_sha256,
                    $statePreparationLocked.after_wal_sha256,
                    $statePreparationLocked.after_shm_sha256,
                    $statePreparationLocked.after_logical_snapshot_sha256
                )) {
                    if ([string]$lockedHash -notmatch '^[0-9a-f]{64}$') {
                        throw "Locked state-preparation snapshot hash is invalid."
                    }
                }
                $backupArguments += @(
                    "--expected-db-sha256", [string]$statePreparationLocked.after_db_sha256,
                    "--expected-wal-sha256", [string]$statePreparationLocked.after_wal_sha256,
                    "--expected-shm-sha256", [string]$statePreparationLocked.after_shm_sha256,
                    "--expected-logical-snapshot-sha256",
                    [string]$statePreparationLocked.after_logical_snapshot_sha256
                )
            }
            $backup = Invoke-DawnstrikeActivationProcess `
                -FilePath $pythonPath `
                -ArgumentList $backupArguments `
                -WorkingDirectory $candidate `
                -Label "SQLite-consistent pre-activation backup" `
                -TimeoutSeconds $ProcessTimeoutSeconds
            try { $backupResult = [string]$backup.Stdout | ConvertFrom-Json }
            catch { throw "SQLite backup did not return valid JSON." }
            if (
                $backupResult.status -ne "PASS" -or
                $backupResult.quick_check -ne "ok" -or
                [int]$backupResult.schema_version -ne [int]$stateInfo.schema_version -or
                [string]$backupResult.source_release_sha -ne $runtimeContract.head
            ) {
                throw "SQLite backup contract validation failed."
            }
            $backupBundlePath = $null
            $backupManifestSha256 = $null
            $backupLogicalSnapshotSha256 = $null
            $backupSourceLogicalSnapshotSha256 = $null
            if ($stateDeclaration.required) {
                if (
                    [string]$backupResult.source_live_main_file_sha256 -ne
                        [string]$statePreparationLocked.after_db_sha256 -or
                    [string]$backupResult.backup_db_sha256 -notmatch '^[0-9a-f]{64}$' -or
                    [string]$backupResult.source_logical_snapshot_sha256 -ne
                        [string]$statePreparationLocked.after_logical_snapshot_sha256 -or
                    [string]$backupResult.backup_logical_snapshot_sha256 -ne
                        [string]$statePreparationLocked.after_logical_snapshot_sha256 -or
                    [string]$backupResult.manifest_sha256 -notmatch '^[0-9a-f]{64}$' -or
                    [string]$backupResult.bundle_path -eq ""
                ) {
                    throw "SQLite backup did not return the exact locked snapshot lineage."
                }
                $backupBundlePath = Resolve-DawnstrikeActivationRoot `
                    ([string]$backupResult.bundle_path) `
                    "Durable-state backup bundle"
                $backupRootResolved = Resolve-DawnstrikeActivationRoot $backupRoot "BackupRoot"
                if (
                    -not $backupBundlePath.StartsWith(
                        $backupRootResolved.TrimEnd('\') + '\',
                        [System.StringComparison]::OrdinalIgnoreCase
                    ) -or
                    [System.IO.Path]::GetFileName($backupBundlePath) -ne $backupId
                ) {
                    throw "Durable-state backup bundle is outside the expected backup root."
                }
                foreach ($backupFileName in @("shadow_real.sqlite", "manifest.json", "receipt.json")) {
                    $backupFile = Join-Path $backupBundlePath $backupFileName
                    Assert-DawnstrikeNoReparseComponents $backupFile "Durable-state backup $backupFileName"
                    if (-not (Test-Path -LiteralPath $backupFile -PathType Leaf)) {
                        throw "Durable-state backup is missing $backupFileName."
                    }
                }
                $backupVerification = Invoke-DawnstrikeActivationProcess `
                    -FilePath $pythonPath `
                    -ArgumentList @(
                        $backupTool, "restore-verify", "--bundle", $backupBundlePath,
                        "--target-db", $dbPath, "--backup-root", $backupRoot,
                        "--state-root", $state
                    ) `
                    -WorkingDirectory $candidate `
                    -Label "Pre-activation durable-state backup lineage verification" `
                    -TimeoutSeconds $ProcessTimeoutSeconds
                try { $backupVerificationResult = [string]$backupVerification.Stdout | ConvertFrom-Json }
                catch { throw "Durable-state backup lineage verification did not return valid JSON." }
                if (
                    [string]$backupVerificationResult.status -ne "VERIFY" -or
                    [string]$backupVerificationResult.backup_id -ne $backupId -or
                    [string]$backupVerificationResult.bundle_path -ne $backupBundlePath -or
                    [string]$backupVerificationResult.manifest_sha256 -ne [string]$backupResult.manifest_sha256 -or
                    [string]$backupVerificationResult.backup_db_sha256 -ne [string]$backupResult.backup_db_sha256 -or
                    [string]$backupVerificationResult.backup_logical_snapshot_sha256 -ne [string]$backupResult.backup_logical_snapshot_sha256 -or
                    [string]$backupVerificationResult.source_logical_snapshot_sha256 -ne [string]$backupResult.source_logical_snapshot_sha256 -or
                    [string]$backupVerificationResult.source_release_sha -ne [string]$runtimeContract.head -or
                    [int]$backupVerificationResult.schema_version -ne [int]$stateInfo.schema_version -or
                    [string]$backupVerificationResult.quick_check -ne "ok" -or
                    $backupVerificationResult.write_performed -ne $false -or
                    $backupVerificationResult.automatic_overwrite -ne $false
                ) {
                    throw "Durable-state backup manifest or receipt lineage does not match the locked proof."
                }
                $backupManifestSha256 = [string]$backupResult.manifest_sha256
                $backupLogicalSnapshotSha256 = [string]$backupResult.backup_logical_snapshot_sha256
                $backupSourceLogicalSnapshotSha256 = [string]$backupResult.source_logical_snapshot_sha256
            }
            $stateAfterBackup = Invoke-DawnstrikeContractCli $pythonPath $candidate @("inspect-state", "--db-path", $dbPath) "Post-backup state validation" $ProcessTimeoutSeconds
            if ($stateAfterBackup.main_file_sha256 -ne $stateLocked.main_file_sha256) {
                throw "Durable state changed while creating the activation backup."
            }

            New-Item -ItemType Directory -Path $rollbackRoot -Force | Out-Null
            $bundleTemporary = "$rollbackBundle.$([guid]::NewGuid().ToString('N')).tmp"
            $null = Invoke-DawnstrikeActivationProcess $gitPath @("-C", $runtime, "bundle", "create", $bundleTemporary, "HEAD") $runtime "Rollback bundle creation" $ProcessTimeoutSeconds
            $null = Invoke-DawnstrikeActivationProcess $gitPath @("bundle", "verify", $bundleTemporary) $runtime "Rollback bundle verification" $ProcessTimeoutSeconds
            [System.IO.File]::Move($bundleTemporary, $rollbackBundle)
            $bundleHash = Get-DawnstrikeSha256File $rollbackBundle

            $preparedAt = [DateTime]::UtcNow.ToString("o")
            $receiptPayload = [ordered]@{
                schema_version = "dawnstrike.runtime_activation_receipt.v2"
                status = "PREPARED"
                activation_id = $activationId
                market_date = $MarketDate
                candidate_sha = $ExpectedSha
                candidate_tree = $candidateContract.tree
                previous_sha = $runtimeContract.head
                previous_tree = $runtimeContract.tree
                ci_evidence_sha256 = [string]$evidence.ci_evidence_sha256
                sol_evidence_sha256 = [string]$evidence.sol_evidence_sha256
                state_backup_id = [string]$backupResult.backup_id
                state_backup_db_sha256 = [string]$backupResult.backup_db_sha256
                state_schema_version = [int]$backupResult.schema_version
                state_quick_check = [string]$backupResult.quick_check
                rollback_bundle_sha256 = $bundleHash
                 task_count = [int]$taskLocked.task_count
                 task_contract_sha256 = [string]$taskLocked.task_contract_sha256
                 task_definition_contract_sha256 = [string]$taskLocked.task_definition_contract_sha256
                 task_action_contract_sha256 = [string]$taskLocked.task_action_contract_sha256
                task_paths_unchanged = $true
                task_enablement_restored = $false
                scheduler_backup_name = [string]$taskBackup.backup_name
                scheduler_backup_manifest_sha256 = [string]$taskBackup.manifest_sha256
                runtime_origin_sha256 = $originHash
                swap_contract = "same_volume_two_rename_with_immediate_restore"
                stage_name = Split-Path -Leaf $stage
                rollback_checkout_name = "previous-runtime"
                rollback_bundle_name = "previous-runtime.bundle"
                prepared_at_utc = $preparedAt
                completed_at_utc = $null
                research_only = $true
                broker_execution_enabled = $false
                previous_runtime_rollback_authorized = [bool]$previousRuntimeAuthorization.authorized
                previous_runtime_disposition = [string]$previousRuntimeAuthorization.disposition
                previous_runtime_authorization_receipt_sha256 = [string]$previousRuntimeAuthorization.receipt_sha256
                previous_runtime_authorization_journal_sha256 = [string]$previousRuntimeAuthorization.journal_sha256
            }
            if ($stateDeclaration.required) {
                $auxiliaryBackupManifest = Get-Content `
                    -LiteralPath (Join-Path $taskBackup.backup_path "manifest.json") `
                    -Raw -Encoding UTF8 | ConvertFrom-Json
                $receiptPayload.state_preparation_required = $true
                $receiptPayload.state_preparation_contract = [string]$stateDeclaration.sidecar_contract
                $receiptPayload.state_preparation_receipt_sha256 = [string]$statePreparation.receipt_sha256
                $receiptPayload.state_preparation_after_db_sha256 = [string]$statePreparation.after_db_sha256
                $receiptPayload.state_preparation_after_wal_sha256 = [string]$statePreparation.after_wal_sha256
                $receiptPayload.state_preparation_after_shm_sha256 = [string]$statePreparation.after_shm_sha256
                $receiptPayload.state_preparation_after_logical_snapshot_sha256 = [string]$statePreparation.after_logical_snapshot_sha256
                $receiptPayload.state_preparation_inventory_sha256 = [string]$statePreparation.inventory_sha256
                $receiptPayload.state_preparation_backup_id = [string]$statePreparation.backup_id
                $receiptPayload.state_preparation_backup_bundle_path = [string]$statePreparation.backup_bundle_path
                $receiptPayload.state_preparation_backup_db_sha256 = [string]$statePreparation.backup_db_sha256
                $receiptPayload.state_preparation_backup_manifest_sha256 = [string]$statePreparation.backup_manifest_sha256
                $receiptPayload.state_preparation_backup_manifest_file_sha256 = [string]$statePreparation.backup_manifest_file_sha256
                $receiptPayload.state_backup_bundle_path = [string]$backupBundlePath
                $receiptPayload.state_backup_manifest_sha256 = [string]$backupManifestSha256
                $receiptPayload.state_backup_logical_snapshot_sha256 = [string]$backupLogicalSnapshotSha256
                $receiptPayload.state_backup_source_logical_snapshot_sha256 = [string]$backupSourceLogicalSnapshotSha256
                $receiptPayload.auxiliary_capture_present = [bool]$auxiliaryBefore.present
                $receiptPayload.auxiliary_capture_state_before = if ($auxiliaryBefore.present) { [string]$auxiliaryBefore.state } else { "ABSENT" }
                $receiptPayload.auxiliary_capture_state_after = if ($auxiliaryBefore.present) { "Disabled" } else { "ABSENT" }
                $receiptPayload.auxiliary_capture_action = if ($auxiliaryBefore.present) { "DISABLED_UNTIL_EXACT_SHA_REBIND" } else { "ABSENT_ALLOWED" }
                $receiptPayload.auxiliary_capture_xml_sha256 = [string]$auxiliaryBefore.xml_sha256
                $receiptPayload.auxiliary_capture_xml_file_sha256 = if ($auxiliaryBefore.present) {
                    [string]$auxiliaryBackupManifest.auxiliary_capture.xml_file_sha256
                }
                else {
                    Get-DawnstrikeSha256Text ""
                }
                $receiptPayload.auxiliary_capture_definition_contract_sha256 = [string]$auxiliaryBefore.definition_contract_sha256
                $receiptPayload.auxiliary_capture_action_contract_sha256 = [string]$auxiliaryBefore.action_contract_sha256
                $receiptPayload.auxiliary_capture_backup_name = if ($auxiliaryBefore.present) { [string]$taskBackup.backup_name } else { "NONE" }
                $receiptPayload.auxiliary_capture_backup_manifest_sha256 = [string]$taskBackup.manifest_sha256
                $receiptPayload.capture_hardening_receipt_relative_path = "NONE"
                $receiptPayload.capture_hardening_receipt_raw_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_receipt_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_xml_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_action_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_principal_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_trigger_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_settings_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_runner_before_sha256 = Get-DawnstrikeSha256Text ""
                $receiptPayload.capture_hardening_runner_target_sha256 = Get-DawnstrikeSha256Text ""
                if ($null -ne $hardeningAttestation) {
                    $receiptPayload.capture_hardening_receipt_relative_path = ([System.IO.Path]::GetFullPath($hardeningAttestation.path).Substring(([System.IO.Path]::GetFullPath($state)).TrimEnd('\').Length + 1) -replace '\\','/')
                    $receiptPayload.capture_hardening_receipt_raw_sha256 = [string]$hardeningAttestation.raw_sha256
                    $receiptPayload.capture_hardening_receipt_sha256 = [string]$hardeningAttestation.payload.receipt_sha256
                    $receiptPayload.capture_hardening_xml_sha256 = [string]$hardeningAttestation.payload.xml_after_sha256
                    $receiptPayload.capture_hardening_action_sha256 = [string]$hardeningAttestation.payload.action_after_sha256
                    $receiptPayload.capture_hardening_principal_sha256 = [string]$hardeningAttestation.payload.principal_after_sha256
                    $receiptPayload.capture_hardening_trigger_sha256 = [string]$hardeningAttestation.payload.trigger_sha256
                    $receiptPayload.capture_hardening_settings_sha256 = [string]$hardeningAttestation.payload.settings_after_sha256
                    $receiptPayload.capture_hardening_runner_before_sha256 = [string]$hardeningAttestation.payload.runner_before_sha256
                    $receiptPayload.capture_hardening_runner_target_sha256 = [string]$hardeningAttestation.payload.runner_sha256
                }
            }
            $inputReceipt = Join-Path $receiptRoot ".$activationId.input.json"
            Write-DawnstrikeActivationJson $receiptPayload $inputReceipt
            try {
                $null = Invoke-DawnstrikeContractCli $pythonPath $candidate @("seal-receipt", "--input", $inputReceipt, "--output", $preparedReceipt) "Prepared activation receipt sealing" $ProcessTimeoutSeconds
            }
            finally {
                if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) { Remove-Item -LiteralPath $inputReceipt -Force }
            }

            $runtimeFinalCheck = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $runtimeContract.head
            if ($runtimeFinalCheck.tree -ne $runtimeContract.tree) {
                throw "Runtime changed after rollback evidence was sealed."
            }
            $taskFinalCheck = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if (
                $taskFinalCheck.disabled_count -ne 5 -or
                $taskFinalCheck.enabled_count -ne 0 -or
                $taskFinalCheck.task_definition_contract_sha256 -ne
                    $taskLocked.task_definition_contract_sha256 -or
                $taskFinalCheck.task_action_contract_sha256 -ne
                    $taskLocked.task_action_contract_sha256
            ) {
                throw "Task definitions changed immediately before runtime swap."
            }
            $null = Assert-DawnstrikeTaskXmlBackup `
                -StateRoot $state `
                -BackupName $taskBackup.backup_name `
                -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
                -ExpectedTaskContractSha256 ([string]$taskLocked.task_contract_sha256) `
                -ExpectedTaskDefinitionContractSha256 ([string]$taskLocked.task_definition_contract_sha256) `
                -ExpectedTaskActionContractSha256 ([string]$taskLocked.task_action_contract_sha256)

            # The candidate checkout remains the source of executable tools
            # until the exact staged checkout is installed. Reassert its
            # commit/tree and declaration binding at the last pre-swap
            # boundary so a delete/restore cannot alter activation semantics.
            $null = Assert-DawnstrikeCandidateIdentityAndDeclaration `
                -GitPath $gitPath `
                -CandidateRoot $candidate `
                -CandidateSha $ExpectedSha `
                -CandidateTree $candidateContract.tree `
                -Declaration $stateDeclaration `
                -TimeoutSeconds $ProcessTimeoutSeconds

            $stageJournalHash = Get-DawnstrikeSha256Text ("$ExpectedSha`:$($candidateContract.tree)`:$stage")
            $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                -StateRoot $state -JournalPath $operationJournal -Lock $activationLock `
                -Operation runtime_activation -Phase PRE_SWAP -CandidateSha $ExpectedSha `
                -CandidateTree ([string]$candidateContract.tree) `
                -CurrentSha ([string]$runtimeContract.head) -CurrentTree ([string]$runtimeContract.tree) `
                -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $preparedReceiptRelative `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                -CompleteReceiptRelativePath $readyReceiptRelative -CompleteReceiptSha256 $emptyJournalHash `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 ([string]$taskFinalCheck.task_contract_sha256) `
                -RuntimeStageContractSha256 $stageJournalHash `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "PRE_SWAP"

            $swapStarted = $true
            [System.IO.Directory]::Move($runtime, $rollbackCheckout)
            if ($InjectCrashBetweenRuntimeRenames) { exit 137 }
            [System.IO.Directory]::Move($stage, $runtime)
            $candidateInstalled = $true

            $installed = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $ExpectedSha
            if ($installed.tree -ne $candidateContract.tree) {
                throw "Installed runtime tree does not match the accepted candidate."
            }
            $installedOrigin = Get-DawnstrikeGitValue $gitPath $runtime @("remote", "get-url", "origin") "Installed origin verification" $ProcessTimeoutSeconds
            if ((Get-DawnstrikeSha256Text $installedOrigin) -ne $originHash) {
                throw "Installed runtime origin does not match the accepted origin."
            }
            $taskAfterDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            if (
                $taskAfterDisabled.disabled_count -ne 5 -or
                $taskAfterDisabled.enabled_count -ne 0 -or
                $taskAfterDisabled.task_definition_contract_sha256 -ne
                    $taskLocked.task_definition_contract_sha256 -or
                $taskAfterDisabled.task_action_contract_sha256 -ne
                    $taskLocked.task_action_contract_sha256
            ) {
                throw "Task definitions changed across the runtime swap."
            }
            $auxiliaryAfterDisabled = if ($stateDeclaration.required) {
                Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            }
            else {
                $auxiliaryBefore
            }
            if ($auxiliaryBefore.present) {
                if (
                    -not $auxiliaryAfterDisabled.present -or
                    $auxiliaryAfterDisabled.state -ne "Disabled" -or
                    $auxiliaryAfterDisabled.definition_contract_sha256 -ne $auxiliaryBefore.definition_contract_sha256 -or
                    $auxiliaryAfterDisabled.action_contract_sha256 -ne $auxiliaryBefore.action_contract_sha256
                ) { throw "Auxiliary capture task changed across the runtime swap." }
            }
            elseif ($auxiliaryAfterDisabled.present) {
                throw "An auxiliary capture task appeared during the runtime swap."
            }
            $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                -StateRoot $state -JournalPath $operationJournal -Lock $activationLock `
                -Operation runtime_activation -Phase POST_SWAP -CandidateSha $ExpectedSha `
                -CandidateTree ([string]$candidateContract.tree) -CurrentSha $ExpectedSha `
                -CurrentTree ([string]$candidateContract.tree) `
                -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $preparedReceiptRelative `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                -CompleteReceiptRelativePath $readyReceiptRelative -CompleteReceiptSha256 $emptyJournalHash `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 ([string]$taskAfterDisabled.task_contract_sha256) `
                -RuntimeStageContractSha256 $stageJournalHash `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "POST_SWAP"
            $null = Assert-DawnstrikeReceiptRecoveryArtifacts `
                -Receipt $receiptPayload `
                -StateRoot $state `
                -BackupRoot $backupRoot `
                -ToolRoot $candidate `
                -GitPath $gitPath `
                -PythonPath $pythonPath `
                -TimeoutSeconds $ProcessTimeoutSeconds `
                -RequireRollbackCheckout
            $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -AllowDisabled
            Set-DawnstrikeCanonicalTaskExpectedSha `
                -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
            $taskAfterDisabled = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
            $null = Assert-DawnstrikeCanonicalTaskSemantics `
                -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha -AllowDisabled
            # Seal a truthful PREPARED receipt for the exact disabled,
            # SHA-bound actions. POST_SWAP_READY is the durable power-loss
            # boundary for enablement; it must never claim COMPLETE before all
            # five canonical tasks are actually Ready.
            $readyPayload = [ordered]@{}
            foreach ($entry in $receiptPayload.GetEnumerator()) {
                $readyPayload[$entry.Key] = $entry.Value
            }
            $readyPayload.status = "PREPARED"
            $readyPayload.task_count = [int]$taskAfterDisabled.task_count
            $readyPayload.task_contract_sha256 = [string]$taskAfterDisabled.task_contract_sha256
            $readyPayload.task_definition_contract_sha256 = [string]$taskAfterDisabled.task_definition_contract_sha256
            $readyPayload.task_action_contract_sha256 = [string]$taskAfterDisabled.task_action_contract_sha256
            $readyPayload.task_enablement_restored = $false
            $readyPayload.completed_at_utc = $null
            Write-DawnstrikeActivationJson $readyPayload $inputReceipt
            try {
                $ready = Invoke-DawnstrikeContractCli $pythonPath $runtime @("seal-receipt", "--input", $inputReceipt, "--output", $readyReceipt) "Ready-to-enable activation receipt sealing" $ProcessTimeoutSeconds
            }
            finally {
                if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) { Remove-Item -LiteralPath $inputReceipt -Force }
            }
            $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                -StateRoot $state -JournalPath $operationJournal -Lock $activationLock `
                -Operation runtime_activation -Phase POST_SWAP_READY -CandidateSha $ExpectedSha `
                -CandidateTree ([string]$candidateContract.tree) -CurrentSha $ExpectedSha `
                -CurrentTree ([string]$candidateContract.tree) `
                -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $preparedReceiptRelative `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                -CompleteReceiptRelativePath $readyReceiptRelative `
                -CompleteReceiptSha256 (Get-DawnstrikeSha256File $readyReceipt) `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 ([string]$taskAfterDisabled.task_contract_sha256) `
                -RuntimeStageContractSha256 $stageJournalHash `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "POST_SWAP_READY"
            if ($TestStageCrashPoint -eq "after_ready_journal") { Stop-Process -Id $PID -Force }
            Enable-DawnstrikeCanonicalTasks
            $taskAfter = Get-DawnstrikeTaskContract $runtime $state
            $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $ExpectedSha
            # The pre-swap receipt fields describe the task contract that was
            # backed up.  Once the externally activated SHA is rebound, seal
            # the final receipt with the exact enabled action contract so an
            # idempotent retry cannot mistake the legacy action for the live
            # task identity.
            $receiptPayload.task_count = [int]$taskAfter.task_count
            $receiptPayload.task_contract_sha256 = [string]$taskAfter.task_contract_sha256
            $receiptPayload.task_definition_contract_sha256 = [string]$taskAfter.task_definition_contract_sha256
            $receiptPayload.task_action_contract_sha256 = [string]$taskAfter.task_action_contract_sha256
            $auxiliaryAfter = if ($stateDeclaration.required) {
                Get-DawnstrikeAuxiliaryCaptureTask $runtime $state
            }
            else {
                $auxiliaryBefore
            }
            if ($auxiliaryBefore.present) {
                if ($auxiliaryAfter.state -ne "Disabled" -or $auxiliaryAfter.definition_contract_sha256 -ne $auxiliaryBefore.definition_contract_sha256) {
                    throw "Auxiliary capture task must remain Disabled until exact-SHA rebind."
                }
            }
            elseif ($auxiliaryAfter.present) {
                throw "An auxiliary capture task appeared after activation."
            }
            $tasksDisabled = $false
            if ($TestStageCrashPoint -eq "after_enable_before_complete") { Stop-Process -Id $PID -Force }
            $receiptPayload.status = "COMPLETE"
            $receiptPayload.task_enablement_restored = $true
            $receiptPayload.completed_at_utc = [DateTime]::UtcNow.ToString("o")
            Write-DawnstrikeActivationJson $receiptPayload $inputReceipt
            try {
                $complete = Invoke-DawnstrikeContractCli $pythonPath $runtime @("seal-receipt", "--input", $inputReceipt, "--output", $completeReceipt) "Complete activation receipt sealing" $ProcessTimeoutSeconds
            }
            finally {
                if (Test-Path -LiteralPath $inputReceipt -PathType Leaf) { Remove-Item -LiteralPath $inputReceipt -Force }
            }
            $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                -StateRoot $state -JournalPath $operationJournal -Lock $activationLock `
                -Operation runtime_activation -Phase COMPLETE -CandidateSha $ExpectedSha `
                -CandidateTree ([string]$candidateContract.tree) -CurrentSha $ExpectedSha `
                -CurrentTree ([string]$candidateContract.tree) `
                -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
                -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $preparedReceiptRelative `
                -PreparedReceiptSha256 (Get-DawnstrikeSha256File $preparedReceipt) `
                -CompleteReceiptRelativePath $completeReceiptRelative `
                -CompleteReceiptSha256 (Get-DawnstrikeSha256File $completeReceipt) `
                -BackupContractSha256 ([string]$taskBackup.manifest_sha256) `
                -TaskContractSha256 ([string]$taskAfter.task_contract_sha256) `
                -RuntimeStageContractSha256 $stageJournalHash `
                -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
            $journalPhase = "COMPLETE"
            $terminalJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                $operationJournal $lockInterpreter.path $lockInterpreter.sha256
            $null = Assert-DawnstrikeActivationCompleteTerminal `
                -Journal $terminalJournal -Receipt $complete -ReceiptPath $completeReceipt `
                -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state -BackupRoot $backupRoot `
                -GitPath $gitPath -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds `
                -ExpectedSha $ExpectedSha -ExpectedTree ([string]$candidateContract.tree) `
                -OriginIdentity $lockOrigin -MarketDate $MarketDate -StateDeclaration $stateDeclaration `
                -ExpectedTask $taskAfter
            if ($TestStageCrashPoint -eq "after_complete_journal") { Stop-Process -Id $PID -Force }
            return $complete
        }
        catch {
            $failure = $_
            # A journal transition can durably replace the file and still
            # throw before its assignment above (for example, a readback or
            # output fault).  Reconcile the phase while the owned lock is
            # still held; otherwise the outer staging cleanup could mistake a
            # PRE_QUIESCE transaction for INIT and strand its artifacts.
            $journalReconciliationFailed = $false
            try {
                $failureJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                    $operationJournal $lockInterpreter.path $lockInterpreter.sha256
                $terminalJournal = $failureJournal
                $journalPhase = [string]$failureJournal.payload.phase
            }
            catch {
                # An unproven journal phase is itself governed evidence. Keep
                # both locks so an operator/next process can recover it.
                $preserveLocks = $true
                $journalReconciliationFailed = $true
            }
            if ($journalReconciliationFailed) {
                throw "Runtime activation journal phase could not be reconciled; operator recovery is required."
            }
            # COMPLETE is an irreversible commit.  A receipt/output or
            # staging-cleanup fault after that transition must reconcile the
            # committed candidate, never run the nonterminal restore below.
            if ($journalPhase -eq "COMPLETE") {
                try {
                    $terminalReceipt = Invoke-DawnstrikeContractCli `
                        $pythonPath $runtime @("verify-receipt", "--receipt", $completeReceipt, "--expected-status", "COMPLETE") `
                        "Complete activation terminal reconciliation" $ProcessTimeoutSeconds
                    $terminalTask = Get-DawnstrikeTaskContract $runtime $state
                    $null = Assert-DawnstrikeActivationCompleteTerminal `
                        -Journal $terminalJournal -Receipt $terminalReceipt -ReceiptPath $completeReceipt `
                        -CandidateRoot $candidate -RuntimeRoot $runtime -StateRoot $state -BackupRoot $backupRoot `
                        -GitPath $gitPath -PythonPath $pythonPath -TimeoutSeconds $ProcessTimeoutSeconds `
                        -ExpectedSha $ExpectedSha -ExpectedTree ([string]$candidateContract.tree) `
                        -OriginIdentity $lockOrigin -MarketDate $MarketDate -StateDeclaration $stateDeclaration `
                        -ExpectedTask $terminalTask
                    return $terminalReceipt
                }
                catch {
                    $preserveLocks = $true
                    throw "Complete activation evidence could not be reconciled; operator recovery is required."
                }
            }
            if ($swapStarted -or $tasksDisabled) {
                try {
                    $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                    $tasksDisabled = $true
                    if ($stateDeclaration.required -and $auxiliaryBefore.present) {
                        $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                        $auxiliaryDisabled = $true
                    }
                }
                catch {
                    $preserveLocks = $true
                    throw "Runtime activation failed and exact task quiescence could not be proven; runtime recovery was not attempted."
                }
            }
            if ($swapStarted -or $tasksDisabled) {
                try {
                    if ($candidateInstalled -and (Test-Path -LiteralPath $runtime -PathType Container)) {
                        $failedCandidate = Join-Path $rollbackRoot "failed-candidate-runtime"
                        if (Test-Path -LiteralPath $failedCandidate) {
                            throw "Failed-candidate preservation path already exists."
                        }
                        [System.IO.Directory]::Move($runtime, $failedCandidate)
                    }
                    if (
                        -not (Test-Path -LiteralPath $runtime) -and
                        (Test-Path -LiteralPath $rollbackCheckout -PathType Container)
                    ) {
                        [System.IO.Directory]::Move($rollbackCheckout, $runtime)
                    }
                    $restoredRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $runtimeContract.head
                    if ($restoredRuntime.tree -ne $runtimeContract.tree) {
                        throw "Automatic restore did not recover the previous runtime tree."
                    }
                    if ($tasksDisabled) {
                        $null = Assert-DawnstrikeTaskXmlBackup `
                            -StateRoot $state `
                            -BackupName $taskBackup.backup_name `
                            -ExpectedManifestSha256 $taskBackup.manifest_sha256 `
                            -ExpectedTaskContractSha256 ([string]$taskLocked.task_contract_sha256) `
                            -ExpectedTaskDefinitionContractSha256 ([string]$taskLocked.task_definition_contract_sha256) `
                            -ExpectedTaskActionContractSha256 ([string]$taskLocked.task_action_contract_sha256)
                        $restoredDisabledTasks = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
                        if (
                            $restoredDisabledTasks.disabled_count -ne 5 -or
                            $restoredDisabledTasks.enabled_count -ne 0 -or
                            $restoredDisabledTasks.task_definition_contract_sha256 -ne
                                $taskLocked.task_definition_contract_sha256
                        ) {
                            throw "Automatic restore did not recover exact disabled task definitions."
                        }
                        if ($stateDeclaration.required) {
                            $null = Restore-DawnstrikeAuxiliaryCaptureTask `
                                -Expected $auxiliaryBefore `
                                -RuntimeRoot $runtime `
                                -StateRoot $state `
                                -RunAsCredential $RunAsCredential
                            $auxiliaryDisabled = $false
                        }
                        $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -AllowDisabled
                        Set-DawnstrikeCanonicalTaskExpectedSha -RuntimeRoot $runtime -StateRoot $state -ExpectedSha ([string]$runtimeContract.head)
                        Enable-DawnstrikeCanonicalTasks
                        $restoredTasks = Get-DawnstrikeTaskContract $runtime $state
                        $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime -StateRoot $state -ExpectedSha ([string]$runtimeContract.head)
                        $tasksDisabled = $false
                    }
                }
                catch {
                    try {
                        $null = Set-DawnstrikeTasksFailClosedDisabled $runtime $state
                        $tasksDisabled = $true
                        if ($stateDeclaration.required -and $auxiliaryBefore.present) {
                            $null = Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state
                            $auxiliaryDisabled = $true
                        }
                        # The journal still describes a nonterminal transaction.
                        # Even when the fail-closed boundary is proven, releasing
                        # either lock would orphan the only admissible recovery.
                        $preserveLocks = $true
                    }
                    catch {
                        $preserveLocks = $true
                        throw "Runtime activation and automatic restore failed; exact task state is unverified and operator recovery is required."
                    }
                throw "Runtime activation failed and automatic restore could not be completed; canonical tasks are proven Disabled and the prepared receipt/rollback tool are required. Original failure: $($failure.Exception.Message)"
                }
            }
            # Once the previous runtime and exact Ready task contract have been
            # restored, finish the transaction as an immutable compensation.
            # Do not leave PRE_SWAP/POST_SWAP evidence behind: those phases
            # require Disabled tasks and candidate/rollback checkout shape.
            if ($journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY") -and -not $tasksDisabled) {
                try {
                    $compensatedRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds $runtimeContract.head
                    $compensatedTasks = Get-DawnstrikeTaskContract $runtime $state
                    if ($compensatedRuntime.tree -ne [string]$runtimeContract.tree -or
                        $compensatedTasks.task_contract_sha256 -ne [string]$taskLocked.task_contract_sha256) {
                        throw "Automatic restore did not prove the exact original Ready boundary."
                    }
                    $journalBefore = Get-DawnstrikeStrictRuntimeOperationJournal `
                        $operationJournal $lockInterpreter.path $lockInterpreter.sha256
                    $compensationAttemptKey = [string]$journalBefore.raw_file_sha256
                    $compensationReceiptRelative = "receipts/runtime-activation/runtime-activation-$activationId.compensated-$compensationAttemptKey.json"
                    $compensationReceipt = Join-Path $state ($compensationReceiptRelative.Replace('/', '\'))
                    $failureReceipt = Join-Path $receiptRoot "runtime-activation-$activationId.failed-$compensationAttemptKey.json"
                    Assert-DawnstrikeNoReparseComponents $compensationReceipt "Activation compensation receipt"
                    Assert-DawnstrikeNoReparseComponents $failureReceipt "Activation failure receipt"
                    $empty = Get-DawnstrikeSha256Text ""
                    $priorReceiptRelative = "NONE"
                    $priorReceiptHash = $empty
                    if (Test-Path -LiteralPath $preparedReceipt -PathType Leaf) {
                        $priorReceiptRelative = $preparedReceiptRelative
                        $priorReceiptHash = Get-DawnstrikeSha256File $preparedReceipt
                    }
                    $failurePayload = [ordered]@{
                        schema_version = "dawnstrike.runtime_activation_failure.v1"
                        status = "FAILED_RESTORED_EXACT_READY"
                        activation_id = $activationId
                        candidate_sha = $ExpectedSha
                        candidate_tree = [string]$candidateContract.tree
                        restored_sha = [string]$runtimeContract.head
                        restored_tree = [string]$runtimeContract.tree
                        restored_origin_identity = [string]$previousRuntimeOriginIdentity
                        restored_origin_sha256 = [string]$previousRuntimeOriginSha256
                        restored_task_contract_sha256 = [string]$compensatedTasks.task_contract_sha256
                        failure_phase = $journalPhase
                        failure_type = $failure.Exception.GetType().Name
                        recovery_evidence = "EXACT_PREVIOUS_RUNTIME_AND_READY_TASKS"
                        research_only = $true
                        broker_execution_enabled = $false
                    }
                    Write-DawnstrikeActivationJson $failurePayload $failureReceipt
                    $compensationInput = "$compensationReceipt.$([guid]::NewGuid().ToString('N')).input.json"
                    $compensationPayload = [ordered]@{
                        schema_version = "dawnstrike.runtime_compensation_receipt.v1"
                        status = "COMPENSATED"
                        operation = "runtime_activation"
                        candidate_sha = $ExpectedSha
                        candidate_tree = [string]$candidateContract.tree
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
                        if ($LASTEXITCODE -ne 0) { throw "Activation compensation receipt strict sealing failed." }
                    }
                    finally { if (Test-Path -LiteralPath $compensationInput) { Remove-Item -LiteralPath $compensationInput -Force } }
                    $compensationHash = Get-DawnstrikeSha256File $compensationReceipt
                    $null = Set-DawnstrikeRuntimeOperationJournalPhase `
                        -StateRoot $state -JournalPath $operationJournal -Lock $activationLock `
                        -Operation runtime_activation -Phase COMPENSATED `
                        -CandidateSha $ExpectedSha -CandidateTree ([string]$candidateContract.tree) `
                        -CurrentSha ([string]$runtimeContract.head) -CurrentTree ([string]$runtimeContract.tree) `
                        -PreviousSha ([string]$runtimeContract.head) -PreviousTree ([string]$runtimeContract.tree) `
                        -OriginIdentity $lockOrigin -PreparedReceiptRelativePath $preparedReceiptRelative `
                        -PreparedReceiptSha256 (if (Test-Path -LiteralPath $preparedReceipt -PathType Leaf) { Get-DawnstrikeSha256File $preparedReceipt } else { $empty }) `
                        -CompleteReceiptRelativePath $readyReceiptRelative -CompleteReceiptSha256 $empty `
                        -BackupContractSha256 ([string]$journalBefore.payload.backup_contract_sha256) `
                        -TaskContractSha256 ([string]$compensatedTasks.task_contract_sha256) `
                        -RuntimeStageContractSha256 $empty `
                        -CompensationReceiptRelativePath $compensationReceiptRelative -CompensationReceiptSha256 $compensationHash `
                        -PythonPath $lockInterpreter.path -PythonSha256 $lockInterpreter.sha256
                    $journalPhase = "COMPENSATED"
                }
                catch {
                    $preserveLocks = $true
                    throw "Runtime activation compensation could not be sealed after exact restore; operator recovery is required."
                }
            }
            if ($journalPhase -in @("PRE_QUIESCE", "PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")) {
                if ($null -eq $activationLock -or $null -eq $dailyLock) {
                    throw "Nonterminal activation recovery lacks its adoptable lock pair."
                }
                if (
                    -not (Test-Path -LiteralPath $activationLock.path -PathType Leaf) -or
                    -not (Test-Path -LiteralPath $dailyLock.lock_path -PathType Leaf)
                ) { throw "Nonterminal activation recovery lock pair was not preserved." }
                # Recovery artifacts are still bound to this journal phase.
                # Releasing either lock would make the next invocation reject
                # the otherwise valid nonterminal evidence as orphaned.
                $preserveLocks = $true
            }
            throw $failure
        }
        finally {
            if (-not $preserveLocks) {
                if ($null -ne $dailyLock) {
                    Exit-DawnstrikeDailyRunLock -Lock $dailyLock
                    $dailyLock = $null
                }
                # Keep INIT's runtime lock for the outer staging cleanup. It
                # releases this final lock before removing the journal so a
                # crash leaves an adoptable INIT tombstone, without a
                # double-release on ordinary pre-transition failures.
                if ($activationBodyStarted) {
                    Exit-DawnstrikeGovernedRuntimeLock $activationLock
                    $activationLock = $null
                }
            }
        }
    }
    catch {
        $stagingFailure = $_
        if (-not $activationBodyStarted -and -not $preserveLocks -and $null -ne $activationLock) {
            try {
                $stagingJournal = Get-DawnstrikeStrictRuntimeOperationJournal `
                    $operationJournal $lockInterpreter.path $lockInterpreter.sha256
                if (
                    [string]$stagingJournal.payload.operation -ne "runtime_activation" -or
                    [string]$stagingJournal.payload.phase -ne "INIT" -or
                    [string]$stagingJournal.payload.candidate_sha -ne $ExpectedSha -or
                    [string]$stagingJournal.payload.candidate_tree -ne [string]$candidateContract.tree -or
                    [string]$stagingJournal.payload.lock_token -ne [string]$activationLock.token -or
                    [string]$stagingJournal.payload.lock_file_sha256 -ne [string]$activationLock.bytes_sha256
                ) { throw "Staging failure journal identity is invalid." }
                $stagingRuntime = Get-DawnstrikeGitContract $gitPath $runtime $ProcessTimeoutSeconds ([string]$runtimeContract.head)
                $stagingTasks = Get-DawnstrikeTaskContract $runtime $state
                if (
                    $stagingRuntime.tree -ne [string]$runtimeContract.tree -or
                    $stagingTasks.task_contract_sha256 -ne [string]$taskBefore.task_contract_sha256
                ) { throw "Runtime or task state changed during failed staging." }
                if (Test-Path -LiteralPath $stage) {
                    Assert-DawnstrikeNoReparseComponents $stage "Failed activation stage"
                    $failedStageItem = Get-Item -LiteralPath $stage -Force -ErrorAction Stop
                    if (-not $failedStageItem.PSIsContainer -or ($failedStageItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                        throw "Failed activation stage is not a safe directory."
                    }
                    $failedStageQuarantineRoot = Join-Path $state "recovery-quarantine"
                    Assert-DawnstrikeNoReparseComponents $failedStageQuarantineRoot "Failed stage quarantine"
                    New-Item -ItemType Directory -Path $failedStageQuarantineRoot -Force | Out-Null
                    $failedStageQuarantine = Join-Path $failedStageQuarantineRoot ("runtime-activation-$activationId-stage-failure-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))")
                    Assert-DawnstrikeNoReparseComponents $failedStageQuarantine "Failed stage quarantine destination"
                    Move-Item -LiteralPath $stage -Destination $failedStageQuarantine -ErrorAction Stop
                    if (Test-Path -LiteralPath $stage) { throw "Failed activation stage quarantine did not complete." }
                }
                if (Test-Path -LiteralPath $schedulerBackupPath) {
                    # Task-backup creation can finish its final rename and
                    # still throw before PRE_QUIESCE is sealed.  Preserve that
                    # exact INIT-bound artifact so it cannot strand the next
                    # activation behind the partial-artifact guard.
                    Assert-DawnstrikeNoReparseComponents $schedulerBackupPath "Failed scheduler backup"
                    $failedSchedulerBackupItem = Get-Item -LiteralPath $schedulerBackupPath -Force -ErrorAction Stop
                    if (
                        -not $failedSchedulerBackupItem.PSIsContainer -or
                        ($failedSchedulerBackupItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
                    ) { throw "Failed scheduler backup is not a safe directory." }
                    $failedSchedulerQuarantineRoot = Join-Path $state "recovery-quarantine"
                    Assert-DawnstrikeNoReparseComponents $failedSchedulerQuarantineRoot "Failed scheduler backup quarantine"
                    New-Item -ItemType Directory -Path $failedSchedulerQuarantineRoot -Force | Out-Null
                    $failedSchedulerQuarantine = Join-Path $failedSchedulerQuarantineRoot ("runtime-activation-$activationId-scheduler-backup-failure-$([DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))")
                    Assert-DawnstrikeNoReparseComponents $failedSchedulerQuarantine "Failed scheduler backup quarantine destination"
                    Move-Item -LiteralPath $schedulerBackupPath -Destination $failedSchedulerQuarantine -ErrorAction Stop
                    if (Test-Path -LiteralPath $schedulerBackupPath) { throw "Failed scheduler backup quarantine did not complete." }
                }
                Exit-DawnstrikeGovernedRuntimeLock $activationLock
                $activationLock = $null
                Remove-Item -LiteralPath $operationJournal -Force
                if (Test-Path -LiteralPath $operationJournal) { throw "Failed staging INIT journal cleanup did not complete." }
            }
            catch {
                throw "Candidate staging failed and its exact INIT recovery cleanup also failed; governed evidence was preserved. Original failure: $($stagingFailure.Exception.Message) Cleanup failure: $($_.Exception.Message)"
            }
        }
        throw $stagingFailure
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    if (
        [string]::IsNullOrWhiteSpace($ExpectedSha) -or
        [string]::IsNullOrWhiteSpace($MarketDate) -or
        [string]::IsNullOrWhiteSpace($CiEvidencePath) -or
        [string]::IsNullOrWhiteSpace($SolEvidencePath)
    ) {
        throw "ExpectedSha, MarketDate, CiEvidencePath, and SolEvidencePath are required."
    }
    if ([string]::IsNullOrWhiteSpace($CandidateRoot)) {
        $CandidateRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    }
    $result = Invoke-DawnstrikeRuntimeActivation `
        -ExpectedSha $ExpectedSha `
        -MarketDate $MarketDate `
        -CiEvidencePath $CiEvidencePath `
        -SolEvidencePath $SolEvidencePath `
        -CandidateRoot $CandidateRoot `
        -RuntimeRoot $RuntimeRoot `
        -StateRoot $StateRoot `
        -BackupRoot $BackupRoot `
        -BackupRetention $BackupRetention `
        -ProcessTimeoutSeconds $ProcessTimeoutSeconds `
        -RunAsCredential $RunAsCredential `
        -PreflightOnly:$PreflightOnly `
        -InjectCrashBetweenRuntimeRenames:$InjectCrashBetweenRuntimeRenames `
        -TestStageCrashPoint $TestStageCrashPoint `
        -TestNowUtc $TestNowUtc
    $result | ConvertTo-Json -Depth 12
}
