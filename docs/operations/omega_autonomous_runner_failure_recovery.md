# OMEGA Autonomous Runner Failure Recovery

1. Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/status_omega_autonomous_tasks.ps1`.
2. Open `data/v2_autonomous_runner/status/latest_status.md`.
3. Open `data/v2_autonomous_runner/health/watchdog_latest.md`.
4. Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_omega_autonomous_tasks.ps1`.
5. Do not hand-edit provider artifacts, PaperOps ledgers, FillTruth outputs, CommitBridge events, or SQLite databases.
