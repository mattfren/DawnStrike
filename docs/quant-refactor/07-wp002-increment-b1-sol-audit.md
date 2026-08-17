# SOL audit — WP002 increment B1 execution-risk evidence

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts only the immutable execution-risk evidence and pure builder
slice. It does not accept TradeDecision changes, the absolute quality gate,
all-pair reconciliation, pipeline integration, persistence, mounted behavior,
empirical validity, or the global mission.

## Accepted scope

- new `intraday_scanner/v2/opportunity/risk.py`;
- public opportunity-package exports for the B1 contracts/builders;
- focused B1 additions in `tests/test_opportunity_universe_risk.py`.

No decision, gate, ranking, pipeline, persistence, UI, network, broker,
scheduler, database, deployment, commit, or push behavior changed.

## Accepted implementation facts

- `RiskNumericEvidence` separates explicit units, value status,
  capability/availability state, and empirical-versus-heuristic lineage.
- Base and derived provenance roles are mutually exclusive and content-bound;
  unavailable derived values retain exact causal input IDs and missing states.
- `ExecutionRiskEvidence` embeds and revalidates the exact
  `StrategyEvaluation`, provider capability receipts, numeric inputs, safety
  evidence, account identity, risk-cap identity, and concentration identity.
- Entry, stop, and target must match the evaluation; account/risk/concentration
  numeric sources must match their bound identities.
- Halt and corporate-action status use sourced, timestamped
  `RiskSafetyEvidence`; provider capability is never inferred to mean CLEAR.
- Observed NBBO requires an exact quote-capability receipt with quotes and
  consolidated/NBBO available. Observed nonconsolidated spread remains visible
  but carries a missing-NBBO veto. Provisional/unavailable quote scopes cannot
  claim a quote receipt.
- Quote age is derived exactly from the spread timestamp using Decimal
  days/seconds/microseconds arithmetic.
- Exact Decimal formulas distinguish gross geometry, per-share cost, total
  round-trip cost, planned loss, risk cap, and after-cost reward/risk.
- Provisional inputs propagate heuristic lineage to all affected derived
  values.
- Missing account, sizing, cost, quote, concentration, safety, or threshold
  truth remains null with an explicit state, reason, and veto; it is never
  replaced by zero.
- Directional geometry, staleness, unknown/blocked safety state, planned-loss
  cap, concentration cap, and minimum after-cost R fail closed. Equality at
  declared cap/threshold boundaries remains eligible at the risk-evidence
  layer.
- Direct construction and JSON round-trip revalidate formula, lineage,
  timestamp, unit, source, identity, and veto coherence.
- B1 does not copy legacy v2 warning-only reward/risk behavior or V5 policy
  rules, and it does not use float-based Alpha risk formulas for exact receipt
  arithmetic.

## Sol adversarial findings remediated

1. numeric capability/availability state omitted from the initial contract;
2. caller-supplied quote age rather than timestamp-derived staleness;
3. nonconsolidated spread could satisfy execution proof without an NBBO veto;
4. optional reasons were accidentally mandatory for all available numerics;
5. provisional/unavailable quote scopes could retain unrelated receipt IDs;
6. bare safety flags incorrectly treated provider capability as status truth;
7. direct payloads could bypass formula and provenance checks;
8. float conversion in quote-age arithmetic;
9. account, risk-cap, and concentration identities were not bound to numeric
   sources;
10. unavailable base numerics could claim fake derived inputs;
11. derived numerics could masquerade as observed/provisional evidence;
12. BOTH direction could lose its veto when geometry was also unavailable.

## Independent proof

Sol independently ran:

```powershell
py -m pytest tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Result: `228 passed`, exit 0.

```powershell
py -m pytest tests/test_alpha_risk_geometry.py tests/test_alpha_tail_risk_controls.py -q -p no:cacheprovider
```

Result: `4 passed`, exit 0.

```powershell
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
git diff --check
```

Results: Ruff exit 0; mypy `254 source files`, exit 0; compileall exit 0;
diff-check exit 0 with inherited LF/CRLF warnings only.

## Requirement adjudication

No global requirement or finding is closed. B1 supplies additive evidence toward
REQ-SAFE-001/002, REQ-DATA-002/006, REQ-GATE-001/002, REQ-RISK-001/002,
REQ-TRACE-001, and REQ-TEST-001. Complete acceptance still depends on the
decision/gate contract, every-pair reconciliation, universe-authoritative
pipeline integration, persistence, validation, mounted read-only behavior, and
final end-to-end proof.
