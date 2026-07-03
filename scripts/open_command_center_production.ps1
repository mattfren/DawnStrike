param(
    [string]$DashboardScript = "app.py",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8502,
    [int[]]$ExtraPortsToStop = @(8503),
    [switch]$NoOpen,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

function Stop-WebListener {
    param([int]$TargetPort)

    $listeners = Get-NetTCPConnection -LocalAddress $HostName -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue
    $ownerIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
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
$PortsToStop = @($Port) + @($ExtraPortsToStop)
foreach ($TargetPort in ($PortsToStop | Select-Object -Unique)) {
    Stop-WebListener -TargetPort $TargetPort
}

$LogDir = Join-Path (Get-Location) "outputs\dev_server"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stdout = Join-Path $LogDir "operator_dashboard_${Port}_stdout.log"
$Stderr = Join-Path $LogDir "operator_dashboard_${Port}_stderr.log"

Start-Process `
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
    -WindowStyle Hidden | Out-Null

Start-Sleep -Seconds 5
$Url = "http://${HostName}:${Port}/"
$response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 20
if ($response.StatusCode -ne 200) {
    throw "Dawnstrike operator dashboard did not respond correctly at $Url"
}

Write-Host "Dawnstrike operator dashboard: $Url"
Write-Host "Serving: $DashboardPath"
Write-Host "Canonical tabs: Today, Review, History, Calendar, Performance, System"
if ($SkipBuild) {
    Write-Host "-SkipBuild is accepted for backward compatibility; the canonical dashboard is live data driven."
}
if (-not $NoOpen) {
    Start-Process -FilePath $Url
}
