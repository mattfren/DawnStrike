# v2 Autonomous Runner Architecture

The v2 autonomous runner is an additive Windows Task Scheduler layer around the existing OMEGA scheduler scripts.

- It registers four local Windows tasks under `\Dawnstrike\`.
- It does not add strategies, broker routing, live execution, provider secrets, external alerts, Streamlit imports, app.py imports, or SQLite writes.
- The PowerShell install script owns Task Scheduler registration.
- The Python module owns deterministic task definitions, status reports, watchdog health, audit docs, and Command Center pages.
- The scheduler scripts remain the execution boundary for after-close, morning-check, and verify operations.
- All tasks use `MultipleInstances IgnoreNew` so a new run is not started while an existing run is still active.
