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
    # Byte equality of the root/static/function manifests is asserted below;
    # tolerate PowerShell/Python newline conventions here after strict schema
    # and duplicate-key validation.
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

function Get-VercelRelativePath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$Path
    )
    $rootFullPath = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
    $rootItem = Get-Item -LiteralPath $rootFullPath -Force -ErrorAction Stop
    if (($rootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Vercel package root must not be a reparse point."
    }
    $rootPrefix = $rootFullPath + "\"
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Vercel package path escaped its expected root."
    }
    $relativeSegments = $fullPath.Substring($rootPrefix.Length).Split("\")
    $currentPath = $rootFullPath
    foreach ($segment in $relativeSegments) {
        if (-not $segment) { continue }
        $currentPath = Join-Path $currentPath $segment
        $item = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Vercel package path must not traverse a reparse point."
        }
    }
    return $fullPath.Substring($rootPrefix.Length)
}

function Get-VercelPackageDirectories {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $output = Join-Path $StageRoot ".vercel\output"
    return @(
        Get-ChildItem -LiteralPath $output -Recurse -Directory -Force -ErrorAction SilentlyContinue |
            ForEach-Object {
                (Get-VercelRelativePath -Root $StageRoot -Path $_.FullName).Replace("\", "/")
            } |
            Sort-Object
    )
}

function Get-VercelPackageInventory {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $output = Join-Path $StageRoot ".vercel\output"
    $inventory = [ordered]@{}
    $files = @(
        Get-ChildItem -LiteralPath $output -Recurse -File -Force -ErrorAction SilentlyContinue |
            Sort-Object -Property FullName
    )
    foreach ($file in $files) {
        $relative = (Get-VercelRelativePath -Root $StageRoot -Path $file.FullName).Replace("\", "/")
        $inventory[$relative] = [ordered]@{
            sha256 = Get-VercelFileSha256 -Path $file.FullName
            size = [int64]$file.Length
        }
    }
    return $inventory
}

function Assert-VercelPackageInventory {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][object[]]$FunctionRouteDirs,
        [object[]]$ReferencedFiles = @()
    )
    $output = Join-Path $StageRoot ".vercel\output"
    $functions = Join-Path $output "functions"
    $static = Join-Path $output "static"
    $directFunctionFiles = @(
        Get-ChildItem -LiteralPath $functions -File -Force -ErrorAction SilentlyContinue
    )
    if ($directFunctionFiles.Count -gt 0) {
        throw "Vercel prebuilt function package contains files outside the expected function routes."
    }

    $publicSource = Join-Path $StageRoot "public"
    $expectedStaticFiles = @(
        Get-ChildItem -LiteralPath $publicSource -Recurse -File -Force -ErrorAction SilentlyContinue
    )
    $actualStaticFiles = @(
        Get-ChildItem -LiteralPath $static -Recurse -File -Force -ErrorAction SilentlyContinue
    )
    $expectedStaticRelative = @(
        $expectedStaticFiles | ForEach-Object {
            (Get-VercelRelativePath -Root $publicSource -Path $_.FullName).Replace("\", "/")
        }
    )
    $actualStaticRelative = @(
        $actualStaticFiles | ForEach-Object {
            (Get-VercelRelativePath -Root $static -Path $_.FullName).Replace("\", "/")
        }
    )
    if (@(Compare-Object `
            -ReferenceObject ($expectedStaticRelative | Sort-Object) `
            -DifferenceObject ($actualStaticRelative | Sort-Object)).Count -ne 0) {
        throw "Vercel prebuilt static package contains an unexpected or missing file."
    }
    foreach ($file in $expectedStaticFiles) {
        $relative = (Get-VercelRelativePath -Root $publicSource -Path $file.FullName).Replace("\", "/")
        $actualPath = Join-Path $static ($relative -replace "/", "\")
        if ((Get-VercelFileSha256 -Path $file.FullName) -ne (Get-VercelFileSha256 -Path $actualPath)) {
            throw "Vercel prebuilt static package bytes changed for $relative."
        }
    }

    $stageFiles = @(
        Get-ChildItem -LiteralPath $StageRoot -Recurse -File -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -notlike "$(Join-Path $StageRoot '.vercel\output')*" }
    )
    foreach ($routeDir in $FunctionRouteDirs) {
        $routeFiles = @(
            Get-ChildItem -LiteralPath $routeDir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue
        )
        foreach ($file in $routeFiles) {
            if ($file.Name -in @(".vc-config.json", "vc__handler__python.py")) { continue }
            $routeRelative = (Get-VercelRelativePath -Root $routeDir.FullName -Path $file.FullName).Replace("\", "/")
            $allowed = @(
                $stageFiles | Where-Object {
                    $stageRelative = (Get-VercelRelativePath -Root $StageRoot -Path $_.FullName).Replace("\", "/")
                    $stageRelative -eq $routeRelative -or
                    $stageRelative.EndsWith("/$routeRelative", [System.StringComparison]::OrdinalIgnoreCase)
                }
            )
            if ($allowed.Count -ne 1) {
                throw "Vercel prebuilt function package contains an unexpected file: $routeRelative"
            }
            if ((Get-VercelFileSha256 -Path $file.FullName) -ne
                (Get-VercelFileSha256 -Path $allowed[0].FullName)) {
                throw "Vercel prebuilt function bytes changed for $routeRelative."
            }
        }
    }

    $packageManifestPath = Join-Path $StageRoot "vercel-package-manifest.json"
    $inventory = Get-VercelPackageInventory -StageRoot $StageRoot
    $expectedManifest = [ordered]@{
        schema_version = "dawnstrike.vercel_package_manifest.v1"
        directories = @(Get-VercelPackageDirectories -StageRoot $StageRoot)
        files = $inventory
        bindings = @(
            @($ReferencedFiles) |
                Sort-Object -Property route, source, target |
                ForEach-Object {
                    [ordered]@{
                        route = [string]$_.route
                        source = [string]$_.source
                        target = [string]$_.target
                        sha256 = [string]$_.sha256
                        size = [int64]$_.size
                    }
                }
        )
    }
    $expectedJson = $expectedManifest | ConvertTo-Json -Depth 20
    if (Test-Path -LiteralPath $packageManifestPath -PathType Leaf) {
        $raw = Get-Content -Raw -LiteralPath $packageManifestPath
        Assert-VercelJsonObjectKeysUnique -RawJson $raw
        try { $parsed = $raw | ConvertFrom-Json }
        catch { throw "Vercel package inventory manifest is unreadable." }
        $canonical = $parsed | ConvertTo-Json -Depth 20
        if (
            $raw -cne $canonical -or
            $canonical -cne $expectedJson -or
            $parsed.schema_version -ne "dawnstrike.vercel_package_manifest.v1"
        ) {
            throw "Vercel prebuilt package inventory changed after the build."
        }
    }
    else {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($packageManifestPath, $expectedJson, $utf8NoBom)
    }
}

function Get-VercelPackageManifestSha256 {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $manifestPath = Join-Path $StageRoot "vercel-package-manifest.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
        throw "Vercel package inventory manifest is missing."
    }
    return Get-VercelFileSha256 -Path $manifestPath
}

function Add-VercelFunctionPublicBindings {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    # @vercel/python 58.4.0 does not materialize includeFiles in filePathMap
    # for this prebuilt layout.  Add the explicitly governed api/public bytes
    # before sealing the package; the validator below then requires these
    # bindings in both functions and hashes every referenced stage file.
    $functions = Join-Path $StageRoot ".vercel\output\functions"
    $publicRoot = Join-Path $StageRoot "api\public"
    if (-not (Test-Path -LiteralPath $publicRoot -PathType Container)) {
        throw "Vercel function public package source is missing."
    }
    $publicFiles = @(
        Get-ChildItem -LiteralPath $publicRoot -Recurse -File -Force -ErrorAction Stop |
            Sort-Object -Property FullName
    )
    $routeDirs = @(
        Get-ChildItem -LiteralPath $functions -Recurse -Directory -Force -ErrorAction Stop |
            Where-Object { $_.Name -like "*.func" }
    )
    if ($routeDirs.Count -ne 2) {
        throw "Vercel function public package must contain exactly two function routes before binding."
    }
    foreach ($routeDir in $routeDirs) {
        $configPath = Join-Path $routeDir.FullName ".vc-config.json"
        if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
            throw "Vercel function public package is missing .vc-config.json."
        }
        $raw = Get-Content -Raw -LiteralPath $configPath
        Assert-VercelJsonObjectKeysUnique -RawJson $raw
        try { $config = $raw | ConvertFrom-Json }
        catch { throw "Vercel function public package config is unreadable." }
        $map = $config.PSObject.Properties["filePathMap"]
        if ($null -eq $map -or $null -eq $map.Value) {
            throw "Vercel function public package config is missing filePathMap."
        }
        foreach ($file in $publicFiles) {
            $relative = (Get-VercelRelativePath -Root $publicRoot -Path $file.FullName).Replace("\", "/")
            $source = "api/public/$relative"
            if ($null -eq $map.Value.PSObject.Properties[$source]) {
                $map.Value | Add-Member -MemberType NoteProperty -Name $source -Value $source
            }
        }
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 100), $utf8NoBom)
    }
}

function Assert-VercelNoEnvironmentArtifacts {
    param([Parameter(Mandatory = $true)][string]$StageRoot)
    $vercelRoot = Join-Path $StageRoot ".vercel"
    if (-not (Test-Path -LiteralPath $vercelRoot -PathType Container)) { return }
    $environmentArtifacts = @(
        Get-ChildItem -LiteralPath $vercelRoot -Recurse -File -Force -ErrorAction Stop |
            Where-Object { $_.Name.StartsWith(".env", [System.StringComparison]::OrdinalIgnoreCase) }
    )
    if ($environmentArtifacts.Count -gt 0) {
        throw "Vercel build left environment artifacts in the publication stage."
    }
}

function Assert-VercelBuiltPackage {
    param(
        [Parameter(Mandatory = $true)][string]$StageRoot,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceSha,
        [Parameter(Mandatory = $true)][string]$ExpectedSourceTree,
        [string]$ExpectedPackageManifestSha256 = ""
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
    $expectedOutputEntries = @("builds.json", "config.json", "diagnostics", "functions", "static")
    $actualOutputEntries = @(
        Get-ChildItem -LiteralPath $output -Force -ErrorAction SilentlyContinue |
            ForEach-Object { [string]$_.Name }
    )
    $unexpectedOutputEntries = @(
        Compare-Object `
            -ReferenceObject ($expectedOutputEntries | Sort-Object) `
            -DifferenceObject ($actualOutputEntries | Sort-Object) |
            Where-Object { $_.SideIndicator -eq "=>" } |
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

    $buildsPath = Join-Path $output "builds.json"
    try {
        $buildsRaw = Get-Content -Raw -LiteralPath $buildsPath
        Assert-VercelJsonObjectKeysUnique -RawJson $buildsRaw
        $buildsConfig = $buildsRaw | ConvertFrom-Json
    }
    catch {
        throw "Vercel build metadata is unreadable."
    }
    $buildRootProperties = @($buildsConfig.PSObject.Properties | ForEach-Object { [string]$_.Name })
    $expectedBuildRootProperties = @("//", "argv", "builds", "cliVersion", "detectedFramework", "target")
    if (@(Compare-Object -ReferenceObject ($expectedBuildRootProperties | Sort-Object) -DifferenceObject ($buildRootProperties | Sort-Object)).Count -ne 0 -or
        [string]$buildsConfig.cliVersion -ne "58.4.0" -or
        [string]$buildsConfig.target -ne "preview" -or
        [string]$buildsConfig.'//' -notmatch 'generated by the .*vercel build.* command') {
        throw "Vercel build metadata contains unexpected properties."
    }
    $frameworkProperties = @($buildsConfig.detectedFramework.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if (@(Compare-Object -ReferenceObject @("status") -DifferenceObject ($frameworkProperties | Sort-Object)).Count -ne 0 -or
        [string]$buildsConfig.detectedFramework.status -ne "skipped" -or
        @($buildsConfig.argv).Count -lt 2 -or
        @($buildsConfig.argv | Where-Object { [string]$_ -eq "build" }).Count -ne 1) {
        throw "Vercel build metadata has an unexpected CLI/framework identity."
    }
    $builds = $buildsConfig.PSObject.Properties["builds"]
    if ($null -eq $builds -or $null -eq $builds.Value) {
        throw "Vercel build metadata is missing builder records."
    }
    $pythonBuilds = @(
        @($builds.Value) | Where-Object {
            [string]$_.use -eq "@vercel/python"
        }
    )
    $expectedPythonSources = @("api/health.py", "api/readiness.py")
    $actualPythonSources = @($pythonBuilds | ForEach-Object { [string]$_.src })
    if (@(Compare-Object `
            -ReferenceObject ($expectedPythonSources | Sort-Object) `
            -DifferenceObject ($actualPythonSources | Sort-Object)).Count -ne 0) {
        throw "Vercel build metadata does not bind exactly the two Python functions."
    }
    if (@($builds.Value).Count -ne 3) {
        throw "Vercel build metadata must contain exactly two Python and one static builder."
    }
    foreach ($build in @($builds.Value)) {
        $buildProperties = @($build.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if (@(Compare-Object -ReferenceObject @("apiVersion", "config", "require", "requirePath", "src", "use") -DifferenceObject ($buildProperties | Sort-Object)).Count -ne 0) {
            throw "Vercel build metadata contains an unexpected builder field."
        }
        if ([string]$build.use -notin @("@vercel/python", "@vercel/static")) {
            throw "Vercel build metadata contains an unexpected builder."
        }
        if ([string]$build.require -ne [string]$build.use -or
            [string]$build.src -notmatch '^[A-Za-z0-9_./*?-]+$') {
            throw "Vercel build metadata contains an invalid builder record."
        }
        if ([string]$build.use -eq "@vercel/python") {
            if ([int]$build.apiVersion -ne -1 -or
                [string]$build.requirePath -notmatch '@vercel[\\/]python[\\/]dist[\\/]index\.js$' -or
                [string]$build.src -notin $expectedPythonSources) {
                throw "Vercel build metadata contains an invalid Python builder record."
            }
            $expectedFunctionConfigProperties = @("includeFiles", "maxDuration")
            $functionConfig = $build.config.PSObject.Properties["functions"].Value.PSObject.Properties[[string]$build.src]
            if ($null -eq $functionConfig -or
                [string]$functionConfig.Value.includeFiles -ne "api/public/**" -or
                [int]$functionConfig.Value.maxDuration -ne 10 -or
                @(Compare-Object -ReferenceObject @("functions", "includeFiles", "zeroConfig") -DifferenceObject @($build.config.PSObject.Properties | ForEach-Object { [string]$_.Name })).Count -ne 0 -or
                [string]$build.config.includeFiles -ne "api/public/**" -or
                [bool]$build.config.zeroConfig -ne $true -or
                @(Compare-Object -ReferenceObject $expectedFunctionConfigProperties -DifferenceObject @($functionConfig.Value.PSObject.Properties | ForEach-Object { [string]$_.Name })).Count -ne 0) {
                throw "Vercel build metadata contains an invalid Python config."
            }
        }
        else {
            if ([int]$build.apiVersion -ne 2 -or [string]$build.requirePath -ne "" -or
                [string]$build.src -ne "public/**/*" -or
                @(Compare-Object -ReferenceObject @("outputDirectory", "zeroConfig") -DifferenceObject @($build.config.PSObject.Properties | ForEach-Object { [string]$_.Name })).Count -ne 0 -or
                [string]$build.config.outputDirectory -ne "public" -or
                [bool]$build.config.zeroConfig -ne $true) {
                throw "Vercel build metadata contains an invalid static builder record."
            }
        }
    }
    $diagnosticsPath = Join-Path $output "diagnostics"
    $unexpectedDiagnostics = @(
        Get-ChildItem -LiteralPath $diagnosticsPath -Recurse -File -Force -ErrorAction SilentlyContinue |
            ForEach-Object { (Get-VercelRelativePath -Root $diagnosticsPath -Path $_.FullName).Replace("\", "/") } |
            Where-Object { $_ -notin @("cli_traces.json", "project-manifest.json", "deploy-manifest.json") }
    )
    if ($unexpectedDiagnostics.Count -gt 0) {
        throw "Vercel build diagnostics contains unexpected files: $($unexpectedDiagnostics -join ', ')"
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
    $allowedConfigProperties = @("version", "routes", "crons")
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
    $cronsProperty = $config.PSObject.Properties["crons"]
    if ($null -eq $cronsProperty -or @($cronsProperty.Value).Count -ne 0) {
        throw "Vercel prebuilt output config must contain an empty crons list."
    }
    $routeKinds = @()
    $expectedSecurityHeaders = [ordered]@{
        "Content-Security-Policy" = "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; upgrade-insecure-requests"
        "X-Content-Type-Options" = "nosniff"
        "Referrer-Policy" = "no-referrer"
        "Permissions-Policy" = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        "X-Frame-Options" = "DENY"
        "Cross-Origin-Opener-Policy" = "same-origin"
        "Cross-Origin-Resource-Policy" = "same-origin"
    }
    foreach ($route in @($routesProperty.Value)) {
        $routeProperties = @($route.PSObject.Properties | ForEach-Object { [string]$_.Name })
        $handle = $route.PSObject.Properties["handle"]
        $destination = $route.PSObject.Properties["dest"]
        $headers = $route.PSObject.Properties["headers"]
        $continue = $route.PSObject.Properties["continue"]
        if ($null -ne $handle -and [string]$handle.Value -eq "filesystem") {
            if (@(Compare-Object `
                    -ReferenceObject @("handle") `
                    -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0) {
                throw "Vercel prebuilt output config contains an unexpected filesystem route field."
            }
            $routeKinds += "filesystem"
            continue
        }
        if ($null -ne $handle -and [string]$handle.Value -in @("error", "miss")) {
            if (@(Compare-Object `
                    -ReferenceObject @("handle") `
                    -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0) {
                throw "Vercel prebuilt output config contains an unexpected handle route field."
            }
            $routeKinds += [string]$handle.Value
            continue
        }
        if ($null -ne $headers) {
            if (@(Compare-Object `
                    -ReferenceObject @("continue", "headers", "src") `
                    -DifferenceObject ($routeProperties | Sort-Object)).Count -ne 0 -or
                $null -eq $continue -or [bool]$continue.Value -ne $true -or
                $null -eq $route.PSObject.Properties["src"] -or
                [string]$route.src -ne "^(?:/(.*))$") {
                throw "Vercel prebuilt output config contains an unexpected security-header route."
            }
            $actualHeaderProperties = @($headers.Value.PSObject.Properties | ForEach-Object { [string]$_.Name })
            if (@(Compare-Object `
                    -ReferenceObject ($expectedSecurityHeaders.Keys | Sort-Object) `
                    -DifferenceObject ($actualHeaderProperties | Sort-Object)).Count -ne 0) {
                throw "Vercel prebuilt security-header route does not bind the exact header set."
            }
            foreach ($headerName in $expectedSecurityHeaders.Keys) {
                if ([string]$headers.Value.$headerName -cne [string]$expectedSecurityHeaders[$headerName]) {
                    throw "Vercel prebuilt security-header route changed $headerName."
                }
            }
            $routeKinds += "headers"
            continue
        }
        $src = $route.PSObject.Properties["src"]
        $status = $route.PSObject.Properties["status"]
        $check = $route.PSObject.Properties["check"]
        if ($null -ne $status -and [int]$status.Value -eq 404 -and
            $null -ne $src -and [string]$src.Value -eq "^/api(/.*)?$" -and
            @(Compare-Object -ReferenceObject @("src", "status") -DifferenceObject ($routeProperties | Sort-Object)).Count -eq 0) {
            $routeKinds += "api404"
            continue
        }
        if ($null -ne $status -and [int]$status.Value -eq 404 -and
            $null -ne $src -and [string]$src.Value -eq "^(?!/api).*$" -and
            $null -ne $destination -and [string]$destination.Value -eq "/404.html" -and
            @(Compare-Object -ReferenceObject @("dest", "src", "status") -DifferenceObject ($routeProperties | Sort-Object)).Count -eq 0) {
            $routeKinds += "nonApi404"
            continue
        }
        if ($null -ne $src -and [string]$src.Value -eq "^/api/(.+)(?:\.(?:py))$" -and
            $null -ne $destination -and [string]$destination.Value -eq '/api/$1' -and
            $null -ne $check -and [bool]$check.Value -eq $true -and
            @(Compare-Object -ReferenceObject @("check", "dest", "src") -DifferenceObject ($routeProperties | Sort-Object)).Count -eq 0) {
            $routeKinds += "pythonRewrite"
            continue
        }
        throw "Vercel prebuilt output config contains an unexpected route or route semantics: $($routeProperties -join ',')"
    }
    $expectedRouteKinds = @("headers", "filesystem", "api404", "error", "nonApi404", "miss", "pythonRewrite")
    if (@(Compare-Object -ReferenceObject ($expectedRouteKinds | Sort-Object) -DifferenceObject ($routeKinds | Sort-Object)).Count -ne 0) {
        throw "Vercel prebuilt output config does not contain exactly the expected transformed routes."
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
    $referencedFiles = @()
    $derivedRouteNames = @()
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
        if (-not $routeApiName) {
            throw "Vercel prebuilt function route has an unrecognized API binding."
        }
        $derivedRouteNames += $routeApiName
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
        $expectedVcConfigProperties = @(
            "handler", "runtime", "architecture", "maxDuration", "environment",
            "supportsResponseStreaming", "filePathMap"
        )
        $actualVcConfigProperties = @($vcConfig.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if (@(Compare-Object -ReferenceObject ($expectedVcConfigProperties | Sort-Object) -DifferenceObject ($actualVcConfigProperties | Sort-Object)).Count -ne 0) {
            throw "Vercel prebuilt function route .vc-config.json contains an unexpected schema."
        }
        $handler = $vcConfig.PSObject.Properties["handler"]
        if ($null -eq $handler -or [string]$handler.Value -ne "vc__handler__python.vc_handler") {
            throw "Vercel prebuilt function route .vc-config.json does not bind the Vercel Python wrapper."
        }
        if ([string]$vcConfig.runtime -notmatch '^python3\.[0-9]+$' -or
            [string]$vcConfig.architecture -notin @("x86_64", "arm64") -or
            [int]$vcConfig.maxDuration -le 0 -or
            [bool]$vcConfig.supportsResponseStreaming -ne $true) {
            throw "Vercel prebuilt function route .vc-config.json contains invalid runtime settings."
        }
        $environmentProperties = @($vcConfig.environment.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if (@(Compare-Object -ReferenceObject @("PYTHONPATH", "PYTHONDONTWRITEBYTECODE") -DifferenceObject ($environmentProperties | Sort-Object)).Count -ne 0 -or
            [string]$vcConfig.environment.PYTHONPATH -ne "_vendor" -or
            [string]$vcConfig.environment.PYTHONDONTWRITEBYTECODE -ne "1") {
            throw "Vercel prebuilt function route .vc-config.json contains invalid environment settings."
        }
        $filePathMapProperty = $vcConfig.PSObject.Properties["filePathMap"]
        if ($null -eq $filePathMapProperty -or $null -eq $filePathMapProperty.Value) {
            throw "Vercel prebuilt function route .vc-config.json is missing filePathMap."
        }
        $expectedApiSource = "api/$routeApiName"
        $requiredMapSources = @(
            ".python-version", "api/health.py", "api/readiness.py", "pyproject.toml", "uv.lock", "vercel.json"
        )
        $expectedPublicMapSources = @(
            Get-ChildItem -LiteralPath (Join-Path $StageRoot "api\public") -Recurse -File -Force -ErrorAction Stop |
                ForEach-Object {
                    "api/public/" + (Get-VercelRelativePath -Root (Join-Path $StageRoot "api\public") -Path $_.FullName).Replace("\", "/")
                }
        )
        $requiredMapSources += $expectedPublicMapSources
        $mapProperties = @($filePathMapProperty.Value.PSObject.Properties)
        $normalizedMapKeys = @{}
        foreach ($mapProperty in $mapProperties) {
            $mapSource = [string]$mapProperty.Name
            $mapTarget = [string]$mapProperty.Value
            if (-not $mapSource -or -not $mapTarget -or
                $mapSource -ne $mapSource.Replace("\", "/") -or
                $mapTarget -ne $mapTarget.Replace("\", "/") -or
                $mapSource.StartsWith("/") -or $mapTarget.StartsWith("/") -or
                $mapSource -match '(^|/)\.\.(/|$)' -or $mapTarget -match '(^|/)\.\.(/|$)' -or
                $mapSource -match '(^|/)\.(/|$)' -or $mapTarget -match '(^|/)\.(/|$)' -or
                $mapSource -notmatch '^[A-Za-z0-9._/-]+$' -or $mapTarget -notmatch '^[A-Za-z0-9._/-]+$') {
                throw "Vercel prebuilt function route filePathMap contains an unsafe path."
            }
            $mapKeyFolded = $mapSource.ToLowerInvariant()
            if ($normalizedMapKeys.ContainsKey($mapKeyFolded)) {
                throw "Vercel prebuilt function route filePathMap contains duplicate paths."
            }
            $normalizedMapKeys[$mapKeyFolded] = $true
            if ($mapSource -notin $requiredMapSources -and $mapSource -notlike "_vendor/*") {
                throw "Vercel prebuilt function route filePathMap contains an unexpected source."
            }
            if ($mapSource -like "_vendor/*" -and $mapTarget -notlike ".vercel/python/.venv/*") {
                throw "Vercel prebuilt function route filePathMap retargets the generated vendor package."
            }
            if ($mapSource -like "_vendor/*" -and
                $mapSource -notmatch '^_vendor/(?:pip|pip-26\.2\.1\.dist-info|vercel_runtime|vercel_runtime-0\.17\.0\.dist-info)/') {
                throw "Vercel prebuilt function route filePathMap contains an unexpected generated vendor root."
            }
            if ($mapSource -notlike "_vendor/*" -and $mapTarget -ne $mapSource) {
                throw "Vercel prebuilt function route filePathMap retargets a governed source."
            }
            $targetPath = Join-Path $StageRoot ($mapTarget -replace "/", "\")
            if (-not (Test-Path -LiteralPath $targetPath -PathType Leaf)) {
                throw "Vercel prebuilt function route filePathMap target is missing: $mapTarget"
            }
            $targetRelative = (Get-VercelRelativePath -Root $StageRoot -Path $targetPath).Replace("\", "/")
            if ($targetRelative -ne $mapTarget) {
                throw "Vercel prebuilt function route filePathMap target escaped the stage root."
            }
            $referencedFiles += [pscustomobject]@{
                route = $relativeRoute.Replace("\", "/")
                source = $mapSource
                target = $mapTarget
                sha256 = Get-VercelFileSha256 -Path $targetPath
                size = [int64](Get-Item -LiteralPath $targetPath -Force).Length
            }
        }
        foreach ($requiredSource in $requiredMapSources) {
            $mapProperty = $filePathMapProperty.Value.PSObject.Properties[$requiredSource]
            if ($null -eq $mapProperty) {
                throw "Vercel prebuilt function route filePathMap is missing $requiredSource."
            }
        }
        $sourceMapProperty = $filePathMapProperty.Value.PSObject.Properties[$expectedApiSource]
        if ($null -eq $sourceMapProperty -or [string]$sourceMapProperty.Value -ne $expectedApiSource) {
            throw "Vercel prebuilt function route filePathMap does not bind its exact API source."
        }
        foreach ($apiSource in @("api/health.py", "api/readiness.py")) {
            $stageApiPath = Join-Path $StageRoot ($apiSource -replace "/", "\")
            $mapProperty = $filePathMapProperty.Value.PSObject.Properties[$apiSource]
            $mappedTargetPath = Join-Path $StageRoot ([string]$mapProperty.Value -replace "/", "\")
            if ((Get-VercelFileSha256 -Path $mappedTargetPath) -ne (Get-VercelFileSha256 -Path $stageApiPath)) {
                throw "Vercel prebuilt function filePathMap bytes do not match exact Git source for $apiSource."
            }
        }
        $wrapperFiles = @(
            Get-ChildItem -LiteralPath $routeDir.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq "vc__handler__python.py" }
        )
        if ($wrapperFiles.Count -ne 1) {
            throw "Vercel prebuilt function route must contain exactly one Vercel Python wrapper."
        }
        $wrapper = Get-Content -Raw -LiteralPath $wrapperFiles[0].FullName
        $entrypoint = "api/$routeApiName"
        $entrypointMarker = '__VC_HANDLER_ENTRYPOINT": "' + $entrypoint + '"'
        $moduleMarker = '__VC_HANDLER_MODULE_NAME": "api.' +
            ($routeApiName -replace '\.py$', '') + '"'
        if (
            $wrapper -notmatch ([regex]::Escape($entrypointMarker)) -or
            $wrapper -notmatch ([regex]::Escape($moduleMarker))
        ) {
            throw "Vercel prebuilt function wrapper does not bind the expected source entrypoint."
        }
    }
    if (@(Compare-Object -ReferenceObject @("health.py", "readiness.py") -DifferenceObject ($derivedRouteNames | Sort-Object)).Count -ne 0) {
        throw "Vercel prebuilt function package must bind exactly one health and one readiness route."
    }
    Assert-VercelPackageInventory -StageRoot $StageRoot -FunctionRouteDirs $functionRouteDirs -ReferencedFiles $referencedFiles
    $packageManifestSha256 = Get-VercelPackageManifestSha256 -StageRoot $StageRoot
    if ($ExpectedPackageManifestSha256 -and
        $packageManifestSha256 -ne $ExpectedPackageManifestSha256.ToLowerInvariant()) {
        throw "Vercel prebuilt package inventory manifest hash changed after the build."
    }
    return $packageManifestSha256
}
