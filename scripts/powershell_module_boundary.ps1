# This file is deliberately executable without module autoloading.  Every
# privileged PowerShell entrypoint sets these values before dot-sourcing it;
# repeat them here so a direct, fresh invocation is fail closed as well.
$global:PSModuleAutoLoadingPreference = 'None'
$env:PSModulePath = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
$env:ComSpec = 'C:\Windows\System32\cmd.exe'
$env:PATHEXT = '.COM;.EXE;.BAT;.CMD'

function New-DawnstrikeProtectedPowerShellDirectorySecurity {
    [CmdletBinding()]
    param()

    $security = [Security.AccessControl.DirectorySecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    $administrators = [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    $security.SetOwner($administrators)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $security.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        ))
    }
    return $security
}

function Assert-DawnstrikeProtectedPowerShellTempPath {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $boundaryRoot = 'C:\ProgramData\Dawnstrike'
    $root = 'C:\ProgramData\Dawnstrike\PowerShellTemp'
    if (
        -not [string]::Equals($full, $root, [StringComparison]::OrdinalIgnoreCase) -and
        -not $full.StartsWith(($root + '\'), [StringComparison]::OrdinalIgnoreCase)
    ) { throw 'Protected PowerShell compilation temp path escaped its fixed root.' }
    if (-not [IO.Directory]::Exists($full)) {
        throw 'Protected PowerShell compilation temp path is missing.'
    }
    $protectedSids = @{
        'S-1-5-18' = $true
        'S-1-5-32-544' = $true
    }
    $writeLikeRights = (
        [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    $cursor = $full
    while ($true) {
        $attributes = [IO.File]::GetAttributes($cursor)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Protected PowerShell compilation temp path contains a reparse point.'
        }
        $acl = [IO.DirectoryInfo]::new($cursor).GetAccessControl()
        $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if (-not $protectedSids.ContainsKey([string]$ownerSid)) {
            throw 'Protected PowerShell compilation temp path has an untrusted owner.'
        }
        foreach ($rule in @($acl.GetAccessRules(
            $true, $true, [Security.Principal.SecurityIdentifier]
        ))) {
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                -not $protectedSids.ContainsKey([string]$rule.IdentityReference.Value) -and
                ($rule.FileSystemRights -band $writeLikeRights) -ne 0
            ) {
                throw 'Protected PowerShell compilation temp path grants non-admin write authority.'
            }
        }
        if ([string]::Equals($cursor, $boundaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) {
            throw 'Protected PowerShell compilation temp path chain is incomplete.'
        }
        $cursor = $parent.FullName.TrimEnd('\')
    }
    return $full
}

function Initialize-DawnstrikeElevatedPowerShellTempBoundary {
    [CmdletBinding()]
    param()

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        return $null
    }

    $programDataRoot = 'C:\ProgramData\Dawnstrike'
    $compilerRoot = [IO.Path]::Combine($programDataRoot, 'PowerShellTemp')
    $security = & ${function:New-DawnstrikeProtectedPowerShellDirectorySecurity}
    foreach ($directory in @($programDataRoot, $compilerRoot)) {
        if (-not [IO.Directory]::Exists($directory)) {
            $null = [IO.Directory]::CreateDirectory($directory, $security)
        }
        # A pre-created or raced directory is never repaired in place.  Exact
        # ownership/no-write proof must already hold before it is used.
        if ($directory -ceq $compilerRoot) {
            $null = & ${function:Assert-DawnstrikeProtectedPowerShellTempPath} `
                -Path $directory
        }
    }
    $processRoot = [IO.Path]::Combine(
        $compilerRoot,
        ([Diagnostics.Process]::GetCurrentProcess().Id.ToString(
            [Globalization.CultureInfo]::InvariantCulture
        ) + '-' + [Guid]::NewGuid().ToString('N'))
    )
    $null = [IO.Directory]::CreateDirectory($processRoot, $security)
    $null = & ${function:Assert-DawnstrikeProtectedPowerShellTempPath} `
        -Path $processRoot
    $guardPath = [IO.Path]::Combine($processRoot, '.dawnstrike-retained-temp-guard')
    $guard = [IO.File]::Open(
        $guardPath,
        [IO.FileMode]::CreateNew,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    $guard.WriteByte(0)
    $guard.Flush($true)
    $global:DawnstrikePowerShellTempGuard = $guard
    $global:DawnstrikePowerShellTempRoot = $processRoot
    $env:TEMP = $processRoot
    $env:TMP = $processRoot
    [Environment]::SetEnvironmentVariable('TMPDIR', $null, 'Process')
    return $processRoot
}

function Assert-DawnstrikeSystemPowerShellPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $windowsRoot = 'C:\Windows'
    if (
        -not [string]::Equals($full, $windowsRoot, [StringComparison]::OrdinalIgnoreCase) -and
        -not $full.StartsWith(($windowsRoot + '\'), [StringComparison]::OrdinalIgnoreCase)
    ) {
        throw "$Label is outside the fixed Windows system root."
    }
    if (-not [IO.File]::Exists($full) -and -not [IO.Directory]::Exists($full)) {
        throw "$Label is missing."
    }

    $protectedSids = @{
        'S-1-5-18' = $true
        'S-1-5-32-544' = $true
        'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464' = $true
    }
    $writeLikeRights = (
        [Security.AccessControl.FileSystemRights]::WriteData -bor
        [Security.AccessControl.FileSystemRights]::AppendData -bor
        [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
        [Security.AccessControl.FileSystemRights]::WriteAttributes -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )

    $cursor = $full
    while ($true) {
        $attributes = [IO.File]::GetAttributes($cursor)
        if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label contains a reparse-point component."
        }
        $acl = if ([IO.Directory]::Exists($cursor)) {
            [IO.DirectoryInfo]::new($cursor).GetAccessControl()
        }
        else {
            [IO.FileInfo]::new($cursor).GetAccessControl()
        }
        $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if (-not $protectedSids.ContainsKey([string]$ownerSid)) {
            throw "$Label has an untrusted owner."
        }
        foreach ($rule in @($acl.GetAccessRules(
            $true, $true, [Security.Principal.SecurityIdentifier]
        ))) {
            if (
                $rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
                -not $protectedSids.ContainsKey([string]$rule.IdentityReference.Value) -and
                ($rule.FileSystemRights -band $writeLikeRights) -ne 0
            ) {
                throw "$Label grants non-system write authority."
            }
        }
        if ([string]::Equals($cursor, $windowsRoot, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { throw "$Label path chain did not reach the Windows root." }
        $cursor = $parent.FullName.TrimEnd('\')
    }
    return $full
}

function Assert-DawnstrikeSystemPowerShellTree {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Label
    )

    $rootFull = & ${function:Assert-DawnstrikeSystemPowerShellPath} `
        -Path $Root -Label $Label
    if (-not [IO.Directory]::Exists($rootFull)) { throw "$Label is not a directory." }
    $pending = [Collections.Generic.Queue[string]]::new()
    $pending.Enqueue($rootFull)
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($entry in [IO.DirectoryInfo]::new($directory).GetFileSystemInfos()) {
            $entryPath = & ${function:Assert-DawnstrikeSystemPowerShellPath} `
                -Path $entry.FullName -Label $Label
            if (($entry.Attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                $pending.Enqueue($entryPath)
            }
        }
    }
}

function Assert-DawnstrikePowerShellCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$ModuleName,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Cmdlet', 'Function')][string]$CommandType
    )

    $commands = @(Microsoft.PowerShell.Core\Get-Command `
        -All -Name $Name -ErrorAction SilentlyContinue)
    if ($commands.Count -ne 1) {
        throw "Protected PowerShell command resolution is ambiguous: $Name"
    }
    $command = $commands[0]
    if (
        [string]$command.CommandType -cne $CommandType -or
        [string]$command.ModuleName -cne $ModuleName -or
        [string]$command.Source -cne $ModuleName
    ) {
        throw "Protected PowerShell command is shadowed or came from the wrong module: $Name"
    }
}

function Initialize-DawnstrikePowerShellModuleBoundary {
    [CmdletBinding()]
    param()

    if (
        [string]$PSVersionTable.PSEdition -cne 'Desktop' -or
        [int]$PSVersionTable.PSVersion.Major -ne 5 -or
        [int]$PSVersionTable.PSVersion.Minor -ne 1
    ) {
        throw 'Dawnstrike privileged PowerShell requires Windows PowerShell 5.1 Desktop.'
    }
    if (
        [string]$env:PSModulePath -cne
        'C:\Windows\System32\WindowsPowerShell\v1.0\Modules' -or
        [string]$global:PSModuleAutoLoadingPreference -cne 'None'
    ) {
        throw 'Dawnstrike PowerShell module isolation was not established.'
    }

    $env:Path = @(
        'C:\Windows\System32',
        'C:\Windows',
        'C:\Windows\System32\Wbem',
        'C:\Windows\System32\WindowsPowerShell\v1.0'
    ) -join ';'
    foreach ($environmentName in @([Environment]::GetEnvironmentVariables().Keys)) {
        $name = [string]$environmentName
        if (
            $name -match '^(?i:COMPLUS_|COR_|CORECLR_|CSC|VBCS|MSBUILD|DOTNET_)' -or
            [string]::Equals($name, '__COMPAT_LAYER', [StringComparison]::OrdinalIgnoreCase)
        ) {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        if (
            $null -ne $global:DawnstrikePowerShellTempGuard -and
            $global:DawnstrikePowerShellTempGuard -is [IO.FileStream] -and
            $global:DawnstrikePowerShellTempGuard.CanWrite -and
            -not [string]::IsNullOrWhiteSpace([string]$global:DawnstrikePowerShellTempRoot)
        ) {
            $tempRoot = & ${function:Assert-DawnstrikeProtectedPowerShellTempPath} `
                -Path ([string]$global:DawnstrikePowerShellTempRoot)
            $env:TEMP = $tempRoot
            $env:TMP = $tempRoot
        }
        else {
            $tempRoot = & ${function:Initialize-DawnstrikeElevatedPowerShellTempBoundary}
        }
        if (
            [string]$env:TEMP -cne [string]$tempRoot -or
            [string]$env:TMP -cne [string]$tempRoot
        ) { throw 'Elevated PowerShell compiler temp isolation was not established.' }
    }

    $moduleRoot = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
    $moduleNames = @(
        'Microsoft.PowerShell.Management',
        'Microsoft.PowerShell.Utility',
        'Microsoft.PowerShell.Security',
        'CimCmdlets',
        'ScheduledTasks'
    )
    $moduleManifests = @{}
    foreach ($moduleName in $moduleNames) {
        $moduleDirectory = [IO.Path]::Combine($moduleRoot, $moduleName)
        $manifestPath = [IO.Path]::Combine(
            $moduleDirectory, ($moduleName + '.psd1')
        )
        $null = & ${function:Assert-DawnstrikeSystemPowerShellTree} `
            -Root $moduleDirectory -Label ("System PowerShell module " + $moduleName)
        $null = & ${function:Assert-DawnstrikeSystemPowerShellPath} `
            -Path $manifestPath -Label ("System PowerShell manifest " + $moduleName)
        $moduleManifests[$moduleName] = $manifestPath
    }

    $allowedLoadedModules = @{}
    foreach ($moduleName in $moduleNames) { $allowedLoadedModules[$moduleName] = $true }
    foreach ($loaded in @(Microsoft.PowerShell.Core\Get-Module)) {
        if (-not $allowedLoadedModules.ContainsKey([string]$loaded.Name)) {
            throw 'A non-system module was loaded before the Dawnstrike PowerShell boundary.'
        }
        $null = & ${function:Assert-DawnstrikeSystemPowerShellPath} `
            -Path ([string]$loaded.Path) `
            -Label ("Preloaded PowerShell module " + [string]$loaded.Name)
    }

    foreach ($moduleName in $moduleNames) {
        $null = Microsoft.PowerShell.Core\Import-Module `
            -Name ([string]$moduleManifests[$moduleName]) `
            -Force -Global -DisableNameChecking -ErrorAction Stop
    }

    $loadedNames = @{}
    foreach ($loaded in @(Microsoft.PowerShell.Core\Get-Module)) {
        if (-not $allowedLoadedModules.ContainsKey([string]$loaded.Name)) {
            throw 'A non-system module is loaded inside the Dawnstrike PowerShell boundary.'
        }
        if ($loadedNames.ContainsKey([string]$loaded.Name)) {
            throw 'A protected PowerShell module has multiple loaded definitions.'
        }
        $loadedNames[[string]$loaded.Name] = $true
        $null = & ${function:Assert-DawnstrikeSystemPowerShellPath} `
            -Path ([string]$loaded.Path) `
            -Label ("Loaded PowerShell module " + [string]$loaded.Name)
    }
    foreach ($moduleName in $moduleNames) {
        if (-not $loadedNames.ContainsKey($moduleName)) {
            throw "Required system PowerShell module did not load: $moduleName"
        }
    }

    foreach ($assemblyName in @(
        'Microsoft.PowerShell.Commands.Management',
        'Microsoft.PowerShell.Commands.Utility',
        'Microsoft.PowerShell.Security',
        'Microsoft.Management.Infrastructure',
        'Microsoft.Management.Infrastructure.CimCmdlets'
    )) {
        $matches = [Collections.Generic.List[Reflection.Assembly]]::new()
        foreach ($assembly in [AppDomain]::CurrentDomain.GetAssemblies()) {
            if ([string]$assembly.GetName().Name -ceq $assemblyName) {
                $matches.Add($assembly)
            }
        }
        if ($matches.Count -ne 1 -or [string]::IsNullOrWhiteSpace([string]$matches[0].Location)) {
            throw "Required system PowerShell assembly is missing or ambiguous: $assemblyName"
        }
        $null = & ${function:Assert-DawnstrikeSystemPowerShellPath} `
            -Path ([string]$matches[0].Location) `
            -Label ("System PowerShell assembly " + $assemblyName)
    }

    $commandContracts = @(
        @('Add-Member', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Add-Type', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Compare-Object', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('ConvertFrom-Json', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('ConvertTo-Json', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('ForEach-Object', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('Get-Acl', 'Microsoft.PowerShell.Security', 'Cmdlet'),
        @('Get-AuthenticodeSignature', 'Microsoft.PowerShell.Security', 'Cmdlet'),
        @('Get-ChildItem', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Get-Command', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('Get-Content', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Get-Credential', 'Microsoft.PowerShell.Security', 'Cmdlet'),
        @('Get-Date', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Get-FileHash', 'Microsoft.PowerShell.Utility', 'Function'),
        @('Get-Item', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Get-Location', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Get-Module', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('Get-Process', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Get-Variable', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Group-Object', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Import-Module', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('Join-Path', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Move-Item', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('New-Item', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('New-Module', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('New-Object', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Out-Null', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('Out-String', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Pop-Location', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Push-Location', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Remove-Item', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Resolve-Path', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Select-Object', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Set-Acl', 'Microsoft.PowerShell.Security', 'Cmdlet'),
        @('Set-Content', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Set-Item', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Set-StrictMode', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('Sort-Object', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Split-Path', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Start-Process', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Start-Sleep', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Stop-Process', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Test-Path', 'Microsoft.PowerShell.Management', 'Cmdlet'),
        @('Where-Object', 'Microsoft.PowerShell.Core', 'Cmdlet'),
        @('Write-Output', 'Microsoft.PowerShell.Utility', 'Cmdlet'),
        @('Disable-ScheduledTask', 'ScheduledTasks', 'Function'),
        @('Enable-ScheduledTask', 'ScheduledTasks', 'Function'),
        @('Export-ScheduledTask', 'ScheduledTasks', 'Function'),
        @('Get-ScheduledTask', 'ScheduledTasks', 'Function'),
        @('Get-ScheduledTaskInfo', 'ScheduledTasks', 'Function'),
        @('New-ScheduledTaskAction', 'ScheduledTasks', 'Function'),
        @('Register-ScheduledTask', 'ScheduledTasks', 'Function'),
        @('Set-ScheduledTask', 'ScheduledTasks', 'Function')
    )
    foreach ($contract in $commandContracts) {
        & ${function:Assert-DawnstrikePowerShellCommand} `
            -Name ([string]$contract[0]) `
            -ModuleName ([string]$contract[1]) `
            -CommandType ([string]$contract[2])
    }

    $global:DawnstrikePowerShellModuleBoundary = [pscustomobject]@{
        schema_version = 'dawnstrike.powershell_module_boundary.v1'
        module_root = $moduleRoot
        autoload = 'None'
    }
}

& ${function:Initialize-DawnstrikePowerShellModuleBoundary}
