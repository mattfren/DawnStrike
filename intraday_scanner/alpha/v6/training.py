"""Versioned, optional-dependency training receipts for the V6 challenger."""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.v6.contracts import (
    ALPHAOPS_V6_MODEL_VERSION,
    canonical_hash,
    utc_now,
)
from intraday_scanner.alpha.v6.models import model_eligibility


def train_shadow_challengers(dataset: dict[str, Any], *, code_sha: str) -> dict[str, Any]:
    """Produce a bounded training receipt; never train below evidence gates.

    The static operator deployment does not require scikit-learn.  When the
    optional local research package is unavailable, enough-data runs report the
    dependency blocker instead of silently falling back to an untracked model.
    """

    rows = list(dataset.get("rows") or [])
    eligibility = model_eligibility(rows).to_dict()
    base = {
        "model_version": ALPHAOPS_V6_MODEL_VERSION,
        "trained_at": utc_now(),
        "training_cutoff": dataset.get("training_cutoff"),
        "dataset_id": dataset.get("dataset_id"),
        "dataset_hash_sha256": dataset.get("dataset_hash_sha256"),
        "feature_schema_version": dataset.get("feature_schema_version"),
        "code_sha": code_sha,
        "eligibility": eligibility,
        "research_only": True,
        "broker_execution_enabled": False,
        "automatic_promotion": False,
    }
    if eligibility["status"] == "NOT_TRAINED_INSUFFICIENT_LABELS":
        return _receipt(base, status="NOT_TRAINED_INSUFFICIENT_LABELS", artifact=None)
    try:
        import sklearn  # type: ignore[import-not-found]  # Optional research extra.
    except ImportError:
        return _receipt(base, status="BLOCKED_RESEARCH_DEPENDENCY", artifact=None)
    artifact = {
        "library": "scikit-learn",
        "library_version": str(getattr(sklearn, "__version__", "unknown")),
        "candidate_families": eligibility["allowed_families"],
        "fitted": False,
        "reason": "Fitting requires registered purged folds and a frozen experiment.",
    }
    return _receipt(base, status="TRAINING_PLAN_REGISTERED", artifact=artifact)


def _receipt(
    base: dict[str, Any], *, status: str, artifact: dict[str, Any] | None
) -> dict[str, Any]:
    payload = {**base, "status": status, "artifact": artifact}
    payload["model_run_id"] = "v6m-" + canonical_hash(payload)[:28]
    payload["model_artifact_hash_sha256"] = canonical_hash(artifact) if artifact else None
    return payload


__all__ = ["train_shadow_challengers"]
