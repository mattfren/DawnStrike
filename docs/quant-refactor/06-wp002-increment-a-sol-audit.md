# SOL audit — WP002 increment A universe, capability, and lifecycle truth

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This accepts only WP002 increment A. It does not accept execution-risk evidence,
all-pair disposition, pipeline integration, persistence, mounted AlphaOps
behavior, empirical validity, or the global mission.

## Accepted scope

- immutable, content-bound `ProviderCapabilityReceipt`;
- versioned `UniversePolicy`, point-in-time `UniverseMemberFact` and
  `UniverseMember`, and bounded `UniverseSnapshot`;
- exact market-dataset content identity over in-memory bars and declared
  lineage;
- immutable `StrategyLifecycleTransition` plus a receipt-returning transition
  API;
- public opportunity-package exports and focused increment-A tests.

No risk, quality-gate, ranking, pipeline, persistence, UI, network, broker,
scheduler, database, or deployment behavior was changed in this increment.

## Accepted implementation facts

- Capability states are explicit `AVAILABLE`, `UNAVAILABLE`, `UNSUPPORTED`, or
  `UNKNOWN`; IEX-like evidence cannot claim consolidated SIP/NBBO and OHLCV
  cannot establish aggressor classification.
- Capability bounds, member facts, dataset bars, and snapshot membership are
  causal relative to their observation/as-of/decision times.
- Available member data must bind a referenced capability receipt whose
  historical coverage contains the exact dataset interval.
- Secret-shaped values, credential URLs, private paths, and private hosts are
  rejected from sanitized capability fields.
- Default policy admits common stock only; ETF and ADR require explicit opt-in;
  unknown metadata and disallowed security types fail closed.
- Informational reasons cannot silently become exclusions, while declared
  exclusions remain binding.
- Every requested trade symbol is included or excluded exactly once; benchmark
  membership remains separate; explicit empty input remains empty.
- Counts, symbols, receipt references, member times, and content identities
  reconcile, including after deterministic JSON round trip.
- Dataset keys with empty bars cannot establish availability; future,
  mismatched-symbol, duplicate, or non-monotonic bars reject.
- Lifecycle graph rules live in the immutable contract as well as the builder,
  so direct construction/deserialization cannot bypass them.
- All 81 lifecycle state pairs are tested against the declared graph, and every
  promotion/reactivation edge rejects automated execution.

## Sol adversarial findings remediated

1. capability observation later than universe as-of;
2. historical coverage later than receipt observation;
3. narrow exact-string IEX guard;
4. incomplete private/secret-value sanitization;
5. available member data without capability evidence or actual bar coverage;
6. member evidence timestamps later than the member fact;
7. future, empty, mismatched, duplicate, and non-monotonic dataset bars;
8. normalized symbol duplicates;
9. provenance reason codes incorrectly forcing exclusion;
10. manually constructed members/snapshots bypassing builder-only invariants;
11. arbitrary empirical labeling without typed validation evidence;
12. noncanonical receipt ordering and blank/duplicate metadata;
13. direct lifecycle payloads bypassing transition/evidence/actor rules;
14. automated reactivation from `DISABLED`;
15. incomplete lifecycle matrix coverage.

## Independent proof

Sol independently ran:

```powershell
py -m pytest tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Result: `192 passed`, exit 0.

```powershell
py -m ruff check intraday_scanner/v2/opportunity tests/test_opportunity_universe_risk.py
py -m mypy intraday_scanner/v2/opportunity
```

Results: Ruff exit 0; mypy `12 source files`, exit 0.

Luna separately reported package compileall exit 0. Later WP002 increments and
the final integrated candidate still require their own focused, affected, full,
static, and boundary proof.

## Requirement adjudication

No global requirement or finding is closed. This increment supplies additive
evidence toward REQ-DATA-001/002/006, REQ-SAFE-002, REQ-LIFE-001,
REQ-TRACE-001, and REQ-TEST-001, but complete acceptance remains dependent on
risk truth, every-pair dispositions, pipeline integration, persistence,
validation, mounted read-only behavior, and final end-to-end proof.
