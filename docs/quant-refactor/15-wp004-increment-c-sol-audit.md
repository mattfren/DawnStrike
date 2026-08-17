# SOL audit — WP004 increment C miss and discovery-metric persistence

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts schema-29 append-only persistence, correction lineage,
historical/current read-only replay, and exact compatibility for the accepted
missed-opportunity and discovery-metric artifacts. It does not accept an
end-of-day runtime mount, active-database migration, chronological validation,
BASE/2X/3X cost stress, strategy promotion evidence, read-only operator
projection, UI, external data acquisition, broker execution, deployment, or
the global mission.

## Accepted scope

- migration 29 and compatibility changes in
  `intraday_scanner/storage/migrations.py`,
  `intraday_scanner/storage/opportunity_store.py`,
  `intraday_scanner/storage/opportunity_outcome_schema.py`, and
  `intraday_scanner/storage/opportunity_outcome_store.py`;
- immutable miss persistence receipts and historical/current replay contracts
  in `intraday_scanner/v2/opportunity/miss_persistence.py`;
- exact miss inventory, schema, errors, and adapter modules in
  `intraday_scanner/storage/opportunity_miss_inventory.py`,
  `intraday_scanner/storage/opportunity_miss_schema.py`,
  `intraday_scanner/storage/opportunity_miss_errors.py`, and
  `intraday_scanner/storage/opportunity_miss_store.py`;
- immutable metric persistence receipts, session bindings, and historical/
  current replay contracts in
  `intraday_scanner/v2/opportunity/miss_metric_persistence.py`;
- exact metric inventory, row projection, schema, verification, errors, and
  adapter modules under `intraday_scanner/storage/opportunity_metric_*.py`;
- migration, compatibility, append, replay, correction, idempotency, raw-
  corruption, schema-forgery, rollback, read-only, cache, and import-firewall
  coverage in the focused persistence tests.

The new miss and metric contracts and adapters remain explicit-import-only.
They are not exported from either package root. No mounted runtime, active
database, UI, network, broker, scheduler, deployment, commit, or push path was
used or changed.

## Accepted schema and compatibility facts

- Schema 29 adds exactly five governed tables:
  `opportunity_miss_receipts`, `opportunity_miss_records`,
  `opportunity_miss_run_bindings`, `opportunity_metric_receipts`, and
  `opportunity_metric_session_bindings`.
- All five tables are append-only and guarded by exact BEFORE UPDATE and BEFORE
  DELETE triggers. Root/successor uniqueness and composite foreign keys make
  orphan, fork, and inconsistent parent shapes structurally unrepresentable
  where SQLite can enforce them; whole-chain audits reject schema-valid cycles
  and disconnected histories.
- The miss and metric schema validators require schema 29, foreign-key
  enforcement, clean governed-table foreign-key checks, exact quote-aware SQL
  fingerprints, and exact PK/UNIQUE/FK/index/trigger structures. A same-named
  object with different timing, event, table, body, key, predicate, CHECK
  literal, or ordered columns fails closed.
- Migration 29 is additive. The run store reads live schemas 27, 28, and 29,
  but new schema-29 run rows retain the accepted v2/schema-28 receipt identity.
  The outcome store reads and writes on live schema 29 while retaining the
  accepted v1/schema-28 outcome receipt identity. Historical v1/schema-27 run
  rows and v2/schema-28 run plus v1/schema-28 outcome rows remain byte- and
  identity-stable after upgrade.
- Disposable 27-to-29 and 28-to-29 rehearsals preserve historical result,
  receipt, and timestamp bodies, are repeatable, pass `quick_check`, and prove
  new post-upgrade run and outcome append/replay. No operator database path was
  resolved or touched.

## Accepted miss persistence facts

- The content-bound miss `analysis_key` represents a logical research scope:
  session identity and UTC bounds, membership as-of time, the declared query
  start through session close, ordered requested symbols, authority claim and
  identity/version, inventory source identity/version/method, exact horizon
  bindings, and qualification policy identity/hash. Revision-only fetched,
  observed, or artifact times cannot silently redefine that scope.
- Premarket runs are supported by binding the exact source-receipt
  `query_started_at`; every bound decision remains inside the declared query
  interval. Changing that logical lower bound changes the analysis key.
- Each receipt inventories exactly one reconciliation batch plus every miss
  record and run binding in canonical order. Empty eligible universes are
  allowed only with complete-market scope and complete-authoritative inventory;
  bounded, partial, pending, unavailable, or caller-asserted empty negatives
  cannot establish an authoritative no-opportunity population.
- Initial and correction rows form one append-only chain per analysis key.
  Corrections require the exact current head and strictly increasing persisted
  chronology. Historical replay reconstructs the exact prefix; current replay
  additionally proves that the miss head and every embedded outcome parent are
  still current.
- Exact batch idempotency is checked before current-head enforcement. A later
  retry of an already stored historical artifact returns its original receipt
  and first timestamp without writes; an earlier retry, changed body, changed
  lineage, stale correction, cross-analysis parent, or fork conflicts.
- Read-only construction is inert. Reads use the existing read-only connector,
  require `query_only=1`, reject WAL sidecars, cannot initialize or append, and
  reconstruct canonical JSON, row projections, inventories, chains, and parent
  bodies before returning.

## Accepted metric persistence facts

- Session metric scope is the metric policy identity/hash, exact session
  identity/open/close, and stable parent miss analysis key. Multi-session scope
  is the metric policy identity/hash plus the canonical ordered child SESSION
  metric scope keys; the empty cohort remains explicit.
- The canonical artifact families are
  `SESSION_DISCOVERY_METRIC_REPORT`, `DISCOVERY_METRIC_REPORT`, and
  `METRIC_SESSION_REPORT_BINDING`. SESSION allocation is `(1, 0, 0)` and MULTI
  allocation is `(0, 1, N)`; the nine metric values remain projections inside
  the report rather than mutable child rows.
- The multi-session binding avoids an identity cycle by binding the parent
  report ID/hash and scope, ordinal/session, and exact child metric receipt,
  child scope, child report, and child miss identities/hashes, while the row's
  parent receipt association is supplied by its composite foreign key. Direct
  and stored reconstruction revalidate the exact ordered binding set.
- SESSION append requires the exact current miss replay. MULTI append requires
  exact current SESSION metric replays and their exact current miss parents.
  Historical replay retains the former bodies; current replay fails with a
  typed stale-parent error after any relevant parent advances.
- Exact report idempotency precedes current-head checks. A byte-identical retry
  after an upstream correction is a no-write return of the original receipt;
  changed parent or binding lineage with the same report identity conflicts.
  Corrections require the exact current metric head and strict chronology.
- Every read runs inside one explicit SQLite snapshot. Every append uses one
  `BEGIN IMMEDIATE` transaction with plain INSERTs and discards its pre-insert
  verification context before post-insert whole-chain verification. No UPDATE,
  DELETE, UPSERT, REPLACE, mutable latest table, public SQL hook, global cache,
  or cross-transaction cache is present.
- `_MetricVerificationContext` is private, connection-local, and transaction-
  local. It memoizes only fully verified historical chains/bodies and separately
  proven current heads. Per-family in-progress sentinels reject recursion and
  are cleared in `finally`; failed values never enter completed caches.

## Sol adversarial findings remediated

1. The first schema design did not bind the requested symbol cohort into the
   miss analysis key. The final key binds exact ordered requested symbols and
   their logical scope while excluding revision-only fetch timing.
2. Initial migration-29 DDL omitted five relied-on structural rules. The final
   DDL binds the miss analysis-key parent UNIQUE/composite child FK, `CAUGHT`
   iff category is null, the metric child's exact receipt/report/scope parent
   FK plus non-self rule, session open before close, and membership/query start
   at or before session open.
3. The initial logical query lower bound was derived too late for premarket
   runs. It now comes exactly from the content-bound inventory-source receipt.
4. Initial current-head logic could reject an exact historical idempotent retry,
   and read-only proof did not establish query-only/WAL behavior. Idempotency
   now precedes head checks, while read-only tests prove connector, query-only,
   absent/stale schema, write refusal, and WAL refusal.
5. Empty-inventory authority initially depended too heavily on DTO validation.
   Exact DDL projections now require zero requested symbols and the empty flag
   together with complete-market source scope and complete-authoritative run
   inventory.
6. Three early miss raw-tamper tests removed append-only guards and then failed
   at schema validation, so they did not prove row reconciliation. The final
   tests restore canonical guard SQL, independently validate schema, and then
   reject eleven payload/inventory mutations plus schema-valid cycle, orphan,
   fork, extra-record, and extra-binding attacks.
7. Metric multi-session persistence initially faced a parent-receipt identity
   cycle and under-bound child scope. The final binding excludes the cyclic
   parent receipt identity but includes exact parent report/scope and child
   receipt/hash/scope/report/miss lineage, with matching composite FK proof.
8. Initial metric idempotent and public-boundary paths did not compare every
   stored parent/binding projection or sanitize every lookup/request shape.
   Exact projection comparisons and typed public validation now precede database
   access or return.
9. A real typed-boundary defect allowed an old MULTI current replay to survive a
   corrected child SESSION head. The exact current child receipt/hash/scope check
   now raises `OpportunityMetricStaleParentError`.
10. The first metric replay recursively re-audited the same metric, miss,
    outcome, and run histories, making the correction fixture superlinear. The
    operation-local verification context reduces each unique metric scope,
    metric receipt, miss analysis, and exact current parent to one full
    computation per SQLite snapshot without weakening historical/current
    separation.
11. Replay contracts initially allowed nonincreasing report chronology in a
    correction-shaped direct construction, and read lookup validation was
    incomplete. Direct and strict JSON paths now reject equal/earlier report
    chronology and malformed/private/secret identifiers before connecting.
12. The first final metric suite lacked the same schema-valid raw-corruption,
    exact FK/UNIQUE, cycle, rollback, WAL, and post-parent-advance idempotency
    matrix already required of miss persistence. Those attacks now exist. Nine
    intended DDL-rejection fixtures initially failed afterward because SQLite
    did not roll back a dropped trigger; canonical trigger restoration moved to
    same-connection `try/finally`, with rollback/table-equality/schema proof.
13. Normal mypy did not inspect one untyped cursor body. Explicit
    `--check-untyped-defs` found and closed the `str` versus `str | None`
    annotation defect; both mypy modes pass on the final metric sources.

No unresolved production correctness defect was reproduced after these fixes.

## Performance and module-boundary disclosure

The accepted performance fixture constructs initial and corrected miss,
outcome, SESSION metric, and MULTI metric histories. The final measured current
replay completed inside its 600-second bound and recorded exactly two metric-
scope computations, four metric-receipt verifications, one miss-analysis audit,
and one exact current-miss-parent verification. Full fixture construction is
still expensive; the final focused metric file took 4,183.5 seconds and the
full combined gate took 7,109.3 seconds. This is research replay, not a mounted
real-time latency claim.

The metric adapter is 916 physical lines and 37,018 bytes. It is below the
controlling 40 KB/no-god-module boundary and delegates schemas, inventories,
row projections, cache state, and errors to one-way helper modules, but it is
16 lines above the lane's earlier 900-line soft target. Sol accepts that narrow
exception for this frozen increment; the adapter must not grow during the next
work package without a behavior-preserving ownership split.

## Independent proof

Final focused metric persistence gate:

```powershell
py -m pytest tests/test_opportunity_metric_persistence.py -q -p no:cacheprovider
```

Result: `53 passed`, exit 0, 4,183.5 seconds. The final miss persistence file
contains 34 passing tests; migration has 7; discovery metrics 32; missed
opportunities 46; outcomes 73; outcome persistence 45; run persistence 23;
contracts 9; features 18; pipeline 57; universe/risk 200.

Final combined WP004/WP003 opportunity, metric, miss, outcome, persistence, and
migration gate:

```powershell
py -m pytest tests/test_opportunity_metric_persistence.py tests/test_opportunity_discovery_metrics.py tests/test_opportunity_miss_persistence.py tests/test_opportunity_missed.py tests/test_opportunity_outcomes.py tests/test_opportunity_outcome_persistence.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Result: `597 passed`, exit 0, 7,109.3 seconds. Collection reconciled exactly as
53 + 32 + 34 + 46 + 73 + 45 + 23 + 7 + 9 + 18 + 57 + 200.

Affected SQLite/data-truth gate:

```powershell
py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

Result: `139 passed`, exit 0, 136.9 seconds.

Final whole-repository Ruff passed; mypy reported no issues in 292 source
files; focused normal mypy and `--check-untyped-defs` both passed on the seven
metric source modules; compileall for `intraday_scanner` and `scripts` passed;
and `git diff --check` passed. Only pre-existing shared-worktree line-ending
notices were emitted during status inspection.

Sol independently scanned the final import graph in a fresh process. Importing
the opportunity root, pipeline, storage root, run store, and outcome store
loaded zero miss or metric persistence modules. An AST scan found no app,
runtime, UI, network, broker, or scheduler dependency in the downstream miss or
metric modules. Final source hashes were captured after the frozen gates; the
metric adapter SHA-256 is
`5F6384E39422A223308CFE469AB8562D99C5841DB543FE6C8E6613D75A52738A`.

## Requirement adjudication

No global requirement or finding is closed. Increment C supplies additive
evidence toward REQ-ARCH-001, REQ-SAFE-001/002, REQ-DATA-002/003/005/006,
REQ-OUT-001/002/003, REQ-MISS-001/002, REQ-METRIC-001, REQ-TRACE-001,
REQ-PERSIST-001, REQ-TEST-001/002, and REQ-DOC-001. Acceptance still depends on
the shared chronological validation harness, purge/embargo and locked OOS
discipline, BASE/2X/3X cost stress, trading and segmented metrics, perturbation
and stability evidence, empirical/external-data evidence, disabled-by-default
mounted read-only projection, operator UI, active-database migration, and final
clean-worktree end-to-end proof.
