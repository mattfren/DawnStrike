[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$RepoRoot,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$LogRoot,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedSourceSha,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ArgumentJson,
    [Parameter()][ValidateRange(1, 1800)][int]$TimeoutSeconds = 1800,
    [Parameter()][string]$DependencyStageRoot = '',
    [Parameter()][string]$DependencyStageReceiptPath = ''
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
$argumentList = @((ConvertFrom-Json -InputObject $ArgumentJson) | ForEach-Object {
    if ($_ -isnot [string]) { throw "Argument JSON must contain only strings." }
    [string]$_
})
if ($argumentList.Count -lt 1) {
    throw "Argument JSON must contain the OPS05 entrypoint."
}
$receipt = Invoke-DawnstrikeNativeProcess `
    -FilePath $python `
    -ArgumentList $argumentList `
    -LogRoot $LogRoot `
    -LogName "ops05_observer" `
    -WorkingDirectory $resolvedRoot `
    -TimeoutSeconds $TimeoutSeconds `
    -DependencyStageRoot $DependencyStageRoot `
    -DependencyStageReceiptPath $DependencyStageReceiptPath `
    -JobMemoryLimitBytes ([UInt64]268435456) `
    -ProcessTreeRssLimitBytes ([UInt64]268435456) `
    -RssSampleMilliseconds 100 `
    -OutputCaptureLimitBytes ([UInt64]8388608) `
    -SuppressConsoleReplay
$receipt | ConvertTo-Json -Depth 8
if ([int]$receipt.exit_code -ne 0) {
    exit ([int]$receipt.exit_code)
}
