"""Performance truth summaries for AlphaOps dashboards and reports."""

from __future__ import annotations

from statistics import median
from typing import Any

from intraday_scanner.alpha.edge_calibrator import outlier_warning, score_decile


def build_truth_report(rows: list[dict[str, Any]], *, real_days_collected: int) -> dict[str, Any]:
    sorted_rows = sorted(rows, key=lambda row: int(_float(row.get("rank"), 999)))
    top1 = sorted_rows[:1]
    top3 = sorted_rows[:3]
    top5 = sorted_rows[:5]
    raw_returns = [_return(row) for row in rows]
    returns = [value for value in raw_returns if value is not None]
    by_decile = _bucket(rows, "score_decile")
    setup_buckets = {
        key: _return_summary(value) for key, value in _bucket(rows, "setup_key").items()
    }
    source_buckets = {key: _return_summary(value) for key, value in _bucket(rows, "source").items()}
    catalyst_buckets = {
        key: _return_summary(value) for key, value in _bucket(rows, "catalyst_category").items()
    }
    risk_buckets = _risk_flag_impact(rows)
    alpha_buckets = {
        key: _return_summary(value) for key, value in _bucket(rows, "edge_bucket").items()
    }
    missing_high = sum(1 for row in rows if row.get("missing_outcome_high") is True)
    drawdowns = [_drawdown(row) for row in rows]
    clean_drawdowns = [value for value in drawdowns if value is not None]
    evidence_warnings = _evidence_warnings(
        rows,
        real_days_collected=real_days_collected,
        missing_rate=(missing_high / len(rows)) * 100.0 if rows else 0.0,
    )
    report = {
        "real_days_collected": real_days_collected,
        "enough_evidence": real_days_collected >= 20,
        "insufficient_sample_warning": real_days_collected < 20,
        "strong_evidence": real_days_collected >= 60,
        "sample_size": len(rows),
        "top1": _return_summary(top1),
        "top3": _return_summary(top3),
        "top5": _return_summary(top5),
        "average_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "median_return_pct": round(float(median(returns)), 4) if returns else 0.0,
        "win_rate_pct": _win_rate(returns),
        "worst_day_return_pct": min(returns) if returns else 0.0,
        "best_day_return_pct": max(returns) if returns else 0.0,
        "max_drawdown_pct": min(clean_drawdowns) if clean_drawdowns else 0.0,
        "outlier": outlier_warning(rows),
        "missing_outcome_rate_pct": round((missing_high / len(rows)) * 100.0, 2) if rows else 0.0,
        "score_decile": {key: _return_summary(value) for key, value in by_decile.items()},
        "alpha_bucket_performance": alpha_buckets,
        "setup_bucket_returns": setup_buckets,
        "source_bucket_returns": source_buckets,
        "catalyst_bucket_returns": catalyst_buckets,
        "risk_flag_impact": risk_buckets,
        "best_worst_setup": _best_worst(setup_buckets),
        "best_worst_source": _best_worst(source_buckets),
        "best_worst_catalyst_bucket": _best_worst(catalyst_buckets),
        "best_worst_risk_flag": _best_worst(risk_buckets),
        "evidence_warnings": evidence_warnings,
        "can_claim_success": not evidence_warnings,
    }
    return report


def _return_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    raw_returns = [_return(row) for row in rows]
    returns = [value for value in raw_returns if value is not None]
    return {
        "sample_size": len(rows),
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "median_return_pct": round(float(median(returns)), 4) if returns else 0.0,
        "win_rate_pct": _win_rate(returns),
        "max_drawdown_pct": min(
            [value for value in (_drawdown(row) for row in rows) if value is not None],
            default=0.0,
        ),
    }


def _bucket(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        value = row.get(key)
        if key == "score_decile" and value in {None, ""}:
            value = score_decile(row.get("alpha_score") or row.get("score"))
        label = str(value or "unknown")
        grouped.setdefault(label, []).append(row)
    return grouped


def _risk_flag_impact(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    expanded: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        flags = _tokens(row.get("risk_flags")) or ["none"]
        for flag in flags:
            expanded.setdefault(flag, []).append(row)
    return {key: _return_summary(value) for key, value in expanded.items()}


def _return(row: dict[str, Any]) -> float | None:
    for key in ("high_after_entry_return", "return_pct", "close_return_pct"):
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _drawdown(row: dict[str, Any]) -> float | None:
    for key in ("low_after_entry_drawdown", "max_adverse_excursion", "drawdown_pct"):
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _best_worst(buckets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    eligible = {
        key: value
        for key, value in buckets.items()
        if int(value.get("sample_size") or 0) > 0
    }
    if not eligible:
        return {"best": None, "worst": None}
    best_key = max(eligible, key=lambda key: float(eligible[key].get("avg_return_pct") or 0.0))
    worst_key = min(eligible, key=lambda key: float(eligible[key].get("avg_return_pct") or 0.0))
    return {
        "best": {"bucket": best_key, **eligible[best_key]},
        "worst": {"bucket": worst_key, **eligible[worst_key]},
    }


def _evidence_warnings(
    rows: list[dict[str, Any]],
    *,
    real_days_collected: int,
    missing_rate: float,
) -> list[str]:
    warnings: list[str] = []
    if real_days_collected < 20:
        warnings.append("fewer_than_20_real_days")
    if real_days_collected < 60:
        warnings.append("fewer_than_60_strong_evidence_days")
    if outlier_warning(rows).get("outlier_dependent"):
        warnings.append("outlier_dependent")
    if missing_rate >= 25:
        warnings.append("high_missing_outcome_rate")
    if rows and all(_non_live_label(row) for row in rows):
        warnings.append("fixture_sample_or_manual_only")
    return warnings


def _non_live_label(row: dict[str, Any]) -> bool:
    if row.get("fixture_only") or row.get("manual_uploaded_data"):
        return True
    return str(row.get("data_source_kind") or "").lower() in {"sample", "fixture", "manual"}


def _win_rate(values: list[float]) -> float:
    if not values:
        return 0.0
    return round((sum(1 for value in values if value > 0) / len(values)) * 100.0, 2)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [
        part.strip()
        for part in str(value or "").replace(",", ";").split(";")
        if part.strip()
    ]
