# Luna WP006 handoff — durable validation and locked-OOS governance

Date: 2026-08-16
Terminal material state: `PASS_CANDIDATE_FOR_SOL_ADJUDICATION`

This handoff reports implementation and reproduced evidence only. It does not
self-accept WP006, close a requirement or finding, claim empirical edge or
profitability, report a real locked-OOS result, or authorize promotion, TAKE,
production, or live execution.

## Frozen source and scope

- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- HEAD before and after: `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- Controlling decision:
  `docs/quant-refactor/20-wp006-sol-design-decision.md`
- Accepted WP001-WP005-C validation semantics were not changed. Integration
  edits are limited to schema-30 compatibility for existing storage readers,
  the additive migration, lazy storage exports, and narrow migration tests.
- No primary checkout, active database, mounted runtime, UI, provider/network,
  broker, scheduler, deployment, branch history, commit, stage, or push action
  occurred.

## Changed implementation

New storage modules:

- `intraday_scanner/storage/opportunity_validation_contracts.py`
- `intraday_scanner/storage/opportunity_validation_errors.py`
- `intraday_scanner/storage/opportunity_validation_rows.py`
- `intraday_scanner/storage/opportunity_validation_schema.py`
- `intraday_scanner/storage/opportunity_validation_store.py`

Narrow integration and compatibility edits:

- `intraday_scanner/storage/migrations.py`
- `intraday_scanner/storage/__init__.py`
- `intraday_scanner/storage/opportunity_store.py`
- `intraday_scanner/storage/opportunity_outcome_schema.py`
- `intraday_scanner/storage/opportunity_outcome_store.py`
- `intraday_scanner/storage/opportunity_miss_schema.py`
- `intraday_scanner/storage/opportunity_miss_store.py`
- `intraday_scanner/storage/opportunity_metric_schema.py`
- `tests/test_intraday_evidence_migration.py`
- `tests/test_opportunity_persistence.py`
- `tests/test_opportunity_outcome_persistence.py`
- `tests/test_opportunity_validation_persistence.py`

Documentation and evidence:

- `docs/quant-refactor/04-execution-log.md`
- this handoff
- `docs/quant-refactor/evidence/wp006-20260816/`

## Durable contracts and bindings

`ValidationPersistenceReceipt` binds all of the following without a
caller-provided receipt ID:

- exact preparation, trading-metric, robustness, and holdout-access IDs and
  content hashes;
- exact corpus, split plan, split declaration, code hash, strategy identity and
  version, confirmatory unit, corpus policy, metric policy, and robustness
  policy identities/hashes;
- exact ordered locked-OOS session IDs, content hashes, exchange-session IDs,
  UTC windows, count, and inventory hash;
- exact result-set hash, UTC persistence time, schema `30`, and explicit
  research-only/non-promotional/no-TAKE/no-lifecycle-mutation state.

`ValidationPersistenceReplay` reconstructs every stored canonical JSON body
independently and rechecks all cross-body bindings. A stored projection,
receipt JSON, body JSON, inventory row, count, hash, identity, chronology, or
status mismatch fails closed.

`ValidationPersistenceStatus` distinguishes `RESEARCH_EVIDENCE`,
`LOCKED_OOS_CONSUMED`, `INVALID_LOCK`, `RETROSPECTIVE`, `REUSED`,
`MISSING_EVIDENCE`, and `NON_PREDECLARED`. A successful consumption requires
an exact required nonempty OOS allocation, pre-OOS split and robustness-policy
declarations, `NO_DURABLE_EVIDENCE`, the accepted not-yet-durably-verified
holdout status, and zero OOS sessions in the robustness population. Invalid
statuses may remain non-promotional evidence but cannot become a successful
fresh consumption.

## Database-enforced one-time use

Migration `30` adds only:

- `opportunity_validation_receipts`
- `opportunity_validation_oos_sessions`
- five ordinary inventory/query indexes;
- three partial unique indexes for consumed rows:
  `uq_opportunity_validation_consumed_lock`,
  `uq_opportunity_validation_consumed_authority`, and
  `uq_opportunity_validation_consumed_inventory`;
- four append-only update/delete guard triggers.

The exact semantic key binds the preparation's transitive corpus/split/lock
body, holdout-access body, code hash, strategy/unit, all policies, and OOS
inventory. The authority key excludes later result and inventory projections,
so changed-result or changed-inventory retries still collide. The inventory
key prevents the exact OOS session set from being consumed again under changed
policy or identity aliases. These are SQLite uniqueness constraints, not
in-memory flags.

`BEGIN IMMEDIATE` covers the receipt row, all session rows, post-insert body
reconstruction, inventory reconciliation, and commit. Any insert, constraint,
decode, binding, or post-insert verification failure rolls back the complete
transaction and leaves no consumed key. An exact byte-equivalent/body-equivalent
retry returns the original receipt; same identity/different content fails.

## Schema and compatibility proof

Two disposable databases were independently migrated to schema `30`, migrated
again idempotently, and validated with `PRAGMA quick_check`. Their exact
governed SQL fingerprints and PRAGMA table/index/foreign-key structures were
identical. The canonical inventory SHA-256 is:

`116bde8f9bb41dca9e262f9ef9961e91f766fc942e7983590d12735f590cc3ce`

Existing opportunity, outcome, miss, and metric schema validators accept the
additive schema `30` while retaining their existing receipt generations and
governed-object fingerprints. No existing opportunity persistence table or
row is altered by migration `30`.

## Durable verification

Evidence root:
`docs/quant-refactor/evidence/wp006-20260816/`

| Gate | Collection/result | Exit | Elapsed seconds |
| --- | ---: | ---: | ---: |
| WP006 focused persistence | 15/15 | 0 | 2459.471 |
| Accepted WP005-C robustness | 19/19 | 0 | 393.320 |
| Exact accepted WP005-B main command | 656/656 | 0 | 9734.464 |
| Exact affected command | 139/139 | 0 | 128.315 |
| Ruff | all passed | 0 | 0.294 |
| mypy | 315 source files | 0 | 2.536 |
| compileall | passed | 0 | 0.363 |
| git diff check | passed | 0 | 0.253 |
| AST/fresh-process import firewall | 28 files, zero violations/modules | 0 | 0.875 |

Collection logs independently reconcile `15`, `19`, `656`, and `139` tests.
Raw stdout/stderr, exact command text, UTC start/end times, elapsed time, and
exit JSON are stored alongside the source/schema/state inventories.

## Active-state invariance

`C:\r\dawnstrike-state\shadow_real.sqlite` was inspected only with SQLite URI
`mode=ro` and `PRAGMA query_only=ON`. `PRAGMA quick_check` returned `ok`; the
active schema remained `26`. Before and after:

- SHA-256:
  `78f4a39fb31f389c05ef7ab626a74f89f840243fa79ff678ac05bad8379f93e6`
- byte length: `198836224`
- `-wal`, `-shm`, and `-journal` sidecars: none

No migration or write was attempted against active state.

## Repair cycles and limitations

- Implementation repair cycles: `0`.
- Evidence-script correction cycles: `2`. Cycle 1 inserted the isolated
  worktree root into the standalone import-firewall script. Cycle 2 explicitly
  closed disposable SQLite handles and completed static import ordering. The
  final versions and all final gates exited `0`.
- Synthetic fixtures establish software, serialization, transaction, schema,
  uniqueness, rollback, tamper, and import-boundary invariants only.
- No real holdout was opened, evaluated, tuned against, or reported. No result
  is empirical edge, profitability, promotion, or final certification.
- Active state intentionally remains at schema `26`; WP006 proves disposable
  migration and read-only invariance only.

## Luna terminal state

`PASS_CANDIDATE_FOR_SOL_ADJUDICATION`

This is evidence for Sol. Luna does not mark REQ-BT-002 or any other
requirement/finding closed and does not authorize WP007 or later work.
