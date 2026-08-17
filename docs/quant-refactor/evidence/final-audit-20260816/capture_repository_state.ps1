param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("before", "after")]
    [string]$Phase
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
Push-Location $repo
try {
    $relativePaths = @(git ls-files -co --exclude-standard) |
        ForEach-Object { $_.Replace("\", "/") } |
        Where-Object {
            $_ -notlike "docs/quant-refactor/evidence/final-audit-20260816/*" -and
            $_ -ne "docs/quant-refactor/24-final-independent-audit.md"
        } |
        Sort-Object -Unique
    $files = @()
    foreach ($relativePath in $relativePaths) {
        $absolutePath = Join-Path $repo $relativePath
        if (-not (Test-Path -LiteralPath $absolutePath -PathType Leaf)) { continue }
        $item = Get-Item -LiteralPath $absolutePath
        $files += [ordered]@{
            path = $relativePath
            length = $item.Length
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $absolutePath).Hash.ToLowerInvariant()
        }
    }
    $payload = [ordered]@{
        phase = $Phase
        captured_at_utc = [DateTimeOffset]::UtcNow.ToString("o")
        cwd = $repo
        branch = (git branch --show-current)
        head = (git rev-parse HEAD)
        origin_main = (git rev-parse origin/main)
        status = @(git status --short --branch)
        environment = [ordered]@{
            computer_name = $env:COMPUTERNAME
            os = [System.Runtime.InteropServices.RuntimeInformation]::OSDescription
            os_architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
            process_architecture = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
            powershell = $PSVersionTable.PSVersion.ToString()
            python = ((& py --version) 2>&1 | Out-String).Trim()
            pip = ((& py -m pip --version) 2>&1 | Out-String).Trim()
            node = ((& node --version) 2>&1 | Out-String).Trim()
            git = ((& git --version) 2>&1 | Out-String).Trim()
        }
        file_count = $files.Count
        files = $files
    }
    $json = ($payload | ConvertTo-Json -Depth 8)
    [System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "repository-state-$Phase.json"), "$json`n", [System.Text.UTF8Encoding]::new($false))
    $payload | Select-Object phase, captured_at_utc, cwd, branch, head, origin_main, file_count | Format-List
}
finally {
    Pop-Location
}
