# Strategy evidence status

Status: `WAITING_FOR_FORWARD_EVIDENCE`

The deterministic promotion policy requires, at minimum, 60 real market days,
100 closed forward paper trades, 98% eligible outcome coverage, positive
after-cost expectancy, profit factor at least 1.20, positive excess return
versus cash and the registered benchmark, drawdown no worse than 8%,
concentration limits, positive walk-forward and untouched holdout results,
positive 1.5x slippage stress, and a passed no-lookahead audit.

The current source evidence does not meet those gates. The policy emits a
versioned decision with component values, source references, action, and vetoes;
it never calls a strategy validated and never uses an LLM or a UI button for
promotion.
