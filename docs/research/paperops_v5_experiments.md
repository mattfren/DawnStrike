# PaperOps v5 Forward Research

PaperOps strategies remain experimental. Current samples are too small to
support a profitability claim or promotion.

## Current evidence at the 2026-07-30 cutoff

The legacy forward snapshot has only 10–12 sessions per active strategy, 0–4
closed trades per strategy, zero committed FillTruth forward fills, no
robustness result, and no complete promotion packet.

| Strategy | Sessions | Closed trades | After-cost expectancy | Profit factor | Status |
|---|---:|---:|---:|---:|---|
| Bullish FVG continuation | 12 | 4 | 46.34 | 1.252 | Watch; tiny sample |
| Cross-sectional relative strength | 12 | 1 | -59.93 | 0.000 | Quarantined |
| Donchian 20/10 | 12 | 3 | 134.05 | 1.656 | Watch; tiny sample |
| Failed-breakout short | 12 | 1 | 102.88 | 102.88 | Block without verified borrow; tiny sample |
| Gap-up continuation | 10 | 0 | null | null | Inert/unproven |
| Gap-up continuation ATR | 10 | 0 | null | null | Inert/unproven |
| Pullback reclaim | 12 | 1 | -232.76 | 0.000 | Quarantined |
| Time-series momentum v1 | 12 | 2 | -172.22 | 0.382 | Quarantined |
| Time-series momentum v2 | 11 | 2 | -104.93 | 0.377 | Archived |
| Volatility contraction | 12 | 1 | 442.38 | 442.38 | Watch; tiny sample |

The apparent large profit factors from one winning trade are not robust
evidence. A zero-trade strategy has null expectancy and profit factor, not
zero-return evidence. The current blotter has 12 blocked, 8 pending, 2
rejected, no closed rows for the current day, and null total P&L.

## Registered one-change experiments

Six forward-only experiments begin after the cutoff:

1. Time-series momentum: generic lifecycle to SMA50 causal invalidation and
   trading-session timeout.
2. Donchian breakout: generic lifecycle to prior causal 10-session channel
   invalidation.
3. Pullback reclaim: generic lifecycle to saved reclaim-low invalidation.
4. Gap-up continuation: generic lifecycle to saved gap-support invalidation.
5. Fleet allocator: additive overlap/correlation limits while preserving every
   individual strategy account.
6. Cross-sectional ranking: stocks rank only against stocks and ETFs only
   against ETFs.

Each contract freezes one controlled change, configuration hash, training
cutoff, chronological validation interval, untouched holdout, stop condition,
and operator-review decision. Existing champions and historical results do not
change.

Short strategies fail closed without verified locate/borrow availability and
modeled borrow cost. Lifecycle timeout counts trading sessions, not calendar
days. All causal exit inputs must exist at or before the decision timestamp.

## Non-negotiable promotion gate

Promotion requires all of:

- at least 60 forward market sessions;
- at least 100 forward closed trades;
- at least 98% truth coverage;
- positive after-cost expectancy;
- profit factor at least 1.20;
- positive return versus cash and benchmark;
- maximum drawdown no worse than -8%;
- gain and loss concentration no greater than 25%;
- positive chronological walk-forward evidence;
- positive untouched holdout evidence;
- positive expectancy under 1.5x slippage;
- no-lookahead and reconciliation proof;
- manual operator review.

The evaluator rejects any configuration that weakens these floors. Automatic
promotion is disabled. Until the complete packet passes, every strategy stays
research-only regardless of an attractive small-sample number.
