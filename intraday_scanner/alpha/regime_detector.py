"""Lightweight market/regime labels for AlphaOps reports."""

from __future__ import annotations

from typing import Any


def detect_regime(
    candidates: list[dict[str, Any]],
    source_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_summary = dict(source_summary or {})
    gaps = [_float(row.get("gap_pct"), 0.0) for row in candidates]
    volumes = [_float(row.get("dollar_volume"), 0.0) for row in candidates]
    clean = [row for row in candidates if str(row.get("avoid_reasons") or "").strip() == ""]
    average_gap = sum(gaps) / len(gaps) if gaps else 0.0
    average_volume = sum(volumes) / len(volumes) if volumes else 0.0
    if not candidates:
        label = "NO_DATA"
    elif average_gap >= 120 and average_volume >= 5_000_000:
        label = "HOT_HIGH_BETA"
    elif len(clean) >= 3:
        label = "SELECTIVE"
    else:
        label = "THIN_OR_RISKY"
    return {
        "regime": label,
        "candidate_count": len(candidates),
        "clean_count": len(clean),
        "average_gap_pct": round(average_gap, 2),
        "average_dollar_volume": round(average_volume, 2),
        "source_status": source_summary.get("status") or "",
    }


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
