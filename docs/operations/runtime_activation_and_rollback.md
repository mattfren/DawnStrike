# Governed pre-session runtime activation and rollback

Dawnstrike has two deliberately separate release phases:

1. **Pre-session activation** installs an accepted `origin/main` commit at the
   fixed `C:\r\dawnstrike-runtime` path. It does not require a same-day
   publication receipt because that receipt can only be created by the market
   day's scheduled chain.
2. **Post-session publication** remains gated by the exact-SHA daily ledger,
   current-day publishable artifact, readiness HTTP 200, and the existing
   independent production verification. An activation receipt is never
   publication authority.

Both phases remain research-only. Neither activation script imports provider
credentials, sends Telegram messages, invokes Vercel, requests an order, or
enables broker execution.

## Preconditions

Run activation before the Morning task and outside every daily run window.
The activation boundary is the next canonical US-equities session: after a
completed session it is the next open date, while an overnight invocation
before the 09:00 Eastern Morning edge may name that current open date. The
tool rejects same-session/post-Morning dates, closed dates, and target dates
that already have authoritative finalizer or public-build evidence. This
prevents a runtime SHA swap from diverging from a frozen daily/public
artifact. A read-only clock seam exists only for guarded tests.

The tool fails closed unless all of the following are true:

- `CandidateRoot` is a clean, self-contained Git checkout whose `HEAD` equals
  both the requested SHA and freshly fetched `origin/main`; it is also the
  checkout containing the invoked tool.
- No ignored executable, library, script, bytecode, `.pth`, or Python startup
  hook exists in the candidate, current runtime, stage, or rollback checkout.
  Inert ignored cache metadata is permitted.
- CI and independent SOL evidence bind the same 40-hex commit and Git tree,
  are at most 30 days old, and contain no critical/high audit finding.
- The current runtime is a clean, self-contained Git checkout.
- All five canonical scheduled tasks are present, enabled, not running, and
  still bind the fixed runtime and durable-state roots.
- No daily lock or runtime-activation lock exists.
- `shadow_real.sqlite` passes read-only `PRAGMA quick_check`, and its schema is
  exactly the candidate application's current schema. Activation never runs a
  migration.
- Runtime, stage, and rollback paths are on the same Windows volume.

Do not delete an unexplained lock, partial activation directory, receipt,
backup bundle, or rollback checkout. Investigate it or run the rollback tool.

## Evidence contracts

Capture CI from the exact completed GitHub run; do not type a success that was
not observed. The unsealed CI JSON must have exactly these fields:

```json
{
  "schema_version": "dawnstrike.runtime_activation_ci_evidence.v1",
  "candidate_sha": "<40 lowercase hex>",
  "candidate_tree": "<40 lowercase hex>",
  "conclusion": "SUCCESS",
  "status": "COMPLETED",
  "head_branch": "main",
  "run_url": "https://github.com/<owner>/<repo>/actions/runs/<id>",
  "checks_total": 19,
  "checks_succeeded": 19,
  "completed_at_utc": "<RFC3339 UTC ending Z>",
  "research_only": true,
  "broker_execution_enabled": false
}
```

The independent SOL JSON must have exactly these fields:

```json
{
  "schema_version": "dawnstrike.runtime_activation_sol_evidence.v1",
  "candidate_sha": "<40 lowercase hex>",
  "candidate_tree": "<40 lowercase hex>",
  "auditor_model": "gpt-5.6-sol",
  "verdict": "ZERO_CRITICAL_HIGH",
  "critical_findings": 0,
  "high_findings": 0,
  "completed_at_utc": "<RFC3339 UTC ending Z>",
  "research_only": true,
  "broker_execution_enabled": false
}
```

Seal each captured object atomically. Sealing supplies tamper evidence; it does
not turn an unverified assertion into CI or audit proof.

```powershell
py scripts\runtime_activation_contract.py seal-evidence `
  --input C:\r\dawnstrike-state\evidence\ci-unsealed.json `
  --output C:\r\dawnstrike-state\evidence\ci.json

py scripts\runtime_activation_contract.py seal-evidence `
  --input C:\r\dawnstrike-state\evidence\sol-unsealed.json `
  --output C:\r\dawnstrike-state\evidence\sol.json
```

Delete the unsealed copies only through the operator's normal recoverable
cleanup workflow. Never put either evidence object in `runtime.env`.

## Read-only preflight

The preflight refreshes `origin/main` and reads Git, Task Scheduler, SQLite,
and evidence state. It does not create a state backup or swap a directory.

```powershell
& C:\r\dawnstrike-main\scripts\activate_dawnstrike_runtime.ps1 `
  -ExpectedSha <accepted-origin-main-sha> `
  -MarketDate 2026-08-31 `
  -CiEvidencePath C:\r\dawnstrike-state\evidence\ci.json `
  -SolEvidencePath C:\r\dawnstrike-state\evidence\sol.json `
  -CandidateRoot C:\r\dawnstrike-main `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -BackupRoot C:\r\dawnstrike-state-backups `
  -PreflightOnly
```

Require `status: PASS`, the requested commit/tree, schema `30` (or the current
source constant if it changes in a future reviewed release), five tasks, and
the expected CI/SOL hashes.

## Activate

Run the same command without `-PreflightOnly`. The tool:

1. clones the accepted commit into a same-volume stage and verifies its exact
   tree and origin identity;
2. acquires a unique activation lock and the target-date daily lock;
3. rechecks all task definitions and the unchanged SQLite main-file identity;
4. persists the exact enabled XML for all five tasks, plus a hash-bound
   manifest, under `scheduler-backups`, then disables all five and requires
   every state to be exactly `Disabled`;
5. creates and validates an atomic SQLite online-backup bundle outside the
   state root;
6. creates and verifies a Git rollback bundle, then seals a `PREPARED`
   receipt;
7. renames the old runtime to its durable rollback checkout and the verified
   stage to the fixed runtime path;
8. verifies the installed commit/tree/origin while every task remains exactly
   `Disabled`, re-enables only the originally enabled five tasks, verifies the
   XML is byte-equivalent to the captured contract, then atomically seals a
   `COMPLETE` receipt.

The two directory renames are individually atomic. If the second rename or
post-swap verification fails in-process, the tool preserves the failed
candidate and immediately restores the previous runtime. It never recursively
deletes a runtime. A crash between renames remains recoverable from the sealed
`PREPARED` receipt, rollback checkout, and hash-bound Git bundle.

Completed receipts are under:

`C:\r\dawnstrike-state\receipts\runtime-activation`

Rollback assets are under:

`C:\r\dawnstrike-state\runtime-rollbacks\<activation-id>`

Scheduler XML evidence is under:

`C:\r\dawnstrike-state\scheduler-backups\runtime-<activation|rollback>-<activation-id>`

The receipt binds the scheduler backup directory and manifest hash. If an
automatic runtime restore or exact task-definition verification is ambiguous,
the scripts leave all five tasks disabled when that state can be proven; they
report the task state as unverified otherwise. If exact disablement cannot be
proven, both operation locks are deliberately preserved to prevent unattended
work until operator recovery.

Re-running a completed exact activation returns its valid receipt. A partial
activation fails closed instead of guessing which directory is authoritative.

## Roll back

Rollback is permitted from a valid `PREPARED` or `COMPLETE` activation receipt:

```powershell
& C:\r\dawnstrike-main\scripts\rollback_dawnstrike_runtime.ps1 `
  -ActivationReceipt C:\r\dawnstrike-state\receipts\runtime-activation\runtime-activation-<id>.json `
  -ContractRoot C:\r\dawnstrike-main `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -BackupRoot C:\r\dawnstrike-state-backups
```

For a crash before the complete receipt, pass the matching `.prepared.json`.
The tool verifies the bundle hash, exact previous commit/tree/origin, current
schema compatibility, all task definitions, persisted scheduler XML evidence,
and both locks. It stages the previous commit from the sealed Git bundle,
captures and disables exact-`Ready` tasks, requires exact `Disabled` state
throughout the swap, preserves the deactivated candidate, restores exact task
XML before re-enabling, and writes an idempotent `ROLLED_BACK` receipt under
`receipts\runtime-rollback`.

Rollback never restores SQLite automatically. The activation's online backup
is immutable recovery evidence; any database restore remains a separately
reviewed operation under `state_disaster_recovery_runbook.md`.

## Hard stops

- Never use an activation receipt to justify Vercel publication.
- Never activate while any canonical task is running or any daily lock exists.
- Never alter task actions to point at a SHA-specific stage or rollback path.
- Never copy `runtime.env` into a candidate, stage, receipt, or rollback bundle.
- Never fabricate CI, SOL, market-session, pick, trade, or return evidence.
- Keep missing truth missing; activation cannot certify strategy performance.
