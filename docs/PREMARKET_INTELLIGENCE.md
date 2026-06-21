# Premarket Intelligence Layer

Dawnstrike classifies each ticker as a research/watchlist cue, not an order or
profit claim. The allowed labels are:

- `WATCH`
- `BREAKOUT WATCH`
- `HIGH VOLATILITY WATCH`
- `CAUTION`
- `AVOID`
- `INVALIDATED`
- `THESIS BROKEN`
- `OUTCOME NEEDED`

The classifier combines the Signal Engine v3 score, catalyst category, premarket
structure, float rotation, liquidity, spread, price band, halt status, dilution
flags, stale-source flags, and source confidence.

## Catalyst Categories

Rows include `catalyst_tier`, `catalyst_category`, `catalyst_summary`,
`catalyst_confidence`, and `catalyst_risk_flags`.

- `confirmed_catalyst`: FDA/clinical, M&A, earnings, major contract, or other
  concrete event language.
- `soft_catalyst`: partnerships, product launches, guidance, analyst actions, or
  vague corporate updates.
- `sympathy_momentum`: theme or social momentum such as AI, semis, nuclear,
  crypto, defense, quantum, robotics, or biotech.
- `no_clear_catalyst`: no verified headline or only low-quality PR context.
- `dilution_risk`: offering, shelf, ATM, warrant, registered direct, or private
  placement language.
- `legal/regulatory_risk`: investigation, lawsuit, delisting, regulatory action,
  subpoena, or clinical-hold language.

## Plan Fields

Each row includes `entry_trigger`, `confirmation_needed`, `invalidation`,
`target_1`, `target_2`, `risk_level`, `why_this_matters`, and
`do_not_enter_if`. These fields are operator review levels only. Weak or unsafe
setups are labeled `AVOID` and say manual review only.

## Data Quality

Rows include `data_confidence_score`, `data_warnings`, `field_sources`,
`source_lineage`, `source_confidence`, and `stale_data_flag`. Missing float,
short interest, catalyst, relative volume, VWAP, SEC risk, or halt enrichment
lowers confidence instead of being fabricated.

## Outcome Evaluation

After manual outcomes are uploaded, run:

```powershell
py -m intraday_scanner.cli evaluate-intelligence-outcomes `
  --db-path data\shadow.sqlite `
  --out-dir outputs\intelligence_outcomes `
  --persist
```

If fewer than 20 similar historical samples exist, probability fields show
`insufficient sample size`. With enough persisted outcomes, Dawnstrike reports
historical priors only and still labels uncertainty.
