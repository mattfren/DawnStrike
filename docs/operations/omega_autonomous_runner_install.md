# OMEGA Autonomous Runner Install

Install command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_omega_autonomous_tasks.ps1 -Yes
```

The installer confirms the repo root, required scheduler scripts, Python imports, OMEGA verification wrapper, no-live-trading boundary, and no embedded task secrets before registration. It registers tasks for the current interactive Windows user and does not store provider keys in task definitions.
