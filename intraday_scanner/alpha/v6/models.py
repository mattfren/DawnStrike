"""Model-family eligibility, deliberately gated by real forward evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MIN_RETURN_MODEL_LABELS = 100
MIN_GRADIENT_BOOSTING_LABELS = 500
MIN_GRADIENT_BOOSTING_DATES = 60


@dataclass(frozen=True)
class ModelEligibility:
    status: str
    allowed_families: tuple[str, ...]
    eligible_label_count: int
    forward_date_count: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "allowed_families": list(self.allowed_families),
            "eligible_label_count": self.eligible_label_count,
            "forward_date_count": self.forward_date_count,
            "reason": self.reason,
            "research_only": True,
            "broker_execution_enabled": False,
        }


def model_eligibility(dataset_rows: list[dict[str, Any]]) -> ModelEligibility:
    """Select only research model families whose data threshold is met."""

    labels = len(dataset_rows)
    dates = len({str(row.get("market_date") or "")[:10] for row in dataset_rows})
    if labels < MIN_RETURN_MODEL_LABELS:
        return ModelEligibility(
            status="NOT_TRAINED_INSUFFICIENT_LABELS",
            allowed_families=("cash_no_trade", "frozen_v5", "empirical_bayes_shadow"),
            eligible_label_count=labels,
            forward_date_count=dates,
            reason="Fewer than 100 eligible after-cost return labels.",
        )
    if labels < MIN_GRADIENT_BOOSTING_LABELS or dates < MIN_GRADIENT_BOOSTING_DATES:
        return ModelEligibility(
            status="RESEARCH_BASELINES_ONLY",
            allowed_families=("regularized_logistic", "regularized_linear", "empirical_bayes"),
            eligible_label_count=labels,
            forward_date_count=dates,
            reason="Complexity gate blocks gradient boosting until 500 labels and 60 dates.",
        )
    return ModelEligibility(
        status="CONTROLLED_CHALLENGERS_ALLOWED",
        allowed_families=(
            "regularized_logistic",
            "regularized_linear",
            "empirical_bayes",
            "controlled_gradient_boosting",
        ),
        eligible_label_count=labels,
        forward_date_count=dates,
        reason="Research-only challenger eligibility; promotion remains manual.",
    )


__all__ = [
    "MIN_GRADIENT_BOOSTING_DATES",
    "MIN_GRADIENT_BOOSTING_LABELS",
    "MIN_RETURN_MODEL_LABELS",
    "ModelEligibility",
    "model_eligibility",
]
