# Performance reconciliation

Status: `PARTIAL`

Authoritative source:
`C:\Users\MattFields\Dawnstrike\data\shadow_real.sqlite`

The daily publisher reads that database through SQLite read-only mode and
creates an online backup at
`C:\r\dawnstrike-10of10-20260729\data\daily_publication.sqlite`. The final
production rehearsal left the source file SHA-256 unchanged at:

`A9CF497463BBA78591D72BB038C7C3374D4B308895D07975E47FE0DB3CE8CEE4`

## Current evidence

As of the 2026-07-29 publication:

- 431 canonical performance rows;
- 223 daily cohort records;
- 49 reconciliation issues;
- 142 eligible outcomes;
- 114 observed outcomes;
- 28 missing outcomes;
- 80.2817% eligible-outcome coverage;
- 190 PaperOps source rows;
- 190 accepted PaperOps rows;
- 0 quarantined PaperOps rows;
- 21 PaperOps component-scope warnings;
- 0 PaperOps source-return mismatches;
- 0 benchmark observations.

Input hash:

`4062e9e0ea86036a930f82c79b0c78e49983a0fe7dd1253703996ff1585c3b8e`

Final production data hash:

`3a134cc971e69a6f01a50c63687f9440883e82e833afa09bd120d319270cd56d`

## PaperOps semantics

The PaperOps adapter:

1. separates mode, strategy, version, policy, and evidence cohorts;
2. derives daily P&L and return from verified prior-to-current equity changes;
3. accepts a row only when its daily or cumulative identity is provable;
4. retains realized/unrealized component-scope differences as warnings when
   the day-over-day equity identity is still auditable;
5. keeps reported costs non-authoritative until their inclusion in ending
   equity is proven; and
6. does not mix replay, research, shadow, historical, and official paper
   totals.

All 190 source `daily_return_pct` values agree with the adapter's derived
equity return. The 21 warning rows do not authorize an after-cost profitability
claim.

## Official-paper limitation

The seven raw official positions and fourteen fills are preserved. Their
source P&L can be audited, but the database lacks a complete official opening
equity, complete fee/slippage evidence, and same-policy benchmark series.
Therefore:

- official daily return is not reported;
- official cumulative return is not reported;
- benchmark and excess return are not reported;
- official net P&L and drawdown are not promoted as complete;
- missing outcomes do not enter averages, win rates, equity curves, or labels.

This is why production readiness is a controlled HTTP 503. It is the expected
truth-preserving result, not a deployment crash.

## Production identity

- deployment: `dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6`;
- source: `51f79ff2a738110b486111d85c4d93cfda9f4ec8`;
- build: `5ef6a274f37fd1dbae87`;
- data:
  `3a134cc971e69a6f01a50c63687f9440883e82e833afa09bd120d319270cd56d`;
- readiness: degraded/not_ready, HTTP 503.
