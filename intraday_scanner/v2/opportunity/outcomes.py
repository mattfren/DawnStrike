"""Explicit downstream-only public facade for causal outcome labeling.

Import this submodule directly.  The opportunity package root deliberately does
not import it, preserving the future-label boundary for real-time code.
"""

from intraday_scanner.v2.opportunity.outcome_contracts import (
    CANONICAL_OUTCOME_METRICS,
    OutcomeAmbiguityPolicy,
    OutcomeCompleteness,
    OutcomeCostSource,
    OutcomeEntryRule,
    OutcomeEntryStatus,
    OutcomeHorizon,
    OutcomeHorizonKind,
    OutcomeLabelPolicy,
    OutcomeMarketStatusKind,
    OutcomeMetric,
    OutcomeNumericEvidence,
    OutcomePathStatus,
    OutcomeReferencePriceKind,
    OutcomeTouchInterval,
    OutcomeUnit,
    OutcomeValueStatus,
    build_outcome_horizon,
    build_outcome_label_policy,
)
from intraday_scanner.v2.opportunity.outcome_records import OutcomeRecord
from intraday_scanner.v2.opportunity.outcome_replay import (
    OutcomeLabelBatch,
    label_pipeline_outcomes,
)
from intraday_scanner.v2.opportunity.outcome_sources import (
    OutcomeBarEvidence,
    OutcomeObservationDataset,
    OutcomeObservationSeries,
    build_outcome_bar_evidence,
    build_outcome_observation_dataset,
    build_outcome_observation_series,
)

__all__ = [
    "CANONICAL_OUTCOME_METRICS",
    "OutcomeAmbiguityPolicy",
    "OutcomeBarEvidence",
    "OutcomeCompleteness",
    "OutcomeCostSource",
    "OutcomeEntryRule",
    "OutcomeEntryStatus",
    "OutcomeHorizon",
    "OutcomeHorizonKind",
    "OutcomeLabelBatch",
    "OutcomeLabelPolicy",
    "OutcomeMarketStatusKind",
    "OutcomeMetric",
    "OutcomeNumericEvidence",
    "OutcomeObservationDataset",
    "OutcomeObservationSeries",
    "OutcomePathStatus",
    "OutcomeRecord",
    "OutcomeReferencePriceKind",
    "OutcomeTouchInterval",
    "OutcomeUnit",
    "OutcomeValueStatus",
    "build_outcome_bar_evidence",
    "build_outcome_horizon",
    "build_outcome_label_policy",
    "build_outcome_observation_dataset",
    "build_outcome_observation_series",
    "label_pipeline_outcomes",
]
