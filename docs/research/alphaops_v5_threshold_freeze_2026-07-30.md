# AlphaOps v5 Threshold Freeze — 2026-07-30

This record freezes the AlphaOps v5 thresholds using only evidence available
before the `2026-07-31T00:00:00-04:00` activation boundary. Forward results
must not be used to retune this contract.

## What failed in v4

The historical ledger contains 8 closed positions: 2 wins, 6 losses,
`-$487.61` gross P&L, `-6.0953%` summed trade-allocation return, median trade
return `-2.9135%`, and profit factor `0.2639`. These are trade-allocation gross
figures, not proven simulated-account returns.

BIYA lost `47.2626%` and caused about `71.34%` of gross losses. Removing BIYA
would leave roughly `-0.2141%`, so one uncapped tail loss explains most of the
damage, but it does not excuse the weak contract.

Of 42 published selections:

| Selection path | Count |
|---|---:|
| `probability_fallback` | 21 |
| `no_trade` | 13 |
| `legacy_body_recovered` | 8 |
| `clean_edge` | 0 |

All eight fills came from low-edge C/D setups marked
`NEEDS_CONFIRMATION`, required manual confirmation, had data confidence 25,
source confidence mostly 22–34.5, no clear catalyst, and six or seven risk
flags. SLND entered after the regular close. SKYQ entered about five minutes
before forced liquidation. The v4 process therefore failed at admission,
timing, tail-risk control, and measurement—not just symbol selection.

## Pre-cutoff distributions

The usable enriched subset contains 29 rows unless noted:

| Field | Min | P25 | Median | P75 | Max | Observation |
|---|---:|---:|---:|---:|---:|---|
| Gap % | 15.06 | 23.51 | 32.38 | 61.50 | 160.62 | Extreme right tail |
| Invalidation risk % (`n=25`) | 9.2231 | 25.2061 | 37.1770 | 42.3544 | 51.4541 | Existing stops were structurally too wide |
| Premarket dollar volume | $0.782m | $22.326m | $48.526m | $107.253m | $328.332m | Low tail needs a floor |
| Premarket volume | 115,739 | — | 9,826,906 | — | 70,497,803 | Wide liquidity dispersion |
| Range position % | — | — | 70.44 | — | — | Entries tended to chase the upper range |
| Source confidence (`n=31`) | 22 | 30 | 34.5 | 34.5 | 80 | Almost every row fails a credible-source bar |
| Data confidence | 25 | 25 | 25 | 25 | 25 | Uniformly weak |
| Source count | — | — | 2 | — | — | 27 rows had 2; 2 rows had 1 |

Spread evidence was absent. Every usable row had unknown/unverified float;
most had unverified halt and SEC status. All 29 had five missing enrichment
fields. The old execution score nevertheless had a median near 99, proving
that it measured field presence or arithmetic completion rather than actual
feasibility. The planned reward/risk clustered at exactly 1.5 because target
was manufactured from risk.

## Frozen thresholds and rationale

| Policy | Frozen value | Why |
|---|---:|---|
| Position risk | 0.25% of simulated equity | Half the authorized ceiling; caps single-trade damage while v5 earns evidence |
| Symbol notional | 10% of simulated equity | Prevents concentration from bypassing risk sizing |
| After-cost reward/risk | 1.50 minimum | Tested only after independently derived target and modeled costs |
| Stop distance | 15% maximum | Above the historical minimum but rejects most v4-style 25–51% invalidations |
| Gap | 50% maximum | Below the pre-cutoff P75 and blocks the extreme tail that contained BIYA-like risk |
| Chase | 2% maximum | Prevents late upper-range entries; no pre-cutoff chase field was trustworthy enough for finer tuning |
| Premarket dollar volume | $1m minimum | Excludes the observed low tail while remaining deliberately permissive pending forward evidence |
| Spread | 200 bps maximum | Fail-closed because v4 had no spread evidence; wide enough for small-cap research, not a claim of cheap execution |
| Source confidence | 80 minimum | Only the top observed confidence reached 80; weak-source rows must not become official |
| Source count | 2 minimum | Matches the pre-cutoff median and rejects single-source decisions |
| Quote age | 360 seconds maximum | Prevents stale entry decisions while accommodating the five-minute monitor cadence |
| Entry window | 09:30–15:30 ET | Blocks after-close and near-liquidation entries |
| Modeled slippage | 50 bps per side | Conservative prospective paper assumption until sourced fill truth supports revision |
| Commission | $0.005/share/side | Explicit modeled cost; never presented as a real broker fee |

Float must be positive and sourced or verified. Catalyst must include text,
URL, and passing status or A/B tier. Halt, SEC-risk, and corporate-action
statuses must be explicit and passing. Missing evidence blocks; it does not
receive a neutral value.

## Interpretation

These thresholds are safety and truth controls, not evidence that v5 has
positive alpha. They deliberately prefer no official trade over a weak trade.
Any future policy change requires a new version, a new prospective activation
boundary, a frozen one-change hypothesis, and an untouched holdout. Existing
v5 decisions and outcomes must remain attributable to this exact contract.
