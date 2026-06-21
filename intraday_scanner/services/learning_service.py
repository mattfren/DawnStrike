"""AlphaOps learning updates from shadow outcomes."""

from __future__ import annotations

import uuid
from typing import Any

from intraday_scanner.alpha.outcome_labeler import label_outcomes
from intraday_scanner.alpha.performance_truth import build_truth_report
from intraday_scanner.alpha.setup_memory import build_setup_memory
from intraday_scanner.models import utc_now_iso
from intraday_scanner.services.source_reliability_service import reliability_score
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def run_alpha_learning(store: SQLiteScanStore) -> dict[str, Any]:
    signals = store.load_alpha_signals(limit=5000)
    manual_outcomes = store.load_manual_outcomes(limit=5000)
    labels = label_outcomes(signals, manual_outcomes)
    now = utc_now_iso()
    for label in labels:
        label["created_at"] = now
        signal = _signal_for_label(signals, label)
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
    if labels:
        store.persist_alpha_outcome_labels(labels)
    all_labels = store.load_alpha_outcome_labels(limit=5000)
    memory = build_setup_memory(all_labels)
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
    payload = {
        "run_id": f"alpha-learn-{uuid.uuid4().hex[:12]}",
        "created_at": now,
        "status": "complete",
        "labels_created": len(labels),
        "total_labels": len(all_labels),
        "setup_memory_count": len(memory_rows),
        "source_reliability_count": len(reliability_rows),
        "truth_report": truth,
    }
    store.persist_alpha_learning_run(payload)
    return payload


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
