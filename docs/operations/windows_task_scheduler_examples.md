# Windows Task Scheduler Examples

These are manual configuration examples. Repository scheduler wrappers do not install tasks automatically.

## Common settings

- Program: `powershell.exe`
- Working directory: the local Dawnstrike repository root.
- If the task is already running: `Do not start a new instance`.
- Run only under the intended local operator account and without highest privileges unless local policy requires them.

## Commands

- After close: `-NoProfile -ExecutionPolicy Bypass -File scripts/run_omega_scheduler_after_close.ps1`
- Morning check: `-NoProfile -ExecutionPolicy Bypass -File scripts/run_omega_scheduler_morning_check.ps1`
- Verification: `-NoProfile -ExecutionPolicy Bypass -File scripts/run_omega_scheduler_verify.ps1`

Do not install tasks automatically from an unattended setup process. Confirm the no-overlap setting, run each wrapper manually once, and inspect `data/v2_scheduler/status/latest_status.json` before enabling a schedule.
