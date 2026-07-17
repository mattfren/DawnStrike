"""AlphaOps learning updates from shadow outcomes."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.setup_memory import build_setup_memory
from intraday_scanner.models import utc_now_iso
from intraday_scanner.services.alpha_paper_service import (
    ALPHAOPS_COHORT,
    ALPHAOPS_STRATEGY_ID,
)
from intraday_scanner.services.source_reliability_service import reliability_score
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def run_alpha_learning(store: SQLiteScanStore) -> dict[str, Any]:
    """Refresh AlphaOps learning only from canonical paper-reconciliation labels."""

    now = utc_now_iso()
    canonical = store.load_strategy_learning_labels(
        strategy_id=ALPHAOPS_STRATEGY_ID,
        cohort=ALPHAOPS_COHORT,
        limit=50_000,
    )
    activation_labels = [
        row
        for row in canonical
        if row.get("label_family") == "activation" and bool(row.get("eligible"))
    ]
    return_labels = [
        row
        for row in canonical
        if row.get("label_family") == "return_after_cost" and bool(row.get("eligible"))
        and row.get("label_value") is not None
    ]
    production_labels = [_production_label(row, now) for row in return_labels]
    store.replace_alpha_production_outcome_labels(production_labels)
    all_labels = production_labels
    memory = build_setup_memory(all_labels)
    activation_by_setup: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in activation_labels:
        activation_by_setup[str(row.get("setup_key") or "unknown")].append(row)
    memory_rows = []
    for setup_key in sorted(set(memory) | set(activation_by_setup)):
        summary = dict(memory.get(setup_key) or _empty_memory(setup_key))
        activation_rows = activation_by_setup.get(setup_key, [])
        activated = sum(float(row.get("label_value") or 0.0) > 0 for row in activation_rows)
        activation_rate = (
            round(activated / len(activation_rows) * 100.0, 4) if activation_rows else None
        )
        activation_adjustment = 0.0
        if activation_rate is not None and len(activation_rows) >= 20:
            activation_adjustment = round(
                max(-5.0, min(5.0, (activation_rate - 50.0) / 10.0)),
                4,
            )
        memory_rows.append(
            {
                **summary,
                "updated_at": now,
                "activation_sample_size": len(activation_rows),
                "activation_rate_pct": activation_rate,
                "activation_score_adjustment": activation_adjustment,
                "activation_adjustment_eligible": len(activation_rows) >= 20,
            }
        )
    store.replace_alpha_setup_memory(memory_rows)
    reliability_rows = _source_reliability_from_labels(
        store.load_alpha_source_reliability(),
        production_labels,
        now,
    )
    if reliability_rows:
        store.persist_alpha_source_reliability(reliability_rows)
    real_days = _real_days(production_labels)
    truth = build_truth_report(production_labels, real_days_collected=real_days)
    payload = {
        "run_id": f"alpha-learn-{uuid.uuid4().hex[:12]}",
        "created_at": now,
        "status": "complete",
        "labels_created": len(production_labels),
        "total_labels": len(production_labels),
        "activation_label_count": len(activation_labels),
        "return_label_count": len(return_labels),
        "learning_source": "strategy_learning_labels",
        "manual_outcomes_consumed": 0,
        "setup_memory_count": len(memory_rows),
        "source_reliability_count": len(reliability_rows),
        "truth_report": truth,
    }
    store.persist_alpha_learning_run(payload)
    return payload


def _production_label(row: dict[str, Any], refreshed_at: str) -> dict[str, Any]:
    value = float(row["label_value"])
    return {
        "label_key": f"strategy_learning:{row['label_id']}",
        "label_source": "strategy_learning",
        "strategy_label_id": row["label_id"],
        "evaluation_id": row.get("evaluation_id"),
        "scan_id": str(row.get("scan_id") or ""),
        "ticker": row.get("ticker"),
        "setup_key": str(row.get("setup_key") or "unknown"),
        "source": str(row.get("source") or ""),
        "created_at": str(row.get("created_at") or refreshed_at),
        "recommendation_timestamp": str(row.get("market_date") or ""),
        "return_pct": value,
        "close_return_pct": value,
        "realized_drawdown_pct": min(0.0, value),
        "winner_close": value > 0,
        "r_multiple": row.get("r_multiple"),
        "forward_observation": True,
        "after_cost": True,
        "source_bar_hash_sha256": row.get("source_bar_hash_sha256"),
    }


def _empty_memory(setup_key: str) -> dict[str, Any]:
    return {
        "setup_key": setup_key,
        "sample_size": 0,
        "avg_return_pct": 0.0,
        "median_return_pct": 0.0,
        "win_rate_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "outlier_dependency": 0.0,
    }


def _signal_for_label(
    signals: list[dict[str, Any]],
    label: dict[str, Any],
) -> dict[str, Any] | None:
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
        str(row.get("created_at") or row.get("recommendation_timestamp") or "")[:10]
        for row in rows
        if str(row.get("created_at") or row.get("recommendation_timestamp") or "")[:10]
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
