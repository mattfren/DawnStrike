# AlphaOps Decision Logic

## Plain-English Summary

AlphaOps starts with the normal Dawnstrike scanner output, then asks a stricter
question: "Is this clean enough to alert, given risk, data quality, source
confidence, and what we have learned from real audited outcomes?" If the answer
is no, it sends no-clean-edge instead of forcing a pick.

## Signal Engine v3 Scoring

Signal Engine v3 scores canonical premarket rows with:

- gap curve
- liquidity thrust
- float rotation
- range control
- squeeze/catalyst
- execution quality
- data quality
- risk penalty

The scanner produces a score from 0 to 100, setup grade, risk flags, avoid
reasons, watch level, exit line, target, expected return bucket, confidence
bucket, and source lineage.

## AlphaOps v4 Scoring

AlphaOps v4 receives ranked scanner candidates and feature vectors. The
rule-first Alpha score combines:

- scanner/base score: 34 percent weight
- explosive score: 20 percent weight
- catalyst score: 12 percent weight
- execution score: 18 percent weight
- expected edge score: 16 percent weight
- source reliability adjustment

Then risk is applied:

```text
risk_adjusted_score = alpha_score * (risk_score / 100)
```

This means a technically strong setup can still become weak if risk is high.

## Features Used

Feature groups include:

- price and momentum
- liquidity and execution
- source/data quality
- catalyst
- risk
- structure
- playbook/setup

Examples:

- price
- gap percent
- dollar volume
- premarket volume
- spread
- range position
- float rotation
- short float
- catalyst category
- source confidence
- source reliability score
- stale data flag
- conflict flags
- risk flags
- setup grade

## Risk Penalties

### Hard Avoid

Hard avoid means the ticker should not be alerted. Current hard avoid reasons
include:

- current halt
- recent/active offering or dilution
- zero volume
- price below minimum
- stale source
- source conflict
- no source confidence
- low source confidence
- invalid ticker
- missing or invalid price
- extreme spread

Hard avoids set `can_alert` to false.

### Soft Penalties

Soft penalties reduce the risk score but do not always block the alert:

- unknown float
- missing previous close
- missing high
- missing low
- no catalyst
- public URL unverified
- low source count
- mega gap
- wide spread

## No-Trade Meaning

No-trade means "no clean watchlist today." It does not mean the app failed. It
means the system decided the current data did not justify an alert.

No-trade can happen when:

- sources returned no usable candidates
- source status is failed, empty, or no data
- all candidates are hard-avoid
- all clean candidates have low source confidence
- the top candidate risk score is too weak
- the Alpha score is below the minimum alert threshold
- drawdown risk is high

## Source Conflict

Source conflict means two or more sources reported materially different values
for the same ticker. Current conflict checks include:

- price differs by more than about 3 percent
- gap differs by more than 10 percentage points
- volume differs by more than about 50 percent

Source conflict is a hard avoid in the risk governor.

## Unknown Float

Unknown float is penalized because float rotation and squeeze quality depend on
share float. If float is missing, the system cannot tell whether premarket
volume is actually meaningful relative to supply.

Unknown float is currently a soft penalty.

## Missing Previous Close

Missing previous close lowers confidence because gap percent depends on it. If
previous close is missing, the row can still exist, but the system treats the
gap/read as less reliable.

## Source Reliability

Source reliability starts from source collection quality:

- rows returned
- rows normalized
- rows rejected
- stale count
- missing critical fields

After outcomes exist, source reliability can incorporate outcome counts and
winner counts. A low source reliability score hurts AlphaOps scoring; a high
score can help slightly.

## Setup Memory

Setup memory groups past outcomes by setup key:

```text
grade:<grade>|gap:<bucket>|volume:<bucket>|catalyst:<category>
```

It stores sample size, average return, median return, win rate, and outlier
dependency. AlphaOps uses this only as evidence grows.

## Before 20 Real Days

Before 20 audited real market days:

- expectancy status is `INSUFFICIENT_SAMPLE`
- expected return buckets are not trusted as empirical proof
- rule-first scoring still works
- features/signals/outcomes are persisted for future learning
- dashboard/report warnings should stay visible

## After 20+ Real Days

After 20 audited real market days:

- empirical priors can start affecting expected edge
- setup/source/risk buckets become more useful
- still watch for outlier dependency
- still avoid profitability claims if the evidence is weak

## After 60+ Real Days

After 60 audited real market days:

- evidence is stronger
- source/setup/risk buckets are more meaningful
- model tuning can be more defensible
- outliers and regime changes still matter

## Offline ML Guardrail

The code contains a small deterministic offline model, but it only activates if:

- at least 80 usable outcome rows exist
- at least 20 dated outcome days exist
- date-ordered train/test evaluation beats the rule baseline

If it does not beat the rule baseline, it is rejected and AlphaOps remains
rule-first.

## Decision Tree

```text
1. Did sources return candidates?
   - No: send no-clean-edge/source check.
   - Yes: continue.

2. Are rows fresh and usable?
   - No: send no-clean-edge/source check.
   - Yes: continue.

3. Are candidates hard-avoid?
   - All hard-avoid: send no-clean-edge.
   - Some clean: continue.

4. Is source confidence high enough?
   - No: send no-clean-edge.
   - Yes: continue.

5. Is risk acceptable?
   - No: send no-clean-edge.
   - Yes: continue.

6. Is there enough edge?
   - No: send no-clean-edge.
   - Yes: continue.

7. If yes, send watchlist.

8. If no, send no-clean-edge.
```
