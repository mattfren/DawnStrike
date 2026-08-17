# SOL audit — WP001 market-first domain and deterministic pipeline core

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts the package as the implementation base for later work. It
does not certify Dawnstrike as complete, production-ready, mounted, validated,
or empirically profitable. No global requirement or finding is closed solely by
this unmounted package.

## Scope and authorship

Luna added only:

- `intraday_scanner/v2/opportunity/` (10 additive modules);
- `tests/test_opportunity_contracts.py`;
- `tests/test_opportunity_features.py`;
- `tests/test_opportunity_pipeline.py`.

Sol separately owns the control documents, the missing-SQLite dashboard baseline
repair in `app.py`/`tests/test_streamlit_app.py`, and two nonsemantic inherited
Ruff line wraps in `tests/test_paper_ops_trade_blotter.py`.

No migration, database operation, CLI change, scheduler change, UI mount,
network provider, deployment, commit, push, or live/broker action occurred.

## Accepted implementation facts

- Frozen typed contracts preserve unavailable/insufficient/unsupported truth.
- Causal OHLCV features reject naive, future, duplicate, non-monotonic,
  non-positive, and non-finite inputs.
- Gap is session-open versus prior-session-close, not an intraday bar gap.
- VWAP is explicitly labeled an OHLCV typical-price proxy.
- Benchmark-relative features require exact timestamp alignment.
- True CVD/aggressor imbalance are unsupported; DS-OF-001/002 are disabled.
- Cheap strategy-independent discovery precedes candidate-only rich features.
- Market and security regimes are separate heuristic receipts.
- Seven required non-order-flow DS families are experimental and each has
  eligible, rejected, and insufficient behavioral fixtures.
- Strategy thresholds are immutable metadata consumed by evaluators.
- Evaluations bind strategy definition, evaluator ID, and evaluator/helper
  behavior hash.
- Expectancy uses exact observed breakeven semantics rather than treating zero R
  as an average loss.
- Global symbol-strategy ranking is deterministic, retains component breakdowns,
  and emits no decision.
- The absolute gate is separate, validates directional geometry, and cannot TAKE
  from an experimental lifecycle.
- Benchmark and global rank inputs are retained in reconstructible pipeline
  receipts.
- Later outcome mutation cannot alter the original decision/trace identity.
- Real-time opportunity modules do not consume outcome/missed-opportunity types.

## Sol adversarial findings remediated inside WP001

1. mixed feature tuple mypy widening;
2. regime measurements observed after decision time;
3. expectancy availability/value incoherence;
4. non-finite outcome/anomaly/config values;
5. rank arithmetic and malformed TAKE receipts;
6. session-gap and configurable acceleration math;
7. observed-zero volatility truth;
8. zero-R expectancy probability math;
9. candidate/snapshot and rank/evaluation identity mismatches;
10. directionally invalid reward/risk geometry;
11. explicit-empty-universe expansion to all symbols;
12. missing benchmark input and understated global ranking trace inputs;
13. malformed expectancy mappings silently discarded;
14. incomplete strategy-threshold metadata and evaluator identity binding;
15. evaluator hashes omitting shared behavior-affecting helpers;
16. invalid zero/minimum window and discovery configuration bounds.

## Independent proof

Sol independently ran:

```powershell
py -m pytest tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py -q -p no:cacheprovider
```

Result: `67 passed`, exit 0.

```powershell
py -m pytest tests/test_alpha_v2_indicators.py tests/test_v2_strategy_catalog_expansion.py tests/test_v2_data_truth_paper_ops.py tests/test_alpha_risk_geometry.py tests/test_alpha_tail_risk_controls.py -q -p no:cacheprovider
```

Result: `98 passed`, exit 0.

```powershell
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
git diff --check
```

Results: Ruff exit 0; mypy `251 source files` exit 0; compileall exit 0;
diff-check exit 0.

The first post-WP001 whole-repository suite reached 100% with exit 0 in 914.4
seconds. It predates only final package-local threshold/evaluator/config
hardening. The focused, affected-regression, and static gates were rerun after
that hardening. A final full suite remains mandatory on the later integrated
candidate.

## Deliberately open gaps

- mounted AlphaOps remains setup/mover-first;
- no point-in-time universe membership/admission/exclusion receipt;
- no consolidated quote, spread, fee, slippage, halt, action, sizing, or account
  risk receipt;
- no immutable lifecycle-transition receipt;
- rejected/insufficient/disabled evaluations have no final TradeDecision;
- no persistent append-only opportunity/evaluation/decision/trace store;
- no all-evaluation outcome capture or ambiguous path labeler;
- no active missed-opportunity engine and required metric suite;
- no chronological walk-forward/locked-OOS/stress/overfit validation harness;
- no empirical edge or profitability claim;
- no read-only operator projection or mounted feature flag;
- no production deployment or runtime verification;
- canonical V5 return-truth remediation remains quarantined in a separate dirty
  worktree and is not imported or certified here.

## Requirement adjudication

WP001 supplies implementation evidence for the IDs listed in its handoff, but
the requirements ledger remains `OPEN` because its acceptance criteria apply to
the complete system, not an unmounted package. WP002 is authorized to address
universe, capability/risk, lifecycle-transition, and all-pair disposition gaps.

