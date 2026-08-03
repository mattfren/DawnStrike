# Dawnstrike extreme hardening audit

Date: 2026-08-01 (America/Chicago)

Authoritative branch: `codex/terra-alphaops-v6`

Production: `https://dawnstrike-command-center-x3.vercel.app`

## Verdict

**Production Dawnstrike is not a return engine and is not operationally current.** It is a visually credible, research-only dashboard publishing stale and incomplete evidence from an old runtime. Its five sourced, triggered AlphaOps outcomes all lost money. The immediate cause was not bad luck alone: the legacy gate preserved `can_alert=true` for rows it had itself labeled `NEEDS_CONFIRMATION`.

The branch now blocks those rows, fixes Calendar cohort truth, adds missing web security headers, and hardens the only recursive staging deletion. Those changes reduce false confidence; they do not prove future returns. Strategy quality remains `WAITING_FOR_FORWARD_EVIDENCE`.

## Scorecard

| Area | Live production | Hardened branch | Honest ceiling today |
|---|---:|---:|---:|
| Visual UI and responsive structure | 7/10 | 8/10 | Calendar and core navigation render cleanly; deeper replay/incident UX remains. |
| Truthful return presentation | 5/10 | 8/10 | Missing values stay unreported, but no complete official account return series exists. |
| Candidate/source quality | 2/10 | 4/10 | Bad evidence is now blocked; the real point-in-time universe and production source config are absent. |
| Strategy evidence | 1/10 | 2/10 | Five activated labels, zero wins, no valid superiority claim. |
| Learning and validation | 0/10 live | 6/10 code | V6 infrastructure exists but is not deployed and still has the gaps below. |
| Scheduler and daily freshness | 1/10 | 7/10 code | All four active task definitions failed their latest runs; corrected registration code is dormant. |
| Release integrity | 2/10 | 7/10 code | Production serves the old SHA and readiness 503. |
| Security and destructive safety | 5/10 | 8/10 | Header and stage-path fixes are local only; dependency/CI controls remain absent. |

Overall: **production 2/10; branch foundation 7/10; return superiority unscored and unproven.**

## P0 findings

### 1. Every completed activated outcome lost

The durable database contains ten signal outcomes: five `not_triggered` and five sourced/complete activations. Under the saved first-touch contract, none touched target or invalidation first, so each resolved at the sourced close.

| Date | Ticker | Entry | Close | Gross signal return |
|---|---:|---:|---:|---:|
| 2026-07-17 | SLND | 1.1758 | 1.1600 | -1.3438% |
| 2026-07-24 | VIVK | 3.6000 | 2.8700 | -20.2778% |
| 2026-07-29 | VRRM | 5.7500 | 5.1250 | -10.8696% |
| 2026-07-30 | NUWE | 5.9596 | 4.4798 | -24.8305% |
| 2026-07-30 | XRX | 3.4974 | 3.4898 | -0.2173% |

Observed signal-level summary: `n=5`, `wins=0`, mean `-11.5078%`, median `-10.8696%`, equal-weight arithmetic sum `-57.5389%`.

This is **not an account return**. It excludes sizing, overlapping exposure, fees, slippage, taxes, a complete benchmark, and independent outcome reconciliation. Yahoo is the sole outcome source for these five rows.

### 2. The old alert predicate contradicted its own gate

All five losses had `old_can_alert=true` while `old_status=NEEDS_CONFIRMATION`. They also carried evidence that should have prevented a named alert:

| Ticker | Alpha | Edge | History | Grade | Source confidence | Gap |
|---|---:|---|---|---|---:|---:|
| SLND | 50.67 | LOW | INSUFFICIENT_SAMPLE | C | 34.5 | 60.34% |
| VIVK | 49.43 | LOW | INSUFFICIENT_SAMPLE | C | 34.5 | 86.50% |
| VRRM | 43.35 | LOW | INSUFFICIENT_SAMPLE | C | 22.0 | 31.03% |
| NUWE | 47.68 | LOW | INSUFFICIENT_SAMPLE | C | 34.5 | 155.96% |
| XRX | 44.85 | LOW | INSUFFICIENT_SAMPLE | D | 34.5 | 21.45% |

Common defects were unverified SEC/halt/public-table identity, unknown float, no clear catalyst, low catalyst confidence, weak edge, poor setup grade, excessive stop distance, free-web-only evidence, and insufficient history. VRRM also had an unresolved source/volume conflict. Four exceeded the new gap policy or other hard-risk bounds.

**Root cause:** warnings and manual-confirmation states changed labels but did not revoke `can_alert`. Heuristic fallback then elevated uncalibrated, weak-evidence rows into Telegram watchlists.

### 3. Production is an old, degraded release

- Branch HEAD before this audit: `9e75ca7d1eb39cca5cecdf955ff9974915da67cb`.
- Stable runtime: `C:\r\dawnstrike-runtime`, clean at `692e785cf8304a8045e88ab221dc644d4eb2e9e7`.
- Production deployment: `dpl_Crdhxte5H7z8hkHxduEH8BDZ4Pjd`.
- Production serves source SHA `692e785c`, build ID `f42ac827`, and readiness HTTP `503`.
- Production is updated only through 2026-07-30/31 evidence and has no deployed V6 learning payload.

Therefore Terra's V6 work and this audit's fixes are not live.

### 4. The daily chain is enabled but failed

The scheduler doctor reports `BLOCKED_EXTERNAL` and four failed tasks:

| Task | Latest result | Principal | Safety defect |
|---|---:|---|---|
| Morning | 1 | Interactive | Old runtime, logon-only, battery stop enabled |
| Monitor | 1 | Interactive | Old runtime, logon-only, battery stop enabled |
| EOD | 1 | Interactive | Old runtime, logon-only, battery stop enabled |
| Finalize | 2 | Interactive | Old runtime, logon-only, battery stop enabled |

The five dangerous legacy producer/publisher tasks are disabled, which is good. The active chain still points at the old runtime and cannot satisfy the corrected branch's noninteractive, battery-safe, exact-runner contract.

### 5. Live data cannot support the strategy claim

- Live DB `quick_check`: `ok`.
- Live schema: `13`; branch schema: `17`.
- `benchmark_observations=0`.
- `official_strategy_cohorts=0`.
- `provider_health=0`.
- `alpha_outcome_labels=0`.
- No V6 tables are active in the production DB.
- No production `config/web_sources.yaml` exists.
- No source-backed V6 universe is registered.
- The latest collection used public StockAnalysis/TradingView scraping, with halt and SEC collection disabled; all source rows started unverified.
- Only 30 of 103 candidates could be enriched, seven of eight selected tickers received Yahoo verification, and malformed ticker artifacts were observed.

This is insufficient for reliable selection, independent outcomes, benchmark-relative learning, or an official return series.

## P1 algorithm and learning findings

1. `alpha-v6-learn` still trains during every EOD run. Daily outcome/drift monitoring and weekly challenger fitting are not separated.
2. The research-packet path repeats learning work instead of reading frozen receipts only.
3. The controlled gradient model can be fit on all rows, but purged walk-forward prediction explicitly calls `_fit_model_suite(... allow_gradient_boosting=False)`. It therefore has no apples-to-apples out-of-fold competition against the regularized baseline.
4. Failure attribution groups only `setup_key|regime_key`. It does not explain source, selection regret, chase/entry, spread/liquidity, stop/target, catalyst, concentration, tail, or outcome-quality failure.
5. The universe command only registers caller-supplied JSON. It does not fetch, validate, diff, or roll back a licensed point-in-time universe.
6. One-time holdout enforcement exists in domain/storage code but has no complete operator CLI workflow.
7. Frozen V5 versus V6/cash/SPY/IWM comparison is not complete and cannot be complete with zero benchmark observations.
8. Five losses are far too few to tune safely. Any rule chosen because it perfectly rejects these five is retrospective overfit until prospectively tested.

## P1 product, release, and security findings

1. The live Calendar falsely inherited another cohort's day status when a filtered official cohort had no row. Local code now shows `Missing`, not a borrowed `No trade`.
2. Production had HSTS but lacked CSP, `nosniff`, referrer policy, permissions policy, clickjacking protection, COOP, and CORP. Local root and staged Vercel configs now emit all seven controls.
3. The Vercel stage builder recursively deleted a caller-controlled destination before proving it was safe. It now permits only a non-source subdirectory of the repository's `build` directory.
4. No `.github` CI workflow exists.
5. Dependencies are lower-bounded but not locked; no reproducible environment, SBOM, or automated dependency audit exists. `pip check` passes locally, but `pip-audit` is unavailable, so vulnerability status is unknown.
6. The original checkout has 1,207 dirty paths. It must never be a production runtime or deployment source.

## Hardening completed in this audit

- Added `dawnstrike-alert-gate-v2.0.0` with hard source, conflict, SEC/halt, identity, quality, gap, stop, catalyst, edge, grade, and target-integrity blocks.
- Made every non-passing gate revoke `can_alert` and write a durable `no_trade_reason`.
- Raised and unified probability-fallback thresholds; prevented the review layer from reintroducing rows below the fallback floor.
- Subordinated the disabled legacy web-Telegram route to the canonical gate; raw scanner ranks now produce a no-pick summary instead of ticker alerts.
- Replayed all five losses: every row changes from `can_alert=true` to `can_alert=false`, status `BLOCKED`.
- Fixed filtered Calendar cells so absent cohort evidence renders `MISSING` on market days and `UNAVAILABLE` on closed days.
- Added Vercel security headers to both repository and generated-stage configurations.
- Added destructive-path containment to the staging script.
- Added regression tests for all changed contracts.

## What actually improves the odds of better returns

No engineer can honestly guarantee wealth or returns. Dawnstrike can improve its research process by doing five things in order:

1. **Abstain aggressively.** A no-pick day is superior to a weak, unverified pick.
2. **Fix input truth.** Use a real point-in-time universe, independent market/outcome sources, corporate actions, halt/SEC checks, catalyst lineage, spreads, and benchmark observations.
3. **Optimize the correct objective.** Select on a predeclared lower confidence bound of after-cost benchmark excess, subject to drawdown/tail/capacity constraints—not raw heuristic score or hit rate.
4. **Learn causally and prospectively.** Attribute failure, register one-change experiments, evaluate with purged date-forward folds, and touch the holdout once.
5. **Prove operations.** The exact clean SHA must collect, label, learn, publish, and notify unattended every market day before any strategy claim is considered.

Expected near-term effect: far fewer ticker alerts and fewer avoidable low-quality losses. That is risk reduction, not proof of alpha.

## Release gates

Do not promote or claim strategy success until all are true:

- clean merged SHA installed in `C:\r\dawnstrike-runtime`;
- schema migration 17 backed up, applied, restart-tested, and idempotent;
- real `web_sources.yaml` and source-backed dated universe registered;
- active tasks use password logon, start-when-available, battery-safe settings, exact runtime, structured receipts, and no overlap;
- one complete unattended market day succeeds through Telegram and verified Vercel promotion;
- readiness HTTP 200, matching source/build/data hashes, V6 JSON present, Calendar/browser proof clean;
- at least 60 forward market sessions and 100 valid sourced closed paper labels;
- at least 98% eligible outcome coverage and complete benchmark/cost truth;
- positive after-cost and benchmark-relative lower-bound evidence under 1.5x/2x slippage stress, drawdown/tail/capacity limits, untouched holdout, and manual approval.

Until then: `WAITING_FOR_FORWARD_EVIDENCE`.

## Verification from this audit

- Focused tests: 41 passed.
- Full pytest suite: 614 passed in 425.87 seconds.
- Ruff on changed Python/tests: passed.
- Full Ruff: passed.
- Mypy: passed across 204 source files.
- Compileall: passed.
- JavaScript syntax: passed.
- PowerShell parser: passed.
- Generated Vercel stage: seven headers present.
- Unsafe outside stage path: rejected.
- Public-source overlap stage path: rejected.
- Diagnostic public build: intentionally degraded, HTTP 503, no fabricated readiness.
- Rendered Calendar: official V5 July 1-30 market days show `Missing`; closed days show `Unavailable`; July 31 remains `Pending` due one unresolved outcome.
- Browser console: zero messages/errors during the rendered check.
- Git diff hygiene and local package consistency: passed.

The exact corrective directive is `docs/audit/dawnstrike_extreme_remediation_prompt_2026-08-01.md`.
