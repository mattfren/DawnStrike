"""Data and calibration drift reports that can quarantine V6 shadow scoring."""

from __future__ import annotations

from collections import Counter
from typing import Any

from intraday_scanner.alpha.v6.contracts import canonical_hash, utc_now


def build_drift_report(
    *, baseline_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Compare setup/regime composition; absent evidence is explicitly unknown."""

    baseline = _distribution(baseline_rows)
    current = _distribution(current_rows)
    if not baseline or not current:
        status = "QUARANTINE_INSUFFICIENT_DRIFT_EVIDENCE"
        score = None
    else:
        categories = sorted(set(baseline) | set(current))
        score = round(
            sum(abs(baseline.get(key, 0.0) - current.get(key, 0.0)) for key in categories)
            / 2.0,
            6,
        )
        status = "QUARANTINE_DRIFT" if score >= 0.25 else "STABLE"
    payload = {
        "created_at": utc_now(),
        "status": status,
        "composition_shift_score": score,
        "baseline_count": len(baseline_rows),
        "current_count": len(current_rows),
        "baseline_distribution": baseline,
        "current_distribution": current,
        "auto_quarantine": status.startswith("QUARANTINE"),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["drift_report_id"] = "v6dr-" + canonical_hash(payload)[:28]
    return payload


def _distribution(rows: list[dict[str, Any]]) -> dict[str, float]:
    keys = [
        "|".join((str(row.get("setup_key") or "unknown"), str(row.get("regime_key") or "UNKNOWN")))
        for row in rows
    ]
    if not keys:
        return {}
    counts = Counter(keys)
    total = len(keys)
    return {key: round(value / total, 6) for key, value in sorted(counts.items())}


__all__ = ["build_drift_report"]
