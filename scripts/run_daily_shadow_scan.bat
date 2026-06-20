@echo off
setlocal
cd /d "%~dp0.."

if "%~1"=="" (
  for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set RUN_DATE=%%i
) else (
  set RUN_DATE=%~1
)

py -m intraday_scanner.cli auto-shadow-daily --date %RUN_DATE% --db-path data\shadow.sqlite --ai-normalizer none
