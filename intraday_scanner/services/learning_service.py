"""AlphaOps learning updates from shadow outcomes."""

from __future__ import annotations

import math
import uuid
from typing import Any

from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.setup_memory import build_setup_memory
from intraday_scanner.models import utc_now_iso
from intraday_scanner.services.source_reliability_service import reliability_score
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

HISTORICAL_ALPHAOPS_STRATEGY_IDS = ("alphaops_v4", "alphaops_v5")
ALPHAOPS_OFFICIAL_COHORT = "official_telegram"
ACTIVATION_OUTCOME_STATUSES = frozenset({"complete_sourced", "not_triggered"})
_NON_ACTIONABLE_SELECTION_DECISIONS = frozenset({
    "blocked",
    "data_ineligible",
    "no_trade",
    "rehearsal_complete",
    "source_failed",
})


def run_alpha_learning(store: SQLiteScanStore) -> dict[str, Any]:
    """Update production Alpha memory from canonical paper reconciliation only.

    Raw/manual outcomes remain available elsewhere for audit and backwards-compatible
    reporting.  They are deliberately excluded here: a production return label must
    be an exact official selection that was delivered, filled, closed, and reconciled
    to a sourced, after-cost ``strategy_learning_labels`` row.
    """

    signals = store.load_alpha_signals(limit=5000)
    manual_outcomes = store.load_manual_outcomes(limit=5000)
    historical_signals = [
        _learning_signal_from_historical(row)
        for row in store.load_historical_signals(limit=50_000)
    ]
    exact_selections, selection_evidence_status = _load_exact_alpha_selections(store)
    all_sourced_outcomes = store.load_signal_outcomes(limit=50_000)
    legacy_sourced_outcomes = [
        row
        for row in all_sourced_outcomes
        if row.get("learning_eligible") is True
        and str(row.get("outcome_status") or "") == "complete_sourced"
    ]
    activation_labels = _build_activation_labels(
        historical_signals,
        all_sourced_outcomes,
        exact_selections,
    )
    activation_report = _build_activation_report(
        activation_labels,
        selected_signal_count=len(exact_selections),
        selection_evidence_status=selection_evidence_status,
    )
    labels, canonical_diagnostics = _canonical_return_labels(
        store,
        historical_signals=historical_signals,
    )
    now = utc_now_iso()
    for label in labels:
        label["created_at"] = str(label.get("created_at") or now)
        signal = _signal_for_label(signals + historical_signals, label)
        if signal:
            for key in (
                "rank",
                "alpha_score",
                "score_decile",
                "setup_key",
                "source",
                "catalyst_category",
                "risk_flags",
            ):
                if key in signal and key not in label:
                    label[key] = signal[key]
    existing_labels = load_production_alpha_learning_labels(store)
    existing_keys = {str(row.get("label_key") or "") for row in existing_labels}
    new_label_count = sum(
        1 for row in labels if str(row.get("label_key") or "") not in existing_keys
    )
    # Replacement is intentional.  If reconciliation corrects a prior trade into a
    # no-entry/unresolved result, its formerly eligible return must disappear from
    # production memory on this run instead of surviving as a stale copied label.
    store.replace_alpha_production_outcome_labels(labels)
    all_labels = _dedupe_labels(labels)
    memory = _merge_activation_setup_memory(
        build_setup_memory(all_labels),
        activation_report,
    )
    memory_rows = [{**row, "updated_at": now} for row in memory.values()]
    if memory_rows:
        store.persist_alpha_setup_memory(memory_rows)
    reliability_rows = _source_reliability_from_labels(
        store.load_alpha_source_reliability(),
        all_labels,
        now,
    )
    if reliability_rows:
        store.persist_alpha_source_reliability(reliability_rows)
    real_days = _real_days(all_labels)
    truth = build_truth_report(all_labels, real_days_collected=real_days)
    status = (
        "complete"
        if new_label_count
        else "no_new_eligible_outcomes"
        if all_labels
        else "complete"
        if activation_labels
        else "insufficient_real_outcomes"
    )
    payload = {
        "run_id": f"alpha-learn-{uuid.uuid4().hex[:12]}",
        "created_at": now,
        "status": status,
        "labels_created": new_label_count,
        "total_labels": len(all_labels),
        "total_return_labels": len(all_labels),
        # Kept for old report readers, but explicitly not production denominators.
        "manual_outcomes_considered": len(manual_outcomes),
        "sourced_outcomes_considered": len(legacy_sourced_outcomes),
        "legacy_manual_outcomes_observed": len(manual_outcomes),
        "legacy_sourced_outcomes_observed": len(legacy_sourced_outcomes),
        "legacy_outcomes_reporting_only": True,
        "legacy_outcomes_excluded_from_production_learning": True,
        "return_label_contract": "exact_delivered_reconciled_net_after_cost_v1",
        "canonical_return_label_diagnostics": canonical_diagnostics,
        "selected_signals_considered": len(exact_selections),
        "selection_evidence_status": selection_evidence_status,
        "activation_labels_considered": len(activation_labels),
        "activation_outcomes_considered": len(activation_labels),
        "activation_dataset": activation_labels,
        "activation_report": activation_report,
        "setup_memory_count": len(memory_rows),
        "source_reliability_count": len(reliability_rows),
        "return_learning_eligible": bool(all_labels),
        "activation_learning_eligible": bool(activation_labels),
        "learning_eligible": bool(all_labels or activation_labels),
        "missing_outcomes_are_zero": False,
        "truth_report": truth,
    }
    store.persist_alpha_learning_run(payload)
    return payload


def load_production_alpha_learning_labels(
    store: SQLiteScanStore,
    *,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Return only the canonical labels allowed to influence Alpha production."""

    return [
        row
        for row in store.load_alpha_outcome_labels(limit=limit)
        if row.get("production_learning_eligible") is True
        and str(row.get("return_label_contract") or "")
        == "exact_delivered_reconciled_net_after_cost_v1"
        and str(row.get("label_family") or "") == "trade_return"
    ]


def _canonical_return_labels(
    store: SQLiteScanStore,
    *,
    historical_signals: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    strategy_labels = [
        row
        for strategy_id in HISTORICAL_ALPHAOPS_STRATEGY_IDS
        for row in store.load_strategy_learning_labels(
            strategy_id=strategy_id,
            cohort=ALPHAOPS_OFFICIAL_COHORT,
            limit=50_000,
        )
    ]
    evaluations = [
        row
        for strategy_id in HISTORICAL_ALPHAOPS_STRATEGY_IDS
        for row in store.load_strategy_evaluations(
            strategy_id=strategy_id,
            cohort=ALPHAOPS_OFFICIAL_COHORT,
            limit=50_000,
        )
    ]
    trades = [
        row
        for strategy_id in HISTORICAL_ALPHAOPS_STRATEGY_IDS
        for row in store.load_strategy_paper_trades(
            strategy_id=strategy_id,
            cohort=ALPHAOPS_OFFICIAL_COHORT,
            limit=50_000,
        )
    ]
    evaluation_by_id = {
        str(row.get("evaluation_id") or ""): row
        for row in evaluations
        if str(row.get("evaluation_id") or "")
    }
    trade_by_selection = {
        str(row.get("selection_id") or ""): row
        for row in trades
        if str(row.get("selection_id") or "")
    }
    signal_by_id = {
        str(row.get("signal_id") or ""): row
        for row in historical_signals
        if str(row.get("signal_id") or "")
    }
    diagnostics = {
        "canonical_rows_observed": 0,
        "eligible_rows": 0,
        "excluded_not_return_label": 0,
        "excluded_not_eligible": 0,
        "excluded_missing_evaluation": 0,
        "excluded_not_delivered": 0,
        "excluded_not_closed": 0,
        "excluded_missing_trade": 0,
        "excluded_integrity_mismatch": 0,
    }
    output: dict[str, dict[str, Any]] = {}
    for label in strategy_labels:
        diagnostics["canonical_rows_observed"] += 1
        if str(label.get("label_family") or "") != "trade_return":
            diagnostics["excluded_not_return_label"] += 1
            continue
        if label.get("eligible") is not True:
            diagnostics["excluded_not_eligible"] += 1
            continue
        evaluation = evaluation_by_id.get(str(label.get("evaluation_id") or ""))
        if evaluation is None:
            diagnostics["excluded_missing_evaluation"] += 1
            continue
        if not (
            evaluation.get("delivered") is True
            and str(evaluation.get("delivery_channel") or "").lower() == "telegram"
            and str(evaluation.get("delivery_status") or "").lower()
            in {"delivered", "delivered_legacy"}
        ):
            diagnostics["excluded_not_delivered"] += 1
            continue
        if not (
            evaluation.get("trade_return_eligible") is True
            and evaluation.get("filled") is True
            and evaluation.get("closed") is True
            and str(evaluation.get("reconciliation_status") or "") == "resolved"
        ):
            diagnostics["excluded_not_closed"] += 1
            continue
        trade = trade_by_selection.get(str(evaluation.get("selection_id") or ""))
        if trade is None:
            diagnostics["excluded_missing_trade"] += 1
            continue
        label_return = _finite_float(label.get("label_value"))
        trade_return = _finite_float(trade.get("net_return_pct"))
        evaluation_return = _finite_float(evaluation.get("net_return_pct"))
        source_hash = str(label.get("source_bar_hash_sha256") or "")
        if not (
            label_return is not None
            and trade_return is not None
            and evaluation_return is not None
            and math.isclose(label_return, trade_return, abs_tol=1e-9)
            and math.isclose(label_return, evaluation_return, abs_tol=1e-9)
            and source_hash
            and source_hash == str(trade.get("source_bar_hash_sha256") or "")
            and source_hash == str(evaluation.get("source_bar_hash_sha256") or "")
            and trade.get("fees") is not None
            and trade.get("slippage_cost") is not None
        ):
            diagnostics["excluded_integrity_mismatch"] += 1
            continue
        signal_id = str(label.get("signal_id") or "")
        signal = signal_by_id.get(signal_id, {})
        label_id = str(label.get("label_id") or "")
        canonical = {
            **signal,
            "label_key": f"strategy_learning:{label_id}",
            "canonical_strategy_label_id": label_id,
            "evaluation_id": label.get("evaluation_id"),
            "trade_id": trade.get("trade_id"),
            "selection_id": evaluation.get("selection_id"),
            "signal_id": signal_id,
            "scan_id": evaluation.get("scan_id") or signal.get("scan_id") or "",
            "market_date": str(label.get("market_date") or "")[:10],
            "ticker": str(label.get("ticker") or "").upper(),
            "strategy_id": label.get("strategy_id"),
            "strategy_version": label.get("strategy_version"),
            "cohort": label.get("cohort"),
            "label_family": "trade_return",
            "created_at": label.get("created_at") or evaluation.get("reconciled_at"),
            "setup_key": signal.get("setup_key") or signal.get("primary_setup") or "unknown",
            "source": trade.get("source") or evaluation.get("source") or "",
            "outcome_source": trade.get("source") or evaluation.get("source") or "",
            "outcome_source_url": trade.get("source_url") or evaluation.get("source_url") or "",
            "outcome_source_bar_hash_sha256": source_hash,
            "source_bar_hash_sha256": source_hash,
            "planned_first_touch_return_pct": label_return,
            "planned_first_touch_outcome": trade.get("exit_reason"),
            "recommended_exit_return_pct": label_return,
            "return_pct": label_return,
            "net_after_cost_return_pct": label_return,
            "r_multiple": trade.get("r_multiple"),
            "fees": trade.get("fees"),
            "slippage_cost": trade.get("slippage_cost"),
            "low_after_entry_drawdown": trade.get("max_adverse_excursion_pct"),
            "max_adverse_excursion": trade.get("max_adverse_excursion_pct"),
            "max_favorable_excursion": trade.get("max_favorable_excursion_pct"),
            "winner_close": label_return > 0,
            "learning_eligible": True,
            "production_learning_eligible": True,
            "automatic_sourced_data": True,
            "manual_uploaded_data": False,
            "delivered": True,
            "research_only": True,
            "broker_execution_enabled": False,
            "return_label_contract": "exact_delivered_reconciled_net_after_cost_v1",
            "return_measure": "reconciled_net_after_cost_return_pct",
        }
        output[canonical["label_key"]] = canonical
        diagnostics["eligible_rows"] += 1
    return list(output.values()), diagnostics


def _finite_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _load_exact_alpha_selections(
    store: SQLiteScanStore,
) -> tuple[list[dict[str, Any]], str]:
    """Load immutable AlphaOps selections without inferring from ranked rows."""

    loader = getattr(store, "load_signal_selections", None)
    if not callable(loader):
        return [], "exact_selection_store_unavailable"
    try:
        rows = [
            row
            for strategy_id in HISTORICAL_ALPHAOPS_STRATEGY_IDS
            for row in loader(
                strategy_id=strategy_id,
                cohort=ALPHAOPS_OFFICIAL_COHORT,
                limit=50_000,
            )
        ]
    except TypeError:
        # Older compatible stores may expose the loader before filtered arguments.
        rows = loader(limit=50_000)
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("strategy_id") or "") not in HISTORICAL_ALPHAOPS_STRATEGY_IDS:
            continue
        if str(row.get("cohort") or "") != ALPHAOPS_OFFICIAL_COHORT:
            continue
        decision = str(row.get("decision") or "").strip().lower()
        if (
            not decision
            or decision in _NON_ACTIONABLE_SELECTION_DECISIONS
            or "block" in decision
        ):
            continue
        signal_id = str(row.get("signal_id") or "").strip()
        if signal_id:
            selected.setdefault(signal_id, row)
    return list(selected.values()), "exact_selection_store"


def _build_activation_labels(
    historical_signals: list[dict[str, Any]],
    sourced_outcomes: list[dict[str, Any]],
    exact_selections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build activation-only labels from exact selected, conclusive source truth."""

    signal_by_id = {
        str(row.get("signal_id") or ""): row
        for row in historical_signals
        if str(row.get("signal_id") or "")
    }
    selection_by_id = {
        str(row.get("signal_id") or ""): row
        for row in exact_selections
        if str(row.get("signal_id") or "")
    }
    labels: dict[str, dict[str, Any]] = {}
    for outcome in sourced_outcomes:
        signal_id = str(outcome.get("signal_id") or "").strip()
        selection = selection_by_id.get(signal_id)
        signal = signal_by_id.get(signal_id)
        if selection is None or signal is None:
            continue
        if not _is_conclusive_sourced_activation_outcome(outcome):
            continue
        outcome_status = str(outcome.get("outcome_status") or "").strip().lower()
        setup_key = str(
            signal.get("setup_key")
            or signal.get("primary_setup")
            or outcome.get("setup_key")
            or "unknown"
        )
        source_hash = str(outcome.get("source_bar_hash_sha256") or "")
        label_key = f"activation:{signal_id}:{source_hash}"
        labels[label_key] = {
            "label_key": label_key,
            "label_family": "activation",
            "signal_id": signal_id,
            "selection_id": str(selection.get("selection_id") or ""),
            "scan_id": str(outcome.get("scan_id") or signal.get("scan_id") or ""),
            "ticker": str(outcome.get("ticker") or signal.get("ticker") or "").upper(),
            "market_date": str(
                outcome.get("market_date") or signal.get("market_date") or ""
            )[:10],
            "setup_key": setup_key,
            "strategy_id": str(selection.get("strategy_id") or ""),
            "strategy_version": str(selection.get("strategy_version") or ""),
            "cohort": str(selection.get("cohort") or ""),
            "activation_status": (
                "triggered" if outcome_status == "complete_sourced" else "not_triggered"
            ),
            "activated": outcome_status == "complete_sourced",
            "activation_learning_eligible": True,
            "return_learning_eligible": bool(
                outcome_status == "complete_sourced"
                and outcome.get("learning_eligible") is True
            ),
            "outcome_status": outcome_status,
            "outcome_source": str(
                outcome.get("outcome_source") or outcome.get("source") or ""
            ),
            "outcome_source_url": str(outcome.get("source_url") or ""),
            "source_bar_hash_sha256": source_hash,
            "source_bar_count": int(outcome.get("source_bar_count") or 0),
            "validated_against_signal_timestamp": True,
            "no_lookahead": True,
            "created_at": str(
                outcome.get("captured_at") or outcome.get("imported_at") or ""
            ),
        }
    return list(labels.values())


def _is_conclusive_sourced_activation_outcome(row: dict[str, Any]) -> bool:
    status = str(row.get("outcome_status") or "").strip().lower()
    return bool(
        status in ACTIVATION_OUTCOME_STATUSES
        and row.get("automatic_sourced_data") is True
        and row.get("validated_against_signal_timestamp") is True
        and row.get("no_lookahead") is True
        and str(row.get("outcome_source") or row.get("source") or "").strip()
        and str(row.get("source_bar_hash_sha256") or "").strip()
    )


def _build_activation_report(
    labels: list[dict[str, Any]],
    *,
    selected_signal_count: int,
    selection_evidence_status: str,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for label in labels:
        grouped.setdefault(str(label.get("setup_key") or "unknown"), []).append(label)
    setup_stats = {
        setup_key: _activation_stats(rows)
        for setup_key, rows in sorted(grouped.items())
    }
    overall = _activation_stats(labels)
    if selection_evidence_status != "exact_selection_store":
        status = "selection_evidence_unavailable"
    elif labels:
        status = "complete"
    elif selected_signal_count:
        status = "no_conclusive_sourced_activation_outcomes"
    else:
        status = "no_exact_selected_signals"
    return {
        "label_family": "activation",
        "status": status,
        "evidence_basis": "exact_selected_conclusive_sourced_outcomes",
        "selected_signal_count": selected_signal_count,
        **overall,
        "setup_stats": setup_stats,
        "missing_activation_truth_is_zero": False,
    }


def _activation_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sample_size = len(rows)
    triggered_count = sum(1 for row in rows if row.get("activated") is True)
    return {
        "activation_sample_size": sample_size,
        "activation_triggered_count": triggered_count,
        "activation_not_triggered_count": sample_size - triggered_count,
        "activation_rate_pct": (
            round((triggered_count / sample_size) * 100.0, 2)
            if sample_size
            else None
        ),
    }


def _merge_activation_setup_memory(
    return_memory: dict[str, dict[str, Any]],
    activation_report: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Keep return and activation denominators explicit in one legacy payload."""

    activation_by_setup = dict(activation_report.get("setup_stats") or {})
    merged: dict[str, dict[str, Any]] = {}
    for setup_key in sorted(set(return_memory) | set(activation_by_setup)):
        return_row = return_memory.get(setup_key)
        if return_row is None:
            row: dict[str, Any] = {
                "setup_key": setup_key,
                "sample_size": 0,
                "return_sample_size": 0,
                "avg_return_pct": None,
                "median_return_pct": None,
                "win_rate_pct": None,
                "max_drawdown_pct": None,
                "outlier_dependency": None,
                "return_metrics_status": "not_available",
            }
        else:
            return_sample_size = int(return_row.get("sample_size") or 0)
            row = {
                **return_row,
                "return_sample_size": return_sample_size,
                "return_metrics_status": (
                    "available" if return_sample_size else "not_available"
                ),
            }
        activation = activation_by_setup.get(setup_key) or {
            "activation_sample_size": 0,
            "activation_triggered_count": 0,
            "activation_not_triggered_count": 0,
            "activation_rate_pct": None,
        }
        row.update(activation)
        row["activation_metrics_status"] = (
            "available" if activation["activation_sample_size"] else "not_available"
        )
        row["memory_contract_version"] = "alpha_setup_memory_v2_activation"
        merged[setup_key] = row
    return merged


def _learning_signal_from_historical(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("raw_payload_json") or {}
    payload = dict(raw) if isinstance(raw, dict) else {}
    return {
        **payload,
        **row,
        "entry_trigger": row.get("entry_watch_level"),
        "breakout_trigger": row.get("entry_watch_level"),
        "first_target": row.get("target_1"),
        "setup_key": row.get("primary_setup") or payload.get("setup_key") or "",
    }


def _dedupe_labels(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in sorted(
        rows,
        key=lambda item: bool(item.get("automatic_sourced_data")),
        reverse=True,
    ):
        output.setdefault(_label_identity(row), row)
    return list(output.values())


def _label_identity(row: dict[str, Any]) -> str:
    signal_id = str(row.get("signal_id") or "")
    if signal_id:
        return f"signal:{signal_id}"
    return ":".join((
        str(row.get("scan_id") or ""),
        str(row.get("ticker") or "").upper(),
        str(row.get("market_date") or ""),
    ))


def _signal_for_label(
    signals: list[dict[str, Any]],
    label: dict[str, Any],
) -> dict[str, Any] | None:
    signal_id = str(label.get("signal_id") or "")
    if signal_id:
        for signal in signals:
            if str(signal.get("signal_id") or "") == signal_id:
                return signal
    ticker = str(label.get("ticker") or "").upper()
    scan_id = str(label.get("scan_id") or "")
    for signal in signals:
        if (
            str(signal.get("ticker") or "").upper() == ticker
            and str(signal.get("scan_id") or "") == scan_id
        ):
            return signal
    for signal in signals:
        if str(signal.get("ticker") or "").upper() == ticker:
            return signal
    return None


def _real_days(rows: list[dict[str, Any]]) -> int:
    dates = {
        str(
            row.get("market_date")
            or row.get("recommendation_timestamp")
            or row.get("created_at")
            or ""
        )[:10]
        for row in rows
        if str(
            row.get("market_date")
            or row.get("recommendation_timestamp")
            or row.get("created_at")
            or ""
        )[:10]
    }
    return len(dates)


def _source_reliability_from_labels(
    previous: dict[str, dict[str, Any]],
    labels: list[dict[str, Any]],
    updated_at: str,
) -> list[dict[str, Any]]:
    if not previous:
        return []
    rows: list[dict[str, Any]] = []
    for source, prior in previous.items():
        source_labels = [
            row for row in labels if str(row.get("source") or "").lower() == source.lower()
        ]
        outcome_count = len(source_labels)
        winner_count = sum(1 for row in source_labels if row.get("winner_close") is True)
        rows_returned = int(prior.get("rows_returned") or 0)
        rows_normalized = int(prior.get("rows_normalized") or 0)
        rows_rejected = int(prior.get("rows_rejected") or 0)
        stale_count = int(prior.get("stale_count") or 0)
        missing_count = int(prior.get("missing_critical_count") or 0)
        rows.append({
            **prior,
            "source": source,
            "updated_at": updated_at,
            "outcome_count": outcome_count,
            "winner_count": winner_count,
            "reliability_score": reliability_score(
                rows_returned=rows_returned,
                rows_normalized=rows_normalized,
                rows_rejected=rows_rejected,
                stale_count=stale_count,
                missing_critical_count=missing_count,
                outcome_count=outcome_count,
                winner_count=winner_count,
            ),
        })
    return rows
