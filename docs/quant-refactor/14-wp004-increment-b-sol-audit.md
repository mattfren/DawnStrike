# SOL audit — WP004 increment B discovery metrics

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts the downstream-only, pure per-session and multi-session
discovery-metric reconciliation core for the nine WP004 metrics. It does not
accept miss/metric persistence, an end-of-day runtime mount, chronological
validation, cost stress, strategy validation, promotion evidence, operator UI,
active-database migration, external data acquisition, broker execution,
deployment, or the global mission.

## Accepted scope

- immutable canonical metric names, definitions, horizon selection, matching
  policy, Decimal rounding, and policy identity in
  `intraday_scanner/v2/opportunity/miss_metric_contracts.py`;
- exact strategy-agnostic session/symbol/direction matching against embedded
  `MissReconciliationBatch` bodies in
  `intraday_scanner/v2/opportunity/miss_metric_matching.py`;
- pure per-session and multi-session population reconciliation in
  `intraday_scanner/v2/opportunity/miss_metric_reconciliation.py`;
- the explicit downstream-only public facade in
  `intraday_scanner/v2/opportunity/metrics.py`;
- formula, matching, rank-boundary, false-positive, no-trade, incomplete-truth,
  tamper, mutation-isolation, and import-boundary coverage in
  `tests/test_opportunity_discovery_metrics.py`.

No opportunity or storage package root imports or exports metric modules. No
persistence schema, active database, mounted runtime, UI, network, broker,
scheduler, deployment, commit, push, or requirement-ledger state changed in
this increment.

## Accepted metric definitions

All values retain exact integer numerator and denominator identities as the
primary evidence. Available fractions are projected with a private Decimal
context using precision 64, scale 12, quantizer `0.000000000001`, and
`ROUND_HALF_EVEN`. A known zero numerator with a positive denominator is
`0E-12`; a complete zero denominator is `INSUFFICIENT` with null value; an
incomplete or causally blocked population is `UNAVAILABLE` with null counts,
null value, and exact blocker identities.

The canonical definitions are:

1. `DAILY_OPPORTUNITY_RECALL`: on-time WATCH/TAKE qualified units divided by
   all qualified session/symbol/direction opportunity units.
2. `TOP_1_RECALL`: qualified units with best on-time rank at most 1 divided by
   all qualified units.
3. `TOP_3_RECALL`: qualified units with best on-time rank at most 3 divided by
   all qualified units.
4. `TOP_5_RECALL`: qualified units with best on-time rank at most 5 divided by
   all qualified units.
5. `PRECISION_AT_1`: qualified on-time top-1 units divided by all matched
   on-time top-1 prediction units.
6. `PRECISION_AT_3`: qualified on-time top-3 units divided by all matched
   on-time top-3 prediction units.
7. `PRECISION_AT_5`: qualified on-time top-5 units divided by all matched
   on-time top-5 prediction units.
8. `FALSE_POSITIVE_RATE`: NOT_QUALIFIED units with an on-time WATCH/TAKE divided
   by all conclusively NOT_QUALIFIED units.
9. `NO_TRADE_ACCURACY`: complete sessions with no WATCH/TAKE and no qualified
   opportunity divided by all complete sessions with no WATCH/TAKE anywhere.

`NO_TRADE_ACCURACY` is therefore the precision of complete no-trade claims, not
generic binary accuracy. PENDING, CENSORED, UNAVAILABLE, incomplete-market, and
incomplete-run-inventory evidence never enters a denominator as an assumed
negative.

## Accepted implementation facts

- A metric policy embeds the exact accepted miss-qualification policy and
  selects exactly one horizon definition from the full embedded qualification
  batch. Other horizons remain in lineage and cannot be silently substituted.
- The matching unit is the strategy-agnostic session, symbol, direction,
  qualification-policy, and horizon opportunity key. Multiple strategies and
  repeated intraday runs may contribute prediction evidence to one unit but
  cannot multiply its denominator weight.
- Every selected-horizon assessment is retained. `QUALIFIED` and
  `NOT_QUALIFIED` establish positive and conclusive-negative populations;
  `PENDING`, `CENSORED`, and `UNAVAILABLE` block the affected aggregate. An
  ineligible or absent assessment is not converted into a negative.
- Matching scans every exact evaluation/rank/decision pair in every current
  stored replay. Rank and WATCH/TAKE evidence are retained separately. The
  best on-time rank is the minimum exact accepted rank across matching runs.
- On-time remains strictly `decision_at < latest_useful_cutoff_at`; equality is
  late. A late WATCH/TAKE cannot improve recall, top-K recall, precision, or
  false-positive rate, while it still prevents a session from being called a
  no-trade prediction.
- Unmatched ranked predictions block only the precision metrics whose top-K
  population they could affect. Unmatched on-time WATCH/TAKE predictions block
  false-positive rate. Recall remains independently computable from exact
  qualified units when its population is otherwise complete.
- Both `EXECUTABLE_TRADE` and `PRICE_MOVE_PROXY` qualify for recall, while
  separate claim-kind counts remain visible at report and metric-population
  level. Missing execution proof is never silently upgraded.
- Multi-session results are exact micro-aggregates over unit identities, not
  unweighted averages of session fractions. Duplicate session inputs are
  rejected, and empty cohorts emit explicit `INSUFFICIENT` values.
- Each public session-evidence and report contract embeds its exact policy,
  miss batch, matching evidence, metric definitions, population identities,
  values, limitations, and stable identity. Direct and strict JSON
  construction recomputes the full parent artifact.
- Metric artifacts remain `research_only=True` and
  `promotion_eligible=False`. Mutating valid hindsight evidence can change
  only downstream qualification, miss, metric-evidence, and metric-report
  identities; stored universe, preparation, evaluation, rank, decision,
  trace, run, and replay identities remain unchanged.
- The final implementation graph is downstream-only and bounded: contracts
  are 366 lines/13.6 KB, matching 534 lines/22.2 KB, reconciliation 768
  lines/29.3 KB, and the facade 47 lines/1.5 KB.

## Sol adversarial findings remediated

1. The first focused test matrix defined all nine metrics but did not provide
   the WP004-required proof for precision at 3/5, rank-1/3/5 boundaries and
   ties, surfaced false positives, false no-trade, multiple qualified units,
   mixed nonconclusive truth, or multi-session non-recall formulas. The final
   matrix uses accepted production pipeline, qualification, outcome, and
   replay bodies to cover ranks 1 through 6, deterministic equal-score
   tie-breaking, exact all-nine calculations, long and short units, repeated
   runs, two matching strategies, FPR, no-trade, claim counts, and micro
   aggregation.
2. The initial public facade exported `DiscoveryMetricUnitEvidence`. Sol
   reproduced a consistently reidentified invented prediction with fake run
   and evaluation identities that the standalone child could not disprove
   because it referenced hashes without embedding the source bodies. The child
   is now private and absent from both public `__all__` surfaces. The public
   `DiscoveryMetricSessionEvidence` embeds the exact miss batch and recomputes
   every private prediction/unit; the exact invented-child parent attack now
   rejects.
3. Incomplete-run inventory was initially the only mixed-truth aggregate
   regression. The final matrix also constructs causal production PENDING,
   CENSORED, and UNAVAILABLE qualification evidence and proves each exact
   assessment identity blocks every aggregate rather than being excluded from
   a denominator.
4. Initial tamper proof covered metric values but not the full cohort boundary.
   Final tests reject unknown fields, duplicate JSON keys, Decimal floats,
   session reorder, cohort drift, unit omission, population omission, formula
   drift, status drift, and consistently reidentified parent artifacts.

No production formula or matching defect was reproduced during this audit.

## Execution-history disclosure

One implementation-lane focused command was accidentally launched twice after
its wrapper timed out while the first child survived. The redundant launch was
discarded. The lane also edited the PENDING fixture after Sol had announced
ownership of a full focused gate; Sol invalidated that entire run even though
its only reported failure was fixture construction. Neither run is used as
acceptance evidence.

The invalid run reported 31 passed and one fixture failure after 740.7 seconds.
The failure occurred before metric reconciliation because the provisional
PENDING fixture declared provider historical coverage beyond its decision
time. The accepted capability contract correctly rejected it. The fixture was
rebuilt causally with minute-two scope, artifact, member, capability, and
missing-series evidence; its coverage ends exactly at its capability decision
time. Sol independently reran the PENDING/CENSORED/UNAVAILABLE reproducer:
`3 passed`, exit 0, 34.3 seconds.

Sol then froze the final files and recorded their SHA-256 hashes. The final
focused and combined gates ran without source or test edits. Hashes before and
after were identical; the metric test hash was
`51FC653EDC0C450F263B03790267B6347A01BFC8EF6FA7406847E63A1E61C429`.

## Independent proof

Final focused metric gate:

```powershell
py -m pytest tests/test_opportunity_discovery_metrics.py -q -p no:cacheprovider
```

Result: `32 passed`, exit 0, 743.9 seconds.

Final combined WP004/WP003 opportunity, outcome, persistence, and migration
gate:

```powershell
py -m pytest tests/test_opportunity_discovery_metrics.py tests/test_opportunity_missed.py tests/test_opportunity_outcomes.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py tests/test_opportunity_outcome_persistence.py -q -p no:cacheprovider
```

Result: `507 passed`, exit 0, 1,735 seconds. Independent collection reconciled
exactly as 32 discovery metrics + 46 missed opportunity + 73 outcomes + 23 run
persistence + 5 migration + 9 contracts + 18 features + 57 pipeline + 200
universe/risk + 44 outcome persistence.

Affected SQLite/data-truth gate:

```powershell
py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

Result: `139 passed`, exit 0, 129.4 seconds.

Final static and compile proof passed: whole-repository Ruff; mypy across 280
source files; compileall for `intraday_scanner` and `scripts`; diff-check; and
the fresh-process import firewall. Core opportunity, pipeline, and both storage
adapters loaded zero metric modules; importing the explicit `metrics` facade
then succeeded, and the weak unit child was not public.

## Requirement adjudication

No global requirement or finding is closed. Increment B supplies additive
evidence toward REQ-ARCH-001, REQ-SAFE-001/002, REQ-DATA-002/003/005/006,
REQ-MISS-001/002, REQ-TRACE-001, REQ-TEST-001/002, and REQ-DOC-001.
Acceptance still depends on governed miss/metric persistence and replay if
justified, shared chronological validation, BASE/2X/3X cost stress,
empirical/external-data evidence, disabled-by-default mounted read-only
projection, operator UI, active-database migration, and final clean-worktree
end-to-end proof.
