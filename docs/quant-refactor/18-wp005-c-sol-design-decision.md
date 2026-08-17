# WP005-C Sol Design Decision

Date: 2026-08-15
Status: `CAPSULE_READY`

This is the single bounded decision pass authorized after WP005-B acceptance.
It resolves the remaining WP005-C contract choices without using observed
confirmatory results to tune thresholds.

## Confirmatory unit and endpoint

- The indivisible confirmatory unit is exact
  `(strategy_id, strategy_version, direction)`.
- Long, short, strategy versions, or strategy IDs must never be pooled to
  manufacture sample size or stability.
- The primary confirmatory endpoint is mean after-cost session R under the
  accepted `COST_3X` execution-stress scenario.
- A session is the clustering unit. Multiple trades/evaluations in one session
  are first reconciled to one session-R observation under the already-accepted
  metric policy; they are not treated as independent observations.
- BASE and COST_2X are disclosures and monotonicity evidence, not substitutes
  for a failed or unavailable COST_3X endpoint.

## Frozen calibration provenance

- Every sample, interval, plateau, control, baseline, regime, complexity, and
  veto threshold must come from one immutable calibration policy.
- The policy embeds its declaration time, source artifact identities/hashes,
  calibration region identity, trial count, bootstrap seed/resample count,
  exact threshold values, and limitations.
- Its declaration time must precede the first confirmatory decision/session.
- Locked OOS may never be a calibration source and may never be used to tune or
  repair the policy.
- Missing, late, inconsistent, or unverifiable calibration provenance fails
  closed.

## Deterministic confidence interval

- The confidence interval is a deterministic session-clustered bootstrap of
  the COST_3X mean session R for one confirmatory unit.
- Sampling is with replacement over whole session clusters using the policy's
  fixed seed and predeclared resample count.
- Decimal conversion, summation, mean, quantile selection, and final
  quantization execute inside the accepted explicit Decimal context; ambient
  Decimal state cannot change the result.
- Too few sessions, non-finite values, an incomplete common population, or an
  invalid bootstrap policy yields explicit unavailable/insufficient evidence,
  never zero and never a promotional result.

## Common-population and causal-control rules

- Population eligibility is declared before arm evaluation from accepted
  validation and outcome evidence.
- Every applicable arm must account for the exact same ordered population
  identities. A missing, injected, duplicated, reordered, or differently
  filtered row is invalid evidence.
- Post-hoc row intersection or filtering is prohibited, even when it would
  make all arms complete.
- A control is either applicable and evaluable on the full population, or it
  carries an exact non-applicability/blocked reason. Applicable but missing
  causal evidence emits `VETO_MISSING_EVIDENCE`.
- Required causal perturbation arms recompute through the same accepted
  opportunity pipeline. They may alter only the predeclared decision-time
  parameter/input under test and must preserve source/outcome identity.
- Future-data sentinels are leakage checks only. They can invalidate evidence
  if future mutation changes a decision-time result, but they are never
  statistical comparison arms and never contribute observations.

## Required controls and disclosures

- Parameter perturbation/plateau analysis for every applicable tunable
  parameter, with explicit non-applicability for genuinely parameter-free
  units.
- At least one causal negative/placebo control, predeclared and evaluated on
  the exact common population.
- Cross-regime and temporal stability using predeclared applicable buckets;
  unavailable buckets remain explicit.
- A simple deterministic baseline on the exact common population.
- Trial-count/multiple-hypothesis disclosure and a predeclared trial limit.
- Complexity disclosure with deterministic feature/parameter/rule counts and
  predeclared limits.

## Verdict semantics

- Any proven fragility, failed required check, policy/provenance defect,
  population mismatch, or adverse control result yields an explicit veto.
- Missing required causal evidence yields exactly
  `VETO_MISSING_EVIDENCE`.
- When every applicable check is complete and no veto fires, the only positive
  state is `NO_CONTROL_VETO`.
- `NO_CONTROL_VETO` is non-promotional. It does not mean validated, approved,
  production-eligible, profitable, or lifecycle-promoted.
- WP005-C cannot mutate strategy lifecycle state and cannot emit a TAKE/live
  authorization.

## Scope and module boundary

- Implement additive downstream-only robustness contracts/builders under
  `intraday_scanner/v2/opportunity/validation_robustness*.py` (split further if
  needed for maintainability).
- Add focused tests in
  `tests/test_opportunity_validation_robustness.py`.
- The new modules may depend on accepted opportunity validation/metric/outcome
  contracts. Real-time discovery, features, regimes, registry evaluation,
  ranking, risk, gate, and pipeline modules must not import them.
- No persistence, mounted runtime, UI, active database, network/provider,
  broker, scheduler, deployment, commit, stage, or push action is in scope.

## Acceptance gate

- Focused direct/from-JSON, formula, determinism, adversarial population,
  control, sentinel, threshold-provenance, and veto-semantics tests pass.
- The accepted WP005-B 656-test gate, the 139-test affected regression gate,
  Ruff, full opportunity mypy/import checks, compileall, and diff-check remain
  green.
- A repository-durable evidence packet binds commands, counts, logs, exits,
  times, source hashes, modifications, and repair cycles for Sol adjudication.
