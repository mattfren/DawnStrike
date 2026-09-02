[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [Parameter(Mandatory = $true)][string]$CandidateRoot,
    [string]$DependencySourcePython = 'C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe',
    [string]$InstallRoot = 'C:\Program Files\Dawnstrike',
    [string]$ReceiptRoot = 'C:\ProgramData\Dawnstrike'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Dawnstrike host-boundary installation requires an elevated administrator process.'
}
if ($InstallRoot -cne 'C:\Program Files\Dawnstrike' -or $ReceiptRoot -cne 'C:\ProgramData\Dawnstrike') {
    throw 'Dawnstrike host-boundary installation paths are fixed host trust anchors.'
}

$candidate = [IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
$git = 'C:\Program Files\Git\cmd\git.exe'
$expectedGitHash = '37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9' # pragma: allowlist secret
$expectedPythonHash = 'ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1' # pragma: allowlist secret
$expectedPythonSubject = 'CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US'
$expectedPythonThumbprint = '9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48' # pragma: allowlist secret
$pythonInstallerUri = 'https://www.python.org/ftp/python/3.13.14/python-3.13.14-amd64.exe'
$expectedPythonInstallerHash = 'c54d9b9bbb8a36e6489363ddd01139707fd781d72f1f9e90c7ec65d0061368e0' # pragma: allowlist secret
$expectedUvHash = '268cd62b99395eb53825795518e067e4b27ec4b445175df343824689f307c807' # pragma: allowlist secret
$installerRelative = 'scripts/install_dawnstrike_host_boundary.ps1'
$launcherRelative = 'scripts/dawnstrike_release_launcher.ps1'
$installerDestination = 'C:\Program Files\Dawnstrike\bin\install_dawnstrike_host_boundary.ps1'
if ([IO.Path]::GetFullPath($PSCommandPath) -cne $installerDestination) {
    throw 'Dawnstrike host-boundary installation must run from the protected installed bootstrap path.'
}
$pythonSource = [IO.Path]::GetFullPath($DependencySourcePython)
$pythonSourceRoot = Split-Path -Parent $pythonSource
$pythonDestination = Join-Path $InstallRoot 'Python313'
$launcherDestination = Join-Path $InstallRoot 'bin\dawnstrike_release_launcher.ps1'

function Get-DawnstrikeInstallSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($Path)))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Copy-DawnstrikePinnedFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedSha256
    )

    $sourceHandle = [IO.File]::Open(
        $Source,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $actual = ([BitConverter]::ToString($sha.ComputeHash($sourceHandle))).Replace('-', '').ToLowerInvariant()
        }
        finally { $sha.Dispose() }
        if ($actual -cne $ExpectedSha256) { throw 'Pinned file source digest is invalid.' }
        $sourceHandle.Position = 0
        $destinationHandle = [IO.File]::Open(
            $Destination,
            [IO.FileMode]::Create,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try { $sourceHandle.CopyTo($destinationHandle) }
        finally { $destinationHandle.Dispose() }
    }
    finally { $sourceHandle.Dispose() }
}

function Set-DawnstrikeProtectedDirectoryAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
    $users = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545')
    $inheritance = [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit
    $none = [Security.AccessControl.PropagationFlags]::None
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($administrators)
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($administrators, 'FullControl', $inheritance, $none, 'Allow'))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($system, 'FullControl', $inheritance, $none, 'Allow'))
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($users, 'ReadAndExecute', $inheritance, $none, 'Allow'))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Assert-DawnstrikeInstalledBoundaryAcl {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string[]]$Paths)

    $writeLikeRights = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Modify -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership -bor
        [Security.AccessControl.FileSystemRights]::FullControl
    )
    foreach ($path in $Paths) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Installed Dawnstrike host boundary contains a reparse point.'
        }
        $acl = Get-Acl -LiteralPath $path -ErrorAction Stop
        if ([string]$acl.Owner -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$') {
            throw 'Installed Dawnstrike host boundary is not administrator-owned.'
        }
        foreach ($rule in @($acl.Access)) {
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                [string]$rule.IdentityReference -notmatch '(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$' -and
                ($rule.FileSystemRights -band $writeLikeRights) -ne 0
            ) {
                throw 'Installed Dawnstrike host boundary grants non-admin write access.'
            }
        }
    }
}

function Copy-DawnstrikeExactGitFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $copyGitEnvironment = @{}
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        $copyGitEnvironment[[string]$entry.Name] = [string]$entry.Value
        Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction Stop
    }
    try {
        $env:GIT_CONFIG_NOSYSTEM = '1'
        $env:GIT_CONFIG_SYSTEM = 'NUL'
        $env:GIT_CONFIG_GLOBAL = 'NUL'
        $env:GIT_ATTR_NOSYSTEM = '1'
        $env:GIT_NO_REPLACE_OBJECTS = '1'
        $source = Join-Path $candidate $RelativePath
        $expectedBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $RelativePath)).Trim().ToLowerInvariant()
        if ($LASTEXITCODE -ne 0 -or $expectedBlob -notmatch '^[0-9a-f]{40}$') {
            throw "Exact candidate file is absent: $RelativePath"
        }
        $sourceHandle = [IO.File]::Open($source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        try {
            $actualBlob = (& $git @safeGit hash-object ('--path=' + $RelativePath) $source).Trim().ToLowerInvariant()
            if ($LASTEXITCODE -ne 0 -or $actualBlob -cne $expectedBlob) {
                throw "Candidate file bytes differ from the exact Git object: $RelativePath"
            }
            $sourceHandle.Position = 0
            $destinationHandle = [IO.File]::Open(
                $Destination,
                [IO.FileMode]::Create,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            try { $sourceHandle.CopyTo($destinationHandle) }
            finally { $destinationHandle.Dispose() }
        }
        finally { $sourceHandle.Dispose() }
    }
    finally {
        foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
            Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction SilentlyContinue
        }
        foreach ($name in $copyGitEnvironment.Keys) {
            Set-Item -LiteralPath ('Env:' + $name) -Value $copyGitEnvironment[$name]
        }
    }
}

if ((Get-DawnstrikeInstallSha256 $git) -cne $expectedGitHash) {
    throw 'Host-boundary installer rejected the pinned Git executable.'
}
$savedGitEnvironment = @{}
foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
    $savedGitEnvironment[[string]$entry.Name] = [string]$entry.Value
    Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction Stop
}
try {
    $env:GIT_CONFIG_NOSYSTEM = '1'
    $env:GIT_CONFIG_SYSTEM = 'NUL'
    $env:GIT_CONFIG_GLOBAL = 'NUL'
    $env:GIT_ATTR_NOSYSTEM = '1'
    $env:GIT_NO_REPLACE_OBJECTS = '1'
    $safeGit = @('-c', 'core.autocrlf=true', '-c', 'core.fsmonitor=false', '-c', 'core.untrackedCache=false', '-c', 'core.hooksPath=NUL', '-c', 'core.attributesFile=NUL', '-c', 'protocol.ext.allow=never', '-C', $candidate)
    $head = (& $git @safeGit rev-parse HEAD).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $head -cne $ExpectedSha) { throw 'Host-boundary installer candidate SHA is invalid.' }
    $remoteMain = (& $git @safeGit rev-parse refs/remotes/origin/main).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $remoteMain -cne $ExpectedSha) { throw 'Host-boundary installer requires exact origin/main identity.' }
    $status = ((& $git @safeGit status --porcelain=v1 --untracked-files=all --ignore-submodules=none) -join "`n").Trim()
    if ($LASTEXITCODE -ne 0 -or $status) { throw 'Host-boundary installer requires a clean candidate checkout.' }
    $launcherBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $launcherRelative)).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $launcherBlob -notmatch '^[0-9a-f]{40}$') { throw 'Host-boundary launcher is absent from the exact candidate.' }
    $expectedInstallerBlob = (& $git @safeGit rev-parse ($ExpectedSha + ':' + $installerRelative)).Trim().ToLowerInvariant()
    $installedInstallerBlob = (& $git @safeGit hash-object ('--path=' + $installerRelative) $installerDestination).Trim().ToLowerInvariant()
    if (
        $LASTEXITCODE -ne 0 -or
        $expectedInstallerBlob -notmatch '^[0-9a-f]{40}$' -or
        $installedInstallerBlob -cne $expectedInstallerBlob
    ) {
        throw 'Protected host-boundary installer bytes do not match the exact candidate.'
    }
}
finally {
    foreach ($entry in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'GIT_*' })) {
        Remove-Item -LiteralPath ('Env:' + [string]$entry.Name) -ErrorAction SilentlyContinue
    }
    foreach ($name in $savedGitEnvironment.Keys) { Set-Item -LiteralPath ('Env:' + $name) -Value $savedGitEnvironment[$name] }
}

Assert-DawnstrikeInstalledBoundaryAcl -Paths @($InstallRoot, (Join-Path $InstallRoot 'bin'), $installerDestination)
if ((Get-DawnstrikeInstallSha256 $pythonSource) -cne $expectedPythonHash) {
    throw 'Host-boundary installer rejected the dependency-source Python hash.'
}
$pythonSignature = Get-AuthenticodeSignature -LiteralPath $pythonSource -ErrorAction Stop
if (
    [string]$pythonSignature.Status -cne 'Valid' -or
    $null -eq $pythonSignature.SignerCertificate -or
    [string]$pythonSignature.SignerCertificate.Subject -cne $expectedPythonSubject -or
    [string]$pythonSignature.SignerCertificate.Thumbprint -cne $expectedPythonThumbprint
) {
    throw 'Host-boundary installer rejected the dependency-source Python signer.'
}
$dependencySourcePaths = @(
    (Join-Path $pythonSourceRoot 'Lib\site-packages')
)
foreach ($dependencySourcePath in $dependencySourcePaths) {
    $sourceReparse = Get-ChildItem -LiteralPath $dependencySourcePath -Recurse -Force -ErrorAction Stop | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    } | Select-Object -First 1
    if ($null -ne $sourceReparse) {
        throw 'Host-boundary installer refuses a reparse point in the dependency-source Python paths.'
    }
}
$uvSource = Join-Path $pythonSourceRoot 'Scripts\uv.exe'
$uvSourceItem = Get-Item -LiteralPath $uvSource -Force -ErrorAction Stop
if (($uvSourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'Host-boundary installer refuses a reparse point for the dependency-source uv executable.'
}

if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $InstallRoot
}
Set-DawnstrikeProtectedDirectoryAcl -Path $InstallRoot
$binRoot = Join-Path $InstallRoot 'bin'
if (-not (Test-Path -LiteralPath $binRoot -PathType Container)) { $null = New-Item -ItemType Directory -Path $binRoot }
Set-DawnstrikeProtectedDirectoryAcl -Path $binRoot

if (Test-Path -LiteralPath $pythonDestination) {
    if (-not (Test-Path -LiteralPath $pythonDestination -PathType Container)) {
        throw 'Protected Python destination exists but is not a directory.'
    }
    $installedPython = Join-Path $pythonDestination 'python.exe'
    if (
        -not (Test-Path -LiteralPath $installedPython -PathType Leaf) -or
        (Get-DawnstrikeInstallSha256 $installedPython) -cne $expectedPythonHash
    ) {
        throw 'Existing protected Python destination is incomplete or unapproved.'
    }
}
else {
    $downloadRoot = Join-Path $InstallRoot ('.host-boundary-download-' + [Guid]::NewGuid().ToString('N'))
    $pythonInstaller = Join-Path $downloadRoot 'python-3.13.14-amd64.exe'
    $null = New-Item -ItemType Directory -Path $downloadRoot
    Set-DawnstrikeProtectedDirectoryAcl -Path $downloadRoot
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $pythonInstallerUri -OutFile $pythonInstaller
        & C:\Windows\System32\icacls.exe $pythonInstaller /setowner '*S-1-5-32-544' /C /Q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Python installer ownership hardening failed.' }
        Assert-DawnstrikeInstalledBoundaryAcl -Paths @($downloadRoot, $pythonInstaller)
        if ((Get-DawnstrikeInstallSha256 $pythonInstaller) -cne $expectedPythonInstallerHash) {
            throw 'Downloaded Python installer does not match the official pinned digest.'
        }
        $installerSignature = Get-AuthenticodeSignature -LiteralPath $pythonInstaller -ErrorAction Stop
        if (
            [string]$installerSignature.Status -cne 'Valid' -or
            $null -eq $installerSignature.SignerCertificate -or
            [string]$installerSignature.SignerCertificate.Subject -cne $expectedPythonSubject -or
            [string]$installerSignature.SignerCertificate.Thumbprint -cne $expectedPythonThumbprint
        ) {
            throw 'Downloaded Python installer does not match the approved signer.'
        }
        $pythonInstall = Start-Process -FilePath $pythonInstaller -ArgumentList @(
            '/quiet',
            'InstallAllUsers=1',
            ('TargetDir=' + $pythonDestination),
            'Include_launcher=0',
            'Include_test=0',
            'Include_doc=0',
            'Include_tcltk=0',
            'PrependPath=0',
            'Shortcuts=0'
        ) -Wait -PassThru -WindowStyle Hidden
        if ($pythonInstall.ExitCode -ne 0) {
            throw "Official Python installation failed with exit $($pythonInstall.ExitCode)."
        }
    }
    finally {
        if (Test-Path -LiteralPath $downloadRoot) {
            Remove-Item -LiteralPath $downloadRoot -Recurse -Force
        }
    }
}

$sitePackagesSource = Join-Path $pythonSourceRoot 'Lib\site-packages'
$sitePackagesDestination = Join-Path $pythonDestination 'Lib\site-packages'
& C:\Windows\System32\robocopy.exe $sitePackagesSource $sitePackagesDestination /E /COPY:DAT /DCOPY:DAT /R:1 /W:1 /XJ /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -gt 7) { throw "Protected dependency copy failed with robocopy exit $LASTEXITCODE." }
$scriptsSource = Join-Path $pythonSourceRoot 'Scripts'
$scriptsDestination = Join-Path $pythonDestination 'Scripts'
if (-not (Test-Path -LiteralPath $scriptsDestination -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $scriptsDestination
}
Copy-DawnstrikePinnedFile -Source (Join-Path $scriptsSource 'uv.exe') `
    -Destination (Join-Path $scriptsDestination 'uv.exe') -ExpectedSha256 $expectedUvHash
& C:\Windows\System32\icacls.exe $pythonDestination /setowner '*S-1-5-32-544' /T /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Protected Python ownership hardening failed.' }

Copy-DawnstrikeExactGitFile -RelativePath $launcherRelative -Destination $launcherDestination
& C:\Windows\System32\icacls.exe $launcherDestination /setowner '*S-1-5-32-544' /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Trusted release launcher ownership hardening failed.' }

$criticalPaths = @(
    $InstallRoot,
    $binRoot,
    $launcherDestination,
    $pythonDestination,
    (Join-Path $pythonDestination 'python.exe'),
    (Join-Path $pythonDestination 'python3.dll'),
    (Join-Path $pythonDestination 'python313.dll'),
    (Join-Path $pythonDestination 'DLLs'),
    (Join-Path $pythonDestination 'DLLs\_hashlib.pyd'),
    (Join-Path $pythonDestination 'Lib'),
    (Join-Path $pythonDestination 'Lib\hashlib.py'),
    (Join-Path $pythonDestination 'Lib\site-packages'),
    (Join-Path $pythonDestination 'Scripts'),
    (Join-Path $pythonDestination 'Scripts\uv.exe')
)
Assert-DawnstrikeInstalledBoundaryAcl -Paths $criticalPaths
if ((Get-DawnstrikeInstallSha256 (Join-Path $pythonDestination 'python.exe')) -cne $expectedPythonHash) {
    throw 'Protected Python bytes differ from the approved interpreter.'
}
$bootstrapRoot = Join-Path $InstallRoot ('.host-boundary-verification-' + [Guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $bootstrapRoot
Set-DawnstrikeProtectedDirectoryAcl -Path $bootstrapRoot
$bootstrap = Join-Path $bootstrapRoot 'dawnstrike_python_bootstrap.py'
$verificationTarget = Join-Path $bootstrapRoot 'verify_dawnstrike_python_environment.py'
Copy-DawnstrikeExactGitFile -RelativePath 'scripts/dawnstrike_python_bootstrap.py' -Destination $bootstrap
Copy-DawnstrikeExactGitFile -RelativePath 'scripts/verify_dawnstrike_python_environment.py' -Destination $verificationTarget
$bootstrapHash = Get-DawnstrikeInstallSha256 $bootstrap
$preloader = "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw(RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
try {
    $verificationOutput = @(
        & (Join-Path $pythonDestination 'python.exe') `
            -I -B -S -c $preloader $bootstrap $bootstrapHash `
            --release-root $candidate --expected-sha $ExpectedSha `
            --script $verificationTarget -- 2>&1
    )
    if ($LASTEXITCODE -ne 0) { throw 'Protected Python dependency verification failed.' }
    try { $verification = (($verificationOutput | ForEach-Object { [string]$_ }) -join "`n").Trim() | ConvertFrom-Json }
    catch { throw 'Protected Python dependency verification did not return valid JSON.' }
    if (
        [string]$verification.schema_version -cne 'dawnstrike.protected_python_verification.v1' -or
        [string]$verification.status -cne 'PASS' -or
        $verification.research_only -ne $true -or
        $verification.broker_execution_enabled -ne $false
    ) {
        throw 'Protected Python dependency verification returned an invalid safety contract.'
    }
}
finally {
    if (Test-Path -LiteralPath $bootstrapRoot) {
        Remove-Item -LiteralPath $bootstrapRoot -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $ReceiptRoot -PathType Container)) {
    $null = New-Item -ItemType Directory -Path $ReceiptRoot
}
Set-DawnstrikeProtectedDirectoryAcl -Path $ReceiptRoot
$receiptPath = Join-Path $ReceiptRoot ('host-boundary-' + $ExpectedSha + '.json')
$receipt = [ordered]@{
    schema_version = 'dawnstrike.host_boundary_installation.v1'
    candidate_sha = $ExpectedSha
    installed_at_utc = [DateTime]::UtcNow.ToString('o')
    installer_principal = [string]$identity.Name
    install_root = $InstallRoot
    installer_path = $installerDestination
    installer_sha256 = Get-DawnstrikeInstallSha256 $installerDestination
    python_path = (Join-Path $pythonDestination 'python.exe')
    python_sha256 = $expectedPythonHash
    python_installer_uri = $pythonInstallerUri
    python_installer_sha256 = $expectedPythonInstallerHash
    uv_sha256 = $expectedUvHash
    launcher_path = $launcherDestination
    launcher_sha256 = Get-DawnstrikeInstallSha256 $launcherDestination
    dependency_verification = 'PASS'
    research_only = $true
    broker_execution_enabled = $false
}
$json = $receipt | ConvertTo-Json -Depth 4
[IO.File]::WriteAllText($receiptPath, $json + "`r`n", [Text.UTF8Encoding]::new($false))
& C:\Windows\System32\icacls.exe $receiptPath /setowner '*S-1-5-32-544' /C /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Host-boundary receipt ownership hardening failed.' }
Assert-DawnstrikeInstalledBoundaryAcl -Paths @($receiptPath)
$receipt | ConvertTo-Json -Depth 4
