@echo off
setlocal EnableExtensions
for %%I in ("%~dp0..") do set "RUNTIME_ROOT=%%~fI"
set "STATE_ROOT=%~1"
if not defined STATE_ROOT set "STATE_ROOT=C:\r\dawnstrike-state"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%RUNTIME_ROOT%\scripts\run_alphaops_monitor.ps1" -RuntimeRoot "%RUNTIME_ROOT%" -StateRoot "%STATE_ROOT%"
exit /b %ERRORLEVEL%
