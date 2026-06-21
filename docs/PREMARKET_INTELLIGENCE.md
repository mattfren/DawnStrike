# Premarket Intelligence Layer

Dawnstrike now classifies each scanned ticker into one operational research
action. The label is not a broker order, trading instruction, or proof of
future return. It is a paper-research workflow cue for what to review after the
open.

## Action Labels

Every candidate row includes exactly one primary action:

- `🟢 Opening Breakout Candidate`
- `🔥 Momentum Continuation Watch`
- `👀 Watch Only`
- `🟡 Needs Confirmation`
- `❌ Avoid / Gap-and-Crap Risk`

The classifier does not rely on score alone. It combines catalyst tier, gap,
premarket volume, dollar volume, float rotation, premarket high/low structure,
spread/liquidity risk, price band, halt risk, and offering/dilution flags when
available.

## Catalyst Tiers

- Tier A: FDA approval or fast-track style catalyst, positive clinical data,
  acquisition/buyout, major earnings beat, major contract, or strategic
  investment.
- Tier B: partnership, contract, supply agreement, product launch, guidance
  update, analyst upgrade, expansion, or collaboration.
- Tier C: vague update, social hype, recycled news, paid-promotion style PR, or
  no clear catalyst.

Rows include `catalyst_tier`, `catalyst_summary`, `catalyst_confidence`, and
`catalyst_risk_flags`.

## Opening Plan

Each row includes:

- `entry_trigger`
- `confirmation_needed`
- `invalidation`
- `target_1`
- `target_2`
- `risk_level`
- `why_this_matters`
- `do_not_buy_if`

The plan is confirmation-first. Weak setups output `No trade unless structure
improves.` Strong setups use levels above the premarket high or opening-range
confirmation. The code does not generate casual premarket buy-now language.

## Data Quality

Rows also include `data_confidence_score`, `data_warnings`, and `field_sources`.
Missing float, catalyst, VWAP, relative volume, or enrichment fields do not
crash the scan. Fixture/sample/stale data is explicitly flagged.

## Outcome Evaluation

After manual outcomes are uploaded, run:

```powershell
py -m intraday_scanner.cli evaluate-intelligence-outcomes `
  --db-path data\shadow.sqlite `
  --out-dir outputs\intelligence_outcomes `
  --persist
```

The evaluator joins saved recommendations to outcome prices and records:

- breakout trigger result
- max gain after trigger
- max drawdown after trigger
- stop hit
- target 1/2 hit
- actual outcome

If enough similar historical samples exist, future scans show historical win
rate, average max gain, average drawdown, and similar setup count. If not, the
fields say `Not enough history yet`.
