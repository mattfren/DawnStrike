"""Create hash-addressable V6 datasets from eligible immutable labels."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_RETURN_TRUTH,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.path_replay import ELIGIBILITY_POLICY_VERSION
from intraday_scanner.alpha.v6.contracts import (
    DATASET_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION,
    LABEL_SCHEMA_VERSION,
    canonical_hash,
    point_in_time_valid,
    utc_now,
)
from intraday_scanner.alpha.v6.models import evidence_lineage
from intraday_scanner.alpha.v6.validation import catalyst_ablation_plan


def build_return_dataset(
    *, decisions: list[dict[str, Any]], labels: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a deterministic net-excess dataset, retaining exclusion counts."""

    decisions_by_id = {str(row.get("decision_id") or ""): row for row in decisions}
    grouped: dict[str, dict[str, Any]] = defaultdict(dict)
    exclusions: dict[str, int] = defaultdict(int)
    for label in labels:
        decision_id = str(label.get("decision_id") or "")
        decision = decisions_by_id.get(decision_id)
        if decision is None or not _current_label(label, decision=decision):
            exclusions["legacy_or_incomplete_label_quarantined"] += 1
            continue
        family = str(label.get("label_family") or "")
        existing = grouped[decision_id].get(family)
        if existing is not None and canonical_hash(existing) != canonical_hash(label):
            raise ValueError(
                f"conflicting current V6 labels for {decision_id}:{family}"
            )
        grouped[decision_id][family] = label
    rows: list[dict[str, Any]] = []
    activation_rows: list[dict[str, Any]] = []
    for decision_id, families in sorted(grouped.items()):
        decision = decisions_by_id.get(decision_id)
        target = families.get("benchmark_relative_excess_return")
        activation = families.get("activation")
        if decision is None:
            exclusions["decision_missing"] += 1
            continue
        if not point_in_time_valid(decision):
            exclusions["decision_not_point_in_time"] += 1
            continue
        common = _dataset_row(decision_id, decision, families)
        if activation and activation.get("learning_eligible") is True:
            activation_value = _number(activation.get("label_value"))
            if activation_value in {0.0, 1.0}:
                activation_rows.append({**common, "activation_label": activation_value})
        if not target or target.get("learning_eligible") is not True:
            exclusions[str((target or {}).get("exclusion_reason") or "target_ineligible")] += 1
            continue
        target_value = _number(target.get("label_value"))
        if target_value is None:
            exclusions["target_missing"] += 1
            continue
        rows.append(
            {
                **common,
                "target_net_excess_return_pct": target_value,
                "activation_label": _label_value(activation),
                "tail_loss_label": _label_value(families.get("tail_loss_event")),
                "source_bar_hash_sha256": target.get("source_bar_hash_sha256"),
                "return_label_eligible": target.get("return_label_eligible") is not False,
            }
        )
    cutoff = max(
        (str(row["market_date"]) for row in [*rows, *activation_rows]),
        default=None,
    )
    ordered_labels = sorted(
        (
            label
            for families in grouped.values()
            for label in families.values()
        ),
        key=lambda row: (
            str(row.get("decision_id") or ""),
            str(row.get("label_family") or ""),
            str(row.get("label_id") or ""),
        ),
    )
    ordered_label_ids = [str(row.get("label_id") or "") for row in ordered_labels]
    ordered_label_hashes = [
        str(row.get("label_payload_hash_sha256") or canonical_hash(row))
        for row in ordered_labels
    ]
    content = {
        "rows": rows,
        "activation_rows": activation_rows,
        "exclusions": dict(sorted(exclusions.items())),
        "schema_version": DATASET_SCHEMA_VERSION,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "ordered_label_ids": ordered_label_ids,
        "ordered_label_hashes": ordered_label_hashes,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "target": "benchmark_relative_excess_return",
        "training_cutoff": cutoff,
        "catalyst_ablation_plan": catalyst_ablation_plan(rows),
        "eligibility_counts": {
            "research_training_eligible": sum(
                1 for row in rows if row.get("retrospective_research_eligible") is True
            ),
            "prospective_promotion_eligible": sum(
                1 for row in rows if row.get("prospective_promotion_eligible") is True
            ),
        },
    }
    dataset_hash = canonical_hash(content)
    return {
        "dataset_id": "v6ds-v2-" + dataset_hash,
        "schema_version": DATASET_SCHEMA_VERSION,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "created_at": utc_now(),
        "training_cutoff": cutoff,
        "row_count": len(rows),
        "activation_row_count": len(activation_rows),
        "exclusion_counts": dict(sorted(exclusions.items())),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "dataset_hash_sha256": dataset_hash,
        "ordered_label_ids": ordered_label_ids,
        "ordered_label_hashes": ordered_label_hashes,
        "rows": rows,
        "activation_rows": activation_rows,
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
        "catalyst_ablation_plan": catalyst_ablation_plan(rows),
        "eligibility_counts": {
            "research_training_eligible": sum(
                1 for row in rows if row.get("retrospective_research_eligible") is True
            ),
            "prospective_promotion_eligible": sum(
                1 for row in rows if row.get("prospective_promotion_eligible") is True
            ),
        },
    }


def _current_label(label: dict[str, Any], *, decision: dict[str, Any]) -> bool:
    """Accept only labels projected from authenticated current return truth."""

    truth = dict(label)
    # Label-family eligibility is narrower than the underlying return receipt.
    # Restore only the receipt's bound eligibility bit for classification; no
    # return value, path, cost, benchmark, or causal field is inferred.
    truth["learning_eligible"] = (
        truth.get("retrospective_research_eligible") is True
    )
    return bool(
        label.get("label_schema_version") == LABEL_SCHEMA_VERSION
        and label.get("eligibility_policy_version") == ELIGIBILITY_POLICY_VERSION
        and classify_canonical_return_truth(truth, decision=decision)
        == CURRENT_RETURN_TRUTH
        and str(label.get("label_id") or "").startswith("v6l-v2-")
        and str(label.get("truth_lineage_hash_sha256") or "")
        and str(label.get("label_payload_hash_sha256") or "")
    )


def _dataset_row(
    decision_id: str,
    decision: dict[str, Any],
    families: dict[str, Any],
) -> dict[str, Any]:
    feature = decision.get("feature_vector")
    feature_data = feature if isinstance(feature, dict) else {}
    feature_json = feature_data.get("feature_json")
    raw = feature_json if isinstance(feature_json, dict) else {}
    target = families.get("benchmark_relative_excess_return") or {}
    lineage = evidence_lineage(target)
    catalyst = _catalyst_features(raw)
    return {
        "decision_id": decision_id,
        "source_decision": decision,
        "source_label": target,
        "market_date": str(decision.get("market_date") or "")[:10],
        "ticker": decision.get("ticker"),
        "action": decision.get("action"),
        "setup_key": decision.get("setup_key"),
        "regime_key": decision.get("regime_key"),
        "source_key": _source_key(decision),
        "liquidity_bucket": _bucket(
            _nested_number(raw, "liquidity_execution", "premarket_dollar_volume"),
            ((5_000_000.0, "under_5m"), (20_000_000.0, "5m_to_20m")),
            "over_20m",
        ),
        "catalyst_bucket": _catalyst_bucket(raw),
        "catalyst_feature_block": catalyst,
        "feature_vector": feature_data,
        "feature_schema_version": decision.get("feature_schema_version"),
        "feature_hash_sha256": decision.get("feature_hash_sha256"),
        "estimated_round_trip_cost_bps": _number(
            decision.get("estimated_round_trip_cost_bps")
        ),
        "inclusion_probability": _inclusion_probability(decision),
        "inverse_probability_weight": _inverse_probability_weight(decision),
        "simulated_fill_label": _label_value(families.get("simulated_fill_feasibility")),
        "source_artifact_hash_sha256": lineage["source_artifact_hash_sha256"],
        "source_artifact_hashes": lineage["source_artifact_hashes"],
        "path_replay_id": lineage["path_replay_id"],
        "benchmark_hash_sha256": lineage["benchmark_hash_sha256"],
        "observed_cost_model_identity": lineage["observed_cost_model_identity"],
        "modeled_cost_model_identity": lineage["modeled_cost_model_identity"],
        "evidence_cohort": lineage["evidence_cohort"],
        "evidence_lineage_hash_sha256": lineage["evidence_lineage_hash_sha256"],
        "retrospective_research_eligible": lineage["retrospective_research_eligible"],
        "prospective_promotion_eligible": lineage["prospective_promotion_eligible"],
    }


def _source_key(decision: dict[str, Any]) -> str:
    summary = decision.get("source_summary")
    data = summary if isinstance(summary, dict) else {}
    return str(data.get("primary_source") or data.get("source") or "unknown")


def _catalyst_bucket(raw: dict[str, Any]) -> str:
    catalyst = raw.get("catalyst")
    data = catalyst if isinstance(catalyst, dict) else {}
    if data.get("confirmed") is True or data.get("sourced") is True:
        return "sourced"
    if data:
        return "unconfirmed"
    return "missing"


def _catalyst_features(raw: dict[str, Any]) -> dict[str, Any]:
    catalyst = raw.get("catalyst")
    data = catalyst if isinstance(catalyst, dict) else {}
    hashes = data.get("evidence_hashes")
    evidence_hashes = (
        sorted({str(item) for item in hashes if str(item).strip()})
        if isinstance(hashes, list)
        else []
    )
    return {
        "availability_status": str(
            data.get("availability_status")
            or data.get("evidence_availability_status")
            or ("missing" if not data else "present")
        ),
        "event_type": data.get("event_type"),
        "polarity": data.get("polarity"),
        "financing_mechanism": data.get("financing_mechanism"),
        "novelty": data.get("novelty"),
        "timing": data.get("timing"),
        "evidence_hashes": evidence_hashes,
    }


def _nested_number(raw: dict[str, Any], group: str, key: str) -> float | None:
    value = raw.get(group)
    data = value if isinstance(value, dict) else {}
    return _number(data.get(key))


def _bucket(
    value: float | None,
    thresholds: tuple[tuple[float, str], ...],
    fallback: str,
) -> str:
    if value is None:
        return "missing"
    for threshold, label in thresholds:
        if value < threshold:
            return label
    return fallback


def _label_value(label: dict[str, Any] | None) -> float | None:
    return _number((label or {}).get("label_value"))


def _inclusion_probability(decision: dict[str, Any]) -> float | None:
    sampling = decision.get("rejected_sampling")
    return _number(sampling.get("inclusion_probability")) if isinstance(sampling, dict) else 1.0


def _inverse_probability_weight(decision: dict[str, Any]) -> float | None:
    probability = _inclusion_probability(decision)
    if probability is None or not 0.0 < probability <= 1.0:
        return None
    # A frozen cap prevents a tiny sampling cell from dominating evaluation.
    return round(min(10.0, 1.0 / probability), 6)


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


__all__ = ["build_return_dataset"]
