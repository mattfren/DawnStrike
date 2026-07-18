param(
    [string]$DashboardScript = "app.py",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8502,
    [int[]]$ExtraPortsToStop = @(8503),
    [string]$RuntimeRoot = "",
    [switch]$NoOpen,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

function Resolve-OperationalPath {
    param(
        [string]$Value,
        [string]$Root
    )

    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $Root $Value))
}

function Get-WebListenerProcessIds {
    param([int]$TargetPort)

    $listeners = Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
    $ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($ownerIds.Count -eq 0) {
        $netstatPattern = "^\s*TCP\s+\S+:$TargetPort\s+\S+\s+LISTENING\s+(\d+)\s*$"
        $ownerIds = @(
            (& netstat.exe -ano -p TCP 2>$null) | ForEach-Object {
                if ($_ -match $netstatPattern) {
                    [int]$Matches[1]
                }
            } | Select-Object -Unique
        )
    }
    return @($ownerIds)
}

function Stop-WebListener {
    param([int]$TargetPort)

    $ownerIds = @(Get-WebListenerProcessIds -TargetPort $TargetPort)
    foreach ($ownerId in $ownerIds) {
        $proc = Get-Process -Id $ownerId -ErrorAction SilentlyContinue
        if ($proc) {
            $command = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" -ErrorAction SilentlyContinue
            $parentId = if ($command) { $command.ParentProcessId } else { $null }
            Stop-Process -Id $ownerId -Force
            if ($parentId) {
                $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId" -ErrorAction SilentlyContinue
                if ($parent -and (
                    $parent.CommandLine -like "*streamlit run $DashboardScript*" -or
                    $parent.CommandLine -like "*streamlit*app.py*"
                )) {
                    Stop-Process -Id $parentId -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }
}

$DashboardPath = Resolve-Path -LiteralPath $DashboardScript
$CheckoutRoot = Split-Path -Parent $DashboardPath
if ([string]::IsNullOrWhiteSpace($RuntimeRoot)) {
    $RuntimeRoot = $CheckoutRoot
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $CommonGitDirectory = (& git -C $CheckoutRoot rev-parse --path-format=absolute --git-common-dir 2>$null)
        if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($CommonGitDirectory)) {
            $RuntimeRoot = Split-Path -Parent $CommonGitDirectory.Trim()
        }
    }
}
$RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)

$ConfiguredDatabase = if ([string]::IsNullOrWhiteSpace($env:INTRADAY_DATABASE_PATH)) {
    "data\shadow_real.sqlite"
} else {
    $env:INTRADAY_DATABASE_PATH
}
$DatabasePath = Resolve-OperationalPath -Value $ConfiguredDatabase -Root $RuntimeRoot
if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
    throw "Dawnstrike operational database not found: $DatabasePath"
}
$DatabaseInfo = Get-Item -LiteralPath $DatabasePath
if ($DatabaseInfo.Length -le 0) {
    throw "Dawnstrike operational database is empty: $DatabasePath"
}
$DatabaseProbeSource = @'
import sqlite3
import sys

uri = "file:" + sys.argv[2].replace("\\", "/") + "?mode=ro"
with sqlite3.connect(uri, uri=True) as connection:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
required = {"schema_version", "historical_signals", "signal_outcomes"}
missing = sorted(required - tables)
if missing:
    raise SystemExit("missing required tables: " + ", ".join(missing))
'@
$DatabaseProbeEncoded = [Convert]::ToBase64String(
    [Text.Encoding]::UTF8.GetBytes($DatabaseProbeSource)
)
$DatabaseProbeRunner = "import base64,sys;exec(base64.b64decode(sys.argv[1]))"
$PreviousErrorActionPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = "Continue"
    $DatabaseProbeResult = (& py -c $DatabaseProbeRunner $DatabaseProbeEncoded $DatabasePath 2>&1)
    $DatabaseProbeExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $PreviousErrorActionPreference
}
if ($DatabaseProbeExitCode -ne 0) {
    throw "Dawnstrike operational database failed read-only schema validation: $DatabasePath`n$DatabaseProbeResult"
}

$ConfiguredPaperOpsRoot = if ([string]::IsNullOrWhiteSpace($env:DAWNSTRIKE_PAPER_OPS_ROOT)) {
    "data\v2_paper_ops_live"
} else {
    $env:DAWNSTRIKE_PAPER_OPS_ROOT
}
$PaperOpsRoot = Resolve-OperationalPath -Value $ConfiguredPaperOpsRoot -Root $RuntimeRoot
$PaperOpsCalendar = Join-Path $PaperOpsRoot "calendar\strategy_daily_returns.csv"
if (-not (Test-Path -LiteralPath $PaperOpsCalendar -PathType Leaf)) {
    throw "Dawnstrike canonical PaperOps calendar not found: $PaperOpsCalendar"
}

$env:DAWNSTRIKE_RUNTIME_ROOT = $RuntimeRoot
$env:INTRADAY_DATABASE_PATH = $DatabasePath
$env:DAWNSTRIKE_PAPER_OPS_ROOT = $PaperOpsRoot
$PortsToStop = @($Port) + @($ExtraPortsToStop)
foreach ($TargetPort in ($PortsToStop | Select-Object -Unique)) {
    Stop-WebListener -TargetPort $TargetPort
    if (@(Get-WebListenerProcessIds -TargetPort $TargetPort).Count -gt 0) {
        throw "Could not stop the existing listener on port $TargetPort."
    }
}

$LogDir = Join-Path (Get-Location) "outputs\dev_server"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stdout = Join-Path $LogDir "operator_dashboard_${Port}_stdout.log"
$Stderr = Join-Path $LogDir "operator_dashboard_${Port}_stderr.log"

$ServerProcess = Start-Process `
    -FilePath "py" `
    -ArgumentList @(
        "-m",
        "streamlit",
        "run",
        "$DashboardPath",
        "--server.address",
        "$HostName",
        "--server.port",
        "$Port",
        "--server.headless",
        "true"
    ) `
    -WorkingDirectory (Get-Location) `
    -RedirectStandardOutput $Stdout `
    -RedirectStandardError $Stderr `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 5
$Url = "http://${HostName}:${Port}/"
$ListenerProcessIds = @(Get-WebListenerProcessIds -TargetPort $Port)
$ServingProcessIds = @(
    foreach ($ListenerProcessId in $ListenerProcessIds) {
        if ($ListenerProcessId -eq $ServerProcess.Id) {
            $ListenerProcessId
            continue
        }
        $ListenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$ListenerProcessId" -ErrorAction SilentlyContinue
        if ($ListenerProcess -and (
            $ListenerProcess.CommandLine -like "*streamlit*" -and
            $ListenerProcess.CommandLine -like "*$DashboardPath*"
        )) {
            $ListenerProcessId
        }
    }
)
if ($ServingProcessIds.Count -eq 0) {
    $stderrTail = if (Test-Path -LiteralPath $Stderr) {
        (Get-Content -LiteralPath $Stderr -Tail 20) -join "`n"
    } else {
        "No stderr log was created."
    }
    throw "The requested Dawnstrike dashboard process does not own port $Port.`n$stderrTail"
}
$response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20
if ($response.StatusCode -ne 200) {
    throw "Dawnstrike operator dashboard did not respond correctly at $Url"
}

Write-Host "Dawnstrike operator dashboard: $Url"
Write-Host "Serving: $DashboardPath"
Write-Host "Operational runtime: $RuntimeRoot"
Write-Host "SQLite evidence: $DatabasePath"
Write-Host "PaperOps evidence: $PaperOpsRoot"
Write-Host "Canonical tabs: Today, Review, History, Calendar, Performance, System"
if ($SkipBuild) {
    Write-Host "-SkipBuild is accepted for backward compatibility; the canonical dashboard is live data driven."
}
if (-not $NoOpen) {
    Start-Process -FilePath $Url
}
