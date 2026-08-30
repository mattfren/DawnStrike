# Intraday shadow strategy protocol

This is a preregistered research packet. The frozen champion remains the only
official strategy. These hypotheses produce paper observations only and are
never broker-routable or automatically promotable.

## Frozen registry

`intraday_scanner.v2.strategies.shadow_intraday.build_shadow_strategy_registry`
registers four versioned families:

| Family | Window (CT) | Geometry | Required truth |
| --- | --- | --- | --- |
| Opening-range continuation | 09:45–10:45 | stop below opening-range low, 2R target, 45-minute timeout | five completed opening bars |
| VWAP reclaim/pullback | 10:00–14:30 | stop below pullback low, 2R target, 60-minute timeout | retained point-in-time VWAP |
| Catalyst continuation | 09:35–12:00 | stop below confirmation low, 2R target, 45-minute timeout | timestamped catalyst and source artifact hash |
| Failed-breakout/gap-fade | 10:00–14:30 | short stop above rejection high, 2R target, 45-minute timeout | quote/spread, halt, borrow, SSR, and corporate-action truth |

The 1% daily figure is an account KPI, not an entry predicate, target, or
promise. No parameter tuning or performance optimization is part of this
protocol.

## Causal and safety rules

Evaluations receive a current event plus prior events. Events at the current
timestamp and all future events are excluded from history. Signals are
decision-at-close observations whose earliest entry is strictly later than the
decision timestamp; same-bar execution is not implied. Every geometry is
validated for direction before it is emitted.

Missing evidence is represented as `NOT_EVALUABLE` with an exact reason code:

- `VWAP_TRUTH_REQUIRED`, `CATALYST_TRUTH_REQUIRED`, and history codes block
  the corresponding long families.
- `QUOTE_TRUTH_REQUIRED`, `SPREAD_TRUTH_REQUIRED`, and `WIDE_SPREAD` block
  the fade family when executable quote quality is unknown or poor.
- `BORROW_TRUTH_REQUIRED`, `BORROW_UNAVAILABLE`, `SSR_TRUTH_REQUIRED`, and
  `SSR_ACTIVE` block short research when locate/SSR truth is absent or unsafe.
- `CORPORATE_ACTION_TRUTH_REQUIRED` blocks unverified price basis.
- `CURRENT_HALT` and `HALT_TRUTH_BLOCKED` block halted or halt-contaminated
  observations.

Each proposed paper entry is routed through `PortfolioRiskAuthority`. A
missing or incomplete account snapshot therefore produces a risk-blocked
research observation rather than an implicit approval. Empirical execution
costs are required before evaluation can be marked evaluable; provisional
costs do not qualify the champion or a challenger for promotion.

## Evidence and promotion boundary

The registry and each signal carry a frozen configuration hash. Outputs carry
`research_only=true`, `broker_execution_enabled=false`, and no promotion
authority. A no-signal or unavailable day remains a no-signal or unavailable
day; it is not a zero return and it is not a forced trade.
