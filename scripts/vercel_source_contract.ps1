[CmdletBinding()]
param()

# Publication is built from a generated artifact, but the function entrypoints
# are executable source.  Keep their identity tied to one clean, immutable Git
# commit so a dirty/racing checkout cannot silently change the deployed code.

function Invoke-VercelGitText {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $output = & git.exe -C $Root @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed: $((@($output) | ForEach-Object { [string]$_ }) -join ' ')"
    }
    return ((@($output) | ForEach-Object { [string]$_ }) -join "`n").Trim()
}

function Get-VercelIgnoredPublicationPaths {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$AllowedStageRoot = ""
    )
    $allowedPrefix = ""
    if ($AllowedStageRoot) {
        $allowedPrefix = [System.IO.Path]::GetFullPath($AllowedStageRoot).TrimEnd('\') + '\'
    }
    $ignored = Invoke-VercelGitText `
        -Root $Root `
        -Arguments @("ls-files", "--others", "--ignored", "--exclude-standard", "-z") `
        -Label "Ignored publication artifact verification"
    return @(
        ([string]$ignored).Split([char]0, [System.StringSplitOptions]::RemoveEmptyEntries) |
            Where-Object {
                $relative = [string]$_
                $full = [System.IO.Path]::GetFullPath((Join-Path $Root $relative))
                $allowed = $allowedPrefix -and $full.StartsWith(
                    $allowedPrefix,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
                if ($allowed) { return $false }
                $name = [System.IO.Path]::GetFileName($relative).ToLowerInvariant()
                $extension = [System.IO.Path]::GetExtension($relative).ToLowerInvariant()
                $extension -in @(
                    ".ps1", ".psm1", ".py", ".pyc", ".pyd", ".dll", ".exe",
                    ".com", ".bat", ".cmd", ".sh", ".pth"
                ) -or $name -in @("sitecustomize.py", "usercustomize.py")
            }
    )
}

function Get-VercelGitSourceContract {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [string]$AllowedStageRoot = ""
    )
    $top = Invoke-VercelGitText -Root $Root -Arguments @("rev-parse", "--show-toplevel") `
        -Label "Publication Git root verification"
    if (-not [System.String]::Equals(
        [System.IO.Path]::GetFullPath($top).TrimEnd('\'),
        [System.IO.Path]::GetFullPath($Root).TrimEnd('\'),
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Publication Git root does not match the requested project root."
    }
    $head = (Invoke-VercelGitText -Root $Root -Arguments @("rev-parse", "HEAD") `
        -Label "Publication Git HEAD verification").ToLowerInvariant()
    $tree = (Invoke-VercelGitText -Root $Root -Arguments @("rev-parse", "HEAD^{tree}") `
        -Label "Publication Git tree verification").ToLowerInvariant()
    if ($head -notmatch '^[0-9a-f]{40}$' -or $tree -notmatch '^[0-9a-f]{40}$') {
        throw "Publication Git identity is invalid."
    }
    $status = Invoke-VercelGitText `
        -Root $Root `
        -Arguments @("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none") `
        -Label "Publication Git cleanliness verification"
    if ($status) {
        throw "Publication Git checkout is not clean."
    }
    $forbiddenIgnored = @(Get-VercelIgnoredPublicationPaths `
        -Root $Root `
        -AllowedStageRoot $AllowedStageRoot)
    if ($forbiddenIgnored.Count -gt 0) {
        throw "Publication Git checkout contains ignored executable or Python-startup artifacts: $($forbiddenIgnored -join ', ')"
    }
    return [pscustomobject]@{ head = $head; tree = $tree }
}

function Assert-VercelGitSourceStable {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree,
        [string]$AllowedStageRoot = ""
    )
    $actual = Get-VercelGitSourceContract -Root $Root -AllowedStageRoot $AllowedStageRoot
    if ($actual.head -ne $ExpectedSourceSha.ToLowerInvariant()) {
        throw "Publication source HEAD changed during staging or deployment."
    }
    if ($actual.tree -ne $ExpectedSourceTree.ToLowerInvariant()) {
        throw "Publication source Git tree changed during staging or deployment."
    }
    return $actual
}

function Write-VercelGitBlob {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$RelativePath,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = "git.exe"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.ArgumentList.Add("-C")
    $startInfo.ArgumentList.Add($Root)
    $startInfo.ArgumentList.Add("cat-file")
    $startInfo.ArgumentList.Add("blob")
    $startInfo.ArgumentList.Add("$Commit`:$RelativePath")
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Could not start Git blob extraction for $RelativePath." }
    try {
        $destinationStream = [System.IO.File]::Open(
            $Destination,
            [System.IO.FileMode]::Create,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None
        )
        try { $process.StandardOutput.BaseStream.CopyTo($destinationStream) }
        finally { $destinationStream.Dispose() }
        $stderr = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) {
            throw "Git blob extraction failed for $RelativePath`: $stderr"
        }
    }
    finally { $process.Dispose() }
}

function Get-VercelFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Expected publication file is missing: $Path"
    }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-VercelStagedSourceManifest {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree
    )
    $sourceManifestPath = Join-Path $StageRoot "vercel-source-manifest.json"
    if (-not (Test-Path -LiteralPath $sourceManifestPath -PathType Leaf)) {
        throw "Vercel source manifest is missing from the candidate."
    }
    try { $sourceManifest = Get-Content -Raw -LiteralPath $sourceManifestPath | ConvertFrom-Json }
    catch { throw "Vercel source manifest is unreadable." }
    if (
        $sourceManifest.schema_version -ne "dawnstrike.vercel_source_manifest.v1" -or
        $sourceManifest.source_sha -ne $ExpectedSourceSha -or
        $sourceManifest.source_tree -ne $ExpectedSourceTree
    ) {
        throw "Vercel source manifest does not match the verified Git commit and tree."
    }
    foreach ($apiPath in @("api/health.py", "api/readiness.py")) {
        $apiProperty = $sourceManifest.api_sha256.PSObject.Properties[$apiPath]
        if ($null -eq $apiProperty -or [string]$apiProperty.Value -notmatch '^[0-9a-f]{64}$') {
            throw "Vercel source manifest is missing a valid hash for $apiPath."
        }
        $stagedApiPath = Join-Path $StageRoot ($apiPath -replace "/", "\")
        $actualApiHash = Get-VercelFileSha256 -Path $stagedApiPath
        if ($actualApiHash -ne [string]$apiProperty.Value) {
            throw "Staged API bytes do not match the immutable Vercel source manifest for $apiPath."
        }
    }
}
