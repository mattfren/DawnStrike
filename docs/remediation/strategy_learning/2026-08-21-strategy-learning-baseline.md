# Dawnstrike strategy-learning baseline and remediation plan

Date: 2026-08-21  
Code baseline: `7c9377696cff097a8368cf165e21262bae90d08a`  
Runtime evidence boundary: read-only `C:\r\dawnstrike-state\shadow_real.sqlite` and immutable PaperOps/DataTruth artifacts  
Execution boundary: research and shadow paper only; broker execution remains disabled

## Verdict

Dawnstrike has enough retained daily-bar and PaperOps evidence to diagnose selection and execution failure modes, but not enough complete per-decision counterfactual truth to claim that every missed winner was knowable at decision time. The first remediation tranche therefore adds deterministic decision traces, outcome eligibility, miss attribution, and versioned research challengers. It does not rewrite champions, coerce missing outcomes to zero, auto-fit, auto-promote, or loosen a risk rule from hindsight.

Two evidence classes must remain separate:

- Forward PaperOps evidence: decisions and account state from 2026-07-15 through 2026-08-21.
- Latest-snapshot retrospective replay: causal prefix replay over the retained two-year snapshot. This is useful research evidence, but provider revisions mean it is not relabeled as forward evidence.

The existing seven-day `historical_backtest` cohort is too short for promotion claims. Existing performance rows also label many open marked-to-market observations as `realized`; closed-trade truth must be derived from position state, not `record_status` alone.

## What the retained evidence says

| Strategy | Forward observation | Retrospective replay | Miss diagnosis | Remediation lane |
|---|---|---|---|---|
| `ts_momentum_sma_atr` | 218 picks, 209 blocks, 7 closes, 2 wins/5 losses; canonical forward return about -1.67% | 134 trades, +0.12%, -4.98% max drawdown | Candidate flood, late/extended entries, capacity and duplicate-symbol contention, gap-through/stop losses | New research challenger: extension and volatility/regime guards, explicit ranking before capacity, predicate trace |
| `donchian_breakout_20_10` | 7 closes, 3 wins/4 losses; about -0.89% | 106 trades, -0.31%, -5.72% max drawdown | False/extended breakouts, weak participation, gap-through-stop risk | New challenger: normalized breakout distance, participation/close-quality guard, conservative gap handling |
| `cross_sectional_relative_strength` | 5 closes, 1 win/4 losses; about -2.16%; duplicate blocks dominate | 99 trades, +4.84%, -3.66% max drawdown | Forward rotation/crowding and portfolio concentration; ranks lack margin/context | New challenger: explicit rank margin and concentration-ready trace; preserve point-in-time ranking |
| `pullback_reclaim_uptrend` | 15 picks, 2 closes, 1 win/1 loss; about -0.09% | 37 trades, +0.03%, -2.26% max drawdown | Sparse activation and falling-knife/broken-trend risk | New challenger: trend-slope and waterfall exclusion; do not loosen thresholds from two closes |
| `volatility_contraction_breakout` | 2 closes, 1 win/1 loss; about +0.49% | 64 trades, +3.38%, -2.24% max drawdown | Best observed alpha, but sample is tiny; dead liquidity and downside expansion are unmeasured | Preserve v1; conservative shadow challenger with participation, direction, and liquidity-quality predicates |
| `failed_breakout_reversal_short` | 6 closes, 3 wins/3 losses; about +0.38% | 164 trades, -16.83%, -17.82% max drawdown | Severe replay failure; squeeze/re-break, borrow, cost, and OHLC path risks | Quarantine; challenger fails closed when borrow/locate evidence is unavailable and requires stronger rejection confirmation |
| `bullish_fvg_continuation` | 7 closes, 1 win/6 losses; about -0.22% | 130 trades, -1.53%, -7.65% max drawdown | Daily OHLC gap is only a proxy for order-flow imbalance; weak continuation quality | New challenger explicitly named as a daily proxy with stronger trend, gap, close, and participation quality |
| `gap_up_continuation` | 7 picks, zero fills; all observed days no-trade at account level | 49 trades, +5.81%, -2.83% max drawdown | Activation/fill evidence gap, not a demonstrated forward return failure | Keep threshold frozen; add per-gate eligibility trace, corporate-action/data-quality rejection, and next-session fill diagnostics |
| `gap_up_continuation_atr` | 8 picks, zero fills | 39 trades, +7.47%, -1.65% max drawdown | Same activation gap; the 0.50 ATR threshold was screened on retained data | Keep threshold frozen; trace ATR gate separately and require untouched holdout comparison with fixed-gap strategy |
| `benchmark_buy_hold_equal_weight` | Comparator rows exist | Existing lifecycle engine skips it | No valid all-strategy comparison until hold-to-end comparator semantics exist | Add dedicated comparator semantics and persist benchmark/excess fields |
| `cash_no_trade_baseline` | Valid opportunity-cost control | 0% by definition | No signal remediation | Preserve as baseline and label positive-benchmark no-trade days without treating them as guaranteed missed trades |

Returns above are diagnostics from different accounting surfaces. Canonical daily portfolio returns and raw close-event P&L are not interchangeable; reports must name the source and eligibility rules.

## Root-cause taxonomy

Every `(strategy, version, symbol, decision_time)` must end with one deterministic disposition:

1. `DATA_UNAVAILABLE` or `FEATURE_UNAVAILABLE`
2. `NO_SETUP`, with every predicate and the first failing predicate
3. `RISK_BLOCKED`
4. `RANKED_OUT` or `CAPACITY_BLOCKED`
5. `ENTRY_NOT_TRIGGERED` or `FILL_UNAVAILABLE`
6. `EXECUTED_LOSS` or `EXIT_POLICY_LOSS`
7. `PROFITABLE_MISS`, only when a causal, after-cost counterfactual is eligible
8. `MISSING_OUTCOME`, `CONFLICTING_OUTCOME`, or `INDETERMINATE_PATH`

Unknown evidence remains unknown. A no-trade day is not automatically a miss, an open mark is not a closed outcome, and a positive future move is not actionable unless the required features were available before the cutoff.

## Daily learner contract

The daily run will:

1. Freeze market date, cutoff, code SHA, strategy versions/config hashes, input artifact IDs, and evidence hashes.
2. Inventory every catalog strategy, including comparators.
3. Run causal prefix replay or consume retained forward decisions without reading future bars at decision time.
4. Produce decision traces, eligible counterfactual outcomes, and per-strategy miss attribution.
5. Emit one-variable, versioned remediation proposals as unapplied research artifacts.
6. Produce stable machine-readable and human-readable receipts.

The daily run will not fit a model, mutate a champion, apply a proposal, promote a challenger, route an order, or convert missing truth to zero. Weekly/manual research may evaluate frozen challengers on purged chronological validation and untouched holdout windows.

## Challenger acceptance gates

A challenger is only reviewable when all of the following hold:

- identical dates, symbols, costs, and execution policy against its parent;
- strategy/version/config/logic hashes are frozen;
- decisions are invariant to future-bar mutation;
- replay and forward evidence are reported separately;
- missing/conflicting outcomes are excluded rather than imputed;
- after-cost return, expectancy, drawdown, turnover, concentration, and sample coverage are present;
- multiple-testing and threshold-screening provenance are disclosed;
- required data is actually available (borrow, corporate action, catalyst, sector, or intraday path evidence is never invented);
- `research_only=true`, `automatic_policy_change=false`, `automatic_promotion=false`, and `broker_execution_enabled=false`;
- a human explicitly approves any later promotion.

## Current proof gaps

- Retained forward decisions do not yet contain counterfactual returns for rejected/no-setup candidates.
- `opportunity_miss_records`, `trade_attribution_cases`, and `trade_attribution_factors` are empty.
- The benchmark comparator is skipped by the lifecycle backtester.
- Gap-up strategies have no forward fills and no historical cohort rows.
- Alpha V6 has zero currently trainable eligible labels in its research packet; legacy/incomplete labels remain quarantined.
- Forward samples per strategy are too small for statistically defensible promotion.
- Public Yahoo daily bars do not establish borrow availability, news/catalyst truth, or intraday order-flow/path ordering.

These gaps block promotion, not implementation of the learning and research instrumentation.
