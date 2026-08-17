# LUNA WORK PACKAGE 003 — append-only persistence, causal outcomes, and replay

## 1. Authority and execution order

This package follows the accepted WP001/WP002 domain core and is implemented in
audited increments. Luna implements only the increment authorized by Sol and
pauses after the named gates. Sol alone adjudicates requirements/findings and
authorizes the next increment.

No development or migration command may touch the active Dawnstrike database.
All migration rehearsals use newly created temporary databases or explicit
disposable copies inside the test temporary directory.

## 2. Stable safety boundary

- research/paper only;
- no broker, order, credential, network, scheduler, deployment, or notification
  behavior;
- no mounted AlphaOps or UI integration in this package;
- no mutation of an existing operator database during tests or development;
- constructors and read-only adapters perform no implicit write;
- append-only means no update, delete, replace, or overwrite path;
- future outcome labels cannot be imported by feature, discovery, regime,
  strategy, ranking, risk, gate, preparation, or decision modules.

## 3. Increment A — persisted opportunity run

Add migration 27 and a narrow opportunity persistence adapter for the accepted
`PipelineResult` v2.

### Schema

Create an `opportunity_pipeline_runs` table containing the canonical complete
result JSON plus run/preparation/decision-context, dataset, universe, decision
time, schema version, content hash, research-only flag, and first-recorded time.

Create an `opportunity_run_artifacts` table containing an exact ordered
inventory of the contracts embedded in the run. Required artifact families:

- universe snapshot;
- prepared pipeline;
- expectancy bindings;
- cheap/rich/benchmark feature snapshots;
- candidates;
- market and security regimes;
- strategy evaluations;
- ranked opportunities;
- pipeline risk policy;
- execution-risk receipts;
- decision context where present;
- trade decisions;
- pair decision traces.

Each artifact row stores run ID, family, ordinal, exact artifact ID, optional
evaluation/decision foreign identity, canonical payload JSON, SHA-256 content
hash, schema version, and first-recorded time. Use composite primary/unique keys
that permit the same deterministic artifact to appear in different runs while
preventing duplicates within one run.

Foreign keys must be enabled by the adapter. Add database triggers that reject
UPDATE and DELETE on both tables. Do not use `INSERT OR REPLACE`, UPSERT-update,
or delete/reinsert semantics.

### Store contract

Add a dedicated `OpportunityStore`; do not add another large surface to
`SQLiteScanStore`.

- constructing a store does not create a file or run migrations;
- `initialize()` is the explicit write that runs migrations in writable mode;
- a read-only mode opens via the existing read-only connector, validates schema,
  and never creates or migrates;
- `append_run(result, recorded_at)` runs one transaction and returns an
  immutable content-bound persistence receipt;
- first append inserts the canonical run and exact artifact inventory;
- same run ID plus byte-identical canonical content is idempotent and returns the
  original persisted receipt/time without writing new rows;
- same run or artifact identity with different content raises a typed source
  conflict and leaves the database unchanged;
- partial writes roll back completely;
- `load_run(run_id)` reconstructs `PipelineResult.from_json`, verifies stored
  hashes/identity fields, recomputes the expected artifact inventory, and rejects
  missing, extra, reordered, or content-mismatched rows;
- `load_run` returns the byte-equivalent pipeline result;
- no generic SQL execution or mutation escape hatch is public.

The persistence receipt binds run ID/hash, preparation ID/hash, decision-context
ID where present, exact per-family counts, ordered artifact-inventory hash,
recorded time, database schema version, and research-only state. Direct
construction and JSON rehydration revalidate the receipt identity and counts.

### Increment-A tests

- migration 26 to 27 is additive and idempotent on two disposable rehearsals;
- pre-existing rows and hashes remain unchanged; quick-check is `ok`;
- store construction creates no file;
- first append/reload is byte-equivalent;
- repeated identical append preserves row counts and original recorded time;
- same run ID/different content and same artifact ID/different content conflict;
- injected failure proves total rollback;
- database UPDATE/DELETE triggers reject direct mutation;
- missing/extra/reordered/tampered artifact rows make `load_run` fail closed;
- read-only load succeeds and every read-only write/initialize attempt rejects;
- empty-universe run persists/reloads with exact zero optional-family counts;
- no opportunity persistence module imports outcome/backtest/UI/network/broker
  modules;
- no test or implementation resolves the active database path.

## 4. Increment B — future-label-isolated outcome contracts

Begin only after Sol accepts increment A. Amend the opportunity outcome contract
coherently with a schema bump. It must bind the exact stored run, evaluation,
decision, decision time, symbol/strategy/version/direction, source dataset and
post-decision observation identities.

Outcome fields remain null unless supported. Model entry/fill status, target/stop
ordering, MFE/MAE, horizon returns, simulated gross/after-cost R, time to target
or stop, and explicit ambiguity/censoring states for no entry, same-bar order,
gap-through, halt, missing bars, unattainable fill, pending horizon, and
unsupported evidence.

All source observations must be strictly after the decision time. `recorded_at`
cannot precede the last used observation. Mutating an outcome or future bar can
never alter the original preparation, evaluation, decision, trace, or run ID.

No outcome may be marked promotion-eligible without separately audited canonical
paper-entry/return evidence. Bounded fixture replay is retrospective research
only and must say so.

## 5. Increment C — append-only outcomes and replay

Begin only after Sol accepts increment B. Add append-only outcome tables and a
pure replay adapter that uses the same accepted contracts. Persist exactly zero
or one current outcome identity per run/evaluation/horizon without updating an
earlier receipt; corrections append a superseding receipt with explicit lineage.

Reconcile stored run evaluations to outcomes, preserve pending/missing states,
and prove future labels are unreachable from the real-time core import graph.

## 6. Prohibited shortcuts

- no pickle or lossy float serialization;
- no payload-only row accepted without content-hash verification;
- no silent artifact omission;
- no `INSERT OR REPLACE`, mutable latest-row table, cascade delete, or cleanup
  command;
- no current-data lookup during historical replay;
- no treating missing/pending/ambiguous truth as zero, loss, no-trade, or clear;
- no synthetic production-eligible performance claim;
- no active database migration, commit, push, deployment, or external write.

## 7. Requirements addressed

WP003 may provide evidence toward REQ-OUT-001/002/003, REQ-TRACE-001,
REQ-OBS-001, REQ-PERSIST-001, REQ-BT-001, REQ-TEST-001/002, and REQ-DOC-001.
Luna must not mark any requirement PASS or any finding CLOSED.

## 8. Increment-A commands

```powershell
py -m pytest tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
git diff --check
```

Pause for Sol audit after increment A. Do not start outcome implementation.
