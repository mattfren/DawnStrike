# Dawnstrike 10/10 remediation baseline

Captured: 2026-07-29 19:41:50 America/Chicago / 2026-07-30 00:41:50 UTC
Remediation branch: `codex/dawnstrike-10of10`
Remediation worktree: `C:\r\dawnstrike-10of10-20260729`
Status: `IN_PROGRESS`

This file is the immutable pre-remediation baseline. It is historical evidence,
not the current release state. Current production, scheduler, reconciliation,
and rollback truth is recorded in `deployment-verification.md`,
`daily-operations-verification.md`, `performance-reconciliation.md`, and
`final-scorecard.md`.

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

## Prior candidate revalidation (historical; superseded below)

Rechecked 2026-07-30 in the isolated candidate after fetching `origin/main`
with no changes to the shared checkout:

```text
shared_branch=codex/command-center-x3-vercel
shared_HEAD=67f02726c915aad7ce5a857567d3d3fcc1b0bf98
origin/main=268b4946bc149f00ec6cc7fd31a040be3baa86a6
shared_dirty_path_count=22972
shared_modified_or_deleted=833
shared_untracked=22139
candidate_branch=codex/dawnstrike-10of10
candidate_HEAD=808bbceabdfc931d83fe9a1d827375a7d622a586
candidate_dirty_path_count=0
```

The current scheduled-task state is:

| Task | State | Enabled | Last result | Next action |
|---|---:|---:|---:|---|
| Dawnstrike AlphaOps EOD Full Report | 3 | true | 1 | Existing task; do not treat as canonical finalize success |
| Dawnstrike AlphaOps Monitor 5m | 3 | true | 0 | Unchanged |
| Dawnstrike AlphaOps Morning | 3 | true | 0 | Unchanged |
| Dawnstrike X3 Vercel Daily Publish | 1 | false | 0 | Contained; do not restore |
| Dawnstrike 10of10 Daily Finalize | missing | n/a | n/a | Register only from approved checkout |

Before the fresh candidate build, the shared database was opened read-only and
contained 7 positions, 14 fills, 8 outcomes, 228 historical signals, 5
canonical performance rows, 2 canonical daily rows, 0 benchmark rows, 0
automation-run rows, and 91 notifications. A later build was mistakenly
pointed at that shared database; the persistence-enabled finalize path added
the derived 425 canonical rows, 222 daily rows, and one console notification
(`id=92`, build `c23bc49297c71c60e62a`). No raw positions, fills, outcomes, or
broker orders were changed. No pre-write copy with the original 5/2 derived
rows was found, so recovery is an owner decision: supply a trusted backup or
explicitly retain and audit the derived read model. The owner approved
retention; do not write the shared DB again from the isolated candidate.

Read-only hashes against the existing evidence copy confirm the raw tables are
unchanged: positions 7,
`bfc24644cee76cfa48a7fec48741ee1c896636375cfc99ceb5bd0b35be5f0c74`; fills 14,
`833187f8ec93dab3dc6a06c4e9e3862e1e224651a435f9119b9dc089053285ce`; outcomes
8, `31e7de936c6666321695651406cabc03707b8c524cd3fe3faa700f4461914d18`;
signals 228,
`b9115a500a89b2305e3e6d06572761299530a67b8d4c9abe104bc85ddacfd93b`.

The current authoritative PaperOps revalidation reports 425 canonical rows,
222 daily records, 46 discrepancies, 190 accepted PaperOps rows, 0 quarantined
rows, 21 PaperOps component-scope warnings, and 0 source return-field
mismatches. Daily returns now derive from the verified prior-to-current equity
delta; readiness remains HTTP 503.

Production was re-inspected as deployment
`dpl_ErcbSKoHYNf595t7zHK6HxyMdLge`
(`dawnstrike-command-center-x3-c0brl0wa9-mattfrens-projects.vercel.app`). It
is still the old X3 deployment: `/api/health` is HTTP 200, `/api/readiness`
is HTTP 500 `FUNCTION_INVOCATION_FAILED`, and health exposes scanner,
Telegram, and cron routes. No production mutation occurred.

The latest isolated preview is deployment
`dpl_9UXadeGZsJTBoQt6g8BLdxopYYVg` at
`https://dawnstrike-command-center-x3-qseiaiddm-mattfrens-projects.vercel.app`.
Its health endpoint is HTTP 200 with source SHA
`6886900de5960a90c8693e476c8c32d26f864375`, build ID
`b0988a1e450a54fceb0e`, research-only true, and live trading false. Its
readiness endpoint is the intended HTTP 503 with
`snapshot_not_publishable` and `pipeline_not_ready`; the build manifest
matches the same source/build/data hashes.

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

## Fresh continuation audit — 2026-07-30 06:09:57 America/Chicago

The following is the current authoritative state after rereading the controlling
directive and fetching `origin` with `git fetch origin --prune`:

```text
directive_sha256=E3E0DA843C0E5CEE80CDAA5B3682BD2F344B7FF76EE9D87289DAF5A68223EA2A
directive_lines=1431
shared_branch=codex/command-center-x3-vercel
shared_HEAD=67f02726c915aad7ce5a857567d3d3fcc1b0bf98
origin/main=268b4946bc149f00ec6cc7fd31a040be3baa86a6
shared_dirty_path_count=1207
candidate_branch=codex/dawnstrike-10of10
candidate_HEAD=19e90e6156fc1161a4e7ee84923d18e3aa7b2aa1
candidate_dirty_path_count=0
database_sha256=ED9FA44363E595DE993D333E71803949F9254F5269867041E6D13F8D5EEF8164
paper_positions=7
paper_trade_fills=14
signal_outcomes=8
historical_signals=228
portfolio_performance_rows=425
portfolio_daily_performance=222
benchmark_performance=0
automation_runs=0
notifications_sent=92
```

The legacy `Dawnstrike X3 Vercel Daily Publish` task remains disabled. The
replacement `Dawnstrike 10of10 Daily Finalize` task remains absent, and
`scheduler-doctor` returns `BLOCKED_EXTERNAL`. The shared checkout remains
untouched; no scheduler registration or shared-database write was performed in
this audit.

The isolated candidate remains clean after the full test/lint/typecheck pass.
The candidate's current build artifact is still based on implementation commit
`6886900d`; commits `8eb82c33` and `19e90e61` add evidence and screenshots only
and were not included in that already-verified preview deployment.

## Phase 0 conclusion

The required isolation and evidence work is active. Production remains an
unmodified rollback reference. No production promotion, broad deletion, or
unrelated task disablement was performed. The single X3 publisher task is
disabled pending replacement by the canonical daily chain.

## Continuation revalidation

Rechecked 2026-07-29 22:36 America/Chicago after the candidate PaperOps and
scheduler work:

- The shared checkout is still untouched at 1,207 dirty paths (833 modified or
  deleted and 374 untracked), on `67f02726c915aad7ce5a857567d3d3fcc1b0bf98`.
- The isolated candidate is clean at
  `86832fdae938f03f07ddbb919a2a5f2f2c5de970`.
- `Dawnstrike X3 Vercel Daily Publish` remains disabled; the canonical
  `Dawnstrike 10of10 Daily Finalize` task is absent. No task was registered.
- The copied evidence database now contains 425 canonical rows and 222 daily
  records after the rehearsal. The shared source database was not used as a
  write target.
- PaperOps source reconciliation is 190 rows, 190 accepted, 0 quarantined,
  and 21 warning-level component-scope issues; readiness is correctly HTTP 503.
