# SOL audit — WP003 increment C outcome persistence and replay

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts migration 28, immutable outcome-batch persistence,
explicit correction lineage, derived current-head state, and pure stored replay.
It does not accept a mounted outcome runtime, missed-opportunity analysis,
research metrics, empirical validation, read-only operator projection, UI,
active-database migration, external data acquisition, broker execution,
deployment, or the global mission.

## Accepted scope

- migration 28 in `intraday_scanner/storage/migrations.py`;
- schema-27/28 run-store compatibility in
  `intraday_scanner/storage/opportunity_store.py`;
- exact run-receipt pair validation in
  `intraday_scanner/v2/opportunity/outcome_replay.py`;
- downstream receipt and replay contracts in
  `intraday_scanner/v2/opportunity/outcome_persistence.py`;
- typed errors in
  `intraday_scanner/storage/opportunity_outcome_errors.py`;
- pure inventory and receipt construction in
  `intraday_scanner/storage/opportunity_outcome_inventory.py`;
- exact governed-schema validation in
  `intraday_scanner/storage/opportunity_outcome_schema.py`;
- the explicit append-only adapter in
  `intraday_scanner/storage/opportunity_outcome_store.py`;
- migration, compatibility, persistence, correction-chain, tamper, replay,
  read-only, rollback, empty-state, and import-boundary tests.

No package root eagerly exports outcome storage. No active database, mounted
runtime, UI, network, broker, scheduler, deployment, commit, or push path was
used or changed.

## Accepted implementation facts

- Migration 28 adds exactly two append-only tables. Receipt predecessors bind
  the same run, content-bound receipt identity, and receipt content hash.
  Record predecessors bind the same run, receipt, outcome identity, and outcome
  content hash. Partial unique indexes enforce one root and one successor for
  receipt and evaluation-horizon record chains.
- Table CHECKs bind the exact accepted batch v2, record v3, and persistence
  receipt v1 schema literals; exact completeness, entry, and path status enum
  sets; nonnegative counts; paired predecessor fields; research-only state; and
  database schema 28.
- Both outcome tables reject UPDATE and DELETE through exact canonical guards.
  There is no replace, upsert, mutable latest table, cascade delete, cleanup
  command, or public SQL escape hatch.
- Historical run receipts remain supported only as the exact v1/schema-27
  pair. Schema-28 runs emit only v2/schema-28 receipts. Migration preserves old
  result and receipt JSON, hashes, identities, and original append time.
- `OpportunityOutcomeStore` construction is inert. Only explicit writable
  `initialize()` creates or migrates a caller-supplied path. Read-only access
  uses the existing query-only connector and cannot initialize or append.
- Append prebuilds canonical batch bytes and inventory, verifies the exact
  stored parent run and run receipt, starts `BEGIN IMMEDIATE`, and uses plain
  INSERT statements. Any typed, SQLite, or injected failure rolls back the
  receipt and all record rows.
- Exact batch-ID reappend verifies the complete stored run chain before
  returning the original receipt and first persistence time. It is checked
  before current-head staleness so deterministic historical retries remain
  idempotent.
- Initial batches require no predecessor. Corrections require the unique
  current head, strict persistence chronology, an exact pair-set superset, and
  changed identity/content for every overlapping evaluation-horizon record.
  Newly declared horizons form explicit record roots; older pairs cannot be
  dropped or silently reused.
- Every return and post-insert commit path audits the complete per-run graph:
  exactly one root and head for nonempty history, no fork, orphan, cycle, or
  disconnected component, exact adjacent chronology, exact record lineage, and
  a unique current leaf for every evaluation-horizon pair.
- Loads decode only strict content-bound contracts, require canonical JSON byte
  equality, reverify the complete parent run artifact inventory, reconstruct
  every receipt and record projection, recompute the two-family inventory and
  hash, and reject missing, extra, reordered, malformed, or inconsistent rows.
- Historical replay returns the exact requested batch and verified receipt
  prefix. Current replay returns the unique head batch and full verified chain.
  No current market lookup, clock, network, or relabeling input participates.
  Pending, censored, unavailable, and empty outcomes remain unchanged.
- Schema validation compares quote-aware exact SQL fingerprints for both
  tables, every explicit index, and all four guards. Independent PRAGMA checks
  bind primary-key order, complete unique and partial-index keys, explicit
  index column order, foreign-key targets/columns/actions/match, foreign-key
  enforcement, and zero governed-table foreign-key violations.
- Public outcome-store operations translate parent-run, schema, connection,
  stored timestamp, and persisted-chain failures into the adapter's typed
  integrity/conflict family. Invalid caller values still fail before database
  access.
- The final implementation is partitioned into bounded acyclic error,
  inventory, schema, adapter, and replay-contract modules. Core opportunity and
  storage package-root imports load none of them.

## Sol adversarial findings remediated

1. The first migration-28 draft did not use the exact accepted entry-status
   enum, did not bind predecessor receipt content hash in its same-run FK, and
   did not CHECK the accepted batch/record/receipt schema literals. The DDL and
   generated enum-set regressions now bind all three persisted status enums and
   exact schema identities.
2. The first idempotent reappend verified only the requested receipt, while
   current APIs could treat a nonempty no-head or disconnected graph as empty.
   Every public return and append now audits the complete per-run root-to-head
   graph; corrupt predecessor, cycle, fork, orphan, or disconnected history
   fails closed.
3. Exact SQL fingerprinting initially lacked the independent structural and
   data-integrity proof required by the design. The final validator adds exact
   PK/unique/partial-index/FK checks and scoped foreign-key checks. Same-named
   no-op guards, wrong indexes, altered quoted CHECK literals, and orphan rows
   all fail through initialize, load/replay, and append paths.
4. Parent `OpportunityStore` failures, malformed stored timestamps, invalid
   persisted corrections, connector failures, and malformed schema-version
   rows could escape as sibling or raw exception types. The adapter now maps
   stored-state failures to its own typed error surface and distinguishes
   append conflicts from persisted corruption.
5. Hardening temporarily grew the adapter beyond 1,500 lines. The final
   behavior-preserving partition leaves the store at 902 lines, with pure
   inventory, schema, and error ownership in separate small modules and no
   circular imports.

## Independent proof

Sol independently inspected the final transaction, chain, schema, inventory,
receipt, load, replay, read-only, and import paths. Sol then ran an independent
high-risk final-state subset covering multi-revision roots/leaves, corrupt
prefix idempotency, same-named forged guards, scoped orphan rejection, raw
stored projection/inventory tamper, typed schema/parent failures, and the AST
firewall.

```powershell
py -m pytest tests/test_opportunity_outcome_persistence.py -q -p no:cacheprovider -k "correction_chronology or identical_correction or same_named_forged or orphan_outcome or raw_stored or schema_27 or ast_import"
```

Sol result: `18 passed`, exit 0, 245.2 seconds.

Final authoritative focused gate:

```powershell
py -m pytest tests/test_opportunity_outcomes.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py tests/test_opportunity_outcome_persistence.py -q -p no:cacheprovider
```

Result: `429 passed`, exit 0, 821.7 seconds. Collection reconciled exactly as
5 migration + 9 contracts + 18 features + 44 outcome persistence + 73 outcomes
+ 23 run persistence + 57 pipeline + 200 universe/risk.

Affected storage/data-truth gate:

```powershell
py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

Result: `139 passed`, exit 0, 128.2 seconds.

Final static and compile proof also passed: whole-repository Ruff; mypy across
266 source files; compileall for `intraday_scanner`; and diff-check. The only
status/stat notices were inherited Windows line-ending notices on shared dirty
files.

One intermediate test expansion run completed 48/49. The sole failure was a
fixture that constructed a distinct `PipelineResult` identity from `__dict__`
instead of the canonical pipeline identity payload. Production code was not
implicated. The exact reproducer passed after the fixture correction, and both
subsequent full 49-test runs passed before the 429-test gate.

## Requirement adjudication

No global requirement or finding is closed. Increment C supplies additive
evidence toward REQ-ARCH-001, REQ-SAFE-001, REQ-DATA-003/005,
REQ-OUT-001/002/003, REQ-TRACE-001, REQ-PERSIST-001, REQ-TEST-001/002, and
REQ-DOC-001. Acceptance still depends on first-class missed-opportunity
classification and discovery metrics, shared chronological validation and
stress, empirical/external-data evidence, disabled-by-default mounted read-only
projection, operator UI, and final clean-worktree end-to-end proof.
