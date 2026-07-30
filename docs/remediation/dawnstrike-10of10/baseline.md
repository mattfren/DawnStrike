# Dawnstrike 10/10 remediation baseline

Captured: 2026-07-29 19:41:50 America/Chicago / 2026-07-30 00:41:50 UTC
Remediation branch: `codex/dawnstrike-10of10`
Remediation worktree: `C:\r\dawnstrike-10of10-20260729`
Status: `IN_PROGRESS`

## Shared checkout

Authoritative shared checkout: `C:\Users\MattFields\Dawnstrike`

```text
branch=codex/command-center-x3-vercel
HEAD=67f02726c915aad7ce5a857567d3d3fcc1b0bf98
origin/main=268b4946bc149f00ec6cc7fd31a040be3baa86a6
dirty_path_count=1207
modified_or_deleted=833
untracked=374
```

The shared checkout remains untouched by remediation implementation. Its dirty
sample begins with `.env.example`, `.gitignore`, `app.py`, generated dashboard
artifacts, and multiple `data/v2_command_center/**` files.

Existing worktrees observed before creating this remediation worktree:

```text
C:/Users/MattFields/Dawnstrike                         67f02726  codex/command-center-x3-vercel
C:/Users/MattFields/Dawnstrike/tmp/worktrees/mover-pattern-lab
                                                         3a35acdd  codex/mover-pattern-lab
C:/Users/MattFields/Dawnstrike-release-gap-strategies  268b4946  codex/gap-strategy-release
C:/Users/MattFields/Dawnstrike-runtime-release-fix    1e11108c  codex/strategy-runtime-release-fix
C:/Users/MattFields/Dawnstrike-runtime-verification   1e11108c  codex/strategy-runtime-release
C:/r/dawnstrike-10of10-20260729                       268b4946  codex/dawnstrike-10of10
```

## Scheduled tasks

| Task | State | Last run | Next run | Result |
|---|---|---:|---:|---:|
| Dawnstrike AlphaOps Morning | Ready | 2026-07-29 08:10 | 2026-07-30 08:10 | 0 |
| Dawnstrike AlphaOps Monitor 5m | Ready | 2026-07-29 15:35 | 2026-07-30 08:35 | 0 |
| Dawnstrike AlphaOps EOD Full Report | Ready | 2026-07-29 15:15 | 2026-07-30 15:15 | 1 |
| Dawnstrike X3 Vercel Daily Publish | Ready | 2026-07-29 18:59:05 | 2026-07-30 15:45 | 0 |
| Dawnstrike AlphaOps EOD Report | Disabled | 1999-11-30 | 2026-07-30 15:15 | 267011 |
| Dawnstrike Daily Scan | Disabled | 1999-11-30 | 2026-07-30 08:20 | 267011 |
| Dawnstrike Setup Monitor 5m | Disabled | 2026-06-20 16:25:01 | n/a | 0 |
| Dawnstrike Web Telegram AutoPilot | Disabled | 2026-06-21 08:12:39 | 2026-07-30 08:00 | 0 |

The active EOD action is `scripts\\run_alphaops_eod_full.bat`; the publisher
action was `scripts\\publish_x3_daily.ps1`. The X3 publisher task was disabled
after its XML, identity, and restore command were captured in
`evidence/x3-vercel-daily-publish-task-before-disable.xml`. The morning,
monitor, and EOD tasks remain unchanged.

Post-containment scheduler recheck: `Dawnstrike X3 Vercel Daily Publish` is
now `Disabled` with last result `0`; no replacement finalize task has been
registered from the isolated candidate. The replacement must be registered
only from an approved merged checkout.

## Database

Database: `data/shadow_real.sqlite`  
Size: `85,684,224` bytes  
Tables: `72`

This is a point-in-time baseline. Later validation against the shared checkout
added only the candidate's additive canonical schema objects; the raw source
rows were not rewritten. The remediation implementation itself remains
isolated in the candidate worktree.

| Table | Rows |
|---|---:|
| notifications_sent | 91 |
| paper_trade_fills | 14 |
| paper_positions | 7 |
| signal_outcomes | 8 |
| daily_signal_performance | 27 |
| signal_return_attribution | 67 |
| strategy_paper_trades | 0 |
| performance_daily | 0 |
| performance_cumulative | 0 |
| benchmark_performance | 0 |

## Production

Production alias: [dawnstrike-command-center-x3.vercel.app](https://dawnstrike-command-center-x3.vercel.app)  
Deployment URL: `https://dawnstrike-command-center-x3-c0brl0wa9-mattfrens-projects.vercel.app`  
Deployment ID: `dpl_ErcbSKoHYNf595t7zHK6HxyMdLge`  
Target/status: `production / Ready`  
Created: `2026-07-29 18:59:59 America/Chicago`

Observed endpoint results:

```text
GET /api/health       HTTP 200, status=ok
GET /api/readiness    HTTP 500
```

`/api/health` reports the backend as `vercel-python-functions`, lists scanner,
Telegram, and cron routes, and reports `static_ui_served_by_rewrite=true`.
The deployment contains six visible 66.31 MB Python functions.

The repeated readiness failure is:

```text
ModuleNotFoundError: No module named 'intraday_scanner.public_data'
api/readiness.py -> vercel_dawnstrike/runtime.py:174
 -> intraday_scanner/v2/autodata/core.py:18
```

The EOD publisher therefore succeeded after the EOD report failed, while the
production readiness endpoint continued to crash.

## Phase 0 conclusion

The required isolation and evidence work is active. Production remains an
unmodified rollback reference. No production promotion, broad deletion, or
unrelated task disablement was performed. The single X3 publisher task is
disabled pending replacement by the canonical daily chain.
