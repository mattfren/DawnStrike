[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$RepoRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$LogRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSourceSha,
    [Parameter(Mandatory = $true)][string[]]$ArgumentList,
    [Parameter()][ValidateRange(1, 1800)][int]$TimeoutSeconds = 1800
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$resolvedRoot = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\')
$runner = Join-Path $resolvedRoot "scripts\dawnstrike_process_runner.ps1"
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "The existing native process runner is missing: $runner"
}
. $runner
$script:DawnstrikeExpectedReleaseSha = $ExpectedSourceSha
$python = "C:\Program Files\Dawnstrike\Python313\python.exe"
$receipt = Invoke-DawnstrikeNativeProcess `
    -FilePath $python `
    -ArgumentList $ArgumentList `
    -LogRoot $LogRoot `
    -LogName "ops05_observer" `
    -WorkingDirectory $resolvedRoot `
    -TimeoutSeconds $TimeoutSeconds `
    -SuppressConsoleReplay
$receipt | ConvertTo-Json -Depth 8
if ([int]$receipt.exit_code -ne 0) {
    exit ([int]$receipt.exit_code)
}
