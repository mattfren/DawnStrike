# OMEGA Scheduler Runbook

This runbook covers the noninteractive Windows wrappers for Dawnstrike's existing OMEGA research and paper-audit workflow.
It does not install scheduled tasks; installation is a separate, explicit operator action.

## Safety boundary

- The wrappers do not install scheduled tasks.
- They do not enable broker execution or live trading.
- They write redacted logs under `data/v2_scheduler/logs/` and portable status under `data/v2_scheduler/status/`.
- They stop on the first failed step and preserve that command's exit code.
- Do not run the after-close, morning-check, and verification wrappers concurrently because they share retained research artifacts.

## Manual commands

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run_omega_scheduler_after_close.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run_omega_scheduler_morning_check.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/run_omega_scheduler_verify.ps1
```

Each wrapper accepts `-Date YYYY-MM-DD`. A published market closure is retained as `skipped_market_closed`; unavailable calendar coverage fails closed.

Review `data/v2_scheduler/status/latest_status.json` and the referenced log before trusting any generated report. Never hand-edit frozen evidence, PaperOps ledgers, provider artifacts, or FillTruth outputs.
