# Luna WP005-C handoff — overfitting and stability controls

Date: 2026-08-15
Terminal material state: `PASS_CANDIDATE_FOR_SOL_ADJUDICATION`

This handoff reports implementation and reproduced evidence only. It does not
self-accept WP005-C, close a requirement or finding, claim profitability, use
locked OOS for tuning, or authorize promotion, TAKE, production, or live
execution.

## Frozen source and scope

- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- HEAD: `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- Accepted WP005-A/B semantics and source files were not edited.
- Added only six downstream robustness modules, one focused test file, this
  handoff, the existing Luna capsule update, execution-log evidence, and the
  repository-durable evidence packet.
- No active database, persistence contract, mounted runtime, UI, network,
  provider, broker, scheduler, deployment, branch, commit, stage, or push
  action occurred.

## Contracts and decisions

- `ConfirmatoryUnit` is exactly `(strategy_id, strategy_version, direction)`.
- `RobustnessCalibrationPolicy` is content-addressed and binds the exact
  confirmatory unit, fixed final-validation eligibility rule, UTC declaration
  time, non-locked calibration region, source artifact identities and SHA-256
  hashes, trial count/limit, bootstrap seed/resamples/quantiles, every numeric
  threshold, exact perturbation values, negative-control names, simple
  baseline name, regime buckets, complexity limits, and limitations.
- `ConfirmatoryPopulation` recomputes from the accepted
  `ValidationTradingMetricReport` final-validation scope. Every declared
  session must contain the exact unit's complete ordered row inventory.
  Missing, injected, duplicated, reordered, differently filtered, or
  incomplete COST_3X evidence rejects; there is no row intersection repair.
- `CausalControlArm` binds the exact population inventory, the canonical
  `run_opportunity_pipeline` entrypoint, causal-recomputation assertion,
  preserved source/outcome identity, and content-hashed output artifacts.
  Parameter perturbations and negative controls cannot become available
  evidence without that binding. Parameter-free units require explicit
  `NOT_APPLICABLE` evidence.
- `RegimeStabilityEvidence`, `ComplexityEvidence`, and
  `FutureDataSentinelEvidence` bind the same population. Future-data
  sentinels retain zero statistical observations and are not a control-arm
  kind.
- `ValidationRobustnessReport` embeds and recomputes the accepted population,
  frozen policy, interval, controls, checks, limitations, and verdict. Missing
  required evidence has global priority and yields exactly
  `VETO_MISSING_EVIDENCE`. Complete non-veto evidence yields exactly
  `NO_CONTROL_VETO`, which is explicitly non-promotional.

## Exact formulas

- Session endpoint: for each declared session and exact confirmatory unit,
  `session_R = sum(COST_3X after_cost_r_unquantized)` over complete resolved
  fills. Exact no-fill and non-TAKE rows contribute no fill return while
  remaining in the source inventory. Incomplete or unavailable COST_3X truth
  rejects the population.
- Primary endpoint: `mean_session_R = sum(session_R) / session_count`.
- Confidence interval: whole session clusters are sampled with replacement.
  Draw index is `SHA256(seed:resample_ordinal:draw_ordinal)[0:8] mod n`.
  Lower index is `floor(q_low * (B - 1))`; upper index is
  `ceil(q_high * (B - 1))`. Mean, sampling sums, quantiles, and 12-place final
  quantization run in the accepted fresh 64-digit ROUND_HALF_EVEN context.
- Plateau: `max(base_mean - perturbation_mean) <= configured degradation` for
  both predeclared lower and upper values of every tunable parameter.
- Negative control: `max(control_mean) <= configured placebo ceiling`.
- Simple baseline: `base_mean - baseline_mean >= configured minimum excess`.
- Regime stability: every predeclared bucket is present, each bucket meets its
  session minimum, and `max(bucket_mean) - min(bucket_mean)` is within the
  configured spread.
- Trial and complexity controls require `trial_count <= trial_limit` and each
  deterministic feature/parameter/rule inventory count within its own limit.

## Durable verification

Evidence root:
`docs/quant-refactor/evidence/wp005-c-20260815/`

- Focused collection reconciled `19`; the exact focused gate passed `19/19`
  with exit `0` in `391.892` seconds.
- The exact accepted WP005-B main command and file order reconciled `656` and
  passed `656/656` with exit `0` in `9025.675` seconds.
- The exact accepted affected command reconciled `139` and passed `139/139`
  with exit `0` in `114.370` seconds.
- Whole-repository Ruff, `mypy intraday_scanner` (`310` source files),
  compileall, and diff-check exited `0`.
- The fresh-process/AST firewall imported `15` core/runtime/storage/UI paths,
  scanned `6` robustness modules, found `0` eager robustness modules, `0`
  forbidden dependencies, and `0` package-root exports.
- Seven implementation/test hashes matched before and after the gate. No gate
  process survived the final process check.
- Gate window: `2026-08-15T21:11:54.0464032Z` through
  `2026-08-15T23:53:12.2583351Z`; total `9678.212` seconds.

## Repairs and limitations

- Implementation repair cycle 1 corrected the strict-boundary negative test
  to assert the actual timezone-aware rejection without weakening the
  contract.
- Implementation repair cycle 2 bound the exact confirmatory unit and fixed
  population-eligibility rule into calibration provenance and predeclared the
  exact negative-control and baseline identities.
- Six evidence-orchestration attempts occurred: two collection-count parser
  repairs, one deliberately aborted pre-final broad run for repair cycle 2,
  one post-broad Ruff discovery in the evidence-only firewall script, one
  evidence-script import-path repair, and the final successful continuation.
  The final focused/main/affected gates were not repaired or selectively
  rerun after a test failure; all three final recorded gates exited `0` on
  frozen source.
- The causal layer is a pure verifier of content-bound recomputation evidence;
  it does not itself schedule or execute research reruns. Artifact hashes and
  the canonical pipeline entrypoint must therefore be independently audited
  against the actual recomputation packet before any empirical claim.
- The accepted metric layer excludes locked OOS pending a later governed
  one-time-consumption adapter. WP005-C therefore proves validation-layer
  control invariants and does not establish a fresh locked-OOS result or close
  REQ-ML-001 for an ML family.
- Synthetic tests prove formulas, causality boundaries, serialization,
  adversarial population rejection, and veto semantics only; they are not
  evidence of edge, profitability, or strategy validity.

Requirement evidence addressed, without closure: `REQ-EV-002`, `REQ-BT-005`,
and applicable portions of `REQ-ML-001`, `REQ-TEST-001`, and `REQ-DOC-001`.
