# Dawnstrike v5 Release and Daily Operations

## Fixed boundaries

- Clean runtime: `C:\r\dawnstrike-runtime`
- Durable mutable state: `C:\r\dawnstrike-state`
- Operational database: `C:\r\dawnstrike-state\shadow_real.sqlite`
- PaperOps state: `C:\r\dawnstrike-state\v2_paper_ops_live`
- Legacy evidence only: `C:\Users\MattFields\Dawnstrike`

No enabled action, wrapper, import, build, or deployment may reference the
legacy path.

## Shared daily DAG

Every eligible market date uses one release-bound run ID and ledger:

```text
morning collection
  -> ranking and Telegram delivery
  -> intraday monitoring
  -> EOD outcome capture
  -> paper reconciliation
  -> sourced learning
  -> attribution and outcome-gap audit
  -> PaperOps forward research
  -> canonical performance
  -> Calendar
  -> atomic publication
  -> readiness
```

A failed required stage returns nonzero, records the earliest causal failure,
marks readiness degraded, and sends an operator Telegram alert. Downstream
failure must not hide the first failed stage.

Daily Finalize publishes only a complete, ready artifact during normal
scheduled operation. `AllowDegraded` is not part of the normal path. A manual
degraded publication is an explicit incident action and must retain a red
readiness state.

## Scheduled tasks

Registration scripts configure:

- `Dawnstrike AlphaOps Morning`
- `Dawnstrike AlphaOps Monitor`
- `Dawnstrike AlphaOps EOD`
- `Dawnstrike Daily Finalize`

Use one backup directory for the pre-change task XML:

```powershell
& C:\r\dawnstrike-runtime\scripts\register_alphaops_tasks.ps1 `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -BackupRoot C:\r\dawnstrike-state\rollback\tasks-before-v5 `
  -ReplaceExisting

& C:\r\dawnstrike-runtime\scripts\register_daily_finalize_task.ps1 `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -BackupRoot C:\r\dawnstrike-state\rollback\tasks-before-v5 `
  -ReplaceExisting
```

Then run scheduler doctor and inspect every enabled Dawnstrike task. Acceptance
requires zero references to the legacy checkout.

## State migration

1. Stop or disable the Dawnstrike tasks during the cutover window.
2. Create a SQLite-consistent backup of the legacy database.
3. Hash and retain the original backup as rollback evidence.
4. Restore the consistent copy under the durable state root.
5. Apply additive migrations from the exact release.
6. Reconcile schema version, WAL mode, table counts, latest timestamps, and
   representative V4 row hashes.
7. Copy PaperOps operational state without modifying its source, then hash the
   source and destination manifests.
8. Run deterministic dry runs before task registration.

Never copy a live SQLite database with an ordinary file copy while writes can
occur.

## Build and deployment

The exact clean release builds `performance.json`, `calendar.json`,
`build-manifest.json`, `publication-set.json`, and `release-manifest.json`.
Performance and Calendar are one atomic publication set. The release manifest
binds source SHA, build SHA, roots, schema, watermark, versions, scheduler, and
artifact hashes.

Normal production finalize:

```powershell
& C:\r\dawnstrike-runtime\scripts\run_daily_finalize.ps1 `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -Environment production
```

The published source SHA must equal the merged Git SHA. Verify `/api/health`,
`/api/readiness`, artifact hashes, Calendar rendering, responsive widths, and
no-cache freshness after promotion.

## Rollback

1. Disable the four v5 tasks.
2. Preserve the failed v5 database and logs; do not delete them.
3. Restore task definitions from the verified XML backup:

```powershell
& C:\r\dawnstrike-runtime\scripts\restore_dawnstrike_tasks.ps1 `
  -BackupRoot C:\r\dawnstrike-state\rollback\tasks-before-v5
```

4. Restore the prior runtime only from its recorded immutable SHA.
5. Point no task at a database until its backup hash and SQLite integrity check
   pass.
6. Re-run scheduler doctor and verify the actual task actions before enabling
   tasks.

Rollback restores operation; it does not erase v5 evidence or mutate V4 rows.
