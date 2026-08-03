# Terra AlphaOps V6 initial audit

Date: 2026-08-01 (America/Chicago)
Scope: deployed Dawnstrike runtime, durable state, archived 2026-07-31 inputs, and the clean source release `692e785cf8304a8045e88ab221dc644d4eb2e9e7`.

## Evidence preserved before change

- Durable database snapshot: `migration-backups/terra-v6-20260801T082756Z/shadow_real.sqlite`
- Snapshot SHA-256: `99189B4B6CB31E8D3DDDFA6A8DFE2295620D02302BB7D3BB36F66EAD4D23663D`
- SQLite `PRAGMA quick_check`: `ok`; schema version: `13`; table count: `86`.
- Production deployment: `dpl_Crdhxte5H7z8hkHxduEH8BDZ4Pjd`; promoted source SHA: `692e785cf8304a8045e88ab221dc644d4eb2e9e7`.
- Production health was `200` and explicitly research-only. Production readiness was `503`.
- Archived replay inputs and copy-on-write reproduction database are retained under `repro/terra-v6-jul31`.

## Reproduced truth, not inference

The direct scanner replay using the archived enriched 2026-07-31 input completed and ranked four candidates. A full copy-on-write replay with network collection and enrichment replaced by those archived inputs also completed with an explicit `no_trade` outcome because the source watermark was stale. No production database row was changed by either replay.

Therefore the 2026-07-31 scheduled failure cannot currently be attributed to the scanner/ranking code path. The wrapper did not preserve a usable native-process receipt or complete stdout/stderr trace. The actionable root cause is an **operational observability and fail-closed scheduling defect**, not a verified alpha-model failure.

## Initial deficiencies

1. All four scheduled tasks failed on 2026-07-31, while `scheduler-doctor` returned exit code zero for `BLOCKED_EXTERNAL`.
2. Scheduled tasks use `Interactive` logon and battery-stop defaults, so they are not durable unattended jobs.
3. The normal runners use pipeline-based native process invocation. Their output/exit receipts are not durable or structured enough for attribution.
4. The runtime falls back to `config/web_sources.example.yaml`; the production source configuration is absent and has disabled/placeholder sources.
5. The shared daily ledger recorded a degraded finalization with missing upstream stages but no material failure reason.
6. The public readiness payload and UI exposed absolute runtime/state paths.
7. The V5 account is research-only and has no realised return truth. The database has zero AlphaOps outcome labels, zero benchmark observations, zero provider-health rows, and no official strategy cohorts/paper trades. Any claim of a learned return improvement would be fabricated.
8. The active scoring path still instantiates the V4 weighted model. Its training is a small, univariate, date-split heuristic without point-in-time training contracts, conditional utility, uncertainty, or tail-risk gates.
9. The existing edge calibrator substitutes absent return samples with zero-valued samples. This must be removed: absent truth is uncalibrated, not flat performance.
10. Calendar output exists in the publication contract but cannot become trusted until canonical performance, source health, safety evidence, and upstream receipts are all complete.

## V6 non-negotiable response

V5 remains the immutable research champion. V6 is an offline deterministic challenger which writes immutable decision, input-lineage, outcome, benchmark, and evaluation ledgers. It may only shadow-publish until it clears the forward-evidence gate: at least 60 closed market sessions, 100 valid closed paper trades, verified source/outcome coverage, net benchmark-relative return, and risk limits. It can never route an order or automatically promote itself.

`PERFORMANCE_STATUS=WAITING_FOR_FORWARD_EVIDENCE` until those criteria are satisfied. The objective is better expected net excess return with measured uncertainty—not a promise of great returns.
