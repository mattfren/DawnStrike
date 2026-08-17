# Dawnstrike quant-refactor audit ledger

Findings are opened and closed only by Sol. Luna reports implementation
evidence but does not change finding status.

Sol's 2026-08-17 final-repair adjudication closes 13 findings from accepted
software and immutable-gate evidence. FINDING-011 is retained as
`EXTERNAL_DATA_BLOCKED`: the software capability/cost contracts pass, but
consolidated historical entitlement was intentionally unavailable in this
offline run. No closed finding represents empirical-edge or promotion proof.

## FINDING-001

FINDING-ID: FINDING-001  
SEVERITY: BLOCKER  
REQUIREMENT: REQ-DISC-001, REQ-ARCH-001  
STATUS: CLOSED  
DESCRIPTION: Mounted AlphaOps is mover/setup-first rather than market-first.  
EVIDENCE: `alpha_cycle_service.alpha_cycle` collects/enriches mover rows, calls
`ScanService.score_universe`, and only then builds Alpha features/regime/V6.  
WHY_IT_MATTERS: Dawnstrike cannot discover anomalies outside existing source and
setup filters.  
EXPECTED_BEHAVIOR: Strategy-independent OpportunityCandidate generation from
normalized market features precedes strategy classification.  
ACTUAL_BEHAVIOR: Existing formula/setup score defines the candidate set first.  
REMEDIATION_REQUIRED: Build and audit the additive opportunity core and staged
pipeline; integrate behind a disabled feature flag.  
TEST_REQUIRED: Stage-order, no-strategy candidate, and import-boundary tests.  
FILES_LIKELY_AFFECTED: `intraday_scanner/v2/opportunity/*`.  
CLOSURE_CRITERIA: Sol proves candidate discovery occurs before any strategy
evaluator and existing behavior is unchanged with the flag off.

## FINDING-002

FINDING-ID: FINDING-002  
SEVERITY: HIGH  
REQUIREMENT: REQ-FEAT-001 through REQ-FEAT-007  
STATUS: CLOSED  
DESCRIPTION: No canonical normalized FeatureSnapshot exists for opportunity
discovery.  
EVIDENCE: Alpha feature JSON uses raw/bucketed candidate fields; current
cross-sectional z-score/percentile and availability contracts are absent.  
WHY_IT_MATTERS: Anomaly strengths are not comparable across market/time.  
EXPECTED_BEHAVIOR: Causal immutable raw/normalized/unavailable feature state.  
ACTUAL_BEHAVIOR: Fragmented formula, Alpha feature, and v2 indicator outputs.  
REMEDIATION_REQUIRED: Reuse indicators and implement deterministic staged
feature snapshots with capability labels.  
TEST_REQUIRED: Hand-calculated features, insufficient samples, future time, VWAP
proxy, and unsupported order-flow cases.  
FILES_LIKELY_AFFECTED: `intraday_scanner/v2/opportunity/features.py`, models.  
CLOSURE_CRITERIA: All supported features recompute exactly and unsupported truth
remains explicit.

## FINDING-003

FINDING-ID: FINDING-003  
SEVERITY: HIGH  
REQUIREMENT: REQ-REG-001, REQ-REG-002  
STATUS: CLOSED  
DESCRIPTION: Current regime labeling is a lightweight candidate-summary label,
not market and security regimes.  
EVIDENCE: `alpha/regime_detector.py` uses average gap/volume and clean count.  
WHY_IT_MATTERS: Strategy selection cannot adapt to causal market/security state.  
EXPECTED_BEHAVIOR: Typed broader-market and per-security regimes with evidence.  
ACTUAL_BEHAVIOR: Four coarse report labels.  
REMEDIATION_REQUIRED: Implement causal regime engine with insufficient state.  
TEST_REQUIRED: Trend, mean-reversion, volatility, chop, breakout, exhaustion, and
insufficient fixtures.  
FILES_LIKELY_AFFECTED: `intraday_scanner/v2/opportunity/regimes.py`.  
CLOSURE_CRITERIA: Sol independently recomputes representative regimes.

## FINDING-004

FINDING-ID: FINDING-004  
SEVERITY: HIGH  
REQUIREMENT: REQ-STRAT-001 through REQ-LIFE-001  
STATUS: CLOSED  
DESCRIPTION: Strategy identities and lifecycle controls are fragmented and the
required DS ensemble is absent.  
EVIDENCE: Current catalogs use daily research IDs; lifecycle is spread across
`status`, `validation_status`, V6, and PaperOps overlays.  
WHY_IT_MATTERS: Eligibility, validation, and production isolation cannot be
audited through one contract.  
EXPECTED_BEHAVIOR: Unique versioned DS registry and explicit lifecycle state.  
ACTUAL_BEHAVIOR: Multiple partially overlapping conventions.  
REMEDIATION_REQUIRED: Add registry/lifecycle, reuse V5 by adapter, disable
unsupported order-flow families.  
TEST_REQUIRED: Duplicate IDs, transition matrix, family eligibility, V5 parity.  
FILES_LIKELY_AFFECTED: opportunity registry/models.  
CLOSURE_CRITERIA: Every required family is uniquely registered and no
non-production state can TAKE.

## FINDING-005

FINDING-ID: FINDING-005  
SEVERITY: HIGH  
REQUIREMENT: REQ-EVAL-001, REQ-EV-001 through REQ-RANK-002  
STATUS: CLOSED  
DESCRIPTION: Current ranking is primarily by symbol/setup score, not validated
symbol-plus-strategy expectancy.  
EVIDENCE: AlphaOps ranks candidate signals; `v2/scanner/latest.py` sorts cards by
setup score.  
WHY_IT_MATTERS: The best stock is not necessarily the best stock-strategy pair.  
EXPECTED_BEHAVIOR: Evaluate compatible strategies, retain rejections, rank pairs
with evidence and uncertainty.  
ACTUAL_BEHAVIOR: Strategy and symbol ranking paths are not integrated.  
REMEDIATION_REQUIRED: Implement pair evaluation, expectancy DTO, and ranker.  
TEST_REQUIRED: Pair-count, order, tie, uncertainty, and missing-expectancy cases.  
FILES_LIKELY_AFFECTED: opportunity evaluation/expectancy/ranking.  
CLOSURE_CRITERIA: Rankings are deterministic, pair-based, and fully decomposed.

## FINDING-006

FINDING-ID: FINDING-006  
SEVERITY: HIGH  
REQUIREMENT: REQ-GATE-001, REQ-GATE-002, REQ-RISK-001  
STATUS: CLOSED  
DESCRIPTION: No canonical quality gate is separated from ranking; the v2 risk
engine can leave a low reward/risk signal allowed.  
EVIDENCE: `evaluate_signal_risk` adds `reward_risk_below_minimum` but excludes it
from the `allowed` calculation.  
WHY_IT_MATTERS: Rank 1 can be treated as actionable without absolute quality.  
EXPECTED_BEHAVIOR: Separate typed gate with mandatory vetoes and four decisions.  
ACTUAL_BEHAVIOR: Legacy no-trade helps AlphaOps, but no shared pair-level gate
exists.  
REMEDIATION_REQUIRED: Implement pure quality gate; later correct/adapt v2 risk.  
TEST_REQUIRED: Rank-1 PASS, experimental no-TAKE, missing evidence, low R/R.  
FILES_LIKELY_AFFECTED: opportunity quality gate and risk adapters.  
CLOSURE_CRITERIA: Exhaustive decision matrix passes and warning-only bypass is
removed from any mandatory path.

## FINDING-007

FINDING-ID: FINDING-007  
SEVERITY: HIGH  
REQUIREMENT: REQ-OUT-001, REQ-OUT-002  
STATUS: CLOSED  
DESCRIPTION: Outcomes are not canonically captured for every evaluated
symbol-strategy pair and threshold-near-miss.  
EVIDENCE: Existing historical/V6/PaperOps cohorts capture selected and sampled
rows under different contracts.  
WHY_IT_MATTERS: The system cannot learn discovery/gate errors from all decisions.  
EXPECTED_BEHAVIOR: Append-only outcome identity for every evaluated pair.  
ACTUAL_BEHAVIOR: Fragmented cohorts and incomplete coverage.  
REMEDIATION_REQUIRED: Add outcome contracts/persistence after core audit.  
TEST_REQUIRED: Evaluation/outcome reconciliation and leakage tests.  
FILES_LIKELY_AFFECTED: opportunity outcomes, migrations, store.  
CLOSURE_CRITERIA: Run counts reconcile and future labels cannot alter decisions.

## FINDING-008

FINDING-ID: FINDING-008  
SEVERITY: HIGH  
REQUIREMENT: REQ-MISS-001, REQ-MISS-002, REQ-METRIC-001  
STATUS: CLOSED  
DESCRIPTION: Historical daily-review tables contain missed-winner data, but no
active writer exists and the required taxonomy/metrics are absent.  
EVIDENCE: `persist_daily_review` is defined but has no call site; active DB has
30 old runs/139 misses.  
WHY_IT_MATTERS: Dawnstrike cannot currently explain missed great trades through
the live closed loop.  
EXPECTED_BEHAVIOR: First-class post-session miss analysis and recall/precision
metrics.  
ACTUAL_BEHAVIOR: Dormant historical evidence and summary rates only.  
REMEDIATION_REQUIRED: Implement miss engine using stored opportunity traces.  
TEST_REQUIRED: Caught, each miss category, correct no-trade, and metric math.  
FILES_LIKELY_AFFECTED: opportunity missed/metrics, persistence, EOD integration.  
CLOSURE_CRITERIA: Deterministic fixture session yields exact categories and
hand-verified metrics.

## FINDING-009

FINDING-ID: FINDING-009  
SEVERITY: CRITICAL  
REQUIREMENT: REQ-OUT-003, REQ-BT-001  
STATUS: CLOSED  
DESCRIPTION: The committed candidate still lacks independent closure proof for
canonical V5 paper-entry/return truth; a separate worktree contains uncommitted
remediation that is not part of this branch.  
EVIDENCE: Prior independent audit retained exact-one-intent, causal-time,
observation/bar-hash, gap/halt-censor, and benchmark proof gaps.  
WHY_IT_MATTERS: Invalid return truth could contaminate expectancy and promotion.  
EXPECTED_BEHAVIOR: Only authenticated entered-paper receipts with complete
causal evidence become promotion eligible.  
ACTUAL_BEHAVIOR: Candidate evidence-spine tests are prerequisites, not closure.  
REMEDIATION_REQUIRED: Independently re-audit and implement equivalent fixes in
this isolated lane; do not copy dirty files blindly.  
TEST_REQUIRED: Seeded intent matrix, adversarial hashes/times, mounted call-path
tests, DB invariance.  
FILES_LIKELY_AFFECTED: Alpha path/outcome/watcher/evidence-store contracts.  
CLOSURE_CRITERIA: Sol proves the mounted capture-to-label path and all adversarial
mutations fail closed.

## FINDING-010

FINDING-ID: FINDING-010  
SEVERITY: HIGH  
REQUIREMENT: REQ-BT-001 through REQ-BT-006  
STATUS: CLOSED  
DESCRIPTION: Strong validation components exist but are not assembled for the
target market-first DS pipeline, and no legitimate target backtest exists.  
EVIDENCE: Existing v2 strategies are daily/fixture or V5 causal research;
mounted model correctly reports insufficient labels.  
WHY_IT_MATTERS: Software completion cannot be confused with quantitative edge.  
EXPECTED_BEHAVIOR: Shared pipeline chronological validation with costs,
perturbation, locked OOS, and explicit limitations.  
ACTUAL_BEHAVIOR: Component-level evidence only.  
REMEDIATION_REQUIRED: Build harness after core/persistence; remain
EXTERNAL_DATA_BLOCKED where real evidence is absent.  
TEST_REQUIRED: Split/leakage/stress/fragility/metric tests.  
FILES_LIKELY_AFFECTED: opportunity research/backtest/validation.  
CLOSURE_CRITERIA: All architecture/tests pass and any empirical status is honest.

## FINDING-011

FINDING-ID: FINDING-011  
SEVERITY: MEDIUM  
REQUIREMENT: REQ-DATA-002, REQ-FEAT-007, REQ-BT-004  
STATUS: EXTERNAL_DATA_BLOCKED  
DESCRIPTION: Consolidated quote, depth, aggressor, impact, and full historical
coverage are not proven; execution cost remains provisional.  
EVIDENCE: Example Alpaca feed is IEX; provider endpoints exist but entitlement
and retained active evidence are incomplete.  
WHY_IT_MATTERS: Spread, CVD, fills, and profitability could be overstated.  
EXPECTED_BEHAVIOR: Capability-gated unavailable fields and BASE/2X/3X stress.  
ACTUAL_BEHAVIOR: Architecture exists; empirical proof does not.  
REMEDIATION_REQUIRED: Complete boundaries and disabled strategies; document
external data requirements.  
TEST_REQUIRED: Capability and cost-stress tests.  
FILES_LIKELY_AFFECTED: provider receipts, opportunity features/registry.  
CLOSURE_CRITERIA: Unsupported truth is disabled and exact external requirements
are documented; Sol may then mark EXTERNAL_DATA_BLOCKED.

## FINDING-012

FINDING-ID: FINDING-012  
SEVERITY: HIGH  
REQUIREMENT: REQ-TRACE-001, REQ-OBS-001  
STATUS: CLOSED  
DESCRIPTION: Component evidence is strong but end-to-end opportunity decision
trace is fragmented.  
EVIDENCE: Universe, Alpha, V6, strategy, PaperOps, and source records live in
different payloads without one reconstruction contract.  
WHY_IT_MATTERS: A TAKE/WATCH/PASS cannot be explained or reproduced in one path.  
EXPECTED_BEHAVIOR: Machine-readable stage trace with stable identities/hashes.  
ACTUAL_BEHAVIOR: Manual multi-table reconstruction.  
REMEDIATION_REQUIRED: Add DecisionTrace to core and persistence.  
TEST_REQUIRED: reconstruction and tamper tests.  
FILES_LIKELY_AFFECTED: opportunity trace/pipeline/store.  
CLOSURE_CRITERIA: Stored fixture decision reconstructs byte-equivalently.

## FINDING-013

FINDING-ID: FINDING-013  
SEVERITY: MEDIUM  
REQUIREMENT: REQ-UI-001, REQ-UI-002  
STATUS: CLOSED  
DESCRIPTION: Current Streamlit/static products do not show the canonical target
opportunity view.  
EVIDENCE: Mounted tabs and static views focus on picks, calendar, performance,
research, scenarios, and system.  
WHY_IT_MATTERS: The new pipeline would not be operator-visible.  
EXPECTED_BEHAVIOR: Honest read-only best-opportunities projection and no-trade
state.  
ACTUAL_BEHAVIOR: No target read model/UI.  
REMEDIATION_REQUIRED: Integrate only after backend contracts pass.  
TEST_REQUIRED: contract, null, heuristic/validated, and rendered regression.  
FILES_LIKELY_AFFECTED: read model, `app.py`, `web/*`, public build.  
CLOSURE_CRITERIA: Backend/UI payload parity and rendered proof pass.

## FINDING-014

FINDING-ID: FINDING-014  
SEVERITY: MEDIUM  
REQUIREMENT: REQ-TEST-002  
STATUS: CLOSED  
DESCRIPTION: The full no-cache baseline exposed one pre-existing regression;
the narrow repair is proven, and all final gates must be rerun after changes.  
EVIDENCE: Baseline completed with one Streamlit missing-DB failure. The narrow
repair now passes the Streamlit, dashboard-loader, and read-only-store suites:
`13 passed`.  
WHY_IT_MATTERS: Adopted Harvest code is large and uncertified for this mission.  
EXPECTED_BEHAVIOR: Exact final gates pass from a clean worktree.  
ACTUAL_BEHAVIOR: The sole known baseline regression is repaired; final
whole-suite and static gate proof is not yet complete.  
REMEDIATION_REQUIRED: Record baseline result, fix regressions, rerun all gates.  
TEST_REQUIRED: Full named gate set.  
FILES_LIKELY_AFFECTED: tests and any defects found.  
CLOSURE_CRITERIA: All final commands exit zero and evidence is logged.

## WP001 SOL adjudication note — 2026-08-11

WP001 is accepted as an additive research foundation after 67/67 focused tests,
98/98 affected regressions, a 100% whole-suite run, whole-repo Ruff, mypy across
251 source files, compileall, and diff-check proof. See
`docs/quant-refactor/05-wp001-sol-audit.md`.

Findings 001 through 014 remain OPEN. The new package is not mounted, does not
provide universe/capability/risk/persistence/outcome/miss/validation/UI truth,
and does not certify the separate canonical-return lane. WP001 evidence narrows
the remediation path for FINDING-001 through FINDING-007 and FINDING-012; it
does not close their end-to-end acceptance criteria.
