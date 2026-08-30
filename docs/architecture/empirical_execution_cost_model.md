# Empirical execution-cost challenger

`intraday_scanner.alpha.empirical_execution_cost_challenger` contains the
research-only empirical cost calibration boundary. It is deliberately separate
from the V5 champion cost policy (`alphaops-v5-cost-model-50bps-0.005ps`).

The strict model receipt accepts only `CommitBridge`-authenticated mappings
whose execution status is `CLOSED`, whose FillTruth status is `COMMITTED`, and
whose research-only/no-broker flags are intact. A plain JSON object, caller
provided digest, or modeled fill is rejected. Each observed sample retains
separate spread, slippage, fees, regulatory, and borrow components. Missing
components stay unknown and prevent an empirical total from being claimed.

The receipt emits deterministic p50, p75, and p90 values, sample/session
counts, confidence labels, and component missing counts. Bucket dimensions are
price, dollar liquidity, participation rate, volatility, time of day, side,
order type, and venue/feed. Resolution uses the declared exact-to-global
fallback hierarchy and labels a sparse fallback; it never substitutes a
universal optimistic value or the champion model. A 2x stress view is emitted
as a shadow diagnostic.

The production model is not evaluable before 300 authenticated fills spanning
at least 60 market sessions; an individual bucket needs at least 30
observations and still inherits the session minimum. The older 20-observation
Cycle-2 API remains only as a legacy shadow diagnostic and is not a calibrated
model claim.

Every receipt is hash-bound to its source manifest, code SHA, window, input
observations, configuration, and model version. `select_empirical_cost` returns
`empirical_claim=True` only after those hashes and evidence minima validate.
Persistence is immutable write-once through `persist_empirical_cost_receipt`.
No promotion, champion mutation, or broker execution is implemented here.
