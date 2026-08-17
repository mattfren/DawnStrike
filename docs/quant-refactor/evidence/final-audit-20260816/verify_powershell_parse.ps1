$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$failures = @()
$files = Get-ChildItem -LiteralPath (Join-Path $repo "scripts") -Filter "*.ps1" -Recurse | Sort-Object FullName
foreach ($file in $files) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) {
        $failures += "$($file.FullName): $($errors.Message -join '; ')"
    }
}
if ($failures.Count -gt 0) { throw ($failures -join [Environment]::NewLine) }
Write-Output ("parsed={0}; failures=0" -f $files.Count)
