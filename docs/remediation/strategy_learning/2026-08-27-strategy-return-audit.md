# Dawnstrike strategy return audit — 2026-08-27

## Verdict

The current forward evidence does not justify automatic champion promotion, threshold loosening,
or a claim that any strategy has a durable live edge. The research slate should still publish up
to five honest, de-duplicated Tier 1 names when safe point-in-time S&P 500, Nasdaq-100, and mover
rows exist. Tier 2/3 plan qualification remains independent and may honestly be zero.

This is a research-only audit. Broker execution remains disabled.

## Evidence boundary

- Source: `C:\r\dawnstrike-state\v2_paper_ops_live\exports\paper_trade_blotter.json`
- Source SHA-256: `fd9d7f584242fa802f25c890c160c01ff7cd31164e7b429c74a096aba26f1df9`
- Source last write: `2026-08-26T20:39:13Z`
- Cohort: `mode=forward`, `series_role=champion`
- Accepted lifecycle states: `blocked`, `closed`, `open`, and `pending`; `no_setup` and
  `rejected` are not accepted decisions.
- Aggregate: 449 accepted rows, 295 unique date/symbol/direction episodes, 39 closed trades,
  14 wins, 25 losses, and -$4,704.60 net P&L.
- Existing learning receipt:
  `C:\r\dawnstrike-state\outputs\strategy_learning\2026-08-26\daily_learning_receipt.json`
  (`7cbce5dc965fec94e4fdf25e3e8175f0deece04e2cb357d1e792711670b1ecb3`). It retains
  47 outcome objects, but none carries a committed FillTruth hash. It is historical/provisional
  evidence, not forward promotion authority under the hardened learning boundary.

The hardened candidate separates four evidence classes that must not be collapsed. Modeled EOD
replay is diagnostic. Operational lifecycle closure may restore paper state. Official realized
performance additionally requires exact, recomputed joins across a closed durable position,
entry and exit fills, admitted intents, identity, timestamps, source hash, cohort/account, and the
research-only/no-broker boundary. Learning is stricter still: without a governed CommitBridge and
committed FillTruth identity, the return remains quarantined even when operational lifecycle rows
exist. EOD repair or reconciliation output cannot authenticate itself as realized return truth.

### Signal-child parentage audit

A read-only SQLite audit returned `quick_check=ok` and 40 rows from the generic
`foreign_key_check`: 20 `signal_outcomes` and 20 `signal_events`. None was an unbound or fabricated
signal. Every row uses a `v6s-*` shadow identity; 20/20 resolve to the exact
`alpha_v6_decisions.shadow_signal_id`, 20/20 resolve to `alpha_v6_outcomes`, every outcome's market
date and ticker agree with its V6 decision, and each V6 decision retains its original
`historical_signals` source through `source_signal_id`.

The report exposed a schema-expression gap: the legacy child tables declare only
`historical_signals.signal_id` as their SQLite parent, while governed V6 shadow children use the
separate immutable decision ledger and Scenario children use an exact
`scenario_signal_links` -> `scenario_decisions` contract. The existing 40 rows were left
byte-for-byte unchanged. A second read-only audit found zero persisted Scenario links or Scenario
outcomes, so no legacy Scenario state needed mutation or exception handling.

The candidate now validates the polymorphic parent at every signal-outcome/event write boundary,
including the atomic trade-watcher lifecycle path. Non-Scenario children require exactly one
historical or V6 parent. A `scenario:<decision_id>` child requires one exact forward Scenario
decision/link, matching day, ticker, strategy, version, cohort, research-only, and broker-disabled
truth; its intentional historical mirror is accepted only when the mirror agrees. Historical-only
Scenario spoofing, duplicate links, cross-domain collisions, event/intent signal swaps, and partial
batch writes fail closed. A raw SQLite single-parent report must therefore be interpreted with this
domain-aware lineage check; it is not permission to delete, relink, or promote the V6 evidence.

`Mean return` below is the arithmetic mean of `trade_return_pct` for closed rows. `PF` is gross
positive net P&L divided by absolute gross negative net P&L. `Ex-best` removes the single best
closed trade to expose concentration.

## Strategy findings

| Strategy | Forward evidence | Audit conclusion | Governed research action |
| --- | --- | --- | --- |
| `bullish_fvg_continuation` | 58 accepted; 8 closed; 2W/6L; -$485.61; PF 0.687; mean -1.721%; ex-best -$1,407.02 | The daily-OHLC construction is only a proxy for an intraday fair-value gap, and one large win masks weak continuation quality. | Keep the champion frozen. Evaluate a shadow challenger with explicit proxy labeling plus trend, gap-quality, close-quality, and participation guards. |
| `cross_sectional_relative_strength` | 55 accepted; 6 closed; 1W/5L; -$1,807.10; PF 0.040; mean -3.934%; ex-best -$1,881.47 | This is the clearest negative forward signal, with likely rank-margin, rotation, and portfolio-concentration failure. | Do not loosen rank thresholds. Add point-in-time rank margin, sector/correlation concentration, and capacity traces to a challenger; require committed closes before promotion. |
| `donchian_breakout_20_10` | 35 accepted; 7 closed; 3W/4L; -$889.05; PF 0.596; mean -1.792%; ex-best -$1,691.51 | False or extended breakouts and gap-through-stop behavior dominate the small sample. | Shadow-test normalized breakout distance, participation, close quality, and conservative gap handling. Keep the 20/10 champion unchanged. |
| `failed_breakout_reversal_short` | 34 accepted; 6 closed; 3W/3L; +$180.58; PF 1.155; mean +0.315%; ex-best -$803.80 | The apparent edge disappears without the best trade, and there is no governed borrow/locate truth. | Keep short alertability quarantined. A challenger must prove borrow/locate, rejection confirmation, squeeze/re-break protection, and after-cost viability. No short broker or alertable execution path is added. |
| `gap_up_continuation` | 7 accepted; 0 closed/fills | There is no forward return sample; this is an activation/fill-observability gap, not evidence of success or failure. | Preserve the fixed-gap threshold. Add exact per-gate and next-session fill diagnostics; require corporate-action and data-quality truth. |
| `gap_up_continuation_atr` | 8 accepted; 0 closed/fills | The ATR variant has the same missing fill truth and cannot be compared honestly with the fixed-gap strategy. | Preserve the ATR threshold and untouched holdout. Trace the ATR gate separately and compare only after committed fills exist. |
| `pullback_reclaim_uptrend` | 5 accepted, 12 rejected; 2 closed; 1W/1L; -$86.38; PF 0.629; mean +1.786%; ex-best -$232.76 | Two closes are insufficient; positive mean return conflicts with negative P&L/R because sizing and loss magnitude dominate. | Do not loosen activation. Shadow-test trend slope and waterfall/broken-trend exclusion, then wait for committed fill truth. |
| `ts_momentum_sma_atr` | 233 accepted; 8 closed; 3W/5L; -$1,964.02; PF 0.173; mean -3.373%; ex-best -$2,176.67 | This lane creates 51.9% of all accepted rows but only eight closes, exposing candidate flood, correlation, capacity contention, and late/extended entry risk. | Rank before portfolio capacity. Shadow-test extension, volatility, regime, and concentration guards; require a meaningful committed sample before any champion change. |
| `volatility_contraction_breakout` | 14 accepted; 2 closed; 1W/1L; +$346.98; PF 4.637; mean +1.985%; ex-best -$95.40 | It is the most promising observed result, but two closes and best-trade dependence cannot establish an edge. | Preserve v1. Run a conservative shadow challenger with direction, liquidity, participation, and downside-expansion predicates; no automatic promotion. |

## What the system learned

1. More daily research breadth must come from broader, current, point-in-time universe coverage,
   not relaxed strategy or safety gates. Partial core batches may contribute only their fresh,
   authenticated rows and must remain labeled limited coverage.
2. Candidate count is not return evidence. Momentum produces most accepted rows while its closed
   cohort is both tiny and materially negative. De-duplication, cross-strategy concentration, and
   portfolio admission must precede any official plan.
3. A positive aggregate with one or two closes is not a promotion signal. Short reversal and
   volatility contraction both fail the ex-best/sample-size robustness check.
4. Zero fills are missing truth, not zero return. The two gap strategies need activation and fill
   lineage before return comparison.
5. Learning must consume one frozen, authenticated, point-in-time cohort. Closed rows without
   committed FillTruth remain quarantined; checked-zero claims require an authenticated query over
   an existing, warning-free source.
6. Operational lifecycle closure and modeled EOD replay are different evidence classes. EOD repair
   may restore state, but it cannot self-authenticate a realized return; only independently bound
   lifecycle evidence can support official performance, and only committed FillTruth can support
   learning or promotion.

## Release implication

The remediation can improve daily discovery and learning integrity without changing a champion.
The integration candidate now includes atomic watcher admission, authenticated fill/position
lifecycle identity, committed-FillTruth quarantine, official performance and Calendar FillTruth
gating, and hostile no-broker/evidence-mismatch regressions. These are candidate-code facts, not
production proof. The old-SHA Aug. 27 chain completed all 14 stages at 17:33 CT with a valid
finalizer receipt and a `NO_TRADE` Calendar outcome; that terminal result does not certify the new
candidate. Release acceptance still requires the complete test/audit gate, a clean exact SHA on
`main`, and proof from the next legitimate scheduled session. An out-of-window Morning replay is
not valid evidence.
