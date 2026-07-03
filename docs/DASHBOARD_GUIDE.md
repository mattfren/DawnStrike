# Operator Dashboard Guide

Dawnstrike has one local application dashboard: the Streamlit operator dashboard
in `app.py`.

Start it with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\open_command_center_production.ps1
```

Open:

```text
http://127.0.0.1:8502/
```

The launcher stops stale local listeners, starts `app.py` with Streamlit, and
keeps the local app focused on one continuously refined dashboard.

## Tabs

- `Today`: current operating state and watchlist.
- `Review`: signal review and outcome needs.
- `History`: prior signal context.
- `Calendar`: historical calendar and day evidence.
- `Performance`: returns, outcomes, and data-status truth.
- `System`: provider, storage, and operational diagnostics.

## Boundary

The dashboard is research-only and paper-only. It does not place orders, route
broker requests, or validate any strategy for live execution. Missing truth must
stay labeled as `n/a`, pending, or outcome-needed instead of falling back to
zero.

## Run Without Opening A Browser

```powershell
powershell -ExecutionPolicy Bypass -File scripts\open_command_center_production.ps1 -NoOpen
```

`-SkipBuild` remains accepted for backward compatibility, but the canonical
dashboard is live-data driven and does not rebuild a static UI before launch.
