# Daily Finalize runbook

Daily Finalize is a production scheduled operation, not a manual script. Its
only production owner is `Dawnstrike 10of10 Daily Finalize` at 17:30
America/Chicago. The task action is bound to the mounted runtime's exact SHA,
the complete launch-manifest hash, protected Windows PowerShell 5.1, durable
state, and the research-only/broker-disabled boundary.

The idempotent chain is:

```text
reconcile raw inputs -> persist canonical rows -> build bounded snapshot
-> seal readiness and daily ledger -> verify publication authorization
-> publish the exact artifact when READY
```

A failed upstream step remains failed. Missing or degraded truth produces a
non-ready artifact and HTTP 503 semantics; it is never relabeled as a green
publication.

## Operator checks

Read Task Scheduler state without changing it:

```powershell
Get-ScheduledTask -TaskName 'Dawnstrike 10of10 Daily Finalize'
Get-ScheduledTaskInfo -TaskName 'Dawnstrike 10of10 Daily Finalize'
```

Then inspect the exact-date finalizer receipt, daily ledger, process receipts,
readiness object, public-artifact verification, and Vercel publication journal
under `C:\r\dawnstrike-state`. A zero task result alone is not publication
proof. The public alias is accepted only when the same-SHA artifact, immutable
deployment, all three governed aliases, source/build/readiness manifests, and
public hashes agree.

Do not invoke `run_daily_finalize.ps1`,
`register_daily_finalize_task.ps1`, or the publisher directly. Do not use
`Start-ScheduledTask` to replay an old market date. Task installation and
repair use `runtime_activation_and_rollback.md`; an interrupted Vercel
publication uses `public_dashboard_rollback.md`.

## Timing and date authority

Production finalization resolves the authoritative current exchange session
from the market calendar and wall clock and publishes only after that
session's scheduled close. Prior, future, closed, and incomplete dates fail
closed. Offline historical research remains separate and cannot write the
production daily ledger or aliases.
