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

Immediately before every host mutation, Task Scheduler must also prove an
ordered EOD-to-Finalizer boundary no earlier than the calendar-derived
preceding open session and before the target date. EOD is a weekday task while
Finalizer is daily, so Sunday-before-Monday correctly binds Friday EOD to
Sunday Finalizer; a holiday weekday can likewise advance both host task
timestamps without pretending that it was an open market session. Every
nonzero canonical `NextRunTime` must be in the future and on or after the
target date. This postpones activation until every intervening weekend or
holiday trigger is finished, permits future target-date triggers overnight,
and rejects stale or overdue triggers. When the preceding open session was
Monday, its Weekly task must also have completed.

A validated recovery journal may additionally admit an exact target-date
EOD-to-Finalizer boundary solely to compensate or clean up an expired
activation; it cannot use that evidence to progress or enable the candidate.
Monday-target recovery waits for the target's Weekly run and its advanced
next trigger. A multi-day stale nonterminal transaction, or a stale `COMPLETE`
transaction awaiting protected StateRoot completion, restores the exact
protected predecessor runtime and definitions but leaves every canonical and
auxiliary task `Disabled`. The activation tool returns an explicit deeply
validated fail-closed terminal envelope; the protected boundary binds its exact
receipt and journal hashes, preserves predecessor authorization and activation
lineage, seals the Disabled disposition, and clears the intent. A new governed
current-target activation is required before any task may be enabled.

The tool fails closed unless all of the following are true:

- `CandidateRoot` is a clean, self-contained clone (not a linked Git worktree)
  whose local configuration keeps `extensions.worktreeConfig` disabled and
  whose `HEAD` equals both the requested SHA and freshly fetched `origin/main`;
  it is also the checkout containing the invoked tool. Git pointer/common-dir
  and configuration bytes remain handle-locked throughout admission.
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
$release = "C:\Program Files\Dawnstrike\releases\$sha"
& 'C:\Program Files\Dawnstrike\Python313\python.exe' -I -B -S `
  (Join-Path $release 'scripts\dawnstrike_python_bootstrap.py') `
  --release-root $release `
  --expected-sha $sha `
  --script (Join-Path $release 'scripts\runtime_activation_contract.py') -- `
  seal-evidence `
  --input C:\r\dawnstrike-state\evidence\ci-unsealed.json `
  --output C:\r\dawnstrike-state\evidence\ci.json

& 'C:\Program Files\Dawnstrike\Python313\python.exe' -I -B -S `
  (Join-Path $release 'scripts\dawnstrike_python_bootstrap.py') `
  --release-root $release `
  --expected-sha $sha `
  --script (Join-Path $release 'scripts\runtime_activation_contract.py') -- `
  seal-evidence `
  --input C:\r\dawnstrike-state\evidence\sol-unsealed.json `
  --output C:\r\dawnstrike-state\evidence\sol.json
```

Delete the unsealed copies only through the operator's normal recoverable
cleanup workflow. Never put either evidence object in `runtime.env`.

## Read-only preflight

The preflight refreshes `origin/main` and reads Git, Task Scheduler, SQLite,
and evidence state. It does not create a state backup or swap a directory.

The first protected release uses an explicit two-install transition. Do not
collapse these steps or install B directly over the legacy runtime:

1. Independently freeze A's 40-hex commit/tree plus the exact byte length and
   SHA-256 of A's outer bridge and committed installer. Admit the bridge through
   the tiny built-in preloader below; never execute its mutable pathname. The
   admitted unelevated bridge extracts the installer from the exact Git object
   and crosses UAC only through a built-in Windows PowerShell encoded command.
   The elevated command reopens and rechecks those exact bytes, publishes the
   administrator-only `installers\A` directory by a same-parent no-replace
   rename, retains the protected installer handle, and invokes it.
2. Invoke the protected A launcher in `BootstrapBaseline` mode. This performs
   the governed legacy b722-to-A runtime transaction, leaves all five canonical
   tasks Disabled, and commits exact BOOTSTRAP current-runtime authorization for
   A. Require A's exact sealed CI/Sol evidence; a host-install receipt alone is
   not activation authority.
3. Independently freeze B plus B's bridge and installer length/SHA-256, admit
   that bridge through the same built-in preloader, and pass all four
   predecessor fields set to A's boundary SHA/tree and runtime SHA/tree. B's
   protected launcher performs the crash-safe candidate migration and must
   preserve A's BOOTSTRAP authorization and Disabled tasks.
4. Invoke the protected B launcher in ordinary `Activate` mode with B's exact
   CI/Sol evidence. Only this transaction may authorize B and restore the
   canonical task enablement contract.

```powershell
$shaA = '<accepted-bootstrap-A-sha>'
$treeA = '<accepted-bootstrap-A-tree>'
$bridgeA = 'C:\r\dawnstrike-main\scripts\bootstrap_dawnstrike_host_boundary.ps1'
$bridgeShaA = '<independently-frozen-sha256-of-exact-A-bridge-bytes>'
$bridgeLengthA = <length-of-exact-A-bridge-bytes>
$installerShaA = '<sha256-of-exact-A-installer-bytes>'
$installerLengthA = <length-of-exact-A-installer-bytes>
$shaB = '<accepted-release-B-sha>'
$bridgeB = 'C:\r\dawnstrike-main\scripts\bootstrap_dawnstrike_host_boundary.ps1'
$bridgeShaB = '<independently-frozen-sha256-of-exact-B-bridge-bytes>'
$bridgeLengthB = <length-of-exact-B-bridge-bytes>
$installerShaB = '<sha256-of-exact-B-installer-bytes>'
$installerLengthB = <length-of-exact-B-installer-bytes>
$marketDate = '<YYYY-MM-DD>'

# Type/audit this tiny built-in preloader independently. Never invoke the
# checkout bridge with -File. The no-share stream stays open through bridge
# execution, so no mutable pathname is parsed after its exact hash admission.
function Invoke-FrozenDawnstrikeBootstrapBridge {
  param(
    [string]$Path,
    [string]$ExpectedSha256,
    [long]$ExpectedLength,
    [hashtable]$Arguments
  )
  if ($ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$' -or
      $ExpectedLength -lt 1 -or $ExpectedLength -gt 1048576) { throw 'Invalid bridge contract.' }
  $stream = [IO.File]::Open($Path, 'Open', 'Read', 'None')
  $bytes = $null
  try {
    if ($stream.Length -ne $ExpectedLength) { throw 'Bridge length mismatch.' }
    $bytes = New-Object byte[] $ExpectedLength
    $offset = 0
    while ($offset -lt $bytes.Length) {
      $read = $stream.Read($bytes, $offset, $bytes.Length - $offset)
      if ($read -le 0) { throw 'Bridge read ended early.' }
      $offset += $read
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
      $actual = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
    if ($actual -cne $ExpectedSha256) { throw 'Bridge SHA-256 mismatch.' }
    $command = [ScriptBlock]::Create([Text.Encoding]::UTF8.GetString($bytes))
    & $command @Arguments
  } finally {
    if ($null -ne $bytes) { [Array]::Clear($bytes, 0, $bytes.Length) }
    $stream.Dispose()
  }
}

# These exact bridge bytes run unelevated. Only their encoded payload crosses UAC.
Invoke-FrozenDawnstrikeBootstrapBridge `
  -Path $bridgeA -ExpectedSha256 $bridgeShaA -ExpectedLength $bridgeLengthA `
  -Arguments @{
    ExpectedSha = $shaA
    CandidateRoot = 'C:\r\dawnstrike-main'
    ExpectedInstallerSha256 = $installerShaA
    ExpectedInstallerLength = $installerLengthA
  }

C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass -Command {
    $runAs = Get-Credential
    & "C:\Program Files\Dawnstrike\releases\$shaA\scripts\dawnstrike_release_launcher.ps1" `
      -Mode BootstrapBaseline -ExpectedSha $shaA -CandidateRoot C:\r\dawnstrike-main `
      -MarketDate $marketDate -CiEvidencePath C:\r\dawnstrike-state\evidence\ci-A.json `
      -SolEvidencePath C:\r\dawnstrike-state\evidence\sol-A.json `
      -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state `
      -BackupRoot C:\r\dawnstrike-state-backups -RunAsCredential $runAs
  }

Invoke-FrozenDawnstrikeBootstrapBridge `
  -Path $bridgeB -ExpectedSha256 $bridgeShaB -ExpectedLength $bridgeLengthB `
  -Arguments @{
    ExpectedSha = $shaB
    CandidateRoot = 'C:\r\dawnstrike-main'
    ExpectedInstallerSha256 = $installerShaB
    ExpectedInstallerLength = $installerLengthB
    BoundaryPredecessorSha = $shaA
    BoundaryPredecessorTree = $treeA
    RuntimePredecessorSha = $shaA
    RuntimePredecessorTree = $treeA
  }

C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass -Command {
    $runAs = Get-Credential
    & "C:\Program Files\Dawnstrike\releases\$shaB\scripts\dawnstrike_release_launcher.ps1" `
      -Mode Activate -ExpectedSha $shaB -CandidateRoot C:\r\dawnstrike-main `
      -MarketDate $marketDate -CiEvidencePath C:\r\dawnstrike-state\evidence\ci-B.json `
      -SolEvidencePath C:\r\dawnstrike-state\evidence\sol-B.json `
      -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state `
      -BackupRoot C:\r\dawnstrike-state-backups -RunAsCredential $runAs
  }
```

After each boundary, retain and verify the exact protected receipts. Step 1
creates `host-boundary-A.json` and `state-boundary-A.json`; step 2 must leave a
COMPLETE runtime-activation receipt/journal plus BOOTSTRAP authorization for A.
Step 3 creates `host-boundary-B.json`, `state-boundary-B.json`, and the protected
candidate-migration intent/completion lineage naming A. Step 4 must end with B's
COMPLETE runtime-activation receipt/journal and current ACTIVATE authorization.
A missing, mismatched, or nonterminal receipt is a stop condition, not permission
to infer or repeat a later step.

The installer anchors MinGit 2.55.0.5 and the Python 3.13.15 core to their
official archive/installer lengths, digests, and signers. Git, Python, and the
lock-content-addressed dependency tree each receive a fixed in-root seal while
still in an administrator-only random stage; the complete sealed directory is
validated before a same-parent no-replace rename. The user-profile Python
environment is only a byte cache: every materialized source, native, and data
payload must be owned by `requirements.lock` and match its source-approved
wheel `RECORD` hash and size. Extra distributions and unowned files are never
copied.
`CandidateRoot` is compatibility/audit input only. The installer independently
fetches live canonical main into an immutable, administrator-owned
`C:\Program Files\Dawnstrike\releases\<sha>` tree, and every promoted or
executed release file comes from that protected exact-SHA repository.
The installer writes only below
`C:\Program Files\Dawnstrike` and `C:\ProgramData\Dawnstrike`, removes
inherited non-admin write access, and records a host-boundary receipt.
Activation and rollback must then enter through the exact protected launcher
at `C:\Program Files\Dawnstrike\releases\<sha>\scripts\dawnstrike_release_launcher.ps1` so
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
    & "C:\Program Files\Dawnstrike\releases\<accepted-origin-main-sha>\scripts\dawnstrike_release_launcher.ps1" `
      -Mode HardenCapture `
      -CandidateRoot C:\r\dawnstrike-main `
      -ExpectedSha <accepted-origin-main-sha> `
      -RuntimeRoot C:\r\dawnstrike-runtime `
      -StateRoot C:\r\dawnstrike-state `
      -RunAsCredential $runAs
  }

C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\Program Files\Dawnstrike\releases\<accepted-origin-main-sha>\scripts\dawnstrike_release_launcher.ps1' `
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
  -File 'C:\Program Files\Dawnstrike\releases\<accepted-origin-main-sha>\scripts\dawnstrike_release_launcher.ps1' `
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
7. after the durable `PRE_SWAP` journal, rechecks the session clock and then
   renames the old runtime to its durable rollback checkout and the verified
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
11. rechecks the session clock after the durable ready journal, re-enables only
    the originally enabled five tasks, verifies the exact new Ready contract,
    then atomically seals a `COMPLETE` receipt. The sealed
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
| `PRE_SWAP` | All five tasks are `Disabled` and match the sealed pre-rebind contract; the old runtime or the exact staged/rollback rename boundary is provable. | Recheck the current session clock immediately before any cutover rename. If the window closed, restore the previous runtime and sealed Ready XML contract and seal `COMPENSATED`; never install or enable the candidate. |
| `POST_SWAP` (rebind intent) | The candidate is installed, the rollback checkout is present, and all five tasks are `Disabled`. Each action must be exactly either its sealed pre-rebind form or its independently derived candidate-SHA form, and all preserved XML sections must match. This is the only phase in which a 1-through-4 partial rebind is admissible. | Verify the installed candidate and exact per-task old-or-target action proof, then idempotently converge every action to the candidate-SHA form. Recheck the current clock immediately before enablement; if the window closed, restore the previous runtime and sealed Ready XML and seal `COMPENSATED`. Any third form fails closed. |
| `POST_SWAP_READY` | The journal binds the ready-to-enable `PREPARED` receipt. A crash may have left zero through five candidate tasks enabled. | Force all five tasks back to the exact sealed Disabled candidate boundary, then recheck the current clock immediately before enablement. If the window closed, restore the previous runtime and sealed Ready XML and seal `COMPENSATED`; otherwise enable, re-prove the exact Ready contract, and seal `COMPLETE`. |
| `COMPLETE` | All five canonical tasks are `Ready` under the exact candidate action/definition hashes. | Verify and return the existing terminal receipt; do not repeat mutation. |

`POST_SWAP` rebind intent is recovery authority only when the journal, lock
pair, candidate and previous runtime identities, prepared receipt,
scheduler-backup manifest, and task contracts all verify together. A fresh
invocation cannot use that rule to excuse a mixed host state. During recovery,
the tool compares each disabled task with the two exact allowed action forms
and proves that `Principal`, `Triggers`, `Settings`, task path, and working
directory did not change. It does not accept an action merely because it
contains a plausible SHA or points somewhere under `C:\r`.

No nonterminal recovery phase inherits an earlier process's activation clock.
An expired recovery is terminally compensated to the exact previous runtime and
pre-activation task definitions/actions with every canonical and auxiliary task
left `Disabled`. This fail-closed disposition applies even when the sealed
backup recorded `Ready`: compensation is never an alternate enablement path,
and a later governed current-target transaction is required to restore task
enablement. In every pre-swap crash shape the candidate is preserved at the
governed failed-candidate path, while the fixed stage and rollback-checkout
paths are proven absent before the compensation journal and locks become
terminal. Candidate or predecessor task enablement is never used as a stale
recovery mechanism after the Morning or market-date boundary.

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
    & "C:\Program Files\Dawnstrike\releases\<accepted-origin-main-sha>\scripts\dawnstrike_release_launcher.ps1" `
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
  -File 'C:\Program Files\Dawnstrike\releases\<exact-candidate-sha>\scripts\dawnstrike_release_launcher.ps1' `
  -Mode Rollback `
  -CandidateRoot C:\r\dawnstrike-main `
  -ExpectedSha <exact-candidate-sha> `
  -ActivationReceipt C:\r\dawnstrike-state\receipts\runtime-activation\runtime-activation-<id>.json `
  -ContractRoot C:\r\dawnstrike-main `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -BackupRoot C:\r\dawnstrike-state-backups
```

Fresh rollback derives `rollback_target_market_date` from the exact candidate
calendar; it never accepts a caller-selected rollback date. It may begin in the
pre-Morning window for the current open session or after the core session/on a
closed day for the next open session. This target is distinct from the
activation receipt's immutable `activation_market_date`. The rollback target
is sealed into the v3 `INIT` journal and runtime lock, every later journal
phase, both rollback receipts, and the retained daily lock. Recovery takes the
target only from that protected evidence and re-derives the preceding open
date using the same exact candidate tree. A changed calendar result, remapped
date, stale unprotected request, or legacy nonterminal rollback journal fails
closed.

Immediately before lock admission, after the runtime and target-date daily
locks are both retained, and before every task enable, rollback requires one
exact instance of each canonical task in `Ready` or `Disabled` state. Every
nonzero `NextRunTime` must be strictly in the future and dated on or after the
rollback target; a `Ready` task may not report `MinValue`. EOD and Finalizer
must have `LastTaskResult=0`, be ordered, no later than now, no earlier than
the preceding open date, and earlier than the target. No canonical
`LastRunTime` may reach the target. The latest elapsed Monday Weekly occurrence
must also have completed successfully. These rules
admit weekday pre-Morning and Sunday-after-trigger completion before Monday,
reject an active session, and block Monday-after-session rollback until the
21:00 Weekly occurrence completes.

A protected in-flight rollback reuses `PROGRESS` only while that original
pre-Morning target window remains open. After target progress, automatic
`RECOVERY_WITH_RUN` requires target-date Morning, Monitor, EOD, and Finalizer
completion with `LastTaskResult=0`, plus the successful elapsed target Weekly
occurrence when the target is Monday. Every canonical trigger must expose a
non-`MinValue` future `NextRunTime`; timestamps from failed lock-denied launches
are not completion proof. A crash during sequential enablement is
admissible only in `POST_SWAP_READY`, when the journal hash-binds the exact
ready receipt and sealed scheduler inventory and the live states form the
canonical Ready-prefix/Disabled-suffix. An exact all-`Ready` recovery with
advanced triggers is finalized without another Task Scheduler mutation. A
shorter prefix is normalized to all `Disabled`; it is replayed only while the
original `PROGRESS` window remains open. If disabling hides trigger advancement
after target progress, the tool preserves both locks and the journal instead of
guessing that a catch-up enable is safe.

If a protected transaction outlives the target without any target run,
canonical `StartWhenAvailable=true` means enabling could launch missed work,
and a Disabled task may legitimately expose `MinValue` for `NextRunTime`.
Until a separately journaled catch-up-neutralization protocol has live Task
Scheduler proof, `EXPIRED_NO_RUN` therefore stops at an exact all-Disabled
boundary and preserves the journal plus both retained locks for governed
operator recovery. It never claims rollback completion or automatically
enables a task. Compensation receipt verification and sealing use the same
bounded child-process timeout as the rollback transaction; a timeout likewise
preserves journal and receipt evidence and removes only an uncommitted input.

For a crash before the complete receipt, pass the matching `.prepared.json`.
The tool verifies the bundle hash, exact previous commit/tree/origin, current
schema compatibility, all task definitions, persisted scheduler XML evidence,
and both locks. It stages the previous commit from the sealed Git bundle,
captures and disables exact-`Ready` tasks, requires exact `Disabled` state
throughout the swap, preserves the deactivated candidate, restores exact task
XML before any allowed enable, and writes an idempotent `ROLLED_BACK` receipt
under `receipts\runtime-rollback`. Prepared and completed rollback receipts
retain both the activation date and rollback-target date as separate fields. A
power loss after the terminal receipt is linked but before the journal reaches
`COMPLETE` adopts that receipt only when it is the exact field-for-field
terminal derivation of the journal-bound ready receipt and live Ready contract.

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
