[CmdletBinding()]
param(
    [string]$RuntimeRoot = "C:\r\dawnstrike-runtime",
    [string]$StateRoot = "C:\r\dawnstrike-state",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ExpectedSha,
    [string]$BackupRoot = "",
    [pscredential]$RunAsCredential,
    [switch]$ReuseExistingPrincipal,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"
throw (
    "Dawnstrike direct AlphaOps task registration is disabled. " +
    "Invoke the administrator-installed protected release launcher with " +
    "BootstrapBaseline or Activate; only the governed scheduler transaction " +
    "may create or replace canonical tasks."
)
