# Mover Pattern Research Register

## Research position

No primary evidence supports a universal rule such as “large gap plus high
volume equals buy.” Price, flow, catalyst provenance, liquidity, corporate
actions, and regime must be studied separately, with transaction costs and
point-in-time availability enforced.

The immediate Dawnstrike hypotheses are deliberately narrow and start in
forward paper observation:

| Hypothesis | Use | Dawnstrike treatment |
|---|---|---|
| Abnormal same-clock flow can strengthen an opening drive | Candidate feature | Frozen in mover_opening_drive_rvol_v1 |
| A verified catalyst can distinguish some gaps from attention-only gaps | Candidate feature | Frozen in mover_verified_catalyst_gap_hold_v1 |
| Positive overnight attention can reverse after the open | Veto rationale | Do not chase an unverified opening gap |
| VWAP retention can confirm another setup | Confirmation only | Never treated as standalone proven alpha |
| Reverse splits, offerings, halts, and wide spreads distort apparent edge | Hard gates | Blocked, not softly penalized |
| Momentum can fail abruptly in crash regimes | Regime and drawdown control | Chronological validation and independent books |

## Primary sources

- Gao, Han, Li, and Zhou, [Market Intraday Momentum](https://www.sciencedirect.com/science/article/abs/pii/S0304405X18301351):
  the market's first-half-hour return predicts its last-half-hour return more
  strongly in several high-volume, high-volatility, and news regimes. This is
  market/ETF evidence, not proof for individual low-float movers.
- Heston, Korajczyk, and Sadka,
  [Intraday Patterns in the Cross-section of Stock Returns](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.2010.01573.x):
  documents same-clock continuation across days and shorter-horizon reversals.
- Berkman et al.,
  [Paying Attention: Overnight Returns and the Hidden Cost of Buying at the Open](https://www.cambridge.org/core/journals/journal-of-financial-and-quantitative-analysis/article/abs/paying-attention-overnight-returns-and-the-hidden-cost-of-buying-at-the-open/F9AAD159B512C651F09D5D52011D88E0):
  supports confirmation and execution-cost controls for high-attention gaps.
- Lee and Swaminathan,
  [Price Momentum and Trading Volume](https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00280):
  past turnover affects momentum magnitude and persistence, but at a longer
  horizon than Dawnstrike's same-day setup.
- Chan,
  [Stock Price Reaction to News and No-News](https://doi.org/10.1016/S0304-405X(03)00146-6):
  public-news moves and extreme no-news moves can exhibit different drift and
  reversal behavior.
- Patell and Wolfson,
  [Intraday Speed of Adjustment to Earnings and Dividend Announcements](https://www.gsb.stanford.edu/faculty-research/publications/intraday-speed-adjustment-stock-prices-earnings-dividend-announcements):
  news reactions can occur within minutes, making publication and first-seen
  timestamps mandatory.
- SEC, [EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces):
  authoritative filing lineage for timestamped catalyst verification.
- SEC, [VWAP definition](https://www.sec.gov/rules-regulations/2001/08/method-determining-market-capitalization-dollar-value-average-daily-trading-volume-application):
  VWAP is a calculation and execution benchmark, not proof of predictive alpha.
- FINRA, [Extended-Hours Trading Risks](https://www.finra.org/investors/insights/extended-hours-trading):
  lower liquidity, greater volatility, unlinked venues, and partial or missing
  fills require separate premarket execution assumptions.
- FINRA, [Stock Splits](https://www.finra.org/investors/investing/investment-products/stocks/stock-splits)
  and [LULD Guardrails](https://www.finra.org/investors/insights/guardrails-market-volatility):
  support explicit corporate-action and trading-pause controls.
- Chabot, Ghysels, and Jagannathan,
  [Momentum Trading and Predictable Crashes](https://www.nber.org/papers/w20660):
  momentum has material crash exposure.
- Harvey, Liu, and Zhu,
  [The Cross-Section of Expected Returns](https://www.nber.org/papers/w20592),
  and Bailey et al.,
  [The Probability of Backtest Overfitting](https://papers.ssrn.com/sol3/Papers.cfm?abstract_id=2326253):
  motivate multiple-testing controls, locked holdouts, and keeping every
  attempted specification in the research record.

## Claims Dawnstrike must not make

- A high win rate alone means the strategy works.
- The closing top-gainer rank was knowable at the entry time.
- Missing outcomes are flat trades.
- A public daily bar proves intraday path or executable fills.
- A backtest-selected threshold is validated forward.
- A higher historical return guarantees a future return.
