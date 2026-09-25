[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSha,
    [Parameter(Mandatory = $true)][string]$CandidateRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedInstallerSha256,
    [Parameter(Mandatory = $true)][ValidateRange(1, 10485760)][long]$ExpectedInstallerLength,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{64}$')]
    [string]$ExpectedPowerShellBoundarySha256,
    [Parameter(Mandatory = $true)][ValidateRange(1, 1048576)]
    [long]$ExpectedPowerShellBoundaryLength,
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$BoundaryPredecessorSha = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$BoundaryPredecessorTree = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$RuntimePredecessorSha = '',
    [ValidatePattern('^$|^[0-9a-f]{40}$')][string]$RuntimePredecessorTree = ''
)

$ErrorActionPreference = 'Stop'

# The outer bridge can run in an existing user scope. Until the fixed System32
# child establishes the sealed module boundary, use only language constructs
# and .NET APIs so inherited functions/aliases cannot replace the UAC command.
function Get-DawnstrikeFrozenBootstrapContract {
    param(
        [Parameter(Mandatory = $true)][IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][long]$ExpectedLength,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if (-not $Stream.CanRead -or -not $Stream.CanSeek) {
        throw "$Label must be held by a readable, seekable stream."
    }
    if ($Stream.Length -ne $ExpectedLength) {
        throw "$Label byte length differs from the independent release value."
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $Stream.Position = 0
        $actual = ([BitConverter]::ToString($sha.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
        $Stream.Position = 0
    }
    if ($actual -cne $ExpectedSha256) {
        throw "$Label SHA-256 differs from the independent release value."
    }
    return [pscustomobject]@{ length = $Stream.Length; sha256 = $actual }
}

function ConvertTo-DawnstrikeBootstrapLiteral {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    return "'" + $Value.Replace("'", "''") + "'"
}

function Resolve-DawnstrikeBootstrapGit {
    $pathValue = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    foreach ($rawDirectory in @([string]$pathValue -split ';')) {
        $directory = [Environment]::ExpandEnvironmentVariables(
            ([string]$rawDirectory).Trim().Trim('"')
        )
        if ([string]::IsNullOrWhiteSpace($directory)) { continue }
        try { $candidate = [IO.Path]::GetFullPath([IO.Path]::Combine($directory, 'git.exe')) }
        catch { continue }
        if ([IO.File]::Exists($candidate)) { return $candidate }
    }
    throw 'Host bootstrap could not locate Git for untrusted exact-byte extraction.'
}

function Copy-DawnstrikeBootstrapGitBlob {
    param(
        [Parameter(Mandatory = $true)][string]$GitPath,
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][IO.Stream]$Destination
    )
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $GitPath
    $start.WorkingDirectory = $Root
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.Arguments = @(
        '-c core.longpaths=true', '-c core.autocrlf=false',
        '-c core.fsmonitor=false', '-c core.hooksPath=NUL',
        '-c core.attributesFile=NUL', '-c protocol.ext.allow=never',
        'cat-file blob', ($Commit + ':' + $RelativePath)
    ) -join ' '
    foreach ($name in @($start.EnvironmentVariables.Keys)) {
        if ([string]$name -like 'GIT_*') { $start.EnvironmentVariables.Remove([string]$name) }
    }
    $start.EnvironmentVariables['GIT_CONFIG_NOSYSTEM'] = '1'
    $start.EnvironmentVariables['GIT_CONFIG_SYSTEM'] = 'NUL'
    $start.EnvironmentVariables['GIT_CONFIG_GLOBAL'] = 'NUL'
    $start.EnvironmentVariables['GIT_ATTR_NOSYSTEM'] = '1'
    $start.EnvironmentVariables['GIT_NO_REPLACE_OBJECTS'] = '1'
    $process = [Diagnostics.Process]::new()
    try {
        $process.StartInfo = $start
        if (-not $process.Start()) { throw 'Exact bootstrap Git extraction did not start.' }
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardOutput.BaseStream.CopyTo($Destination)
        $process.WaitForExit()
        $stderr = $stderrTask.Result
        if ($process.ExitCode -ne 0) { throw "Exact bootstrap Git extraction failed: $stderr" }
        $Destination.Flush($true)
        $Destination.Position = 0
    }
    finally { $process.Dispose() }
}

if (
    [string]$PSVersionTable.PSEdition -cne 'Desktop' -or
    [int]$PSVersionTable.PSVersion.Major -ne 5 -or
    [int]$PSVersionTable.PSVersion.Minor -ne 1
) { throw 'Host bootstrap requires built-in Windows PowerShell 5.1 Desktop.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Host bootstrap must begin unelevated and cross UAC only through its encoded command.'
}
$migrationInputs = @(
    $BoundaryPredecessorSha, $BoundaryPredecessorTree,
    $RuntimePredecessorSha, $RuntimePredecessorTree
)
$migrationCount = 0
foreach ($migrationInput in $migrationInputs) {
    if (-not [string]::IsNullOrWhiteSpace([string]$migrationInput)) { $migrationCount++ }
}
if ($migrationCount -notin @(0, 4)) {
    throw 'Host bootstrap migration requires all four predecessor SHA/tree identities.'
}
$candidate = [IO.Path]::GetFullPath($CandidateRoot).TrimEnd('\')
if (
    -not [IO.Directory]::Exists($candidate) -or
    ([IO.File]::GetAttributes($candidate) -band [IO.FileAttributes]::ReparsePoint) -ne 0
) { throw 'Host bootstrap candidate is not a regular directory.' }
$git = Resolve-DawnstrikeBootstrapGit
$temporaryRoot = [IO.Path]::Combine(
    [IO.Path]::GetTempPath(), ('dawnstrike-host-bootstrap-' + [Guid]::NewGuid().ToString('N'))
)
$null = [IO.Directory]::CreateDirectory($temporaryRoot)
$frozenInstaller = [IO.Path]::Combine($temporaryRoot, 'install_dawnstrike_host_boundary.ps1')
$frozenBoundary = [IO.Path]::Combine($temporaryRoot, 'powershell_module_boundary.ps1')
$frozenInstallerStream = $null
$frozenBoundaryStream = $null
$frozenInstallerGuard = $null
$frozenBoundaryGuard = $null
try {
    $frozenInstallerStream = [IO.File]::Open(
        $frozenInstaller, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite, [IO.FileShare]::None
    )
    Copy-DawnstrikeBootstrapGitBlob `
        -GitPath $git -Root $candidate -Commit $ExpectedSha `
        -RelativePath 'scripts/install_dawnstrike_host_boundary.ps1' `
        -Destination $frozenInstallerStream
    $null = Get-DawnstrikeFrozenBootstrapContract `
        -Stream $frozenInstallerStream -ExpectedSha256 $ExpectedInstallerSha256 `
        -ExpectedLength $ExpectedInstallerLength -Label 'Frozen installer'
    $frozenInstallerStream.Dispose()
    $frozenInstallerStream = $null

    $frozenBoundaryStream = [IO.File]::Open(
        $frozenBoundary, [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite, [IO.FileShare]::None
    )
    Copy-DawnstrikeBootstrapGitBlob `
        -GitPath $git -Root $candidate -Commit $ExpectedSha `
        -RelativePath 'scripts/powershell_module_boundary.ps1' `
        -Destination $frozenBoundaryStream
    $null = Get-DawnstrikeFrozenBootstrapContract `
        -Stream $frozenBoundaryStream `
        -ExpectedSha256 $ExpectedPowerShellBoundarySha256 `
        -ExpectedLength $ExpectedPowerShellBoundaryLength `
        -Label 'Frozen PowerShell boundary'
    $frozenBoundaryStream.Dispose()
    $frozenBoundaryStream = $null

    # Retain both user-directory names across UAC. The elevated child also
    # verifies the exact independently supplied boundary and installer bytes.
    $frozenInstallerGuard = [IO.File]::Open(
        $frozenInstaller, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
    )
    $frozenBoundaryGuard = [IO.File]::Open(
        $frozenBoundary, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
    )

    $sourceLiteral = ConvertTo-DawnstrikeBootstrapLiteral $frozenInstaller
    $boundaryLiteral = ConvertTo-DawnstrikeBootstrapLiteral $frozenBoundary
    $shaLiteral = ConvertTo-DawnstrikeBootstrapLiteral $ExpectedSha
    $hashLiteral = ConvertTo-DawnstrikeBootstrapLiteral $ExpectedInstallerSha256
    $boundaryHashLiteral = ConvertTo-DawnstrikeBootstrapLiteral $ExpectedPowerShellBoundarySha256
    $candidateLiteral = ConvertTo-DawnstrikeBootstrapLiteral $candidate
    $boundaryShaLiteral = ConvertTo-DawnstrikeBootstrapLiteral $BoundaryPredecessorSha
    $boundaryTreeLiteral = ConvertTo-DawnstrikeBootstrapLiteral $BoundaryPredecessorTree
    $runtimeShaLiteral = ConvertTo-DawnstrikeBootstrapLiteral $RuntimePredecessorSha
    $runtimeTreeLiteral = ConvertTo-DawnstrikeBootstrapLiteral $RuntimePredecessorTree
    $elevatedCommand = @"
`$global:PSModuleAutoLoadingPreference='None';`$env:PSModulePath='C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
foreach(`$environmentName in @([Environment]::GetEnvironmentVariables('Process').Keys)){`$name=[string]`$environmentName;if(`$name-match'^(?i:COMPLUS_|COR_|CORECLR_|CSC|VBCS|MSBUILD|DOTNET_)'-or [string]::Equals(`$name,'__COMPAT_LAYER',[StringComparison]::OrdinalIgnoreCase)){throw ('Protected bootstrap inherited a forbidden CLR/compiler variable: '+`$name)}}
if([string][Environment]::GetEnvironmentVariable('PSModulePath','Process')-cne'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'-or [string][Environment]::GetEnvironmentVariable('PATH','Process')-cne'C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0'-or [string][Environment]::GetEnvironmentVariable('ComSpec','Process')-cne'C:\Windows\System32\cmd.exe'-or [string][Environment]::GetEnvironmentVariable('PATHEXT','Process')-cne'.COM;.EXE;.BAT;.CMD'){throw 'Protected bootstrap did not inherit the canonical system-only process environment.'}
`$boundaryPath=$boundaryLiteral;`$boundaryExpected=$boundaryHashLiteral;`$boundaryLength=$ExpectedPowerShellBoundaryLength
`$boundaryStream=[IO.File]::Open(`$boundaryPath,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);try{if(`$boundaryStream.Length-ne `$boundaryLength){throw 'Frozen PowerShell boundary length changed across UAC.'};`$boundaryBuffer=[IO.MemoryStream]::new();`$boundaryStream.CopyTo(`$boundaryBuffer);`$boundaryBytes=`$boundaryBuffer.ToArray();`$boundaryHash=[Security.Cryptography.SHA256]::Create();try{`$boundaryActual=([BitConverter]::ToString(`$boundaryHash.ComputeHash(`$boundaryBytes))).Replace('-','').ToLowerInvariant()}finally{`$boundaryHash.Dispose()};if(`$boundaryActual-cne `$boundaryExpected){throw 'Frozen PowerShell boundary hash changed across UAC.'};. ([ScriptBlock]::Create([Text.Encoding]::UTF8.GetString(`$boundaryBytes)))
`$ErrorActionPreference='Stop';Set-StrictMode -Version Latest
if([string]`$PSVersionTable.PSEdition -cne 'Desktop'-or [int]`$PSVersionTable.PSVersion.Major -ne 5-or [int]`$PSVersionTable.PSVersion.Minor -ne 1){throw 'Protected bootstrap requires Windows PowerShell 5.1 Desktop.'}
`$id=[Security.Principal.WindowsIdentity]::GetCurrent();`$p=[Security.Principal.WindowsPrincipal]::new(`$id);if(-not `$p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Protected bootstrap requires elevation.'}
`$src=$sourceLiteral;`$sha=$shaLiteral;`$expected=$hashLiteral;`$length=$ExpectedInstallerLength;`$candidate=$candidateLiteral
`$install='C:\Program Files\Dawnstrike';`$parent=Join-Path `$install 'installers';`$final=Join-Path `$parent `$sha;`$dest=Join-Path `$final 'install_dawnstrike_host_boundary.ps1'
`$protected=@{'S-1-5-18'=`$true;'S-1-5-32-544'=`$true;'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'=`$true}
function New-DirAcl{`$a=[Security.AccessControl.DirectorySecurity]::new();`$a.SetAccessRuleProtection(`$true,`$false);`$admins=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544');`$a.SetOwner(`$admins);foreach(`$sid in @('S-1-5-18','S-1-5-32-544')){`$a.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new(`$sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow'))};`$a.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'),'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'));`$a}
function New-FileAcl{`$a=[Security.AccessControl.FileSecurity]::new();`$a.SetAccessRuleProtection(`$true,`$false);`$admins=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544');`$a.SetOwner(`$admins);foreach(`$sid in @('S-1-5-18','S-1-5-32-544')){`$a.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new(`$sid),'FullControl','Allow'))};`$a.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'),'ReadAndExecute','Allow'));`$a}
function Sid(`$v){try{if(`$v-is [Security.Principal.SecurityIdentifier]){return `$v.Value};return `$v.Translate([Security.Principal.SecurityIdentifier]).Value}catch{throw 'Protected bootstrap ACL contains an unresolved principal.'}}
function Assert-Protected(`$path){`$item=Get-Item -LiteralPath `$path -Force;if((`$item.Attributes-band [IO.FileAttributes]::ReparsePoint)-ne 0){throw 'Protected bootstrap path contains a reparse point.'};`$acl=Get-Acl -LiteralPath `$path;if(-not `$protected.ContainsKey((Sid `$acl.Owner))){throw 'Protected bootstrap path owner is untrusted.'};`$write=[Security.AccessControl.FileSystemRights]::WriteData-bor [Security.AccessControl.FileSystemRights]::AppendData-bor [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes-bor [Security.AccessControl.FileSystemRights]::WriteAttributes-bor [Security.AccessControl.FileSystemRights]::Delete-bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles-bor [Security.AccessControl.FileSystemRights]::ChangePermissions-bor [Security.AccessControl.FileSystemRights]::TakeOwnership;foreach(`$r in @(`$acl.Access)){if(`$r.AccessControlType-eq 'Allow'-and -not `$protected.ContainsKey((Sid `$r.IdentityReference))-and (`$r.FileSystemRights-band `$write)-ne 0){throw 'Protected bootstrap path grants non-admin write authority.'}}}
function Digest(`$stream){`$h=[Security.Cryptography.SHA256]::Create();try{`$stream.Position=0;`$d=([BitConverter]::ToString(`$h.ComputeHash(`$stream))).Replace('-','').ToLowerInvariant();`$stream.Position=0;`$d}finally{`$h.Dispose()}}
function Verify-Final{foreach(`$path in @('C:\Program Files',`$install,`$parent,`$final,`$dest)){Assert-Protected `$path};`$all=@(Get-ChildItem -LiteralPath `$final -Recurse -Force);if(`$all.Count-ne 1-or `$all[0].PSIsContainer-or -not [string]::Equals(`$all[0].FullName,`$dest,[StringComparison]::OrdinalIgnoreCase)){throw 'Protected installer directory has an unexpected file set.'};`$s=[IO.File]::Open(`$dest,'Open','Read','Read');if(`$s.Length-ne `$length-or (Digest `$s)-cne `$expected){`$s.Dispose();throw 'Protected installer bytes differ from the frozen contract.'};`$s}
if(-not(Test-Path -LiteralPath `$install)){[IO.Directory]::CreateDirectory(`$install,(New-DirAcl))|Out-Null};Assert-Protected 'C:\Program Files';Assert-Protected `$install
if(-not(Test-Path -LiteralPath `$parent)){[IO.Directory]::CreateDirectory(`$parent,(New-DirAcl))|Out-Null};Assert-Protected `$parent
if(-not(Test-Path -LiteralPath `$final)){`$stage=Join-Path `$parent ('.installer-stage-'+[Guid]::NewGuid().ToString('N'));[IO.Directory]::CreateDirectory(`$stage,(New-DirAcl))|Out-Null;try{`$stageFile=Join-Path `$stage 'install_dawnstrike_host_boundary.ps1';`$input=[IO.File]::Open(`$src,'Open','Read','Read');try{if(`$input.Length-ne `$length-or (Digest `$input)-cne `$expected){throw 'Frozen installer changed before protected copy.'};`$output=[IO.File]::Open(`$stageFile,'CreateNew','ReadWrite','Read');try{`$input.CopyTo(`$output);`$output.Flush(`$true);if(`$output.Length-ne `$length-or (Digest `$output)-cne `$expected){throw 'Protected staged installer copy is inexact.'}}finally{`$output.Dispose()};Set-Acl -LiteralPath `$stageFile -AclObject (New-FileAcl);Assert-Protected `$stage;Assert-Protected `$stageFile}finally{`$input.Dispose()};if(Test-Path -LiteralPath `$final){throw 'Protected installer destination appeared during staging.'};[IO.Directory]::Move(`$stage,`$final);`$stage=''}finally{if(`$stage-and(Test-Path -LiteralPath `$stage)){if(-not([IO.Path]::GetFullPath(`$stage).StartsWith(([IO.Path]::GetFullPath(`$parent).TrimEnd('\')+'\'),[StringComparison]::OrdinalIgnoreCase))){throw 'Protected bootstrap cleanup target escaped.'};Remove-Item -LiteralPath `$stage -Recurse -Force}}}
`$guard=Verify-Final;try{`$args=@{ExpectedSha=`$sha;CandidateRoot=`$candidate};`$bs=$boundaryShaLiteral;`$bt=$boundaryTreeLiteral;`$rs=$runtimeShaLiteral;`$rt=$runtimeTreeLiteral;if(`$bs){`$args.BoundaryPredecessorSha=`$bs;`$args.BoundaryPredecessorTree=`$bt;`$args.RuntimePredecessorSha=`$rs;`$args.RuntimePredecessorTree=`$rt};& `$dest @args;if(-not `$?){throw 'Protected host-boundary installer failed.'}}finally{`$guard.Dispose()}
}finally{`$boundaryStream.Dispose()}
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($elevatedCommand))
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    $start.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand ' + $encoded
    $start.UseShellExecute = $true
    $start.Verb = 'runas'
    $start.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    # CLR profiler/startup variables and executable/module lookup authority
    # must be canonical before the elevated CLR initializes.  Clearing them
    # inside the child would already be too late.  Preserve the unelevated
    # caller's environment and restore it only after the child is terminal.
    $elevationEnvironmentSnapshot = @{}
    foreach ($name in @('PSModulePath', 'PATH', 'ComSpec', 'PATHEXT')) {
        $elevationEnvironmentSnapshot[$name] = `
            [Environment]::GetEnvironmentVariable($name, 'Process')
    }
    try {
        foreach ($environmentName in @([Environment]::GetEnvironmentVariables('Process').Keys)) {
            $name = [string]$environmentName
            if (
                $name -match '^(?i:COMPLUS_|COR_|CORECLR_|CSC|VBCS|MSBUILD|DOTNET_)' -or
                [string]::Equals($name, '__COMPAT_LAYER', [StringComparison]::OrdinalIgnoreCase)
            ) {
                if (-not $elevationEnvironmentSnapshot.ContainsKey($name)) {
                    $elevationEnvironmentSnapshot[$name] = `
                        [Environment]::GetEnvironmentVariable($name, 'Process')
                }
                [Environment]::SetEnvironmentVariable($name, $null, 'Process')
            }
        }
        [Environment]::SetEnvironmentVariable(
            'PSModulePath',
            'C:\Windows\System32\WindowsPowerShell\v1.0\Modules',
            'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'PATH',
            'C:\Windows\System32;C:\Windows;C:\Windows\System32\Wbem;' +
                'C:\Windows\System32\WindowsPowerShell\v1.0',
            'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'ComSpec', 'C:\Windows\System32\cmd.exe', 'Process'
        )
        [Environment]::SetEnvironmentVariable(
            'PATHEXT', '.COM;.EXE;.BAT;.CMD', 'Process'
        )
        foreach ($environmentName in @([Environment]::GetEnvironmentVariables('Process').Keys)) {
            $name = [string]$environmentName
            if (
                $name -match '^(?i:COMPLUS_|COR_|CORECLR_|CSC|VBCS|MSBUILD|DOTNET_)' -or
                [string]::Equals($name, '__COMPAT_LAYER', [StringComparison]::OrdinalIgnoreCase)
            ) {
                throw 'Host bootstrap could not clear a forbidden CLR/compiler variable.'
            }
        }
        $child = [Diagnostics.Process]::Start($start)
        if ($null -eq $child) { throw 'Protected host-boundary UAC child did not start.' }
        try {
            $child.WaitForExit()
            if ($child.ExitCode -ne 0) {
                throw "Protected host-boundary bootstrap failed with exit code $($child.ExitCode)."
            }
        }
        finally { if ($null -ne $child) { $child.Dispose() } }
    }
    finally {
        foreach ($name in @($elevationEnvironmentSnapshot.Keys)) {
            [Environment]::SetEnvironmentVariable(
                [string]$name, $elevationEnvironmentSnapshot[$name], 'Process'
            )
        }
    }
}
finally {
    foreach ($stream in @(
        $frozenInstallerStream, $frozenBoundaryStream,
        $frozenInstallerGuard, $frozenBoundaryGuard
    )) { if ($null -ne $stream) { $stream.Dispose() } }
    foreach ($file in @($frozenInstaller, $frozenBoundary)) {
        if ([IO.File]::Exists($file)) { [IO.File]::Delete($file) }
    }
    if ([IO.Directory]::Exists($temporaryRoot)) {
        $temporaryFull = [IO.Path]::GetFullPath($temporaryRoot).TrimEnd('\')
        $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
        if (-not $temporaryFull.StartsWith($tempPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Host bootstrap cleanup target escaped the temporary root.'
        }
        # Never recurse through a same-SID attacker-created descendant.
        if ([IO.Directory]::GetFileSystemEntries($temporaryFull).Length -eq 0) {
            [IO.Directory]::Delete($temporaryFull, $false)
        }
    }
}
