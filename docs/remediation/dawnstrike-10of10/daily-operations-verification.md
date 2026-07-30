# Daily operations verification

The legacy `Dawnstrike X3 Vercel Daily Publish` task is disabled and its exact
definition plus restore path are recorded in the baseline evidence. Morning,
monitor, and EOD tasks remain enabled; the EOD task last returned code 1 in the
fresh baseline.

The replacement chain is implemented as one lock-protected local operation:

`clean-source gate -> reconcile all raw history -> publish bounded snapshot -> validate readiness -> write stage manifest`

It retries transient exceptions with a bounded delay, records retry count,
preserves historical canonical days, clears stale canonical rows on a full
rebuild, records the source-to-promotion stage chain with hashes, timestamps,
errors, and next actions, and returns 503 semantics for no-data/degraded
states. The real copied-source rehearsal produced 425 rows, 222 daily records,
and 46 discrepancies (25 missing-outcome warnings plus 21 PaperOps
component-scope warnings); it did not publish a green snapshot. Each finalize
attempt now records one auditable
console notification containing market date, stage, build ID, data hash,
coverage, PaperOps summary, deployment URL (null until one is supplied), and
next action. `scheduler-doctor` confirms that
`Dawnstrike 10of10 Daily Finalize` is absent and returns `BLOCKED_EXTERNAL`.
The replacement task is not registered yet because the isolated candidate has
no approved live checkout. Register it only against the approved merged
checkout, then rerun the doctor and one dated finalize rehearsal.

Current authoritative scheduler recheck on 2026-07-30: the legacy X3 task is
disabled, `Dawnstrike AlphaOps EOD Full Report` is enabled but last returned
code `1`, and `Dawnstrike 10of10 Daily Finalize` is missing. The shared source
database was read-only during this audit; no scheduler registration or daily
finalize was run from the isolated worktree.

Correction: the fresh artifact build was later invoked with the shared
database as its persistence target, so a persistence-enabled finalize did run
against the shared DB by mistake. It wrote the derived 425/222 read model and
one console notification. No raw source rows or broker state changed. The
shared DB must not be used again by this candidate until recovery is explicitly
decided by the owner.

## Fresh continuation audit — 2026-07-30 06:09 America/Chicago

The exact Windows task inventory still shows the legacy X3 publisher disabled
with last result `0`, `Dawnstrike AlphaOps EOD Full Report` enabled with last
result `1`, and no `Dawnstrike 10of10 Daily Finalize` task. The doctor output is:

```json
{
  "expected_task_name": "Dawnstrike 10of10 Daily Finalize",
  "next_action": "Register exactly one replacement task on the approved checkout, then rerun scheduler-doctor.",
  "scheduled_task": {
    "enabled": null,
    "last_run_time": null,
    "last_task_result": null,
    "name": "Dawnstrike 10of10 Daily Finalize",
    "next_run_time": null,
    "state": "missing"
  },
  "status": "BLOCKED_EXTERNAL"
}
```

Registration remains intentionally unperformed because the candidate is not an
approved merged checkout and the user has not approved that external task
mutation. The registration script itself is covered by the passing scheduler
tests and refuses to overwrite an existing task.
