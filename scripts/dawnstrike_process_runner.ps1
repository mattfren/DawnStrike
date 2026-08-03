Set-StrictMode -Version Latest

function Invoke-DawnstrikeNativeProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter()][string[]]$ArgumentList = @(),
        [Parameter(Mandatory = $true)][string]$LogRoot,
        [Parameter(Mandatory = $true)][string]$LogName
    )

    $startedAt = (Get-Date).ToUniversalTime()
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    $safeName = $LogName -replace "[^A-Za-z0-9._-]", "_"
    $stdoutPath = Join-Path $LogRoot "$safeName.stdout.log"
    $stderrPath = Join-Path $LogRoot "$safeName.stderr.log"
    $receiptPath = Join-Path $LogRoot "$safeName.receipt.json"
    $exitCode = 127
    $startError = $null

    try {
        # Direct redirection preserves the native process exit code.  Do not put
        # this invocation in a PowerShell pipeline: `$LASTEXITCODE` would then
        # describe Tee-Object rather than the Python process on Windows PS 5.1.
        & $FilePath @ArgumentList 1> $stdoutPath 2> $stderrPath
        $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    }
    catch {
        $startError = $_.Exception.Message
        Set-Content -LiteralPath $stderrPath -Value $startError -Encoding UTF8
    }

    $completedAt = (Get-Date).ToUniversalTime()
    $stdoutHash = if (Test-Path -LiteralPath $stdoutPath -PathType Leaf) {
        (Get-FileHash -LiteralPath $stdoutPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { $null }
    $stderrHash = if (Test-Path -LiteralPath $stderrPath -PathType Leaf) {
        (Get-FileHash -LiteralPath $stderrPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else { $null }
    $receipt = [ordered]@{
        schema_version = "dawnstrike.native_process_receipt.v1"
        process_name = [IO.Path]::GetFileName($FilePath)
        argument_count = @($ArgumentList).Count
        started_at = $startedAt.ToString("o")
        completed_at = $completedAt.ToString("o")
        duration_ms = [math]::Round(($completedAt - $startedAt).TotalMilliseconds)
        exit_code = $exitCode
        stdout_path = $stdoutPath
        stderr_path = $stderrPath
        stdout_sha256 = $stdoutHash
        stderr_sha256 = $stderrHash
        start_error = $startError
        research_only = $true
        broker_execution_enabled = $false
    }
    $receipt | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $receiptPath -Encoding UTF8

    foreach ($path in @($stdoutPath, $stderrPath)) {
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            Get-Content -LiteralPath $path | ForEach-Object { [Console]::Out.WriteLine($_) }
        }
    }
    $receipt["receipt_path"] = $receiptPath
    return [pscustomobject]$receipt
}

function Resolve-DawnstrikeReleaseSha {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RuntimeRoot,
        [Parameter(Mandatory = $true)][string]$LogRoot
    )

    $receipt = Invoke-DawnstrikeNativeProcess `
        -FilePath "git.exe" `
        -ArgumentList @("-C", $RuntimeRoot, "rev-parse", "HEAD") `
        -LogRoot $LogRoot `
        -LogName "resolve_release_sha"
    if ($receipt.exit_code -ne 0) {
        throw "Could not resolve the deployed runtime release SHA."
    }
    $sha = (Get-Content -LiteralPath $receipt.stdout_path -Raw).Trim()
    if ($sha -notmatch "^[0-9a-fA-F]{40}$") {
        throw "Runtime release SHA was not a full Git commit SHA."
    }
    return $sha.ToLowerInvariant()
}
