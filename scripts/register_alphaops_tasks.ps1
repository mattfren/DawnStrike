param(
    [string]$Root = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
} else {
    $Root = (Resolve-Path $Root).Path
}

$logsDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$registrationDate = (Get-Date).ToString("s")
$startDate = (Get-Date).ToString("yyyy-MM-dd")

function ConvertTo-TaskXmlText {
    param([Parameter(Mandatory = $true)][string]$Value)
    return [System.Security.SecurityElement]::Escape($Value)
}

function New-TaskXml {
    param(
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$Arguments,
        [Parameter(Mandatory = $true)][string]$StartTime,
        [bool]$Repeats = $false
    )

    $descriptionText = ConvertTo-TaskXmlText $Description
    $argumentsText = ConvertTo-TaskXmlText $Arguments
    $userText = ConvertTo-TaskXmlText $currentUser
    $startBoundary = "$startDate`T$StartTime"
    $repetitionXml = ""

    if ($Repeats) {
        $repetitionXml = @"
      <Repetition>
        <Interval>PT5M</Interval>
        <Duration>PT6H</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
"@
    }

    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>$registrationDate</Date>
    <Author>$userText</Author>
    <Description>$descriptionText</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
$repetitionXml
      <ScheduleByWeek>
        <DaysOfWeek>
          <Monday />
          <Tuesday />
          <Wednesday />
          <Thursday />
          <Friday />
        </DaysOfWeek>
        <WeeksInterval>1</WeeksInterval>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$userText</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>$argumentsText</Arguments>
      <WorkingDirectory>$(ConvertTo-TaskXmlText $Root)</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

function Register-AlphaOpsTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Description,
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$StartTime,
        [bool]$Repeats = $false
    )

    $arguments = "/c cd /d `"$Root`" && $CommandLine"
    $xml = New-TaskXml `
        -Description $Description `
        -Arguments $arguments `
        -StartTime $StartTime `
        -Repeats $Repeats

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Xml $xml `
        -Force | Out-Null

    Write-Host "Registered task: $TaskName"
}

Register-AlphaOpsTask `
    -TaskName "Dawnstrike AlphaOps Morning" `
    -Description "Dawnstrike AlphaOps weekday morning research/watchlist cycle. No orders placed." `
    -StartTime "08:10:00" `
    -CommandLine "py -m intraday_scanner.cli alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram >> logs\alpha_morning.log 2>&1"

Register-AlphaOpsTask `
    -TaskName "Dawnstrike AlphaOps Monitor 5m" `
    -Description "Dawnstrike AlphaOps weekday 5-minute research monitor. No orders placed." `
    -StartTime "08:35:00" `
    -Repeats $true `
    -CommandLine "py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify telegram >> logs\alpha_monitor.log 2>&1"

Register-AlphaOpsTask `
    -TaskName "Dawnstrike AlphaOps EOD Report" `
    -Description "Dawnstrike AlphaOps weekday end-of-day research report. No orders placed." `
    -StartTime "15:15:00" `
    -CommandLine "py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report >> logs\alpha_report.log 2>&1"

Write-Host "AlphaOps scheduled tasks registered for root: $Root"
