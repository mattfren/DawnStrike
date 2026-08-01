"""Create hash-addressable V6 datasets from eligible immutable labels."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from intraday_scanner.alpha.v6.contracts import (
    DATASET_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION,
    canonical_hash,
    point_in_time_valid,
    utc_now,
)


def build_return_dataset(
    *, decisions: list[dict[str, Any]], labels: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a deterministic net-excess dataset, retaining exclusion counts."""

    decisions_by_id = {str(row.get("decision_id") or ""): row for row in decisions}
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    for label in labels:
        grouped[str(label.get("decision_id") or "")][str(label.get("label_family") or "")] = label
    rows: list[dict[str, Any]] = []
    exclusions: dict[str, int] = defaultdict(int)
    for decision_id, families in sorted(grouped.items()):
        decision = decisions_by_id.get(decision_id)
        target = families.get("benchmark_relative_excess_return")
        if decision is None:
            exclusions["decision_missing"] += 1
            continue
        if not point_in_time_valid(decision):
            exclusions["decision_not_point_in_time"] += 1
            continue
        if not target or target.get("learning_eligible") is not True:
            exclusions[str((target or {}).get("exclusion_reason") or "target_ineligible")] += 1
            continue
        target_value = _number(target.get("label_value"))
        if target_value is None:
            exclusions["target_missing"] += 1
            continue
        rows.append(
            {
                "decision_id": decision_id,
                "market_date": str(decision.get("market_date") or "")[:10],
                "ticker": decision.get("ticker"),
                "setup_key": decision.get("setup_key"),
                "regime_key": decision.get("regime_key"),
                "feature_vector": decision.get("feature_vector"),
                "target_net_excess_return_pct": target_value,
                "activation_label": _label_value(families.get("activation")),
                "tail_loss_label": _label_value(families.get("tail_loss_event")),
                "source_bar_hash_sha256": target.get("source_bar_hash_sha256"),
                "inclusion_probability": _inclusion_probability(decision),
            }
        )
    cutoff = max((str(row["market_date"]) for row in rows), default=None)
    content = {
        "rows": rows,
        "exclusions": dict(sorted(exclusions.items())),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "target": "benchmark_relative_excess_return",
        "training_cutoff": cutoff,
    }
    return {
        "dataset_id": "v6ds-" + canonical_hash(content)[:28],
        "schema_version": DATASET_SCHEMA_VERSION,
        "created_at": utc_now(),
        "training_cutoff": cutoff,
        "row_count": len(rows),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "dataset_hash_sha256": canonical_hash(content),
        "rows": rows,
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
    }


def _label_value(label: dict[str, Any] | None) -> float | None:
    return _number((label or {}).get("label_value"))


def _inclusion_probability(decision: dict[str, Any]) -> float | None:
    sampling = decision.get("rejected_sampling")
    return _number(sampling.get("inclusion_probability")) if isinstance(sampling, dict) else 1.0


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


__all__ = ["build_return_dataset"]
