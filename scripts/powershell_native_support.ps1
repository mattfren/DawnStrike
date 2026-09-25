# Exact precompiled support for every Dawnstrike PowerShell call graph that
# previously used Add-Type -TypeDefinition.  This file must be admitted with
# its base64 asset from one exact protected release and loaded in a fresh
# process; different support hashes are never mixed in one AppDomain.
$script:DawnstrikePowerShellSupportAssetName = 'dawnstrike_powershell_support.dll.b64'
$script:DawnstrikePowerShellSupportEncodedLength = 31405L
$script:DawnstrikePowerShellSupportEncodedSha256 =
    'e020cd6d299844ffb5d0c6434ee6a4c69b35e951fd0e5526c760d31a906bc3a6'
$script:DawnstrikePowerShellSupportDecodedLength = 23552L
$script:DawnstrikePowerShellSupportDecodedSha256 =
    'c33ead9a6e32663604ab07250b3422c187e2470d7345854c7d827234ced929f4'
$script:DawnstrikePowerShellSupportAssemblyFullName =
    'Dawnstrike.PowerShellSupport, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null'
$script:DawnstrikePowerShellSupportMvid = 'b57182cf-522b-41a5-bd64-ff682ce826f5'

function Get-DawnstrikePowerShellSupportSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)

    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace(
            '-', ''
        ).ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Assert-DawnstrikePowerShellSupportAssembly {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][Reflection.Assembly]$Assembly,
        [Parameter(Mandatory = $true)][string]$DecodedSha256
    )

    if (
        [string]$Assembly.FullName -cne $script:DawnstrikePowerShellSupportAssemblyFullName -or
        [string]$Assembly.ManifestModule.ModuleVersionId.ToString('D') -cne
            $script:DawnstrikePowerShellSupportMvid -or
        -not [string]::IsNullOrEmpty([string]$Assembly.Location) -or
        $Assembly.GlobalAssemblyCache -or
        $DecodedSha256 -cne $script:DawnstrikePowerShellSupportDecodedSha256
    ) { throw 'Dawnstrike PowerShell support assembly identity is invalid.' }

    $expectedTypes = @(
        'Dawnstrike.Locking.DailyLockNative',
        'Dawnstrike.Locking.FileDispositionInfo',
        'Dawnstrike.Locking.RuntimeFileDispositionInfo',
        'Dawnstrike.Locking.RuntimeLockNative',
        'Dawnstrike.Native.JobProcessResult',
        'Dawnstrike.Native.JobProcessRunner',
        'Dawnstrike.Native.JobProcessRunner+IO_COUNTERS',
        'Dawnstrike.Native.JobProcessRunner+JOBOBJECT_BASIC_ACCOUNTING_INFORMATION',
        'Dawnstrike.Native.JobProcessRunner+JOBOBJECT_BASIC_LIMIT_INFORMATION',
        'Dawnstrike.Native.JobProcessRunner+JOBOBJECT_EXTENDED_LIMIT_INFORMATION',
        'Dawnstrike.Native.JobProcessRunner+PROCESS_INFORMATION',
        'Dawnstrike.Native.JobProcessRunner+SECURITY_ATTRIBUTES',
        'Dawnstrike.Native.JobProcessRunner+STARTUPINFO',
        'Dawnstrike.Security.ProtectedDirectoryNative',
        'Dawnstrike.StateBoundary.AtomicFile',
        'Dawnstrike.StateBoundary.BoundPath',
        'Dawnstrike.StateBoundary.BoundPathChain',
        'Dawnstrike.StateBoundary.ByHandleFileInformation',
        'Dawnstrike.StateBoundary.DisposableGroup',
        'Dawnstrike.StateBoundary.NativeMethods'
    )
    $actualTypes = [Collections.Generic.List[string]]::new()
    foreach ($type in $Assembly.GetTypes()) { $actualTypes.Add([string]$type.FullName) }
    $actualTypes.Sort([StringComparer]::Ordinal)
    if ($actualTypes.Count -ne $expectedTypes.Count) {
        throw 'Dawnstrike PowerShell support type surface is not closed.'
    }
    for ($index = 0; $index -lt $expectedTypes.Count; $index++) {
        if ([string]$actualTypes[$index] -cne [string]$expectedTypes[$index]) {
            throw 'Dawnstrike PowerShell support type surface changed.'
        }
    }

    $expectedReferences = @(
        'System, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089',
        'mscorlib, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b77a5c561934e089'
    )
    $actualReferences = [Collections.Generic.List[string]]::new()
    foreach ($reference in $Assembly.GetReferencedAssemblies()) {
        $actualReferences.Add([string]$reference.FullName)
    }
    $actualReferences.Sort([StringComparer]::Ordinal)
    if ($actualReferences.Count -ne $expectedReferences.Count) {
        throw 'Dawnstrike PowerShell support reference surface is not closed.'
    }
    for ($index = 0; $index -lt $expectedReferences.Count; $index++) {
        if ([string]$actualReferences[$index] -cne [string]$expectedReferences[$index]) {
            throw 'Dawnstrike PowerShell support reference surface changed.'
        }
    }
    if (@($Assembly.GetManifestResourceNames()).Count -ne 0) {
        throw 'Dawnstrike PowerShell support assembly contains an unexpected resource.'
    }

    $expectedPInvoke = @(
        'Dawnstrike.Locking.DailyLockNative::CreateFileW|kernel32.dll|CreateFileW|Unicode|True',
        'Dawnstrike.Locking.DailyLockNative::GetFinalPathNameByHandleW|kernel32.dll|GetFinalPathNameByHandleW|Unicode|True',
        'Dawnstrike.Locking.DailyLockNative::SetFileInformationByHandleRaw|kernel32.dll|SetFileInformationByHandle|None|True',
        'Dawnstrike.Locking.DailyLockNative::SetFileInformationByHandle|kernel32.dll|SetFileInformationByHandle|None|True',
        'Dawnstrike.Locking.RuntimeLockNative::CreateFileW|kernel32.dll|CreateFileW|Unicode|True',
        'Dawnstrike.Locking.RuntimeLockNative::GetFinalPathNameByHandleW|kernel32.dll|GetFinalPathNameByHandleW|Unicode|True',
        'Dawnstrike.Locking.RuntimeLockNative::SetFileInformationByHandle|kernel32.dll|SetFileInformationByHandle|None|True',
        'Dawnstrike.Native.JobProcessRunner::AssignProcessToJobObject|kernel32.dll|AssignProcessToJobObject|None|True',
        'Dawnstrike.Native.JobProcessRunner::CloseHandle|kernel32.dll|CloseHandle|None|True',
        'Dawnstrike.Native.JobProcessRunner::CreateJobObject|kernel32.dll|CreateJobObject|Unicode|True',
        'Dawnstrike.Native.JobProcessRunner::CreatePipe|kernel32.dll|CreatePipe|None|True',
        'Dawnstrike.Native.JobProcessRunner::CreateProcessW|kernel32.dll|CreateProcessW|Unicode|True',
        'Dawnstrike.Native.JobProcessRunner::GetExitCodeProcess|kernel32.dll|GetExitCodeProcess|None|True',
        'Dawnstrike.Native.JobProcessRunner::QueryInformationJobObject|kernel32.dll|QueryInformationJobObject|None|True',
        'Dawnstrike.Native.JobProcessRunner::ResumeThread|kernel32.dll|ResumeThread|None|True',
        'Dawnstrike.Native.JobProcessRunner::SetHandleInformation|kernel32.dll|SetHandleInformation|None|True',
        'Dawnstrike.Native.JobProcessRunner::SetInformationJobObject|kernel32.dll|SetInformationJobObject|None|True',
        'Dawnstrike.Native.JobProcessRunner::TerminateJobObject|kernel32.dll|TerminateJobObject|None|True',
        'Dawnstrike.Native.JobProcessRunner::TerminateProcess|kernel32.dll|TerminateProcess|None|True',
        'Dawnstrike.Native.JobProcessRunner::WaitForSingleObject|kernel32.dll|WaitForSingleObject|None|True',
        'Dawnstrike.Security.ProtectedDirectoryNative::CreateFileW|kernel32.dll|CreateFileW|Unicode|True',
        'Dawnstrike.StateBoundary.NativeMethods::CreateFileW|kernel32.dll|CreateFileW|Unicode|True',
        'Dawnstrike.StateBoundary.NativeMethods::GetFileInformationByHandle|kernel32.dll|GetFileInformationByHandle|None|True',
        'Dawnstrike.StateBoundary.NativeMethods::MoveFileExW|kernel32.dll|MoveFileExW|Unicode|True'
    )
    $actualPInvoke = [Collections.Generic.List[string]]::new()
    foreach ($type in $Assembly.GetTypes()) {
        foreach ($method in $type.GetMethods(
            [Reflection.BindingFlags]'Public,NonPublic,Static,Instance,DeclaredOnly'
        )) {
            $attributes = @($method.GetCustomAttributes(
                [Runtime.InteropServices.DllImportAttribute], $false
            ))
            if ($attributes.Count -eq 0) { continue }
            if ($attributes.Count -ne 1) {
                throw 'Dawnstrike PowerShell support P/Invoke metadata is ambiguous.'
            }
            $attribute = $attributes[0]
            $actualPInvoke.Add(
                [string]$type.FullName + '::' + [string]$method.Name + '|' +
                [string]$attribute.Value + '|' + [string]$attribute.EntryPoint + '|' +
                [string]$attribute.CharSet + '|' + [string]$attribute.SetLastError
            )
        }
    }
    $actualPInvoke.Sort([StringComparer]::Ordinal)
    if ($actualPInvoke.Count -ne $expectedPInvoke.Count) {
        throw 'Dawnstrike PowerShell support P/Invoke surface is not closed.'
    }
    for ($index = 0; $index -lt $expectedPInvoke.Count; $index++) {
        if ([string]$actualPInvoke[$index] -cne [string]$expectedPInvoke[$index]) {
            throw 'Dawnstrike PowerShell support P/Invoke surface changed.'
        }
    }
}

function Import-DawnstrikePowerShellSupport {
    [CmdletBinding()]
    param()

    if ($null -ne $global:DawnstrikePowerShellSupportBoundary) {
        if (
            [string]$global:DawnstrikePowerShellSupportBoundary.schema_version -cne
                'dawnstrike.powershell_native_support.v1' -or
            [string]$global:DawnstrikePowerShellSupportBoundary.decoded_sha256 -cne
                $script:DawnstrikePowerShellSupportDecodedSha256 -or
            $global:DawnstrikePowerShellSupportBoundary.assembly -isnot [Reflection.Assembly]
        ) {
            throw 'A different Dawnstrike support generation is already loaded; use a fresh process.'
        }
        & ${function:Assert-DawnstrikePowerShellSupportAssembly} `
            -Assembly $global:DawnstrikePowerShellSupportBoundary.assembly `
            -DecodedSha256 $script:DawnstrikePowerShellSupportDecodedSha256
        return $global:DawnstrikePowerShellSupportBoundary
    }

    $publicTypes = @(
        'Dawnstrike.Locking.DailyLockNative',
        'Dawnstrike.Locking.RuntimeLockNative',
        'Dawnstrike.Native.JobProcessResult',
        'Dawnstrike.Native.JobProcessRunner',
        'Dawnstrike.Security.ProtectedDirectoryNative',
        'Dawnstrike.StateBoundary.AtomicFile',
        'Dawnstrike.StateBoundary.BoundPath',
        'Dawnstrike.StateBoundary.BoundPathChain',
        'Dawnstrike.StateBoundary.DisposableGroup'
    )
    foreach ($loadedAssembly in [AppDomain]::CurrentDomain.GetAssemblies()) {
        if ([string]$loadedAssembly.GetName().Name -ceq 'Dawnstrike.PowerShellSupport') {
            throw 'A Dawnstrike PowerShell support assembly was preloaded without exact proof.'
        }
        foreach ($typeName in $publicTypes) {
            if ($null -ne $loadedAssembly.GetType($typeName, $false, $false)) {
                throw 'A Dawnstrike PowerShell support type was preloaded from another assembly.'
            }
        }
    }

    $assetPath = if (-not [string]::IsNullOrWhiteSpace([string]$PSScriptRoot)) {
        [IO.Path]::Combine($PSScriptRoot, $script:DawnstrikePowerShellSupportAssetName)
    }
    elseif (-not [string]::IsNullOrWhiteSpace(
        [string]$global:DawnstrikePowerShellSupportAssetPath
    )) {
        [IO.Path]::GetFullPath([string]$global:DawnstrikePowerShellSupportAssetPath)
    }
    else { throw 'In-memory support loading requires an exact retained asset path.' }
    if (([IO.FileInfo]::new($assetPath).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'Dawnstrike PowerShell support asset is a reparse point.'
    }
    $stream = [IO.File]::Open(
        $assetPath,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    try {
        if ($stream.Length -ne $script:DawnstrikePowerShellSupportEncodedLength) {
            throw 'Dawnstrike PowerShell support encoded length changed.'
        }
        $encodedBuffer = [IO.MemoryStream]::new([int]$stream.Length)
        $stream.CopyTo($encodedBuffer)
        [byte[]]$encodedBytes = $encodedBuffer.ToArray()
        if (
            (& ${function:Get-DawnstrikePowerShellSupportSha256} -Bytes $encodedBytes) -cne
            $script:DawnstrikePowerShellSupportEncodedSha256
        ) { throw 'Dawnstrike PowerShell support encoded hash changed.' }
        try {
            [byte[]]$assemblyBytes = [Convert]::FromBase64String(
                [Text.Encoding]::ASCII.GetString($encodedBytes)
            )
        }
        catch { throw 'Dawnstrike PowerShell support asset is not canonical base64.' }
        if (
            $assemblyBytes.Length -ne $script:DawnstrikePowerShellSupportDecodedLength -or
            (& ${function:Get-DawnstrikePowerShellSupportSha256} -Bytes $assemblyBytes) -cne
                $script:DawnstrikePowerShellSupportDecodedSha256
        ) { throw 'Dawnstrike PowerShell support decoded identity changed.' }
        $assembly = [Reflection.Assembly]::Load($assemblyBytes)
        & ${function:Assert-DawnstrikePowerShellSupportAssembly} `
            -Assembly $assembly `
            -DecodedSha256 $script:DawnstrikePowerShellSupportDecodedSha256
        $global:DawnstrikePowerShellSupportBoundary = [pscustomobject]@{
            schema_version = 'dawnstrike.powershell_native_support.v1'
            encoded_sha256 = $script:DawnstrikePowerShellSupportEncodedSha256
            decoded_sha256 = $script:DawnstrikePowerShellSupportDecodedSha256
            asset_path = [IO.Path]::GetFullPath($assetPath)
            assembly = $assembly
        }
        return $global:DawnstrikePowerShellSupportBoundary
    }
    finally { $stream.Dispose() }
}

$null = & ${function:Import-DawnstrikePowerShellSupport}
