# Dawnstrike Formula

Dawnstrike uses a versioned research-ranking equation:

```text
score =
  gap_curve
  + liquidity_thrust
  + float_rotation
  + range_control
  + squeeze_catalyst
  + execution_quality
  + data_quality
  - risk_penalty
```

The current equation version is `dawnstrike-signal-engine-v3`. Scores are
clamped to `0-100`.

This is not a return forecast, trading recommendation, or order engine. It is a prioritization model for aggressive intraday research and paper-audit review.

The Premarket Intelligence Layer sits on top of this score. It assigns a
confirmation-first action label from catalyst quality, structure, liquidity,
float rotation, price band, and risk flags; score alone is not enough for a
trade classification.

## Components

- `gap_curve`: rewards meaningful premarket gaps while reducing credit for extreme, less credible gaps.
- `liquidity_thrust`: combines premarket dollar volume and share volume.
- `float_rotation`: estimates how much of the reported float has already traded
  premarket when float is known. Unknown float is penalized, not fabricated.
- `range_control`: rewards names holding near premarket highs while penalizing disorderly wide ranges.
- `squeeze_catalyst`: combines news, short float, and float scarcity.
- `execution_quality`: rewards tighter spreads and prices inside the configured price band.
- `data_quality`: 0-100 score for required market and metadata inputs.
- `risk_penalty`: hard brake for halt, offering, no previous close, zero
  volume, wide spread, extreme gap, bad price band, stale source, low source
  confidence, and low liquidity conditions.

## Added Output Fields

- `float_rotation_pct`
- `range_position_pct`
- `data_quality_score`
- `liquidity_tier`
- `setup_grade`
- `volatility_signature`
- `equation_version`
- `action`
- `catalyst_tier`
- `premarket_structure`
- `entry_trigger`
- `invalidation`
- `target_1`
- `target_2`
- `data_confidence_score`
- `data_warnings`
- `source_lineage`
- `expected_return_bucket`
- `confidence_bucket`
- `model_version`
- `config_hash`
- `historical_win_rate`
- `similar_setup_count`

## Calibration Path

The next level is not adding more constants. It is collecting paper-audit outcomes by formula version and tuning weights against actual intraday behavior:

1. Persist every scan and paper-audit result.
2. Group results by `equation_version`.
3. Track lunch, close, and high-after-entry outcomes by score band and signature.
4. Adjust component weights only after enough samples exist.
5. Keep a holdout date range so improvements are not fitted to noise.
