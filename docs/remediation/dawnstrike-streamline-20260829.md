# Dawnstrike streamlining and Monday-readiness ledger

Base SHA: `1e0829b0dce50803e6ad7f547b06937051a7af84`

This ledger is the acceptance contract for the 2026-08-29 remediation. It does
not authorize broker execution, automatic strategy promotion, fabricated
market evidence, or rewriting historical receipts.

## Non-negotiable invariants

- Research-only remains true and broker/live execution remains false.
- Missing bars, benchmarks, fills, positions, equity, and returns stay missing;
  they are never coerced to zero.
- Every release, artifact, evaluation, and deployment is bound to an exact Git
  SHA and immutable input/output hashes.
- Champion behavior changes only after prospective shadow evidence clears the
  existing manual promotion gates.
- The live scheduler worktree and active SQLite state are not implementation
  sandboxes.

## Cycle 1 correctness and reliability

- [x] Source data quality and outcome-conditioned alpha reliability are
  separated; zero-outcome sources cannot alter ranking.
- [x] Recoverable outcome gaps are distinct from authoritative terminal
  invalidity, with append-only retry/supersession lineage.
- [x] V6 loaders cannot silently freeze on the oldest 50,000 rows.
- [x] Promotion-grade risk metrics aggregate deterministic portfolio returns by
  market date rather than treating candidate rows as daily observations.
- [x] Untouched holdout and V5 comparison receipts are hash-bound into manual
  promotion readiness; missing or mismatched evidence remains blocked.
- [x] Calibration display is sample-qualified and bound to the exact model run.
- [x] Drift detection uses frozen reference/recent windows and covers source,
  missingness, feature, score, calibration, liquidity, cost, and outcome drift
  with minimum-sample and staged fail-closed policy.
- [x] PaperOps uses one immutable registered train/validation/untouched-holdout
  contract and one byte-stable evaluation receipt per experiment.
- [x] A Monitor cycle reuses one immutable, freshness-checked observation
  bundle across alpha monitoring and trade watching.
- [x] SQLite initialization/migration is once per process/schema version while
  cold-start and external-version changes remain correct.
- [x] Morning and EOD take fail-closed SQLite online backups before mutation;
  each atomic bundle is held outside the state root, binds the supported
  Dawnstrike schema and exact release SHA, is independently self-hashed, and
  never includes credentials. Restore tooling is non-mutating VERIFY/PLAN
  only, and retention never removes the last known-good bundle.
- [x] Scheduled children have bounded descendant-killing deadlines, live lock
  owners are not age-evicted, and skipped Monitor intervals are receipted.

## Cycle 2 research improvements

- [x] Strategy-family scores are compared only through a frozen shadow,
  after-cost utility reranker; champion ranking remains unchanged initially.
- [x] A versioned empirical execution-cost challenger retains the conservative
  fallback and is trained only from authenticated FillTruth.
- [x] Feature schema v2 matches the current 1%-50% gap universe and includes
  volatility-normalized, cross-sectional, liquidity, breadth, and residual
  features without lookahead.
- [x] Regime evidence uses point-in-time benchmark/breadth inputs; unreachable
  legacy thresholds no longer drive the challenger.
- [x] Immutable catalyst evidence is joined at decision time and evaluated with
  exact-common-OOS ablations.
- [x] The existing fleet allocator runs as an isolated paired shadow portfolio
  with point-in-time sector/correlation evidence.
- [x] Scenario Intelligence prefilters low-value material and prospectively
  calibrates abstain/watch/avoid outcomes without treating them as trades.
- [x] Rejected-candidate attribution uses inclusion probabilities and typed
  reason codes with effective-sample reporting.
- [x] Sampled rejects have a precommitted prospective non-trade observation
  policy; their outcomes remain counterfactual research and never FillTruth or
  official paper P&L.
- [x] Existing one-variable challengers run through immutable weekly purged
  walk-forward and prospective shadow receipts instead of latest-snapshot
  retrospective comparison.
- [x] Repeated learning proposals coalesce into an evidence-ranked,
  operator-owned queue; low-sample items remain collect-evidence and nothing is
  auto-applied.
- [x] V6 learning bytes and semantics are bound into release identity and
  verifier checks.
- [x] Closed-market finalization writes a terminal not-applicable heartbeat and
  daily no-trade funnel evidence with returns kept null.
- [x] Monitor queries use indexed projections/counts rather than history-wide
  row hydration, with result-hash parity tests.
- [x] EOD acquisition has one bounded retry owner and deterministic bounded
  concurrency/batching.
- [x] Finalize retry classes are explicit and terminal failures stop without
  blind retries.
- [x] Canonical writes are batched without weakening full-rebuild parity.
- [x] Immutable readiness integrity is cached per build and public packaging
  avoids duplicate byte/base64 work while retaining byte-exact verification.
- [x] Independent Morning acquisition lanes use bounded deterministic
  scheduling (governed max concurrency 1) and preserve identical frozen
  output hashes. Parallel overlap remains explicitly deferred until provider
  pacing and receipt coordination are process-wide contracts.

## Completion evidence

- [x] Focused hostile tests for every changed contract.
- [ ] Full test suite, Ruff, mypy, compileall, and PowerShell parser checks pass
  on a clean exact candidate SHA.
- [ ] Disposable-state Morning, Monitor, EOD, Finalize, scheduler-doctor, and
  scenario-doctor rehearsal passes without provider or broker mutation.
- [ ] Independent SOL audit reports no unresolved critical/high findings.
- [ ] Candidate is merged/pushed to GitHub and `origin/main` equals the accepted
  SHA.
- [ ] Vercel publication uses the repository's existing pinned script only
  after exact-SHA daily publication and readiness gates pass.
- [ ] Cache-bypassed production `/`, `/api/health`, `/api/readiness`, manifests,
  and every declared asset match the accepted source/build/data hashes.
- [ ] Monday task definitions are enabled, rooted at the accepted runtime,
  Morning is exactly 08:00 America/Chicago, and no unexpected enabled legacy
  runner exists.

## Current external gates (2026-08-30)

- The prior durable state root was not recoverable from an authentic backup.
  The replacement database is deliberately schema-only and fail-closed; it
  contains no reconstructed picks, fills, positions, P&L, returns, or secrets.
- Both supported Telegram credential pairs are absent. A fresh open-session
  Morning run therefore fails the required publication boundary instead of
  falling back to console or claiming delivery. This remains an external
  Monday blocker until valid credentials are restored without exposing them.
- Scenario Intelligence remains disabled because no OpenAI key is configured;
  this is an allowed optional-lane state and does not weaken core readiness.
- 2026-08-30 is a closed market date. The candidate cannot honestly produce a
  current-session READY publication receipt or pass the exact-SHA Vercel and
  runtime-cutover gates on this date.

## Convergence rule

SOL audits only integrated, green candidates. Critical/high findings return to
Luna for another implementation round. Stop after the first zero-critical/high
SOL audit, with a maximum of three SOL/Luna rounds; any unresolved item at that
limit remains an explicit blocker rather than being waived.
