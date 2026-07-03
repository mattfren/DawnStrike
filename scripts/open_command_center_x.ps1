param(
    [string]$OutputRoot = "data/v2_command_center_x"
)

$ErrorActionPreference = "Stop"

Write-Host "Command Center X is archived. Opening the canonical operator dashboard instead."
powershell -ExecutionPolicy Bypass -File scripts\open_command_center_production.ps1
