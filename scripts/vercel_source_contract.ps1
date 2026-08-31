[CmdletBinding()]
param()

# Publication is built from a generated artifact, but the function entrypoints
# are executable source.  Keep their identity tied to one clean, immutable Git
# commit so a dirty/racing checkout cannot silently change the deployed code.
$jobProcessScript = Join-Path $PSScriptRoot "dawnstrike_job_process.ps1"
if (Test-Path -LiteralPath $jobProcessScript -PathType Leaf) {
    . $jobProcessScript
}

# Windows PowerShell's ConvertFrom-Json keeps the last value for a duplicate
# object key.  That is unsafe for a receipt/config boundary: an attacker can
# append a second handler or route and rely on a different parser downstream.
# Keep a tiny dependency-free JSON grammar guard beside the contract so every
# JSON object is rejected before PowerShell materializes it.
if (-not ("Dawnstrike.Native.VercelJsonGuard" -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Text;

namespace Dawnstrike.Native {
    public static class VercelJsonGuard {
        public static void AssertUniqueObjectKeys(string json) {
            new Parser(json).ParseDocument();
        }

        private sealed class Parser {
            private readonly string text;
            private int index;

            internal Parser(string value) {
                if (value == null) { throw new ArgumentNullException("json"); }
                text = value;
            }

            internal void ParseDocument() {
                SkipWhitespace();
                ParseValue();
                SkipWhitespace();
                ExpectEnd();
            }

            private void ParseValue() {
                SkipWhitespace();
                if (index >= text.Length) { Fail("value is missing"); }
                switch (text[index]) {
                    case '{': ParseObject(); return;
                    case '[': ParseArray(); return;
                    case '"': ParseString(); return;
                    case 't': ParseLiteral("true"); return;
                    case 'f': ParseLiteral("false"); return;
                    case 'n': ParseLiteral("null"); return;
                    default: ParseNumber(); return;
                }
            }

            private void ParseObject() {
                index++;
                var keys = new HashSet<string>(StringComparer.Ordinal);
                SkipWhitespace();
                if (Take('}')) { return; }
                while (true) {
                    SkipWhitespace();
                    if (index >= text.Length || text[index] != '"') {
                        Fail("object key must be a string");
                    }
                    string key = ParseString();
                    if (!keys.Add(key)) { Fail("duplicate object key: " + key); }
                    SkipWhitespace();
                    Expect(':');
                    ParseValue();
                    SkipWhitespace();
                    if (Take('}')) { return; }
                    Expect(',');
                }
            }

            private void ParseArray() {
                index++;
                SkipWhitespace();
                if (Take(']')) { return; }
                while (true) {
                    ParseValue();
                    SkipWhitespace();
                    if (Take(']')) { return; }
                    Expect(',');
                }
            }

            private string ParseString() {
                Expect('"');
                var value = new StringBuilder();
                while (index < text.Length) {
                    char current = text[index++];
                    if (current == '"') { return value.ToString(); }
                    if (current < 0x20) { Fail("control character in string"); }
                    if (current != '\\') { value.Append(current); continue; }
                    if (index >= text.Length) { Fail("truncated string escape"); }
                    char escaped = text[index++];
                    switch (escaped) {
                        case '"': value.Append('"'); break;
                        case '\\': value.Append('\\'); break;
                        case '/': value.Append('/'); break;
                        case 'b': value.Append('\b'); break;
                        case 'f': value.Append('\f'); break;
                        case 'n': value.Append('\n'); break;
                        case 'r': value.Append('\r'); break;
                        case 't': value.Append('\t'); break;
                        case 'u': value.Append(ParseUnicodeEscape()); break;
                        default: Fail("invalid string escape"); break;
                    }
                }
                Fail("unterminated string");
                return null;
            }

            private char ParseUnicodeEscape() {
                if (index + 4 > text.Length) { Fail("truncated unicode escape"); }
                int code = 0;
                for (int i = 0; i < 4; i++) {
                    int digit = Hex(text[index++]);
                    if (digit < 0) { Fail("invalid unicode escape"); }
                    code = (code * 16) + digit;
                }
                return (char)code;
            }

            private void ParseNumber() {
                int start = index;
                if (Take('-')) { }
                if (Take('0')) { }
                else {
                    RequireDigits();
                }
                if (Take('.')) { RequireDigits(); }
                if (Take('e') || Take('E')) {
                    if (Take('+') || Take('-')) { }
                    RequireDigits();
                }
                if (index == start) { Fail("invalid value"); }
            }

            private void RequireDigits() {
                int start = index;
                while (index < text.Length && text[index] >= '0' && text[index] <= '9') { index++; }
                if (index == start) { Fail("digits are missing"); }
            }

            private void ParseLiteral(string literal) {
                if (index + literal.Length > text.Length ||
                    !String.Equals(text.Substring(index, literal.Length), literal, StringComparison.Ordinal)) {
                    Fail("invalid literal");
                }
                index += literal.Length;
            }

            private void SkipWhitespace() {
                while (index < text.Length && " \t\r\n".IndexOf(text[index]) >= 0) { index++; }
            }

            private bool Take(char expected) {
                if (index < text.Length && text[index] == expected) { index++; return true; }
                return false;
            }

            private void Expect(char expected) {
                if (!Take(expected)) { Fail("expected '" + expected + "'"); }
            }

            private void ExpectEnd() {
                if (index != text.Length) { Fail("trailing JSON content"); }
            }

            private static int Hex(char value) {
                if (value >= '0' && value <= '9') return value - '0';
                if (value >= 'a' && value <= 'f') return value - 'a' + 10;
                if (value >= 'A' && value <= 'F') return value - 'A' + 10;
                return -1;
            }

            private void Fail(string message) {
                throw new FormatException("Invalid JSON at offset " + index + ": " + message);
            }
        }
    }
}
'@ -ErrorAction Stop
}

function Assert-VercelJsonObjectKeysUnique {
    param([Parameter(Mandatory = $true)][string]$RawJson)
    try {
        [Dawnstrike.Native.VercelJsonGuard]::AssertUniqueObjectKeys($RawJson)
    }
    catch {
        throw "Vercel JSON is invalid or contains duplicate object keys: $($_.Exception.Message)"
    }
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
    Assert-VercelJsonObjectKeysUnique -RawJson $RawJson
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

    # The Build Output API package is deliberately a tiny closed-world
    # boundary.  Do not let an additional generated config, middleware, or
    # function become reachable merely because Vercel happened to accept it.
    $expectedOutputEntries = @("config.json", "functions", "static")
    $actualOutputEntries = @(
        Get-ChildItem -LiteralPath $output -Force -ErrorAction SilentlyContinue |
            ForEach-Object { [string]$_.Name }
    )
    $unexpectedOutputEntries = @(
        Compare-Object `
            -ReferenceObject ($expectedOutputEntries | Sort-Object) `
            -DifferenceObject ($actualOutputEntries | Sort-Object) |
            Where-Object { $_.SideIndicator -eq ">" } |
            ForEach-Object { [string]$_.InputObject }
    )
    if ($unexpectedOutputEntries.Count -gt 0) {
        throw "Vercel prebuilt output contains unexpected entries: $($unexpectedOutputEntries -join ', ')"
    }
    foreach ($requiredEntry in $expectedOutputEntries) {
        if (-not (Test-Path -LiteralPath (Join-Path $output $requiredEntry))) {
            throw "Vercel prebuilt output is missing required entry: $requiredEntry"
        }
    }

    $configPath = Join-Path $output "config.json"
    try {
        $configRaw = Get-Content -Raw -LiteralPath $configPath
        Assert-VercelJsonObjectKeysUnique -RawJson $configRaw
        $config = $configRaw | ConvertFrom-Json
    }
    catch {
        throw "Vercel prebuilt output config is unreadable."
    }
    if ($config.version -ne 3) {
        throw "Vercel prebuilt output config must use Build Output API version 3."
    }
    $allowedConfigProperties = @("version", "routes")
    $unexpectedConfigProperties = @(
        $config.PSObject.Properties |
            Where-Object { $_.Name -notin $allowedConfigProperties } |
            ForEach-Object { [string]$_.Name }
    )
    if ($unexpectedConfigProperties.Count -gt 0) {
        throw "Vercel prebuilt output config contains unexpected properties: $($unexpectedConfigProperties -join ', ')"
    }
    $routesProperty = $config.PSObject.Properties["routes"]
    if ($null -eq $routesProperty -or $null -eq $routesProperty.Value) {
        throw "Vercel prebuilt output config is missing routes."
    }
    $routeBindings = @()
    $filesystemRoutes = 0
    foreach ($route in @($routesProperty.Value)) {
        $routeProperties = @($route.PSObject.Properties | ForEach-Object { [string]$_.Name })
        $handle = $route.PSObject.Properties["handle"]
        $destination = $route.PSObject.Properties["dest"]
        if ($null -ne $handle -and [string]$handle.Value -eq "filesystem") {
            if (@(Compare-Object `
                    -ReferenceObject @("handle") `
                    -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0) {
                throw "Vercel prebuilt output config contains an unexpected filesystem route field."
            }
            $filesystemRoutes++
            continue
        }
        if ($null -eq $destination) {
            throw "Vercel prebuilt output config contains an unexpected route."
        }
        $dest = ([string]$destination.Value).TrimStart("/")
        $apiName = switch -Regex ($dest) {
            '^api/health(?:\.py)?$' { "health.py"; break }
            '^api/readiness(?:\.py)?$' { "readiness.py"; break }
            default { $null }
        }
        $source = $route.PSObject.Properties["src"]
        if (-not $apiName -or $null -eq $source) {
            throw "Vercel prebuilt output config contains an unexpected route destination."
        }
        if (@(Compare-Object `
                -ReferenceObject @("dest", "src") `
                -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0) {
            throw "Vercel prebuilt output config contains an unexpected API route field."
        }
        $sourcePattern = [string]$source.Value
        $routeName = [System.IO.Path]::GetFileNameWithoutExtension($apiName)
        if ($sourcePattern -notmatch ("(^|/)api/" + [regex]::Escape($routeName) + "(?:[^A-Za-z0-9_]|$)")) {
            throw "Vercel prebuilt output config route does not bind api/$apiName exactly."
        }
        $routeBindings += $apiName
    }
    if ($filesystemRoutes -ne 1) {
        throw "Vercel prebuilt output config must contain exactly one filesystem route."
    }
    $expectedRouteBindings = @("health.py", "readiness.py")
    if (@(Compare-Object `
            -ReferenceObject ($expectedRouteBindings | Sort-Object) `
            -DifferenceObject ($routeBindings | Sort-Object)).Count -ne 0) {
        throw "Vercel prebuilt output config does not bind exactly the expected API routes."
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

    $expectedFunctionRouteNames = @(
        "api\health.func", "api\readiness.func",
        "api_health.func", "api_readiness.func",
        "api\health.py.func", "api\readiness.py.func",
        "api_health.py.func", "api_readiness.py.func"
    )
    $functionRouteDirs = @(
        Get-ChildItem -LiteralPath $functions -Recurse -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "*.func" }
    )
    if ($functionRouteDirs.Count -ne 2) {
        throw "Vercel prebuilt function package must contain exactly two function routes."
    }
    $functionsPrefix = [System.IO.Path]::GetFullPath($functions).TrimEnd("\") + "\"
    foreach ($routeDir in $functionRouteDirs) {
        $routeFullPath = [System.IO.Path]::GetFullPath($routeDir.FullName)
        if (-not $routeFullPath.StartsWith(
                $functionsPrefix,
                [System.StringComparison]::OrdinalIgnoreCase
            )) {
            throw "Vercel prebuilt function route escaped the functions root."
        }
        # Path.GetRelativePath is unavailable in Windows PowerShell 5.1.
        $relativeRoute = $routeFullPath.Substring($functionsPrefix.Length)
        if ($relativeRoute -notin $expectedFunctionRouteNames) {
            throw "Vercel prebuilt function package contains an unexpected function route: $relativeRoute"
        }
        $routeApiName = if ($relativeRoute -match "health(?:\.py)?\.func$") {
            "health.py"
        }
        elseif ($relativeRoute -match "readiness(?:\.py)?\.func$") {
            "readiness.py"
        }
        else { $null }
        $vcConfigs = @(
            Get-ChildItem -LiteralPath $routeDir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq ".vc-config.json" }
        )
        if ($vcConfigs.Count -ne 1) {
            throw "Vercel prebuilt function route must contain exactly one .vc-config.json binding."
        }
        try {
            $vcConfigRaw = Get-Content -Raw -LiteralPath $vcConfigs[0].FullName
            Assert-VercelJsonObjectKeysUnique -RawJson $vcConfigRaw
            $vcConfig = $vcConfigRaw | ConvertFrom-Json
        }
        catch {
            throw "Vercel prebuilt function route has an unreadable .vc-config.json binding."
        }
        $handler = $vcConfig.PSObject.Properties["handler"]
        if ($null -eq $handler -or [string]$handler.Value -match "\.\.[\\/]" -or
            ([string]$handler.Value -replace "\\", "/").TrimStart("/") -notmatch ("(?:^|/)" + [regex]::Escape($routeApiName) + "$")) {
            throw "Vercel prebuilt function route .vc-config.json does not bind the expected handler."
        }
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
