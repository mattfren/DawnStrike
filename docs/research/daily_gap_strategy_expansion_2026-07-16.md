# Daily Gap Strategy Expansion — 2026-07-16

## Decision

Admit two frozen daily strategies to **experimental forward paper observation**:

1. `gap_up_continuation@v1.0` — the predeclared fixed-gap rule.
2. `gap_up_continuation_atr@v1.0` — a volatility-normalized challenger selected
   from a small threshold-sensitivity grid.

Neither strategy is validated, promoted, or suitable for a return promise. Each
receives its own `$100,000` paper account and must earn new forward evidence.
Adding an account does not pool capital with, or improve, any existing account.

## Frozen mechanics

Both strategies require a bullish signal bar, close-location value of at least
`0.70`, a close above `SMA(100)`, and current volume at least the prior-20-bar
mean. The stop is `low[t] - 0.25 * ATR(14)[t]`; the target is `2R`. A signal on
day `t` may fill no earlier than the next valid daily open. PaperOps then applies
1 bp fees per fill, 5 bps adverse slippage, actual-fill risk/reward checks, a
three-position limit, 100% gross-exposure limit, 2% aggregate open-risk limit,
0.5% risk per trade, stop-first same-bar ambiguity, and a ten-calendar-day
forced exit.

The only difference is the gap threshold:

- Fixed: `open[t] / close[t-1] - 1 >= 0.0075`.
- ATR challenger: `open[t] - close[t-1] >= 0.50 * ATR(14)[t-1]`.

## Retained-snapshot screen

Source: the retained 2026-07-16 public Yahoo daily OHLCV snapshot for the exact
18-symbol production universe. Normalized CSV SHA-256:
`91988d560a1a42b37cf4303406ae62d37aff41e2bd65ce0cf61069e61c1e62a0`.
The common evaluable period was 2024-12-06 through 2026-07-16. Each chronological
half was restarted with a fresh `$100,000` paper account; half-boundary open
positions remain marked to market, so half trade counts need not sum to the full
run.

| Frozen rule | Full | First half | Second half |
|---|---:|---:|---:|
| Fixed 0.75% | +7.0586%; 48 fills / 48 closed; PF 1.691; DD -2.817% | +2.7217%; 21 fills / 18 closed / 3 open; PF 1.475; DD -2.817% | +2.8244%; 27 fills / 27 closed; PF 1.533; DD -1.467% |
| 0.50 prior ATR | +8.0843%; 40 fills / 40 closed; PF 2.144; DD -2.785% | +2.4858%; 19 fills / 18 closed / 1 open; PF 1.599; DD -2.785% | +5.0497%; 21 fills / 21 closed; PF 2.613; DD -0.926% |

The table uses production card priority `(-setup_score, symbol, strategy_id)`,
pending-order reservations, signal-time frozen quantity, next-open fill gates,
and the same account/position lifecycle as PaperOps. An earlier alphabetical
research simulation was discarded because its ordering changed which orders
survived portfolio risk limits. First-half returns include close-marked open
positions; the realized-only first-half returns were still positive at
`+2.2245%` and `+2.2917%`, respectively.

These are in-sample research results on one retained universe, not expected
returns. Public daily bars do not prove intraday event ordering, and this
snapshot lacks independent corporate-action reconciliation. The ATR threshold
was grid-selected and therefore carries more selection-bias risk than the fixed
rule.

## Rejections

- Every tested gap-down reversal variant lost money in the full period and the
  second half. The least-bad fixed-gap/SMA100/median-volume form returned
  `-0.74%` with profit factor `0.910`.
- The predeclared cross-sectional five-day reversal returned `-10.84%`, profit
  factor `0.683`, and maximum drawdown `-12.47%`; both chronological halves were
  negative.
- Volume-breakout, 52-week-high, downside Donchian, Bollinger-reversion, and
  other screened variants were not admitted after failing stability or
  after-cost gates.

Earlier positive reversal results were invalid because the evaluator omitted
PaperOps' ten-calendar-day timeout and exact pending-order lifecycle. They must
not be cited as strategy evidence.

## Evidence boundary and promotion gate

The mechanical hypotheses are consistent with the broader literature on
testable continuation/reversal rules, but the literature does not validate
these Dawnstrike parameterizations. See [Brock, Lakonishok, and LeBaron
(1992)](https://www.jstor.org/stable/2328994), [Jegadeesh and Titman
(1999)](https://www.nber.org/papers/w7159), and the transaction-cost warning in
[Lesmond, Schill, and Zhou
(2004)](https://www.sciencedirect.com/science/article/pii/S0304405X0300206X).

Promotion remains blocked until each exact identity/version/fingerprint has at
least 30 eligible forward sessions, 30 exact closed trades, at least 95% source
coverage, non-overlapping walk-forward evidence, an untouched chronological
holdout, positive after-cost return, positive benchmark-relative excess, and
maximum drawdown no worse than -15%. Missing outcomes remain missing; they are
never converted to zero or a fabricated trade.

This is research/paper infrastructure only. No broker integration, live order
placement, or personalized investment recommendation is introduced.
