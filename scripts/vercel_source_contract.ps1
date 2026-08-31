[CmdletBinding()]
param()

# Publication is built from a generated artifact, but the function entrypoints
# are executable source.  Keep their identity tied to one clean, immutable Git
# commit so a dirty/racing checkout cannot silently change the deployed code.
$jobProcessScript = Join-Path $PSScriptRoot "dawnstrike_job_process.ps1"
if (Test-Path -LiteralPath $jobProcessScript -PathType Leaf) {
    . $jobProcessScript
}

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
    if (-not ("Dawnstrike.Native.JobProcessRunner" -as [type])) {
        throw "The bounded Dawnstrike process helper is unavailable for Git blob extraction."
    }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = "git.exe"
    $startInfo.Arguments = (
        @("-C", $Root, "cat-file", "blob", "$Commit`:$RelativePath") |
            ForEach-Object {
                [Dawnstrike.Native.JobProcessRunner]::QuoteArgument([string]$_)
            }
    ) -join " "
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) { throw "Could not start Git blob extraction for $RelativePath." }
    try {
        $bytes = [System.IO.MemoryStream]::new()
        $stdoutTask = $process.StandardOutput.BaseStream.CopyToAsync($bytes)
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            try { $process.Kill() } catch { }
            throw "Git blob extraction timed out for $RelativePath."
        }
        [System.Threading.Tasks.Task]::WaitAll(@($stdoutTask, $stderrTask), 30000)
        $stderr = $stderrTask.Result
        if ($process.ExitCode -ne 0) {
            throw "Git blob extraction failed for $RelativePath`: $stderr"
        }
        [System.IO.File]::WriteAllBytes($Destination, $bytes.ToArray())
        $bytes.Dispose()
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

function Convert-VercelSourceManifestToCanonicalJson {
    param([Parameter(Mandatory = $true)][string]$RawJson)
    try { $parsed = $RawJson | ConvertFrom-Json }
    catch { throw "Vercel source manifest is unreadable." }
    $health = $parsed.api_sha256.PSObject.Properties["api/health.py"]
    $readiness = $parsed.api_sha256.PSObject.Properties["api/readiness.py"]
    if (
        $parsed.schema_version -ne "dawnstrike.vercel_source_manifest.v1" -or
        [string]$parsed.source_sha -notmatch '^[0-9a-f]{40}$' -or
        [string]$parsed.source_tree -notmatch '^[0-9a-f]{40}$' -or
        $null -eq $health -or [string]$health.Value -notmatch '^[0-9a-f]{64}$' -or
        $null -eq $readiness -or [string]$readiness.Value -notmatch '^[0-9a-f]{64}$'
    ) {
        throw "Vercel source manifest has an invalid schema or hash."
    }
    # Rebuild from the exact allowlisted shape and key order.  Comparing this
    # to the raw bytes rejects duplicate keys, extra fields, and reordering.
    $canonical = [ordered]@{
        schema_version = "dawnstrike.vercel_source_manifest.v1"
        source_sha = [string]$parsed.source_sha
        source_tree = [string]$parsed.source_tree
        api_sha256 = [ordered]@{
            "api/health.py" = [string]$health.Value
            "api/readiness.py" = [string]$readiness.Value
        }
    }
    return ($canonical | ConvertTo-Json -Depth 8)
}

function Get-VercelSourceManifestCanonicalJson {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Vercel source manifest is missing: $Path"
    }
    $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
    try { $raw = $utf8.GetString([System.IO.File]::ReadAllBytes($Path)) }
    catch { throw "Vercel source manifest is not valid UTF-8: $Path" }
    $canonical = Convert-VercelSourceManifestToCanonicalJson -RawJson $raw
    if ($raw -cne $canonical) {
        throw "Vercel source manifest is not the deterministic canonical encoding: $Path"
    }
    return $canonical
}

function Assert-VercelSourceManifestJson {
    param(
        [Parameter(Mandatory = $true)][string]$RawJson,
        [Parameter(Mandatory = $true)][string]$ExpectedCanonicalJson,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $trimmed = $RawJson.Trim()
    $canonical = Convert-VercelSourceManifestToCanonicalJson -RawJson $trimmed
    if ($trimmed -cne $canonical) {
        throw "$Label source manifest is not the deterministic canonical encoding."
    }
    if ($canonical -cne $ExpectedCanonicalJson) {
        throw "$Label source manifest does not match the verified package manifest."
    }
}

function Assert-VercelManifestBytesEqual {
    param(
        [Parameter(Mandatory = $true)][string]$ExpectedPath,
        [Parameter(Mandatory = $true)][string]$ActualPath,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $expected = [System.IO.File]::ReadAllBytes($ExpectedPath)
    $actual = [System.IO.File]::ReadAllBytes($ActualPath)
    if (
        $expected.Length -ne $actual.Length -or
        -not [System.Linq.Enumerable]::SequenceEqual([byte[]]$expected, [byte[]]$actual)
    ) {
        throw "$Label source manifest bytes do not match the root manifest."
    }
}

function Assert-VercelStagedSourceManifest {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree
    )
    $sourceManifestPath = Join-Path $StageRoot "vercel-source-manifest.json"
    $canonical = Get-VercelSourceManifestCanonicalJson -Path $sourceManifestPath
    $expectedCanonical = [ordered]@{
        schema_version = "dawnstrike.vercel_source_manifest.v1"
        source_sha = $ExpectedSourceSha
        source_tree = $ExpectedSourceTree
        api_sha256 = [ordered]@{
            "api/health.py" = $null
            "api/readiness.py" = $null
        }
    }
    $parsed = $canonical | ConvertFrom-Json
    $expectedCanonical.api_sha256["api/health.py"] = [string]$parsed.api_sha256.PSObject.Properties["api/health.py"].Value
    $expectedCanonical.api_sha256["api/readiness.py"] = [string]$parsed.api_sha256.PSObject.Properties["api/readiness.py"].Value
    if (($expectedCanonical | ConvertTo-Json -Depth 8) -cne $canonical) {
        throw "Vercel source manifest does not match the verified Git commit and tree."
    }
    Assert-VercelManifestBytesEqual -ExpectedPath $sourceManifestPath `
        -ActualPath (Join-Path $StageRoot "public\vercel-source-manifest.json") `
        -Label "Static package"
    Assert-VercelManifestBytesEqual -ExpectedPath $sourceManifestPath `
        -ActualPath (Join-Path $StageRoot "api\public\vercel-source-manifest.json") `
        -Label "Function public package"
    $sourceManifest = $parsed
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

function Assert-VercelBuiltPackage {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree
    )
    Assert-VercelStagedSourceManifest `
        -StageRoot $StageRoot `
        -ExpectedSourceSha $ExpectedSourceSha `
        -ExpectedSourceTree $ExpectedSourceTree
    $output = Join-Path $StageRoot ".vercel\output"
    $staticManifest = Join-Path $output "static\vercel-source-manifest.json"
    $functions = Join-Path $output "functions"
    if (-not (Test-Path -LiteralPath $output -PathType Container)) {
        throw "Vercel prebuilt output package is missing."
    }
    if (-not (Test-Path -LiteralPath $staticManifest -PathType Leaf)) {
        throw "Vercel prebuilt static source manifest is missing."
    }
    Assert-VercelManifestBytesEqual `
        -ExpectedPath (Join-Path $StageRoot "vercel-source-manifest.json") `
        -ActualPath $staticManifest `
        -Label "Vercel prebuilt static package"
    if (-not (Test-Path -LiteralPath $functions -PathType Container)) {
        throw "Vercel prebuilt function package is missing."
    }
    foreach ($apiName in @("health.py", "readiness.py")) {
        $stageApi = Join-Path $StageRoot ("api\" + $apiName)
        $baseName = [System.IO.Path]::GetFileNameWithoutExtension($apiName)
        $routeDirs = @(
            Get-ChildItem -LiteralPath $functions -Recurse -Directory -Force -ErrorAction SilentlyContinue |
                Where-Object {
                    ($_.Name -eq "$baseName.func" -and $_.Parent.Name -eq "api") -or
                    $_.Name -eq "api_$baseName.func"
                }
        )
        if ($routeDirs.Count -ne 1) {
            throw "Vercel prebuilt function package does not have exactly one route for api/$baseName.py."
        }
        $candidates = @(
            Get-ChildItem -LiteralPath $routeDirs[0].FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq $apiName }
        )
        if ($candidates.Count -ne 1) {
            throw "Vercel prebuilt function route does not contain exactly one $apiName source copy."
        }
        $expectedHash = Get-VercelFileSha256 -Path $stageApi
        if ((Get-VercelFileSha256 -Path $candidates[0].FullName) -ne $expectedHash) {
            throw "Vercel prebuilt function bytes do not match exact Git source for $apiName."
        }
    }
}
