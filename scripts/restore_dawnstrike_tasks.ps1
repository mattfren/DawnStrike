[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-f]{40}$')]
    [string]$ExpectedSha,
    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,
    [Parameter(Mandatory = $true)]
    [string]$StateRoot,
    [switch]$ReplaceCurrent
)

$ErrorActionPreference = "Stop"
throw (
    "Dawnstrike direct XML task restore is disabled. Invoke governed runtime " +
    "Rollback through the administrator-installed protected release launcher; " +
    "BackupRoot XML is never direct scheduler authority."
)
