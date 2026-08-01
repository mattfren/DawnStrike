# AlphaOps V6 experiment registry

Status: `NO_V6_EXPERIMENT_APPLIED`

An AlphaOps V6 experiment can only be registered after data supports a single,
frozen, forward-only hypothesis. Registering an experiment never changes V5,
changes a live policy, routes an order, or promotes a challenger.

## Contract

Each experiment must persist all of the following before it begins:

| Required field | Rule |
|---|---|
| Hypothesis | Concrete and falsifiable. |
| Training cutoff | Strictly earlier than validation and holdout starts. |
| Configuration hash | Candidate configuration is immutable. |
| Changed field | Exactly one field differs from the frozen control. |
| Unchanged controls | Every other control is listed. |
| Validation interval | Date-grouped, purged, embargoed forward window. |
| Untouched holdout | Evaluated once only after the experiment is frozen. |
| Stop condition | Safety, drawdown, calibration, or data-quality stop. |
| Promotion requirements | Full V6 gate plus manual operator review. |

## Current registry

| Experiment | State | Reason |
|---|---|---|
| V6 candidate-policy change | Not registered | No sourced V6 forward labels support a one-change hypothesis. |
| V6 threshold change | Not registered | Safety gates cannot be lowered to create picks. |
| V6 model-family change | Not registered | Fewer than 100 eligible after-cost return labels blocks a return model. |

The legacy `learning_backfeed_events` rows are hypotheses, not approved
changes. Their presence cannot change a production weight. V4/V5 and PaperOps
experiments retain their own immutable contracts and are not retroactively
reinterpreted as V6 evidence.
