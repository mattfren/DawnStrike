# Daily finalize runbook

The daily chain is one idempotent operation:

`reconcile raw inputs -> persist canonical rows -> write bounded snapshot -> write readiness -> write stage manifest`

Run it locally with:

```powershell
pwsh -File scripts\run_daily_finalize.ps1 -ProjectRoot C:\path\to\Dawnstrike
```

The wrapper defaults to the local SQLite database, the static `build/public`
output, two retries, and a 15-minute retry interval. A failed upstream step
does not produce a green readiness file. The lock prevents overlapping runs;
`daily_finalize_runs` records the final status, retry count, hashes, and error.

## Schedule

Register one local daily task only after the candidate checkout is the intended
operator checkout:

```powershell
pwsh -File scripts\register_daily_finalize_task.ps1 -ProjectRoot C:\path\to\Dawnstrike
```

The registration script refuses to overwrite an existing task with the same
name. Inspect it with:

```powershell
Get-ScheduledTask -TaskName 'Dawnstrike 10of10 Daily Finalize'
Get-ScheduledTaskInfo -TaskName 'Dawnstrike 10of10 Daily Finalize'
```

## Operator checks

```powershell
py scripts\verify_public_artifact.py --root build\public
Get-Content build\public\readiness.json
Get-Content build\public\stage-manifest.json
```

Expected behavior is conservative: a complete or explicit no-trade snapshot
is ready; missing or degraded truth is published only as a non-ready artifact
with HTTP 503 semantics.
