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
  still bind the fixed runtime and durable-state roots. Their executable must
  either already be the pinned full Windows PowerShell path or, for the
  one-time host migration, all five must use the literal legacy executable
  `powershell.exe`. This is an exact fresh-state rule: without a valid
  nonterminal activation journal, the legacy count must be either zero or
  five. A mixed legacy/pinned set is rejected before any mutation.
- No daily lock or runtime-activation lock exists. No global Vercel publication
  lock exists, and the durable Vercel publication history must verify as an
  exact terminal history for the governed project, project name, provider
  scope, and complete production-alias set. A nonterminal or malformed current
  journal, a foreign target tuple, or an incomplete compensated archive/intent
  pair blocks cutover. Activation never performs provider recovery: recover an
  interrupted publication against its exact prior runtime SHA before retrying
  activation. The publisher holds its publication lock while checking for the
  runtime-activation lock, and activation holds its runtime lock while checking
  publication history, so concurrent starts fail closed before either provider
  mutation or a runtime rename.
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
  "broker_execution_enabled": false,
  "report_sha256": "<64 lowercase hex SHA-256>",
  "codex_share_url": "https://chatgpt.com/share/<immutable-id>"
}
```

Activation requires a live GitHub authority check in addition to the local
self-hash. It reads the exact commit's comments from repository ID `1275588712`
and accepts exactly one comment whose commit is the candidate SHA/tree, whose
author association is `OWNER` and actor ID is `274126974`, and whose immutable
created and updated timestamps are equal. The comment URL and ID must be
canonical, and its body must be the exact canonical JSON binding the candidate
SHA/tree, `gpt-5.6-sol`, `ZERO_CRITICAL_HIGH`, zero critical/high counts, the
report SHA-256, immutable Codex share URL, `research_only: true`,
`broker_execution_enabled: false`, and
`authorization: "OWNER_RELEASE_AUTHORIZATION"`. Requests use no proxy and do
not follow redirects. This proves owner authorization of an independently
reviewed report; it does not prove cryptographic Sol identity.

After the protected host boundary described below is installed, seal each
captured object atomically with its protected interpreter and exact candidate
SHA. Sealing supplies tamper evidence; it does not turn an unverified
assertion into CI or audit proof.

```powershell
$sha = '<accepted-origin-main-sha>'
& 'C:\Program Files\Dawnstrike\Python313\python.exe' -I -B -S `
  C:\r\dawnstrike-main\scripts\dawnstrike_python_bootstrap.py `
  --release-root C:\r\dawnstrike-main `
  --expected-sha $sha `
  --script C:\r\dawnstrike-main\scripts\runtime_activation_contract.py -- `
  seal-evidence `
  --input C:\r\dawnstrike-state\evidence\ci-unsealed.json `
  --output C:\r\dawnstrike-state\evidence\ci.json

& 'C:\Program Files\Dawnstrike\Python313\python.exe' -I -B -S `
  C:\r\dawnstrike-main\scripts\dawnstrike_python_bootstrap.py `
  --release-root C:\r\dawnstrike-main `
  --expected-sha $sha `
  --script C:\r\dawnstrike-main\scripts\runtime_activation_contract.py -- `
  seal-evidence `
  --input C:\r\dawnstrike-state\evidence\sol-unsealed.json `
  --output C:\r\dawnstrike-state\evidence\sol.json
```

Delete the unsealed copies only through the operator's normal recoverable
cleanup workflow. Never put either evidence object in `runtime.env`.

## Read-only preflight

The preflight refreshes `origin/main` and reads Git, Task Scheduler, SQLite,
and evidence state. It does not create a state backup or swap a directory.

Before the first activation on a host, an administrator must establish the
protected bootstrap at
`C:\Program Files\Dawnstrike\bin\install_dawnstrike_host_boundary.ps1`.
The initial copy is an operator-controlled trust ceremony: hold the source
file read-locked, require its filtered Git blob to equal
`<accepted-origin-main-sha>:scripts/install_dawnstrike_host_boundary.ps1`,
then write those exact bytes into the administrator-owned destination. Do not
launch the installer directly from the user-writable checkout. Once the
protected bootstrap exists, run it from an elevated PowerShell process:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\Program Files\Dawnstrike\bin\install_dawnstrike_host_boundary.ps1' `
  -ExpectedSha <accepted-origin-main-sha> `
  -CandidateRoot C:\r\dawnstrike-main
```

The installer anchors the Python 3.13.14 core to the official PSF installer
digest and signer. It treats the existing user-profile environment only as a
byte cache: only `requirements.lock` distributions are materialized, and every
copied source, native, and data payload must match its source-approved wheel
`RECORD` hash and size. Extra distributions and unowned files are never copied.
The installer writes only below
`C:\Program Files\Dawnstrike` and `C:\ProgramData\Dawnstrike`, removes
inherited non-admin write access, and records a host-boundary receipt.
Activation and rollback must then enter through the installed launcher so
their candidate entry bytes are verified and held read-locked before
PowerShell parses them.

The protected launcher is also the only entry point allowed to admit the
all-five legacy executable set. `Activate -PreflightOnly` reports
`legacy_canonical_execute_rebind_required`, the exact count, and the exact task
names without changing them. The corresponding non-preflight activation must
run in an elevated administrator process. No environment variable or direct
checkout invocation can opt into this migration path. Do not call the
candidate activation script directly even when all five actions are already
pinned; the installed launcher is the governed entry point for both ordinary
activation and executable normalization.

If the delayed-SIP task exists, harden it to the exact candidate while it is
disabled, then prepare the schema-30 sidecar. Prompt locally; never persist or
forward the credential:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass -Command {
    $runAs = Get-Credential
    & 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1' `
      -Mode HardenCapture `
      -CandidateRoot C:\r\dawnstrike-main `
      -ExpectedSha <accepted-origin-main-sha> `
      -RuntimeRoot C:\r\dawnstrike-runtime `
      -StateRoot C:\r\dawnstrike-state `
      -RunAsCredential $runAs
  }

C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1' `
  -Mode Prepare `
  -CandidateRoot C:\r\dawnstrike-main `
  -ExpectedSha <accepted-origin-main-sha> `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -BackupRoot C:\r\dawnstrike-state-backups
```

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1' `
  -Mode Activate `
  -CandidateRoot C:\r\dawnstrike-main `
  -ExpectedSha <accepted-origin-main-sha> `
  -MarketDate '<next-open-session-YYYY-MM-DD>' `
  -CiEvidencePath C:\r\dawnstrike-state\evidence\ci.json `
  -SolEvidencePath C:\r\dawnstrike-state\evidence\sol.json `
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
   `Disabled`, seals the pre-rebind task identity and rebind intent in the
   durable `POST_SWAP` journal before changing the first action;
9. binds every canonical action to the full pinned Windows PowerShell
   executable and an exact-SHA launch manifest. Each update must preserve the
   exact task path, principal, triggers, settings, working directory, runtime
   root, and durable-state root proven by the sealed scheduler XML;
10. proves all five disabled actions are the exact candidate-SHA contract,
    seals the ready-to-enable `PREPARED` receipt, and enters
    `POST_SWAP_READY` before enabling any task; and
11. re-enables only the originally enabled five tasks, verifies the exact new
    Ready contract, then atomically seals a `COMPLETE` receipt. The sealed
    scheduler backup retains the pre-activation action contract for
    compensation or rollback.

The two directory renames are individually atomic. If the second rename or
post-swap verification fails in-process, the tool preserves the failed
candidate and immediately restores the previous runtime. It never recursively
deletes a runtime. A crash between renames remains recoverable from the sealed
`PREPARED` receipt, rollback checkout, and hash-bound Git bundle.

A crash in `PRE_QUIESCE`, before the runtime swap, restores the exact actions
from the sealed scheduler XML and re-enables that original Ready contract. It
must never bind the old runtime to the candidate SHA. A crash after the swap
keeps tasks Disabled until the candidate action rebind and exact contract can
be proven or the prior runtime/XML pair can be restored.

### Scheduler normalization and journal recovery

The operation journal, not the visible executable count by itself, determines
which recovery state is admissible:

| Journal phase | Admissible scheduler boundary | Governed recovery |
| --- | --- | --- |
| `PRE_QUIESCE` | The sealed pre-activation XML exists; enablement may be partially quiesced. Each action must be exactly either its sealed legacy form or the pinned-executable-only normalization derived from that form, with every non-action XML invariant unchanged. | Restore every exact XML-backed action and the original Ready contract. Do not write the candidate SHA into the old runtime's tasks. Any third action form or XML drift fails closed. |
| `PRE_SWAP` | All five tasks are `Disabled` and match the sealed pre-rebind contract; the old runtime or the exact staged/rollback rename boundary is provable. | Complete the two-directory swap or compensate to the previous runtime and XML contract. |
| `POST_SWAP` (rebind intent) | The candidate is installed, the rollback checkout is present, and all five tasks are `Disabled`. Each action must be exactly either its sealed pre-rebind form or its independently derived candidate-SHA form, and all preserved XML sections must match. This is the only phase in which a 1-through-4 partial rebind is admissible. | Verify the installed candidate and exact per-task old-or-target action proof, then idempotently converge every action to the candidate-SHA form or compensate using the previous runtime and sealed XML. Any third form fails closed. |
| `POST_SWAP_READY` | All five tasks are `Disabled`, all five actions are exactly candidate-SHA-bound, and the journal binds the ready-to-enable `PREPARED` receipt. | Finish a partial enablement, re-prove the exact Ready contract, and seal `COMPLETE`. |
| `COMPLETE` | All five canonical tasks are `Ready` under the exact candidate action/definition hashes. | Verify and return the existing terminal receipt; do not repeat mutation. |

`POST_SWAP` rebind intent is recovery authority only when the journal, lock
pair, candidate and previous runtime identities, prepared receipt,
scheduler-backup manifest, and task contracts all verify together. A fresh
invocation cannot use that rule to excuse a mixed host state. During recovery,
the tool compares each disabled task with the two exact allowed action forms
and proves that `Principal`, `Triggers`, `Settings`, task path, and working
directory did not change. It does not accept an action merely because it
contains a plausible SHA or points somewhere under `C:\r`.

Never normalize a task manually with Task Scheduler, `Set-ScheduledTask`, or
`schtasks`. Those edits would not be journaled, would break the XML/action
hashes, and can destroy the only deterministic distinction between a partial
governed rebind and untrusted scheduler drift.

Completed receipts are under:

`C:\r\dawnstrike-state\receipts\runtime-activation`

Rollback assets are under:

`C:\r\dawnstrike-state\runtime-rollbacks\<activation-id>`

Scheduler XML evidence is under:

`C:\r\dawnstrike-state\scheduler-backups\runtime-<activation|rollback>-<activation-id>`

The pre-swap `PREPARED` receipt binds the prior runtime, task action/definition
contracts, scheduler-backup directory, and manifest hash. The durable
`POST_SWAP` journal additionally binds the exact rebind intent and recovery
lineage while candidate-SHA action rebinding is in progress. The ready-to-enable `PREPARED`
receipt binds the new disabled candidate-SHA action/definition contracts; only
the terminal `COMPLETE` receipt asserts restored enablement. No receipt alone
is authority to enable a task unless its journal phase and live hashes also
verify.

If an automatic runtime restore or exact task-definition verification is
ambiguous, the scripts leave all five tasks disabled when that state can be
proven; they report the task state as unverified otherwise. If exact
disablement cannot be proven, both operation locks are deliberately preserved
to prevent unattended work until operator recovery.

For a nonterminal activation, do not delete the journal or either lock, do not
move a runtime directory, and do not edit or enable a task. Re-run the same
protected launcher from an elevated administrator PowerShell process with the
same candidate SHA, market date, evidence paths, and roots. The tool may adopt
the exact dead-owner lock pair and resume only the journal-authorized phase. If
it rejects recovery, preserve the journal, prepared/ready receipts, scheduler
XML and manifest, rollback checkout/bundle, and current task state for review;
use the rollback command only with the matching verified receipt. An operator
must not manufacture an all-pinned state to make preflight pass.

Re-running a completed exact activation returns its valid receipt. A partial
activation fails closed instead of guessing which directory is authoritative.

After activation, rebind the delayed-SIP task to the exact runtime and only
enable it with separately hashed, current provider inputs:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass -Command {
    $runAs = Get-Credential
    & 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1' `
      -Mode RebindCapture `
      -CandidateRoot C:\r\dawnstrike-main `
      -ExpectedSha <accepted-origin-main-sha> `
      -RuntimeRoot C:\r\dawnstrike-runtime `
      -StateRoot C:\r\dawnstrike-state `
      -SymbolsManifest <absolute-path> `
      -SymbolsManifestSha256 <64-lowercase-hex> `
      -EntitlementReceipt <absolute-path> `
      -EntitlementReceiptSha256 <64-lowercase-hex> `
      -SourceConfig <absolute-path> `
      -SourceConfigSha256 <64-lowercase-hex> `
      -RunAsCredential $runAs `
      -EnableCapture
  }
```

## Roll back

Rollback is permitted from a valid `PREPARED` or `COMPLETE` activation receipt:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1' `
  -Mode Rollback `
  -CandidateRoot C:\r\dawnstrike-main `
  -ExpectedSha <exact-candidate-sha> `
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
- Never use Task Scheduler, `Set-ScheduledTask`, or `schtasks` to normalize,
  repair, or re-enable a canonical action during activation recovery.
- Never delete or edit a nonterminal activation journal, lock, ready receipt,
  scheduler XML backup, rollback checkout, or rollback bundle to force a retry.
- Never alter task actions to point at a SHA-specific stage or rollback path.
- Never copy `runtime.env` into a candidate, stage, receipt, or rollback bundle.
- Never fabricate CI, SOL, market-session, pick, trade, or return evidence.
- Keep missing truth missing; activation cannot certify strategy performance.
