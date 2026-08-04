[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StateRoot
)

$ErrorActionPreference = "Stop"
$resolvedStateRoot = [System.IO.Path]::GetFullPath($StateRoot)
$secretsDirectory = Join-Path $resolvedStateRoot "secrets"
$environmentPath = Join-Path $secretsDirectory "runtime.env"
$keyName = "DAWNSTRIKE_FORWARD_GAP_HMAC_KEY"

New-Item -ItemType Directory -Path $secretsDirectory -Force | Out-Null
$lines = if (Test-Path -LiteralPath $environmentPath -PathType Leaf) {
    [System.IO.File]::ReadAllLines($environmentPath)
}
else {
    @()
}
$existing = @(
    $lines | Where-Object { $_ -match "^\s*$keyName\s*=" }
)
if ($existing.Count -gt 1) {
    throw "$keyName appears more than once in runtime.env."
}
if ($existing.Count -eq 1) {
    $value = ($existing[0] -split "=", 2)[1].Trim().Trim('"').Trim("'")
    if ($value.Length -lt 32) {
        throw "$keyName exists but is shorter than 32 characters."
    }
    Write-Output "Forward-session gap signing key is already configured."
    exit 0
}

$bytes = [byte[]]::new(32)
$generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $generator.GetBytes($bytes)
}
finally {
    $generator.Dispose()
}
$value = -join ($bytes | ForEach-Object { $_.ToString("x2") })
$updated = @($lines) + @("$keyName=$value")
$temporary = "$environmentPath.tmp-$PID-$([Guid]::NewGuid().ToString('N'))"
try {
    [System.IO.File]::WriteAllLines(
        $temporary,
        $updated,
        [System.Text.UTF8Encoding]::new($false)
    )
    Move-Item -LiteralPath $temporary -Destination $environmentPath -Force
}
finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
}

Write-Output "Forward-session gap signing key was generated and stored in runtime.env."
