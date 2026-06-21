# Dawnstrike Signal Engine v3

`dawnstrike-signal-engine-v3` is an explainable local research ranking model for
aggressive intraday momentum discovery. It ranks candidates for review; it does
not claim profitability or place orders.

## Output Contract

Each candidate row includes:

- `total_score`
- `explosive_score`
- `tradability_score`
- `catalyst_score`
- `risk_score`
- `data_quality_score`
- `expected_return_bucket`
- `confidence_bucket`
- `invalidation_level`
- `breakout_trigger`
- `pullback_zone`
- `first_target`
- `stretch_target`
- `exit_bias`
- `risk_flags`
- `avoid_reasons`
- `source_lineage`
- `model_version`
- `config_hash`

## Component Logic

- Momentum/gap quality: gap curve, position in premarket range, and mega-gap
  exhaustion penalties.
- Liquidity/tradability: dollar volume, share volume, price band, spread, and
  execution-quality penalty.
- Volume abnormality: float rotation when float exists. Unknown float is a risk
  penalty, not a fabricated rotation.
- Catalyst quality: category and confidence from headline text and known risk
  terms.
- Risk penalties: current halt, recent offering, reverse split, sub-min price,
  wide spread, stale source, missing required fields, low volume, low source
  confidence, and unverified public URL data.
- Source quality: every row carries source, URL when available, extraction mode,
  source timestamp, extraction timestamp, stale flag, and confidence.

## Expected Return Buckets

When historical outcomes are sparse, Dawnstrike uses rule-based buckets only:
`HIGH_UPSIDE`, `MEDIUM_UPSIDE`, `LOW_CONFIDENCE`, or `AVOID`. With at least 20
similar persisted outcomes, it uses empirical priors and labels uncertainty.
