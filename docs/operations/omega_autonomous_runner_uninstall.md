# OMEGA Autonomous Runner Uninstall

Uninstall command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/uninstall_omega_autonomous_tasks.ps1 -Yes
```

The uninstaller deletes only Dawnstrike-owned tasks under `\Dawnstrike\`. It does not delete evidence, logs, provider environment variables, generated reports, or local data artifacts.
