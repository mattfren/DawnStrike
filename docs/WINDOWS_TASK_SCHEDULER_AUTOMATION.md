# Windows Task Scheduler Automation

Use these scripts from the repo root.

Run one pass:

```powershell
scripts\run_automation_once.bat
```

Run the daemon:

```powershell
scripts\run_automation_daemon.bat
```

Register at login:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_dawnstrike_automation.ps1 -AtLogon
```

Register every market weekday around the morning window:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_dawnstrike_automation.ps1 -MarketDays
```

Stop the daemon by ending the scheduled task or closing the process. Outputs are
under `outputs\automation`; logs are under `logs`.

The scheduled task runs:

```powershell
py -m intraday_scanner.cli automation-daemon
```

It does not place orders.
