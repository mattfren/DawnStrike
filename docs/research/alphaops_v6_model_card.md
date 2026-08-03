# AlphaOps V6 model card

## Identity and purpose

`dawnstrike-alphaops-v6-shadow` is a research-only challenger. It estimates
activation, conditional after-cost benchmark-relative return, uncertainty, and
tail risk only from point-in-time, sourced paper evidence. It does not execute,
recommend, size, or promote trades.

## Inputs and target

Inputs are immutable candidate facts, feature snapshots, source-lineage hashes,
regime/setup cohort, safety vetoes, frozen cost assumptions, and decision-time
timestamps. The primary return target is after-cost excess return versus the
predeclared benchmark. Missing source bars, benchmark, fill, reconciliation, or
lineage make the target ineligible rather than zero.

## Model eligibility

| Eligible after-cost labels | Allowed research family |
|---:|---|
| Fewer than 100 | Cash/no-trade, frozen V5, empirical-Bayes shadow evidence only. |
| 100–499 | Regularized logistic/linear and empirical-Bayes baselines only. |
| 500+ and 60 dates | Controlled gradient-boosting challenger may compete. |

Any fitting requires a registered experiment, date-grouped purged expanding
folds, and an untouched holdout. Preprocessing is fit within each training fold.
No rank, post-decision high, later catalyst state, or random row split is an
eligible feature path.

## Frozen family-comparison rule

`dawnstrike.alphaops-v6.model-competition.v1` compares every permitted model
family on the same exact date-grouped, purged, embargoed out-of-fold decisions
and the same cost, benchmark, eligibility, and sampled-reject weighting rules.
The primary objective is the bootstrap lower 95% confidence bound of after-cost
benchmark-excess return; the tie-breaker is rank correlation. A challenger can
become a research winner only when it has a material positive primary-objective
improvement and is non-inferior on drawdown, CVaR, profit factor, turnover,
concentration, capacity, calibration, interval coverage, rank lift, 1.5x/2x
slippage, and segmented stability. Missing or failed evidence rejects the
challenger. A research winner never changes serving policy or V5.

## Output and safeguards

The conservative utility is expected net excess minus tail, uncertainty, and
capacity penalties. A safety veto yields no score. Automatic training receipts
are allowed; automatic production promotion is forbidden. Drift or calibration
failure may quarantine V6 to no-trade research output.

## Current limitation

`PERFORMANCE_STATUS=WAITING_FOR_FORWARD_EVIDENCE`. There are not enough
eligible forward labels to train or validate a V6 return model.
