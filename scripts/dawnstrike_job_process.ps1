$global:PSModuleAutoLoadingPreference = 'None'
$env:PSModulePath = 'C:\Windows\System32\WindowsPowerShell\v1.0\Modules'
. ([IO.Path]::Combine($PSScriptRoot, 'powershell_module_boundary.ps1'))
. ([IO.Path]::Combine($PSScriptRoot, 'powershell_native_support.ps1'))

$null = & ${function:Import-DawnstrikePowerShellSupport}
if (-not ("Dawnstrike.Native.JobProcessRunner" -as [type])) {
    throw 'Precompiled Dawnstrike Job Process support did not load.'
}

function Invoke-DawnstrikeJobProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$WorkingDirectory,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][ValidateRange(1, 86400)][int]$TimeoutSeconds,
        [Parameter()][ValidateRange(1, 60)][int]$OutputDrainTimeoutSeconds = 5,
        [Parameter()][hashtable]$EnvironmentOverrides = @{}
    )

    $environmentPairs = @(
        $EnvironmentOverrides.GetEnumerator() |
            Sort-Object -Property Key |
            ForEach-Object { "{0}={1}" -f $_.Key, $_.Value }
    )
    return [Dawnstrike.Native.JobProcessRunner]::Run(
        $FilePath,
        @($ArgumentList),
        $WorkingDirectory,
        $Label,
        $TimeoutSeconds * 1000,
        $OutputDrainTimeoutSeconds * 1000,
        $environmentPairs
    )
}
