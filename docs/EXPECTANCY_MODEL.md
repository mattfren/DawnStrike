# Expectancy Model

The expectancy model estimates an expected paper return and a confidence score for each clean scanner candidate.

It is not a prediction guarantee, trade recommendation, or order system. It is a calibration layer for research and paper-audit review.

## Output

For each candidate:

- `expected_return_pct`: weighted expected paper return for the candidate's target exit.
- `confidence_pct`: confidence in the estimate, not probability of profit.
- `lower_return_pct` and `upper_return_pct`: a conservative likely range.
- `win_probability_pct`: weighted share of positive paper outcomes.
- `risk_adjusted_return_pct`: expected return discounted by confidence.
- `uncertainty_width_pct`: width of the low/high estimate band.
- `downside_risk_pct`: amount below zero inside the likely range.
- `sample_size`: number of paper-audit rows used.
- `effective_sample_size`: similarity-weighted sample size.
- `target_exit`: lunch, close, or blend.
- `model_basis`: whether the estimate is prior-only, sparse audit, early calibration, or empirical calibration.
- `confidence_tier`: plain-English quality tier for the estimate.
- `next_confidence_step`: the next data collection step that would improve confidence.

## Method

The model uses empirical shrinkage:

```text
expected_return =
  weighted_mean(score_prior, similar_paper_audit_returns)
```

The score prior is a conservative feature-derived baseline from:

- Dawnstrike score
- Range position
- Data quality
- Liquidity tier
- Risk flags

Paper-audit samples are weighted by similarity:

```text
similarity =
  exp(-score_gap / 28)
  * exp(-gap_gap / 130)
  * exp(-float_rotation_gap / 18)
  * signature_match_factor
  * liquidity_match_factor
```

Exact ticker paper-audit rows receive extra weight.

The return range uses weighted standard deviation, effective sample size, and an extra sparse-sample margin.

Confidence combines:

- similarity-weighted paper-audit sample size
- data quality
- outcome dispersion
- scanner score

Confidence is capped by sample size:

- 0 audits: max 18%
- 1-4 audits: max 38%
- 5-19 audits: max 58%
- 20-49 audits: max 74%
- 50+ audits: max 88%

This prevents the dashboard from pretending that a tiny sample is statistically certain.

## Risk-Adjusted Return

Risk-adjusted expectancy is deliberately simple:

```text
risk_adjusted_return = expected_return * (confidence_pct / 100)
```

This makes low-confidence estimates visually smaller even when the raw expected return is high.

## Confidence Roadmap

The model tells the operator what would improve confidence next:

- no audits: run paper audits for the current scanner output
- fewer than 5 audits: leave sparse-sample mode
- fewer than 20 audits: reach early calibration
- fewer than 50 audits: reach stronger calibration
- weak effective sample size: collect outcomes similar to today's setup profile
- wide range: collect more same-signature outcomes
