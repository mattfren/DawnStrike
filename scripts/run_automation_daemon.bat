@echo off
setlocal
cd /d "%~dp0.."
py -m intraday_scanner.cli automation-daemon --config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
