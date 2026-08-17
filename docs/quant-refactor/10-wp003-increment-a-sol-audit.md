# SOL audit — WP003 increment A append-only opportunity persistence

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts migration 27 and the dedicated, unmounted, research-only
append-only store for canonical `PipelineResult` v2 artifacts. It does not
accept future outcome labels, outcome persistence, replay, missed-opportunity
analysis, mounted runtime behavior, UI, external data, scheduling, broker
execution, deployment, or the global mission.

## Accepted scope

- migration 27 in `intraday_scanner/storage/migrations.py`;
- the dedicated `OpportunityStore`, artifact-family/count contracts, persistence
  receipt, typed errors, and strict schema/inventory verification in
  `intraday_scanner/storage/opportunity_store.py`;
- narrow public storage exports;
- disposable migration rehearsals in
  `tests/test_intraday_evidence_migration.py`;
- focused append/load/idempotency/conflict/rollback/corruption/read-only tests in
  `tests/test_opportunity_persistence.py`.

No active database path, runtime mount, outcome module, UI, network, broker,
scheduler, deployment, commit, or push behavior changed.

## Accepted implementation facts

- Constructing the store only records a caller-supplied path and mode. It does
  not create a file, directory, connection, or schema. Only writable
  `initialize()` creates/upgrades a database; append/load reject absent and stale
  schemas without migrating them.
- Read-only mode uses the existing URI read-only/query-only adapter and rejects
  initialization and append operations.
- Migration 27 adds one canonical run table and one ordered artifact table. The
  artifact table permits deterministic IDs to recur in different runs while
  preventing duplicate inventory ordinals, family ordinals, or family/ID pairs
  within one run.
- The schema has the exact 16 accepted artifact families, parent foreign key,
  required lookup indexes, and four guards that abort every UPDATE or DELETE on
  the two opportunity tables.
- Store schema validation binds canonical table, index, and trigger SQL while
  preserving quoted literal content. It separately rechecks primary-key order,
  complete unique-key sets, foreign-key semantics, explicit index column order,
  the exact governed object set, and enabled foreign-key enforcement.
- First append constructs the canonical complete result JSON and exact artifact
  inventory before opening the transaction, uses one `BEGIN IMMEDIATE`
  transaction and plain INSERT statements, verifies the inserted count, and
  commits atomically.
- Byte-identical repeated append verifies the persisted parent, receipt, and
  complete inventory, rolls back without writes, and returns the original
  receipt and first-recorded time.
- Same-run persisted drift raises `OpportunityPersistenceConflictError`; partial
  insertion failures roll back both the parent and every partial artifact row.
- `load_run` reconstructs only through `PipelineResult.from_json`, requires
  canonical byte equality, recomputes all parent identities and all 16 artifact
  families, and compares every ordinal, family ordinal, artifact ID,
  evaluation/decision association, schema version, payload byte string, content
  hash, and first-recorded time.
- The immutable persistence receipt binds run/preparation/optional-context IDs
  and hashes, decision and recorded times, exact canonical family counts, total
  artifact count, ordered inventory hash, schema version, and research-only
  state. Direct construction and JSON rehydration recompute its content identity.
- Valid distinct runs may contain the same non-content-unique evaluation ID with
  different content hashes; each run reloads independently and exactly.
- Empty runs still persist the four required singleton artifacts and explicit
  zero counts for absent optional families.

## Sol adversarial findings remediated

1. Initial schema validation trusted required trigger names. A disposable
   database accepted a harmless same-named `AFTER INSERT` trigger in place of the
   parent UPDATE guard; `load_run` succeeded and a direct UPDATE was possible.
   Validation now binds exact trigger timing, event, table, and body for all four
   guards and rejects unexpected governed objects.
2. The first SQL fingerprint lowercased quoted string literals. A real rebuilt
   artifact table whose CHECK used `'UNIVERSE_SNAPSHOT'` instead of
   `'universe_snapshot'` therefore passed despite different SQLite semantics.
   Quote-aware normalization now changes case/whitespace only outside quoted
   content and preserves doubled escaped quote characters.

## Independent proof

Sol independently reproduced both original attacks, reviewed the connection,
transaction, receipt, inventory, and load-verification paths, and replayed the
remediations against disposable databases. Same-named forged guards and altered
case/whitespace family CHECK literals now reject with
`OpportunityPersistenceIntegrityError` before initialize, load, or append can
accept the schema.

```powershell
py -m pytest tests/test_intraday_evidence_migration.py tests/test_opportunity_persistence.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Result: `311 passed`, exit 0.

```powershell
py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

Result: `139 passed`, exit 0.

```powershell
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
git diff --check
```

Results: Ruff exit 0; mypy `255 source files`, exit 0; compileall exit 0;
diff-check exit 0 with inherited LF/CRLF warnings only.

## Requirement adjudication

No global requirement or finding is closed. Increment A supplies additive
evidence toward REQ-ARCH-001/002/003, REQ-SAFE-001/002, REQ-DATA-005/006,
REQ-EVAL-001, REQ-TRACE-001, and REQ-TEST-001. Acceptance still depends on
future-label-isolated outcome contracts, append-only outcome persistence and
replay, missed-opportunity metrics, empirical validation, disabled-by-default
read-only projection, and final end-to-end proof.
