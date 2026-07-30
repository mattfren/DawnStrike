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
and 156 discrepancies (including 105 quarantined PaperOps rows); it did not
publish a green snapshot. Each finalize attempt now records one auditable
console notification containing market date, stage, build ID, data hash,
coverage, PaperOps summary, deployment URL (null until one is supplied), and
next action. `scheduler-doctor` confirms that
`Dawnstrike 10of10 Daily Finalize` is absent and returns `BLOCKED_EXTERNAL`.
The replacement task is not registered yet because the isolated candidate has
no approved live checkout. Register it only against the approved merged
checkout, then rerun the doctor and one dated finalize rehearsal.
