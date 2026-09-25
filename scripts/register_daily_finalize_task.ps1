[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ExpectedSha,
    [string]$TaskName = "Dawnstrike 10of10 Daily Finalize",
    [datetime]$StartTime = (Get-Date -Hour 17 -Minute 30 -Second 0),
    [ValidateSet("LocalOnly", "Preview", "Production")]
    [string]$PublicationMode = "Production",
    [string]$VercelProjectId = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
    [string]$BackupRoot = "",
    [pscredential]$RunAsCredential,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"
throw (
    "Dawnstrike direct daily-finalize task registration is disabled. " +
    "Invoke the administrator-installed protected release launcher with " +
    "BootstrapBaseline or Activate; only the governed scheduler transaction " +
    "may create or replace canonical tasks."
)
