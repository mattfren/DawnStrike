# Quant-refactor final independent technical audit

Audit date: 2026-08-16  
Auditor role: fresh-context independent Luna technical auditor  
Repository: `C:\r\dawnstrike-quant-refactor-20260811`  
Branch: `codex/sol-quant-refactor-20260811`  
Audited base HEAD: `bec32fe752b91f4e1357236a538a6dfea5da56bf`  
Controlling brief SHA-256: `a68728218cbbcede5df2e92151aa24321064f976903e8953d27695d9f696e556`  
Evidence root: `docs/quant-refactor/evidence/final-audit-20260816/`
Evidence index: `docs/quant-refactor/evidence/final-audit-20260816/evidence-index.md`

## Material verdict

`HASH_DRIFT`

This is a software and certification verdict, not a profitability verdict. The
candidate contains substantial deterministic, causal, research-only machinery,
and many focused and broad historical gates are strong. The active SQLite hash
and mtime changed during the audit while both auditor inspections were strictly
read-only. Source/branch/HEAD remained invariant, but active-state attribution
is unknown, so the audit cannot bind one frozen runtime state. Independent of
that drift, the candidate also has confirmed repair-required findings: a current
return-truth defect, no final commit, mandatory mission gaps, open ledgers, and
failing repository/security gates.

Unresolved independent-audit findings:

- CRITICAL: 2
- HIGH: 6
- MEDIUM: 2
- LOW: 0
- Total: 10

This audit does not close any requirement or prior finding, accept or reject a
work package, promote a strategy, or certify the program.

## Materials reviewed

The audit read the complete controlling brief; `00-current-state-audit.md`,
`01-target-architecture.md`, `02-requirements-ledger.md`,
`03-audit-ledger.md`, `04-execution-log.md`, `recovery-ledger.md`, and
`agent-operating-protocol.md`; every accepted Sol audit 05 through 23; Luna
handoffs 001 through 007 including the WP005 variants; and the durable WP005-B,
WP005-C, WP006, and WP007 evidence directories. Existing summaries were not
trusted for content identity: manifests and recorded source hashes were
independently rehashed. The absent canonical WP006 `evidence-packet.md` is
reported as M-02 rather than silently inferred from neighboring artifacts.

## Findings

### CRITICAL

#### C-00 — Active SQLite hash and mtime drifted during the audit

Before the gate, `C:\r\dawnstrike-state\shadow_real.sqlite` had SHA-256
`78f4a39fb31f389c05ef7ab626a74f89f840243fa79ff678ac05bad8379f93e6`,
length 198,836,224, and mtime `2026-08-15T23:34:09.687280+00:00`. After the gate
it had SHA-256
`3ec2ffd2b83181ee14b918b88c87beac5c4831e28bd893e718d38f1acb69805c`
and mtime `2026-08-16T12:26:00.982574+00:00`; length remained 198,836,224.
No WAL, SHM, or journal sidecar existed at either capture.

Both auditor captures used SQLite URI `mode=ro`, immediately enabled
`PRAGMA query_only=ON`, observed `query_only=1`, `quick_check=ok`, schema 26,
and proved identity stable within each individual read. Repository comparison
proves branch, HEAD, origin/main, and all 1,024 candidate file identities were
unchanged. Therefore the auditor did not establish one invariant active-state
identity. Attribution to an external scheduled/runtime writer versus a test
path is unknown and is not inferred.

Impact: runtime preservation and any state-bound certification claim are
invalid. This is the direct basis for the `HASH_DRIFT` verdict.

Required action: hold all active writers, identify the exact writer/event at
`2026-08-16T12:26:00.982574+00:00` from process/task/runtime evidence, adjudicate
whether the write was authorized, and rerun state-bound verification from a
declared frozen identity. Do not restore, migrate, or mutate the database
without separate authority.

#### C-01 — V5/V6 return learning still accepts unauthenticated legacy return truth

Requirements/findings: `REQ-OUT-003`, `REQ-BT-001`, existing `FINDING-009`.

Evidence:

- `docs/quant-refactor/03-audit-ledger.md:165-186` records `FINDING-009` as
  CRITICAL and OPEN, requiring exact-one intent, causal time, source/bar hashes,
  halt/gap censoring, and mounted-path proof.
- `intraday_scanner/services/alpha_outcome_capture_service.py:348-357` keeps the
  first eligible `ENTER_LONG` row per signal; it does not prove exactly one
  canonical authenticated entry receipt.
- `intraday_scanner/services/alpha_outcome_capture_service.py:1203-1211` makes
  V5/V6 return learning eligible when benchmark evidence is complete and merely
  `entry_intent is not None`.
- `intraday_scanner/services/alpha_outcome_capture_service.py:1790-1897` accepts
  the selected intent, sorts generic fills by string time, and falls back from
  an observed entry fill to an expected entry price.
- `intraday_scanner/alpha/v6/label_builder.py:188-203` explicitly returns
  `eligible: True` and `LEGACY_CONTRACT` when no additive return-truth keys are
  present. Lines 228-236 permit causal identity to be inferred from generic
  decision fields rather than an authenticated entry receipt.
- `intraday_scanner/alpha/canonical_return_truth.py` is absent from this branch.

Impact: invalid entry/return evidence can enter learning labels and contaminate
expectancy or later promotion evidence. Synthetic green tests do not close the
mounted production-data path.

Required repair: port or equivalently implement the canonical return-truth
contract in this lane; require exactly one authenticated entered-paper receipt,
bind causal time, observation/source/bar hashes, enforce halt/gap/path censoring,
and fail legacy/missing contracts closed. Add seeded intent matrices,
adversarial hash/time mutations, mounted capture-to-label tests, and independent
Sol closure evidence.

### HIGH

#### H-01 — No commit or clean VCS object binds the audited candidate

Evidence: `candidate-analysis-v2.json` records HEAD
`bec32fe752b91f4e1357236a538a6dfea5da56bf`, 16 tracked changes, 367 untracked
candidate files, and 95 untracked files outside `docs/quant-refactor`. The
current HEAD is the pre-refactor base. `git diff --check` exits zero but cannot
inspect untracked files; a CI checkout of HEAD cannot contain them.

Impact: the implementation, tests, documentation, and evidence do not have one
reproducible final source identity. The controlling certification template
requires a final commit/hash.

Required repair: after all code repairs, intentionally commit the complete
candidate, verify the exact tree and commit hash from a clean isolated worktree,
rerun all tests/security gates there, and bind the final evidence manifest to
that commit. Do not treat the current base HEAD as the refactor identity.

#### H-02 — The market-first closed loop has no mounted producer

Evidence: non-test reachability in `candidate-analysis-v2.json` finds only the
definitions of `prepare_opportunity_pipeline` at
`intraday_scanner/v2/opportunity/pipeline.py:207` and
`run_opportunity_pipeline` at line 920. It finds no non-test `.append_run(` call.
The only application-side `OpportunityStore` use is the read-only projection
loader at `intraday_scanner/dashboard/opportunity_projection_store.py:95`.
Accepted WP007 documentation itself states at
`docs/quant-refactor/23-wp007-sol-audit.md:65-74` that active state remains
schema 26, the feature is off by default, and `DATA_UNAVAILABLE` is expected
until compatible persisted runs exist.

Impact: the implemented research core can be exercised by fixtures but the
application cannot produce and persist a real opportunity run for the read-only
projection. The product loop is therefore not end to end.

Required repair: add an explicitly authorized, disabled-by-default producer
adapter that supplies causal provider/universe evidence to the shared pipeline
and persists the immutable run in a rehearsed non-active database. Prove the
mounted call graph, old-path preservation, no-network core boundary, and
read-only projection against persisted output before any active migration.

#### H-03 — Several mandatory requirements are absent rather than merely awaiting empirical data

Evidence:

- `REQ-STRAT-004` (`docs/quant-refactor/02-requirements-ledger.md:36`) requires an
  AlphaOps V5 delegating adapter. The registry at
  `intraday_scanner/v2/opportunity/registry.py:119-231` registers only DS
  families; there is no V5 adapter/parity producer.
- `REQ-FEAT-006` (ledger line 25) requires point-in-time catalyst evidence.
  `intraday_scanner/v2/opportunity/features.py:147-162` always emits unavailable
  catalyst state using `point_in_time_catalyst_adapter_not_in_package_001`.
- `REQ-OBS-001` (ledger line 64) requires duration/count/status/failure codes.
  `StageTraceEntry` at `models.py:1079-1088` and `_stage` at
  `pipeline.py:1278-1298` provide counts/reasons but no duration, stage status,
  or failure code contract.
- `REQ-PERF-001` (ledger line 67) requires evidence-identity cache reuse and
  invalidation. The pipeline has no cache/incremental producer or call-count
  invalidation contract.
- `REQ-ARCH-002` (ledger line 10) requires byte-equivalent current/historical
  adapters over shared rules. Fixtures prove deterministic core calls, but no
  current/historical producer pair exists.

Impact: the implementation package chain does not cover the controlling
requirements ledger. These are software gaps, not absent-provider-data gaps.

Required repair: issue bounded work packages for the V5 adapter, point-in-time
catalyst adapter, structured stage telemetry, evidence-keyed cache, and shared
current/historical adapters. Add the exact positive, negative, parity, call-count,
redaction, invalidation, and causal-time tests named by the ledger.

#### H-04 — Requirement, prior-finding, and certification adjudication is absent

Evidence: `candidate-analysis-v2.json` counts 63 requirements and 63 OPEN
statuses in `02-requirements-ledger.md`. It counts 14 prior audit findings and
14 OPEN statuses: BLOCKER 1, CRITICAL 1, HIGH 9, MEDIUM 3. Every accepted Sol
increment says it does not globally close requirements/findings. Section 21 of
the controlling brief requires zero unresolved critical/high findings and a
final commit/hash.

Impact: accepted additive packages cannot substitute for requirement-by-
requirement coverage or finding closure. The program cannot be certified while
its authoritative ledgers say every requirement and every prior finding is open.

Required repair: after implementation/security repairs and a frozen final
commit, Sol must adjudicate every requirement and finding against exact evidence,
record explicit closures or justified non-closures, and independently confirm
zero unresolved critical/high findings. Luna must not self-close them.

#### H-05 — Exact configured security gates fail

Evidence:

- Exact Bandit CI command exited 1 and reported eight medium B608 findings:
  `opportunity_metric_store.py:462,469`,
  `opportunity_miss_store.py:348,361,385`,
  `opportunity_validation_schema.py:184`, and
  `opportunity_validation_store.py:296,308`. The raw output is
  `bandit.stdout.txt`; metadata is `bandit.exit.json`.
- Exact tracked-file detect-secrets CI command exited 1 with 52 findings. Most
  observed values are evidence hashes or test tokens, suggesting reviewed
  false-positive/baseline drift rather than exposed credentials, but the gate
  still fails. Changed-candidate-only scanning covered 111 changed/untracked
  non-quant-doc files and exited zero.

Impact: the final candidate cannot pass the repository's security workflow.
The SQL inputs appear to use fixed column-order constants and parameter
placeholders, but exploitability must be reviewed, not inferred from this audit.

Required repair: review each B608 site, use static SQL or an approved safe helper
where practical, and add narrowly justified baseline entries only after human
security adjudication. Review the 52 tracked-file secret findings and update the
baseline/allowlists without suppressing real secrets. Rerun exact gates on the
committed candidate.

#### H-06 — Full repository pytest contains reproducible failures and an order-dependent failure marker

The exact full command collected 1,909 tests and ran for 12,577.279 seconds
before the bounded audit window required termination of only the verified pytest
child. Its preserved stdout contains 1,127 result markers (59.0% of collection)
and three `F` markers. Collection-order reconciliation maps them to:

- `tests/test_daily_publish_gate.py::test_artifact_gate_accepts_clean_explicit_no_trade_fixture`;
- `tests/test_daily_publish_gate.py::test_artifact_gate_accepts_only_explicitly_approved_degraded_fixture`;
- `tests/test_opportunity_validation_persistence.py::test_invalid_retrospective_reused_missing_and_nonpredeclared_fail_closed[no_durable_evidence-True-True-retrospective]`.

The exact three-node rerun reproduced both daily-publish failures and passed the
validation-persistence node. The publish verifier now requires
`data/opportunity-projection.json`, but the otherwise publishable fixtures do not
create it; expected PASS/error lists therefore diverge at
`tests/test_daily_publish_gate.py:150,168`. The third full-run marker remains an
order-dependent or cross-test-interference determinism gap because it passes in
isolation.

Impact: `REQ-TEST-002` is not met; the repository suite is not green, and one
observed failure cannot be reproduced from its node alone.

Required repair: reconcile daily publish fixtures and compatibility semantics
with the new projection artifact, then identify and eliminate the shared-state
or ordering dependency behind the validation-persistence marker. Rerun the full
suite from the committed clean candidate with a viable CI time budget.

### MEDIUM

#### M-01 — The full-suite CI command cannot finish inside its configured timeout

`.github/workflows/ci.yml:18` gives the quality job 20 minutes and line 36 runs
the entire `python -m pytest` suite. The independent exact full attempt ran
12,577.279 seconds and still reached only 59.0% before bounded termination.
Historical accepted WP007 evidence
also records a 656-test subset taking 9,811 seconds. The current CI timeout
therefore cannot reproduce the required full gate.

Required repair: shard or separately classify deterministic long-running
validation tests, or raise job timeouts with bounded resource expectations. The
committed candidate must then complete the actual CI workflow.

#### M-02 — The durable evidence/doc chain has one missing canonical packet and one unrecorded supersession seam

`docs/quant-refactor/evidence/wp006-20260816/evidence-packet.md` is absent even
though the WP006 handoff, run summary, manifest, and external manifest seal exist.
Independent source rehash also finds that WP006's recorded hash/length for
`docs/quant-refactor/04-execution-log.md` differs from current content. The later
WP007 inventory explicitly owns the execution-log append and all WP007 current
source hashes match, so this is authorized historical supersession, not
implementation hash drift. The lineage should say so directly.

Required repair: add the missing bounded WP006 packet or an explicit canonical
index to the existing sealed artifacts, and record the authorized WP007
supersession of the WP006 execution-log hash without rewriting immutable audits.

## Section 20 coverage matrix

| Audit category | Result | Evidence/conclusion |
| --- | --- | --- |
| Requirement coverage | REPAIR | 63/63 ledger rows OPEN; H-02/H-03 identify current software gaps. |
| Test evidence | REPAIR | Full suite incomplete with 3 observed failure markers; 2 reproduce exactly and 1 passes alone. Focused safety/contracts 41 passed; two exact security gates fail. |
| Determinism | PASS for implemented synthetic core | Contract/hash/tie/order tests and accepted packets are strong; no real current/historical adapter parity proof. |
| Leakage protections | REPAIR | Core import firewall passes, but C-01 allows legacy return truth into learning. |
| Causal/statistical controls | REPAIR | Validation robustness exists and fails missing control evidence closed; canonical entry causality and real locked OOS remain absent. |
| Safety constraints | SOFTWARE PASS / RUNTIME DRIFT | No broker/LLM/network path in opportunity core; 41 focused safety/import tests pass; auditor performed no external action. Active SQLite changed from an unattributed writer during the gate. |
| Promotion-veto semantics | PASS for implemented robustness report | `validation_robustness_report.py:95-157,574-592` hard-codes research-only/no TAKE/no lifecycle mutation and vetoes missing evidence. This is not promotion approval. |
| Source/evidence hash binding | HASH DRIFT | Candidate source/branch/HEAD invariant; 230 accepted manifest entries rehash; active SQLite identity changed during the audit; current candidate has no commit identity. |
| Documentation consistency | REPAIR | Missing WP006 packet; all ledgers open; accepted package scope is narrower than program completion. |
| TODO/FIXME/placeholder | REPAIR | No blocking literal TODO/FIXME in quant modules; catalyst is an explicit unimplemented adapter. Other placeholder matches are SQL parameter names, test fixtures, or fail-closed configuration checks. |
| Work-package closure | ACCEPTED ADDITIVE PACKAGES ONLY | Sol audits 05-23 accept bounded increments. They explicitly do not close global requirements or prove empirical edge. |
| Certification prerequisites | HASH DRIFT / REPAIR | C-00/C-01, H-01 through H-06, open ledgers, failing gates, and absent final commit prevent certification. |

## Independent verification

All commands ran in `C:\r\dawnstrike-quant-refactor-20260811` with no network,
provider, broker, deployment, Git mutation, or active-state mutation. Python byte
code was redirected to the final-audit evidence directory.

| Gate | Exact command | Result |
| --- | --- | --- |
| Full repository tests | `py -m pytest -q -p no:cacheprovider` | incomplete/failed partial gate: 1,127/1,909 markers, 3 `F`; bounded child termination at 12,577.279s; wrapper exit -1 |
| Observed failure rerun | exact three mapped node IDs in `observed-failures.command.txt` | 2 failed reproducibly, 1 passed alone; exit 1; 70.98s |
| Focused safety/import/promotion contracts | `py -m pytest -q -p no:cacheprovider tests/test_network_safety.py tests/test_sql_safety.py tests/test_opportunity_contracts.py tests/test_opportunity_validation_robustness.py` | 41 passed; exit 0; 433.78s |
| Ruff | `py -m ruff check .` | pass; exit 0 |
| mypy | `py -m mypy intraday_scanner` | pass, 318 files; exit 0 |
| compileall | `py -m compileall -q intraday_scanner scripts` | pass; exit 0 |
| pip check | `py -m pip check` | pass; exit 0 |
| Node syntax | `node --check web/assets/dawnstrike.js` | pass; exit 0 |
| PowerShell syntax | parser over all `scripts/**/*.ps1` | 29 parsed, 0 failures |
| Git whitespace | `git diff --check` | pass for tracked diff; exit 0; untracked files are outside this command's coverage |
| Bandit | `py -m bandit -r intraday_scanner scripts -ll -b config/security/bandit-baseline.json` | fail; exit 1; 8 medium findings |
| detect-secrets, exact CI | `py -m detect_secrets.pre_commit_hook --baseline .secrets.baseline <tracked files>` | fail; exit 1; 52 findings |
| detect-secrets, changed candidate | same hook over 111 changed/untracked non-doc files | pass; exit 0 |
| CycloneDX | exact reproducible environment SBOM command, output redirected under final evidence | pass; exit 0 |
| TODO/FIXME/placeholder scan | `scan_todos.ps1` | completed; adjudication above |
| pip-audit | exact CI command not run | intentionally omitted because it invokes an external vulnerability service and this audit forbids network actions |

Two exploratory whole-candidate detect-secrets attempts are preserved but are
not certification gates: the first could not start because the Windows command
line was too long; the chunked rerun completed but was dominated by expected
high-entropy SHA-256 values in untracked evidence packets. The scoped 111-file
changed/non-doc scan is the meaningful candidate-code supplement; the exact
tracked-file CI scan remains the authoritative configured gate and fails.

Raw stdout, stderr, literal commands, UTC timing, elapsed seconds, and exit codes
are stored per gate under the evidence root. Environment identity and the full
candidate file inventory are in `repository-state-before.json` and
`repository-state-after.json`.

## Independent evidence rehash

- `accepted-evidence-rehash-v2.json`: 230/230 accepted manifest entries match;
  no content mismatch; all present external seals match.
- `accepted-source-rehash.json`: 58 recorded source identities checked; 2
  mismatch records both describe the WP006 historical hash/length of the same
  later-appended execution log. WP007's current source set is 17/17.
- `repository-state-invariance.json`: invariant; branch, HEAD, origin/main, file
  set, lengths, and SHA-256 identities unchanged (authorized final-audit paths
  excluded).
- Evidence manifest: `evidence-manifest.json`; its detached SHA-256 is
  `evidence-manifest.sha256`.

## Active SQLite preservation

The only active-state inspection used SQLite URI `mode=ro`, then
`PRAGMA query_only=ON`; `PRAGMA quick_check` returned `ok` and schema version was
26. Before identity: SHA-256
`78f4a39fb31f389c05ef7ab626a74f89f840243fa79ff678ac05bad8379f93e6`,
length 198,836,224, mtime `2026-08-15T23:34:09.687280+00:00`, no WAL/SHM/journal
sidecars. Final comparison is **not invariant**: after SHA-256
`3ec2ffd2b83181ee14b918b88c87beac5c4831e28bd893e718d38f1acb69805c`,
same length, mtime `2026-08-16T12:26:00.982574+00:00`, no sidecars. See
`active-state-before.json`, `active-state-after.json`, and
`active-state-invariance.json`.

## Empirical boundary

The software gates do not supply real provider data, a newly locked one-time
OOS cohort, observed market edge, or profitability. Synthetic fixtures establish
implementation behavior only. Missing empirical evidence remains missing; it is
not zero, failure, success, promotion, or authorization. No live execution,
broker, provider, network, active migration, deployment, or publication action
was performed.

## Certification conclusion

Certification prerequisites are not met. First resolve the active-state hash
drift and establish a frozen runtime identity; then perform bounded software
repair followed by a new frozen-commit verification and Sol adjudication. This
auditor does not self-certify the program and does not close requirements or
findings.
