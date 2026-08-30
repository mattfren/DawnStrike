"""Forward-only one-change experiment and manual promotion contracts."""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from intraday_scanner.alpha.v6.contracts import (
    canonical_hash,
    is_valid_code_sha,
    is_valid_sha256,
    utc_now,
)

_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _canonical_date(value: object, label: str) -> str:
    raw = str(value)
    if not _ISO_DATE.fullmatch(raw):
        raise ValueError(f"{label} must be an exact ISO date")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} is an invalid calendar date") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{label} must equal its canonical ISO date")
    return raw


def _canonical_evaluated_at(value: str | None, holdout_end: str) -> str:
    """Normalize the caller's actual timezone-aware evaluation instant.

    A date-only as-of is interpreted at UTC midnight on that date.  It is
    never replaced with the holdout end or an end-of-day synthetic timestamp.
    """
    end = date.fromisoformat(_canonical_date(holdout_end, "holdout_end"))
    boundary = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    if value is None:
        raise ValueError("actual timezone-aware evaluated_at/as-of is required")
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("evaluated_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("evaluated_at must be timezone-aware")
    actual = parsed.astimezone(UTC)
    if actual < boundary:
        raise ValueError("the full frozen holdout must be eligible before evaluation")
    return actual.isoformat()


def _strict_number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _hash_if_present(value: str | None, *, code: bool = False) -> str | None:
    if value is None or not str(value).strip():
        return None
    valid = is_valid_code_sha(value) if code else is_valid_sha256(value)
    if not valid:
        raise ValueError("experiment lineage hash is malformed")
    return str(value).lower()


def _frozen_dates(values: list[str] | tuple[str, ...] | None) -> list[str]:
    if values is None:
        return []
    raw = [str(value) for value in values]
    if any(not _ISO_DATE.fullmatch(value) for value in raw):
        raise ValueError("experiment market dates must be exact ISO dates")
    if len(set(raw)) != len(raw):
        raise ValueError("experiment market dates must be unique")
    output = sorted(raw)
    for value in output:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("experiment market dates must be ISO dates") from exc
        if parsed.isoformat() != value:
            raise ValueError("experiment market dates must equal canonical ISO dates")
    return output


def register_experiment(
    *,
    hypothesis: str,
    training_cutoff: str,
    baseline_config: dict[str, Any],
    candidate_config: dict[str, Any],
    validation_start: str,
    holdout_start: str,
    stop_condition: str,
    promotion_requirements: list[str],
    training_dates: list[str] | tuple[str, ...] | None = None,
    validation_dates: list[str] | tuple[str, ...] | None = None,
    holdout_dates: list[str] | tuple[str, ...] | None = None,
    holdout_end: str | None = None,
    data_hash_sha256: str | None = None,
    source_hash_sha256: str | None = None,
    code_sha: str | None = None,
    window_hash_sha256: str | None = None,
    v5_comparison_hash_sha256: str | None = None,
    input_hash_sha256: str | None = None,
    validation_end: str | None = None,
) -> dict[str, Any]:
    """Register exactly one prospective policy difference; never apply it."""

    changed = sorted(
        key
        for key in set(baseline_config) | set(candidate_config)
        if baseline_config.get(key) != candidate_config.get(key)
    )
    if len(changed) != 1:
        raise ValueError("A V6 challenger experiment must change exactly one field.")
    if not hypothesis.strip() or not stop_condition.strip() or not promotion_requirements:
        raise ValueError("Experiment hypothesis, stop condition, and promotion rules are required.")
    training_cutoff = _canonical_date(training_cutoff, "training_cutoff")
    validation_start = _canonical_date(validation_start, "validation_start")
    holdout_start = _canonical_date(holdout_start, "holdout_start")
    if not (training_cutoff < validation_start < holdout_start):
        raise ValueError("Experiment windows must be strictly forward of the training cutoff.")
    holdout_end = (
        _canonical_date(holdout_end, "holdout_end") if holdout_end is not None else None
    )
    validation_end = (
        _canonical_date(validation_end, "validation_end") if validation_end is not None else None
    )
    if holdout_end is not None and holdout_end < holdout_start:
        raise ValueError("Experiment holdout end must not precede its start.")
    if validation_end is not None and validation_end < validation_start:
        raise ValueError("Experiment validation end must not precede its start.")
    frozen_windows: dict[str, dict[str, Any]] = {
        "training": {
            "start": (
                min(_frozen_dates(training_dates)) if _frozen_dates(training_dates) else ""
            ),
            "end": training_cutoff,
            "cutoff": training_cutoff,
            "market_dates": _frozen_dates(training_dates),
        },
        "validation": {
            "start": validation_start,
            "end": validation_end,
            "market_dates": _frozen_dates(validation_dates),
        },
        "untouched_holdout": {
            "start": holdout_start,
            "end": holdout_end,
            "market_dates": _frozen_dates(holdout_dates),
        },
    }
    derived_window_hash = canonical_hash(frozen_windows)
    for value in frozen_windows["training"]["market_dates"]:
        if value > training_cutoff or value >= validation_start:
            raise ValueError("training market dates must not exceed training cutoff")
    for value in frozen_windows["validation"]["market_dates"]:
        if value < validation_start or (
            validation_end is not None and value > validation_end
        ) or value >= holdout_start:
            raise ValueError("validation market dates fall outside the frozen validation window")
    for value in frozen_windows["untouched_holdout"]["market_dates"]:
        if value < holdout_start or (holdout_end is not None and value > holdout_end):
            raise ValueError("holdout market dates fall outside the frozen holdout window")
    all_dates = [
        *frozen_windows["training"]["market_dates"],
        *frozen_windows["validation"]["market_dates"],
        *frozen_windows["untouched_holdout"]["market_dates"],
    ]
    if len(set(all_dates)) != len(all_dates):
        raise ValueError("experiment partitions must have disjoint market dates")
    frozen_data_hash = _hash_if_present(data_hash_sha256)
    frozen_source_hash = _hash_if_present(source_hash_sha256)
    frozen_code_sha = _hash_if_present(code_sha, code=True)
    frozen_v5_hash = _hash_if_present(v5_comparison_hash_sha256)
    frozen_input_hash = _hash_if_present(input_hash_sha256)
    contract_lineage_complete = bool(
        frozen_windows["training"]["market_dates"]
        and frozen_windows["validation"]["market_dates"]
        and frozen_windows["untouched_holdout"]["market_dates"]
        and validation_end
        and holdout_end
        and frozen_data_hash
        and frozen_source_hash
        and frozen_code_sha
        and window_hash_sha256
        and frozen_input_hash
        and frozen_v5_hash
    )
    payload = {
        "hypothesis": hypothesis.strip(),
        "training_cutoff": training_cutoff,
        "baseline_config": baseline_config,
        "candidate_config": candidate_config,
        "changed_field": changed[0],
        "unchanged_controls": sorted(key for key in baseline_config if key != changed[0]),
        "baseline_configuration_hash_sha256": canonical_hash(baseline_config),
        "validation_start": validation_start,
        "untouched_holdout_start": holdout_start,
        "untouched_holdout_end": holdout_end,
        "frozen_windows": frozen_windows,
        "window_hash_sha256": _hash_if_present(window_hash_sha256) or derived_window_hash,
        "data_hash_sha256": frozen_data_hash,
        "source_hash_sha256": frozen_source_hash,
        "code_sha": frozen_code_sha,
        "v5_comparison_hash_sha256": frozen_v5_hash,
        "input_hash_sha256": frozen_input_hash,
        "holdout_evaluated_at": None,
        "stop_condition": stop_condition.strip(),
        "promotion_requirements": promotion_requirements,
        "schema_version": "dawnstrike.alphaops_v6.experiment.v2",
        "status": (
            "REGISTERED_NOT_APPLIED"
            if contract_lineage_complete
            else "REGISTERED_NOT_EVALUABLE_MISSING_LINEAGE"
        ),
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if window_hash_sha256 is not None and str(window_hash_sha256).lower() != derived_window_hash:
        raise ValueError("window_hash_sha256 does not match frozen experiment windows")
    payload["experiment_id"] = "v6x-" + canonical_hash(payload)[:28]
    payload["created_at"] = utc_now()
    payload["configuration_hash_sha256"] = canonical_hash(candidate_config)
    payload["immutable_contract_hash_sha256"] = canonical_hash(
        {key: value for key, value in payload.items() if key != "created_at"}
    )
    return payload


def promotion_review_packet(
    *, evidence: dict[str, Any], operator: str | None = None
) -> dict[str, Any]:
    """Make promotion a recorded human decision, never an automated outcome."""

    payload = {
        "created_at": utc_now(),
        "operator": operator,
        "evidence": evidence,
        "status": "PENDING_MANUAL_REVIEW",
        "approved": False,
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["review_id"] = "v6pr-" + canonical_hash(payload)[:28]
    return payload


def record_untouched_holdout_evaluation(
    *,
    experiment: dict[str, Any],
    evidence: dict[str, Any],
    existing_evaluations: list[dict[str, Any]],
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    """Create the sole immutable holdout receipt for one frozen experiment."""

    experiment_id = str(experiment.get("experiment_id") or "")
    if not experiment_id:
        raise ValueError("A registered experiment_id is required.")
    if any(row.get("experiment_id") == experiment_id for row in existing_evaluations):
        raise ValueError("The untouched holdout for this experiment was already evaluated.")
    if str(experiment.get("status") or "").endswith("MISSING_LINEAGE"):
        raise ValueError("experiment is not evaluable because its lineage is incomplete")
    holdout_start_raw = str(experiment.get("untouched_holdout_start") or "")
    if not holdout_start_raw:
        raise ValueError("The experiment has no frozen untouched holdout.")
    holdout_start = _canonical_date(holdout_start_raw, "holdout_start")
    frozen_holdout = (
        experiment.get("frozen_windows", {}).get("untouched_holdout", {})
        if isinstance(experiment.get("frozen_windows"), dict)
        else {}
    )
    holdout_end_raw = str(
        frozen_holdout.get("end") or experiment.get("untouched_holdout_end") or ""
    )
    if not holdout_end_raw:
        raise ValueError("The experiment has no complete frozen untouched holdout.")
    holdout_end = _canonical_date(holdout_end_raw, "holdout_end")
    timestamp = _canonical_evaluated_at(evaluated_at, holdout_end)
    if holdout_end < holdout_start:
        raise ValueError("The untouched holdout cannot be evaluated before its start date.")
    if evidence.get("no_lookahead") is not True:
        raise ValueError("Holdout evidence must pass the no-lookahead audit.")
    expected_window = experiment.get("frozen_windows")
    supplied_window = evidence.get("evaluation_window")
    if supplied_window is None or supplied_window != expected_window:
        raise ValueError("Holdout evidence evaluation window conflicts with frozen experiment.")
    evidence_experiment_id = evidence.get("experiment_id")
    if evidence_experiment_id is not None and str(evidence_experiment_id) != experiment_id:
        raise ValueError("Holdout evidence experiment identity conflicts with frozen experiment.")
    for field in (
        "configuration_hash_sha256",
        "data_hash_sha256",
        "source_hash_sha256",
        "code_sha",
        "window_hash_sha256",
        "input_hash_sha256",
        "v5_comparison_hash_sha256",
    ):
        expected = experiment.get(field)
        supplied = evidence.get(field)
        if expected is None or supplied is None or str(expected) != str(supplied):
            raise ValueError(f"Holdout evidence {field} conflicts with frozen experiment.")
    raw_holdout_dates = [str(value) for value in (frozen_holdout.get("market_dates") or [])]
    holdout_dates = {_canonical_date(value, "holdout market date") for value in raw_holdout_dates}
    if len(holdout_dates) != len(raw_holdout_dates):
        raise ValueError("Holdout evidence cohort contains duplicate market dates.")
    coverage = evidence.get("coverage")
    if not holdout_dates or not isinstance(coverage, dict):
        raise ValueError("Holdout evidence must include the complete frozen cohort.")
    for arm in ("baseline", "candidate"):
        arm_coverage = coverage.get(arm)
        if not isinstance(arm_coverage, dict) or set(
            arm_coverage.get("market_dates") or []
        ) != holdout_dates:
            raise ValueError("Holdout evidence cohort does not exactly cover frozen holdout dates.")
    expectancy_raw = evidence.get("after_cost_expectancy_pct")
    expectancy = _strict_number(expectancy_raw)
    if expectancy_raw is not None and expectancy is None:
        raise ValueError("Holdout expectancy must be a finite numeric value")
    status = (
        "POSITIVE_HOLDOUT"
        if expectancy is not None and expectancy > 0.0
        else "NEGATIVE_OR_INCOMPLETE_HOLDOUT"
    )
    payload = {
        "experiment_id": experiment_id,
        "evaluated_at": timestamp,
        "holdout_start": holdout_start,
        "configuration_hash_sha256": experiment.get("configuration_hash_sha256"),
        "data_hash_sha256": experiment.get("data_hash_sha256"),
        "source_hash_sha256": experiment.get("source_hash_sha256"),
        "code_sha": experiment.get("code_sha"),
        "window_hash_sha256": experiment.get("window_hash_sha256"),
        "v5_comparison_hash_sha256": experiment.get("v5_comparison_hash_sha256"),
        "status": status,
        "evidence": evidence,
        "evidence_hash_sha256": canonical_hash(evidence),
        "evaluated_once": True,
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    model_run_id = str(evidence.get("model_run_id") or "")
    if not model_run_id:
        raise ValueError("governed V2 holdout evidence requires an exact model_run_id")
    if model_run_id:
        evidence_experiment_id = str(evidence.get("experiment_id") or "")
        evidence_configuration_hash = str(evidence.get("configuration_hash_sha256") or "")
        source_lineage_hash = str(
            evidence.get("source_lineage_hash_sha256") or evidence.get("source_hash_sha256") or ""
        )
        code_sha = str(evidence.get("code_sha") or "")
        evaluation_window = evidence.get("evaluation_window")
        window_data = evaluation_window if isinstance(evaluation_window, dict) else {}
        expected_window = {
            "training_cutoff": experiment.get("training_cutoff"),
            "validation_start": experiment.get("validation_start"),
            "untouched_holdout_start": experiment.get("untouched_holdout_start"),
        }
        if isinstance(experiment.get("frozen_windows"), dict):
            expected_window = experiment["frozen_windows"]
        if (
            evidence_experiment_id != experiment_id
            or evidence_configuration_hash != str(experiment.get("configuration_hash_sha256") or "")
            or not source_lineage_hash
            or not code_sha
            or window_data != expected_window
            or not is_valid_sha256(evidence_configuration_hash)
            or not is_valid_sha256(source_lineage_hash)
            or not is_valid_code_sha(code_sha)
        ):
            raise ValueError(
                "Model-bound holdout evidence must carry exact experiment, "
                "configuration, source, code, and evaluation-window lineage."
            )
        for field in ("data_hash_sha256", "source_hash_sha256", "window_hash_sha256"):
            expected = str(experiment.get(field) or "")
            if expected and str(evidence.get(field) or "") != expected:
                raise ValueError(f"Holdout evidence {field} does not match frozen experiment.")
        payload["model_run_id"] = model_run_id
        payload["model_binding_hash_sha256"] = canonical_hash(
            {
                "model_run_id": model_run_id,
                "experiment_id": evidence_experiment_id,
                "configuration_hash_sha256": evidence_configuration_hash,
                "source_lineage_hash_sha256": source_lineage_hash,
                "code_sha": code_sha,
                "evaluation_window": window_data,
                "data_hash_sha256": evidence.get("data_hash_sha256"),
                "source_hash_sha256": evidence.get("source_hash_sha256"),
                "evidence_hash_sha256": payload["evidence_hash_sha256"],
            }
        )
    payload["holdout_evaluation_id"] = (
        "v6h-"
        + canonical_hash(
            {"experiment_id": experiment_id, "evidence_hash": payload["evidence_hash_sha256"]}
        )[:28]
    )
    payload["receipt_hash_sha256"] = canonical_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"receipt_hash_sha256", "created_at"}
        }
    )
    return payload


__all__ = [
    "promotion_review_packet",
    "record_untouched_holdout_evaluation",
    "register_experiment",
]
