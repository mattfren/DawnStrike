Set-StrictMode -Version Latest

function Assert-MoverLabInputFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label does not exist: $Path"
    }
}

function Invoke-MoverLabJson {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [string]$Operation,

        [int[]]$AllowedExitCodes = @(0)
    )

    $outputLines = @(& $Python @Arguments)
    $exitCode = $LASTEXITCODE
    $outputText = $outputLines -join [Environment]::NewLine

    if ([string]::IsNullOrWhiteSpace($outputText)) {
        throw "$Operation returned no JSON (exit code $exitCode)."
    }

    try {
        $payload = $outputText | ConvertFrom-Json
    }
    catch {
        throw "$Operation returned invalid JSON (exit code $exitCode): $outputText"
    }

    if ($exitCode -notin $AllowedExitCodes) {
        $rejected = $payload.PSObject.Properties["rejected_path"]
        $failed = $payload.PSObject.Properties["failed_checks"]
        $status = $payload.PSObject.Properties["status"]
        $detail = if ($rejected -and $rejected.Value) {
            " Inspect rejected evidence at $($rejected.Value)."
        }
        elseif ($failed -and $failed.Value) {
            " Failed checks: $($failed.Value -join ', ')."
        }
        else {
            " Status: $(if ($status) { $status.Value } else { 'unknown' })."
        }
        throw "$Operation failed closed with exit code $exitCode.$detail"
    }

    return $payload
}

function ConvertTo-MoverLabAwareTimestamp {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ($Value -notmatch '(Z|[+-]\d{2}:\d{2})$') {
        throw "Timestamp must include Z or an explicit UTC offset: $Value"
    }

    try {
        $parsed = [DateTimeOffset]::Parse(
            $Value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
    }
    catch {
        throw "Timestamp is not valid ISO 8601: $Value"
    }

    return $parsed.ToString("o")
}
