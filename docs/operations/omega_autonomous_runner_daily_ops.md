# OMEGA Autonomous Runner Daily Ops

Installed schedules:

- `\Dawnstrike\OMEGA Morning Check` at `09:10` local time; runs Sentinel with AutoData, Learning Foundry, and Market Masters.
- `\Dawnstrike\OMEGA After Close` at `16:35` local time; runs Sentinel with AutoData, Learning Foundry, and Market Masters.
- `\Dawnstrike\OMEGA Verify` at `17:10` local time.
- `\Dawnstrike\OMEGA Watchdog` at `18:00` local time.

Task Scheduler must keep `Do not start a new instance` enabled. Daily review starts at `data/v2_command_center/production.html` and the autonomous pages.
