"""One-time, forward-only V6 holdout evaluation from immutable shadow receipts."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_RETURN_TRUTH,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.v6.contracts import canonical_hash, is_valid_code_sha, is_valid_sha256
from intraday_scanner.alpha.v6.registry import record_untouched_holdout_evaluation
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

_MIN_DECISIONS_PER_ARM = 10
_MIN_SESSIONS_PER_ARM = 5
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


def _parse_as_of(value: str) -> datetime:
    raw = str(value or "").strip()
    if _ISO_DATE.fullmatch(raw):
        _canonical_date(raw, "as_of_date")
        return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "as_of_date must be an exact ISO date or timezone-aware timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of_date timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def evaluate_registered_holdout(
    store: SQLiteScanStore,
    *,
    experiment_id: str,
    as_of_date: str,
    model_run_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate one registered experiment once, only after its frozen holdout.

    Both experiment arms must be explicitly tagged on immutable decisions with
    their frozen configuration hashes.  Untagged historical rows are never
    retrofitted into a holdout, which prevents post-hoc cohort selection.
    """

    experiment_loader = getattr(store, "load_alpha_v6_experiment", None)
    if callable(experiment_loader):
        experiment = experiment_loader(experiment_id)
    else:
        experiments = {
            str(row.get("experiment_id") or ""): row
            for row in store.load_alpha_v6_experiments(limit=50_000)
        }
        experiment = experiments.get(experiment_id)
    if experiment is None:
        return _blocked("EXPERIMENT_NOT_FOUND", experiment_id=experiment_id)
    if str(experiment.get("status") or "").endswith("MISSING_LINEAGE"):
        return _blocked(
            "EXPERIMENT_NOT_EVALUABLE_MISSING_LINEAGE",
            experiment_id=experiment_id,
        )
    model_lineage: dict[str, Any] | None = None
    if model_run_id:
        model_loader = getattr(store, "load_alpha_v6_model_run", None)
        model_run = (
            model_loader(model_run_id)
            if callable(model_loader)
            else next(
                (
                    row
                    for row in store.load_alpha_v6_model_runs(limit=50_000)
                    if str(row.get("model_run_id") or "") == model_run_id
                ),
                None,
            )
        )
        if model_run is None:
            return _blocked(
                "MODEL_RUN_LINEAGE_MISSING_OR_MISMATCHED",
                experiment_id=experiment_id,
                model_run_id=model_run_id,
                blockers="model_run_not_persisted",
            )
        model_lineage, lineage_blockers = _model_lineage(model_run, experiment)
        if lineage_blockers:
            return {
                "status": "MODEL_RUN_LINEAGE_MISSING_OR_MISMATCHED",
                "experiment_id": experiment_id,
                "model_run_id": model_run_id,
                "blockers": lineage_blockers,
                "persisted": False,
                "research_only": True,
                "broker_execution_enabled": False,
            }
    existing = [
        row
        for row in store.load_alpha_v6_holdout_evaluations(limit=50_000)
        if row.get("experiment_id") == experiment_id
    ]
    if existing:
        return {
            "status": "ALREADY_EVALUATED_IMMUTABLE",
            "experiment_id": experiment_id,
            "persisted": False,
            "existing_evaluation": existing[0],
            "research_only": True,
            "broker_execution_enabled": False,
        }
    try:
        holdout_start = _canonical_date(
            experiment.get("untouched_holdout_start") or "", "holdout_start"
        )
        as_of_at = _parse_as_of(as_of_date)
    except ValueError:
        return _blocked("HOLDOUT_DATE_REQUIRED", experiment_id=experiment_id)
    cutoff = as_of_at.date().isoformat()
    if cutoff < holdout_start:
        return _blocked(
            "PRE_HOLDOUT_EVALUATION_REJECTED",
            experiment_id=experiment_id,
            holdout_start=holdout_start,
            as_of_date=cutoff,
        )
    frozen_windows = experiment.get("frozen_windows")
    holdout_window: dict[str, Any] = {}
    if isinstance(frozen_windows, dict):
        candidate_window = frozen_windows.get("untouched_holdout")
        if isinstance(candidate_window, dict):
            holdout_window = candidate_window
    raw_frozen_dates = [str(value) for value in (holdout_window.get("market_dates") or [])]
    frozen_dates: set[str] = set()
    try:
        for value in raw_frozen_dates:
            frozen_dates.add(_canonical_date(value, "holdout market date"))
    except ValueError:
        return _blocked("HOLDOUT_WINDOW_NOT_FROZEN", experiment_id=experiment_id)
    if len(frozen_dates) != len(raw_frozen_dates):
        return _blocked(
            "NOT_EVALUABLE_CONFLICTING_HOLDOUT_COHORT_IDENTITY",
            experiment_id=experiment_id,
            blockers="duplicate_frozen_market_date",
        )
    frozen_end_raw = str(
        holdout_window.get("end")
        or experiment.get("untouched_holdout_end")
        or (max(frozen_dates) if frozen_dates else "")
    )
    try:
        frozen_end = _canonical_date(frozen_end_raw, "holdout_end")
    except ValueError:
        frozen_end = ""
    if not frozen_dates or not frozen_end:
        return _blocked("HOLDOUT_WINDOW_NOT_FROZEN", experiment_id=experiment_id)
    if frozen_end < holdout_start:
        return _blocked("HOLDOUT_WINDOW_NOT_FROZEN", experiment_id=experiment_id)
    frozen_end_boundary = datetime.combine(
        date.fromisoformat(frozen_end) + timedelta(days=1), time.min, tzinfo=UTC
    )
    if as_of_at < frozen_end_boundary:
        return _blocked(
            "HOLDOUT_NOT_FULLY_ELIGIBLE",
            experiment_id=experiment_id,
            holdout_end=frozen_end,
            as_of_date=as_of_at.isoformat(),
        )
    # A complete governed V2 experiment may only be evaluated against the
    # exact persisted model run that produced its lineage. Legacy callers may
    # receive an explicit non-evaluable result, but can never promote.
    if not model_run_id:
        return _blocked(
            "NOT_EVALUABLE_MODEL_RUN_LINEAGE_REQUIRED",
            experiment_id=experiment_id,
            blockers="exact_model_run_id_required",
        )

    decision_rows = store.load_alpha_v6_decisions()
    decision_ids = [str(row.get("decision_id") or "") for row in decision_rows]
    duplicate_decisions = len(decision_ids) != len(set(decision_ids))
    decisions = {str(row.get("decision_id") or ""): row for row in decision_rows}
    rows_by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    blockers: set[str] = {"duplicate_decision_identity"} if duplicate_decisions else set()
    selected_ids: dict[str, set[str]] = {"baseline": set(), "candidate": set()}
    for outcome in store.load_alpha_v6_outcomes():
        decision = decisions.get(str(outcome.get("decision_id") or ""))
        if decision is None:
            continue
        assignment = _assignment(decision)
        if assignment.get("experiment_id") != experiment_id:
            continue
        try:
            market_date = _canonical_date(outcome.get("market_date") or "", "outcome market date")
        except ValueError:
            blockers.add("holdout_outcome_market_date_invalid")
            continue
        try:
            decision_date = _canonical_date(
                decision.get("market_date") or "", "decision market date"
            )
        except ValueError:
            blockers.add("holdout_decision_market_date_invalid")
            continue
        if market_date != decision_date:
            blockers.add("holdout_decision_outcome_date_mismatch")
            continue
        if not (holdout_start <= market_date <= cutoff):
            continue
        if frozen_dates and market_date not in frozen_dates:
            continue
        arm = str(assignment.get("arm") or "")
        expected_hash = _expected_hash(experiment, arm)
        if arm not in {"baseline", "candidate"}:
            blockers.add("experiment_arm_missing_or_invalid")
            continue
        if assignment.get("configuration_hash_sha256") != expected_hash:
            blockers.add(f"{arm}_configuration_hash_mismatch")
            continue
        if not _eligible_outcome(outcome, decision=decision):
            blockers.add("incomplete_or_unsourced_holdout_outcome")
            continue
        return_value = _number(outcome.get("net_excess_return_pct"))
        if return_value is None:
            blockers.add("holdout_return_missing")
            continue
        decision_id = str(decision.get("decision_id") or "")
        if decision_id in selected_ids[arm]:
            blockers.add(f"{arm}_duplicate_holdout_identity")
            continue
        selected_ids[arm].add(decision_id)
        rows_by_arm[arm].append(
            {
                "decision_id": str(decision.get("decision_id") or ""),
                "market_date": market_date,
                "net_excess_return_pct": return_value,
                "source_bar_hash_sha256": outcome.get("source_bar_hash_sha256"),
            }
        )
    coverage = _coverage(rows_by_arm)
    if frozen_dates:
        for arm in ("baseline", "candidate"):
            coverage[arm]["frozen_market_dates_complete"] = set(
                coverage[arm]["market_dates"]
            ) == frozen_dates
    if blockers or not _enough_evidence(coverage, expected_dates=frozen_dates):
        return {
            "status": "NOT_EVALUABLE_INSUFFICIENT_PROSPECTIVE_HOLDOUT_EVIDENCE",
            "experiment_id": experiment_id,
            "holdout_start": holdout_start,
            "as_of_date": cutoff,
            "coverage": coverage,
            "blockers": sorted(blockers | _coverage_blockers(coverage)),
            "persisted": False,
            "missing_truth_is_zero": False,
            "research_only": True,
            "broker_execution_enabled": False,
        }
    baseline = _mean(rows_by_arm["baseline"])
    candidate = _mean(rows_by_arm["candidate"])
    evidence = {
        "evaluation_method": "immutable_tagged_prospective_two_arm_holdout",
        # Bind the receipt to the observed frozen cohort, not to a caller's
        # retry timestamp. This keeps identical retries byte-stable.
        "as_of_date": frozen_end,
        "holdout_start": holdout_start,
        "baseline_after_cost_expectancy_pct": baseline,
        "candidate_after_cost_expectancy_pct": candidate,
        "after_cost_expectancy_pct": round(candidate - baseline, 6),
        "candidate_excess_vs_baseline_pct": round(candidate - baseline, 6),
        "coverage": coverage,
        "no_lookahead": True,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    for field in (
        "configuration_hash_sha256",
        "data_hash_sha256",
        "source_hash_sha256",
        "code_sha",
        "window_hash_sha256",
        "input_hash_sha256",
        "v5_comparison_hash_sha256",
    ):
        if experiment.get(field) is not None:
            evidence[field] = experiment[field]
    if isinstance(frozen_windows, dict):
        evidence["evaluation_window"] = frozen_windows
    if model_run_id:
        evidence["model_run_id"] = model_run_id
        evidence.update(model_lineage or {})
    receipt = record_untouched_holdout_evaluation(
        experiment=experiment,
        evidence=evidence,
        existing_evaluations=[],
        evaluated_at=as_of_at.isoformat(),
    )
    persisted = store.persist_alpha_v6_holdout_evaluation(receipt)
    return {
        "status": "HOLDOUT_RECORDED" if persisted else "ALREADY_EVALUATED_IMMUTABLE",
        "experiment_id": experiment_id,
        "persisted": persisted,
        "holdout_evaluation": receipt,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _assignment(decision: dict[str, Any] | None) -> dict[str, Any]:
    data = decision or {}
    nested = data.get("experiment_assignment")
    if isinstance(nested, dict):
        return nested
    return {
        "experiment_id": data.get("experiment_id"),
        "arm": data.get("experiment_arm"),
        "configuration_hash_sha256": data.get("experiment_configuration_hash_sha256"),
    }


def _expected_hash(experiment: dict[str, Any], arm: str) -> str:
    if arm == "candidate":
        return str(experiment.get("configuration_hash_sha256") or "")
    return str(experiment.get("baseline_configuration_hash_sha256") or "")


def _eligible_outcome(outcome: dict[str, Any], *, decision: dict[str, Any]) -> bool:
    return bool(
        classify_canonical_return_truth(outcome, decision=decision) == CURRENT_RETURN_TRUTH
        and outcome.get("learning_eligible") is True
        and outcome.get("prospective_promotion_eligible") is True
        and outcome.get("no_lookahead") is True
        and str(outcome.get("source_bar_hash_sha256") or "")
    )


def _coverage(rows_by_arm: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {
        arm: {
            "decision_count": len(rows_by_arm.get(arm, [])),
            "session_count": len(
                {str(row.get("market_date") or "") for row in rows_by_arm.get(arm, [])}
            ),
            "market_dates": sorted(
                {str(row.get("market_date") or "") for row in rows_by_arm.get(arm, [])}
            ),
            "source_hashes_complete": all(
                bool(row.get("source_bar_hash_sha256")) for row in rows_by_arm.get(arm, [])
            ),
        }
        for arm in ("baseline", "candidate")
    }


def _enough_evidence(
    coverage: dict[str, dict[str, Any]],
    *,
    expected_dates: set[str] | None = None,
) -> bool:
    return all(
        int(row["decision_count"]) >= _MIN_DECISIONS_PER_ARM
        and int(row["session_count"]) >= _MIN_SESSIONS_PER_ARM
        and row["source_hashes_complete"] is True
        and (
            expected_dates is None
            or row.get("frozen_market_dates_complete") is True
        )
        for row in coverage.values()
    )


def _coverage_blockers(coverage: dict[str, dict[str, Any]]) -> set[str]:
    blockers: set[str] = set()
    for arm, row in coverage.items():
        if int(row["decision_count"]) < _MIN_DECISIONS_PER_ARM:
            blockers.add(f"{arm}_minimum_decisions_not_met")
        if int(row["session_count"]) < _MIN_SESSIONS_PER_ARM:
            blockers.add(f"{arm}_minimum_sessions_not_met")
        if row["source_hashes_complete"] is not True:
            blockers.add(f"{arm}_source_lineage_incomplete")
        if row.get("frozen_market_dates_complete") is False:
            blockers.add(f"{arm}_frozen_market_dates_incomplete")
    return blockers


def _model_lineage(
    model_run: dict[str, Any], experiment: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Validate a persisted model run's immutable experiment lineage."""

    source_hash = str(
        model_run.get("source_lineage_hash_sha256") or model_run.get("dataset_hash_sha256") or ""
    )
    window = model_run.get("evaluation_window")
    window_data = window if isinstance(window, dict) else {}
    expected_window = {
        "training_cutoff": experiment.get("training_cutoff"),
        "validation_start": experiment.get("validation_start"),
        "untouched_holdout_start": experiment.get("untouched_holdout_start"),
    }
    if isinstance(experiment.get("frozen_windows"), dict):
        expected_window = experiment["frozen_windows"]
    lineage = {
        "experiment_id": str(model_run.get("experiment_id") or ""),
        "configuration_hash_sha256": str(model_run.get("configuration_hash_sha256") or ""),
        "source_lineage_hash_sha256": source_hash,
        "code_sha": str(model_run.get("code_sha") or ""),
        "evaluation_window": window_data,
    }
    blockers: list[str] = []
    if lineage["experiment_id"] != str(experiment.get("experiment_id") or ""):
        blockers.append("experiment_id_mismatch")
    if lineage["configuration_hash_sha256"] != str(
        experiment.get("configuration_hash_sha256") or ""
    ):
        blockers.append("configuration_hash_mismatch")
    if not is_valid_sha256(lineage["source_lineage_hash_sha256"]):
        blockers.append("source_lineage_hash_missing")
    if not is_valid_code_sha(lineage["code_sha"]):
        blockers.append("code_sha_missing")
    if not is_valid_sha256(lineage["configuration_hash_sha256"]):
        blockers.append("configuration_hash_invalid")
    if model_run.get("training_cutoff") != experiment.get("training_cutoff"):
        blockers.append("model_training_cutoff_mismatch")
    if window_data != expected_window:
        blockers.append("evaluation_window_mismatch_or_missing")
    return lineage, blockers


def _mean(rows: list[dict[str, Any]]) -> float:
    return sum(float(row["net_excess_return_pct"]) for row in rows) / len(rows)


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


def _blocked(status: str, **details: str) -> dict[str, Any]:
    payload = {
        "status": status,
        **details,
        "persisted": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["receipt_hash_sha256"] = canonical_hash(payload)
    return payload


__all__ = ["evaluate_registered_holdout"]
