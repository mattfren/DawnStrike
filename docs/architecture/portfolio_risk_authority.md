# Portfolio risk authority

`intraday_scanner.risk.portfolio.evaluate_portfolio_risk` is the single
fail-closed admission authority for a paper or shadow order proposal. It
evaluates the complete account snapshot, including positions and pending
orders from every strategy account.

The authority rejects missing equity, proposal price/stop, price timestamps,
position marks, risk inputs, invalid metadata, stale prices, daily loss,
drawdown, gross/net exposure, concentration, open-risk, and simultaneous
position violations. Rejection codes are stable uppercase identifiers and the
receipt hash covers the proposal, snapshot, limits, computed values, and
decision.

The 1% daily return is recorded as `daily_return_target_pct` for measurement
only. It never changes sizing, permits an entry, or overrides a stop. Broker
execution remains disabled; `live_execution_requested` is always a hard block.

Historical replay keeps its existing immutable per-symbol economics while
still routing through the same authority. Forward PaperOps admissions use the
governed aggregate limits.
