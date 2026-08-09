"""Model-family eligibility, deliberately gated by real forward evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from intraday_scanner.alpha.v6.contracts import canonical_hash

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
    retrospective_research_eligible_count: int = 0
    prospective_promotion_eligible_count: int = 0
    exact_exclusions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "allowed_families": list(self.allowed_families),
            "eligible_label_count": self.eligible_label_count,
            "forward_date_count": self.forward_date_count,
            "reason": self.reason,
            "retrospective_research_eligible_count": self.retrospective_research_eligible_count,
            "prospective_promotion_eligible_count": self.prospective_promotion_eligible_count,
            "exact_exclusions": list(self.exact_exclusions),
            "research_only": True,
            "broker_execution_enabled": False,
        }


def model_eligibility(dataset_rows: list[dict[str, Any]]) -> ModelEligibility:
    """Select only research model families whose data threshold is met."""

    labels = len(dataset_rows)
    dates = len({str(row.get("market_date") or "")[:10] for row in dataset_rows})
    research_rows = sum(
        1 for row in dataset_rows if row.get("retrospective_research_eligible") is True
    )
    promotion_rows = sum(
        1 for row in dataset_rows if row.get("prospective_promotion_eligible") is True
    )
    if labels < MIN_RETURN_MODEL_LABELS:
        return ModelEligibility(
            status="NOT_TRAINED_INSUFFICIENT_LABELS",
            allowed_families=("cash_no_trade", "frozen_v5", "empirical_bayes_shadow"),
            eligible_label_count=labels,
            forward_date_count=dates,
            reason="Fewer than 100 eligible after-cost return labels.",
            retrospective_research_eligible_count=research_rows,
            prospective_promotion_eligible_count=promotion_rows,
            exact_exclusions=(
                "return_model_minimum_100_eligible_rows_not_met",
                "insufficient_labels_are_not_imputed_or_subsampled",
            ),
        )
    if labels < MIN_GRADIENT_BOOSTING_LABELS or dates < MIN_GRADIENT_BOOSTING_DATES:
        return ModelEligibility(
            status="RESEARCH_BASELINES_ONLY",
            allowed_families=("regularized_logistic", "regularized_linear", "empirical_bayes"),
            eligible_label_count=labels,
            forward_date_count=dates,
            reason="Complexity gate blocks gradient boosting until 500 labels and 60 dates.",
            retrospective_research_eligible_count=research_rows,
            prospective_promotion_eligible_count=promotion_rows,
            exact_exclusions=(
                "gradient_boosting_minimum_500_labels_or_60_dates_not_met",
            ),
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
        retrospective_research_eligible_count=research_rows,
        prospective_promotion_eligible_count=promotion_rows,
    )


def evidence_lineage(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize immutable evidence identity carried through V6 receipts.

    This helper is intentionally tolerant of historical field names.  It never
    turns an absent artifact or eligibility dimension into a fabricated value.
    """

    artifact_hashes = _text_list(payload.get("source_artifact_hashes"))
    for key in ("source_artifact_hash_sha256", "source_bar_hash_sha256"):
        value = _text(payload.get(key))
        if value and value not in artifact_hashes:
            artifact_hashes.append(value)
    benchmark_hash = _text(payload.get("benchmark_hash_sha256")) or _text(
        payload.get("benchmark_artifact_hash_sha256")
    ) or _text(payload.get("benchmark_source_bar_hash_sha256"))
    lineage = {
        "source_artifact_hash_sha256": artifact_hashes[0] if artifact_hashes else None,
        "source_artifact_hashes": sorted(artifact_hashes),
        "path_replay_id": _text(payload.get("path_replay_id")),
        "benchmark_hash_sha256": benchmark_hash,
        "observed_cost_model_identity": _identity(
            payload.get("observed_cost_model_identity")
            or payload.get("observed_cost_model_version")
            or payload.get("observed_cost_model_id")
            or payload.get("observed_cost_model")
        ),
        "modeled_cost_model_identity": _identity(
            payload.get("modeled_cost_model_identity")
            or payload.get("modeled_cost_model_version")
            or payload.get("modeled_cost_model_id")
            or payload.get("modeled_cost_model")
        ),
        "evidence_cohort": _text(payload.get("evidence_cohort"))
        or _text(payload.get("cohort_id"))
        or _text(payload.get("cohort")),
        "retrospective_research_eligible": payload.get(
            "retrospective_research_eligible"
        )
        is not False,
        "prospective_promotion_eligible": payload.get(
            "prospective_promotion_eligible"
        )
        is True,
    }
    lineage["evidence_lineage_hash_sha256"] = canonical_hash(lineage)
    return lineage


def _identity(value: object) -> str | None:
    if isinstance(value, dict):
        return "identity-" + canonical_hash(value)
    return _text(value)


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _text_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({text for item in value if (text := _text(item))})


__all__ = [
    "MIN_GRADIENT_BOOSTING_DATES",
    "MIN_GRADIENT_BOOSTING_LABELS",
    "MIN_RETURN_MODEL_LABELS",
    "ModelEligibility",
    "evidence_lineage",
    "model_eligibility",
]
