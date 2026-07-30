[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")),
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$resolvedRoot = (Resolve-Path $ProjectRoot).Path
$publicRoot = Join-Path $resolvedRoot "build\public"
if (-not (Test-Path -LiteralPath (Join-Path $publicRoot "index.html") -PathType Leaf)) {
    throw "Build the public dashboard first: $publicRoot"
}
Start-Process -FilePath py.exe `
    -ArgumentList @("-m", "http.server", $Port, "--directory", $publicRoot) `
    -WorkingDirectory $resolvedRoot `
    -WindowStyle Hidden
Write-Output "Serving $publicRoot at http://127.0.0.1:$Port"
