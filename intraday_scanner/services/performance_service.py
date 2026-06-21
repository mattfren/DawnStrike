"""Historical performance reporting from persisted paper audits."""

from __future__ import annotations

from statistics import median
from typing import Any

from intraday_scanner.models import utc_now_iso


def build_performance_report(
    trades: list[dict[str, Any]], latest_summary: dict[str, Any] | None = None
) -> dict[str, Any]:
    latest_summary = latest_summary or {}
    sorted_trades = [row for row in reversed(trades) if _is_audited(row)]
    close_values = _numeric_values(sorted_trades, "close_return_pct")
    lunch_values = _numeric_values(sorted_trades, "lunch_return_pct")
    high_values = _numeric_values(sorted_trades, "high_return_pct")
    low_values = _numeric_values(sorted_trades, "low_drawdown_pct")
    cumulative = _cumulative_curve(close_values)
    compounded_curve = _compounded_curve(close_values)
    sample_size = len(close_values)
    return {
        "created_at": utc_now_iso(),
        "report_date": utc_now_iso()[:10],
        "run_id": latest_summary.get("run_id"),
        "fixture_only": bool(latest_summary.get("fixture_only", False)),
        "trade_count": len(sorted_trades),
        "sample_size": sample_size,
        "sample_status": "insufficient sample size" if sample_size < 20 else "enough sample",
        "insufficient_sample_warning": (
            "insufficient sample size." if sample_size < 20 else ""
        ),
        "avg_close_return_pct": _average(close_values),
        "avg_lunch_return_pct": _average(lunch_values),
        "avg_high_return_pct": _average(high_values),
        "median_close_return_pct": round(median(close_values), 2) if close_values else 0.0,
        "hit_rate_close_pct": _hit_rate(close_values),
        "max_drawdown_pct": min(low_values) if low_values else 0.0,
        "max_adverse_excursion_pct": min(low_values) if low_values else 0.0,
        "max_favorable_excursion_pct": max(high_values) if high_values else 0.0,
        "best_pick": _extreme_trade(sorted_trades, "close_return_pct", best=True),
        "worst_pick": _extreme_trade(sorted_trades, "close_return_pct", best=False),
        "best_day": _extreme_day(sorted_trades, best=True),
        "worst_day": _extreme_day(sorted_trades, best=False),
        "outlier_dependency_warning": _outlier_dependency_warning(close_values),
        "setup_bucket_returns": _bucket_returns(sorted_trades, "setup_grade"),
        "source_bucket_returns": _bucket_returns(sorted_trades, "source"),
        "catalyst_bucket_returns": _bucket_returns(sorted_trades, "catalyst_category"),
        "score_decile_returns": _score_decile_returns(sorted_trades),
        "top_1_close_return_pct": _portfolio_return(sorted_trades, "close_return_pct", 1),
        "top_3_close_return_pct": _portfolio_return(sorted_trades, "close_return_pct", 3),
        "top_5_close_return_pct": _portfolio_return(sorted_trades, "close_return_pct", 5),
        "cumulative_close_return_curve": cumulative,
        "cumulative_close_return_note": "Simple sum of audited close-return percentages.",
        "compounded_close_equity_curve": compounded_curve,
        "compounded_top_1_equity_curve": _compounded_portfolio_curve(
            sorted_trades, "close_return_pct", 1
        ),
        "compounded_top_3_equity_curve": _compounded_portfolio_curve(
            sorted_trades, "close_return_pct", 3
        ),
        "compounded_top_5_equity_curve": _compounded_portfolio_curve(
            sorted_trades, "close_return_pct", 5
        ),
        "latest_audit_summary": latest_summary,
    }


def format_performance_report(report: dict[str, Any]) -> str:
    return (
        f"performance: trades={report.get('trade_count', 0)} "
        f"avg_close={report.get('avg_close_return_pct', 0)}% "
        f"median_close={report.get('median_close_return_pct', 0)}% "
        f"hit_rate={report.get('hit_rate_close_pct', 0)}% "
        f"max_drawdown={report.get('max_drawdown_pct', 0)}%"
    )


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 2) if values else 0.0


def _hit_rate(values: list[float]) -> float:
    if not values:
        return 0.0
    return round((sum(1 for value in values if value > 0) / len(values)) * 100, 2)


def _portfolio_return(rows: list[dict[str, Any]], key: str, count: int) -> float:
    selected = rows[:count]
    values = _numeric_values(selected, key)
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def _cumulative_curve(values: list[float]) -> list[dict[str, Any]]:
    total = 0.0
    curve = []
    for index, value in enumerate(values, start=1):
        total += value
        curve.append({"step": index, "cumulative_return_pct": round(total, 2)})
    return curve


def _compounded_curve(values: list[float]) -> list[dict[str, Any]]:
    equity = 1.0
    curve = []
    for index, value in enumerate(values, start=1):
        equity *= 1 + (value / 100.0)
        curve.append(
            {
                "step": index,
                "equity": round(equity, 6),
                "compounded_return_pct": round((equity - 1) * 100, 2),
            }
        )
    return curve


def _compounded_portfolio_curve(
    rows: list[dict[str, Any]], key: str, count: int
) -> list[dict[str, Any]]:
    equity = 1.0
    curve = []
    for step, index in enumerate(range(0, len(rows), count), start=1):
        basket = rows[index : index + count]
        if not basket:
            continue
        basket_return = sum(_number(row.get(key)) for row in basket) / len(basket)
        equity *= 1 + (basket_return / 100.0)
        curve.append(
            {
                "step": step,
                "basket_size": len(basket),
                "basket_return_pct": round(basket_return, 2),
                "equity": round(equity, 6),
                "compounded_return_pct": round((equity - 1) * 100, 2),
            }
        )
    return curve


def _extreme_trade(rows: list[dict[str, Any]], key: str, *, best: bool) -> dict[str, Any] | None:
    rows_with_values = [row for row in rows if _number_or_none(row.get(key)) is not None]
    if not rows_with_values:
        return None
    return max(rows_with_values, key=lambda row: _number(row.get(key))) if best else min(
        rows_with_values, key=lambda row: _number(row.get(key))
    )


def _extreme_day(rows: list[dict[str, Any]], *, best: bool) -> dict[str, Any] | None:
    by_day: dict[str, list[float]] = {}
    for row in rows:
        day = str(row.get("trade_date") or row.get("run_date") or row.get("date") or "")
        value = _number_or_none(row.get("close_return_pct"))
        if not day or value is None:
            continue
        by_day.setdefault(day, []).append(value)
    if not by_day:
        return None
    day, values = (
        max(by_day.items(), key=lambda item: sum(item[1]) / len(item[1]))
        if best
        else min(by_day.items(), key=lambda item: sum(item[1]) / len(item[1]))
    )
    return {
        "date": day,
        "avg_close_return_pct": round(sum(values) / len(values), 2),
        "sample_size": len(values),
    }


def _bucket_returns(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    buckets: dict[str, list[float]] = {}
    for row in rows:
        bucket = str(row.get(key) or "unknown")
        value = _number_or_none(row.get("close_return_pct"))
        if value is None:
            continue
        buckets.setdefault(bucket, []).append(value)
    return [
        {
            "bucket": bucket,
            "sample_size": len(values),
            "avg_close_return_pct": _average(values),
            "median_close_return_pct": round(median(values), 2) if values else 0.0,
        }
        for bucket, values in sorted(buckets.items())
    ]


def _score_decile_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bucketed = []
    for row in rows:
        score = _number_or_none(row.get("score"))
        value = _number_or_none(row.get("close_return_pct"))
        if score is None or value is None:
            continue
        decile = int(max(0, min(9, score // 10))) * 10
        bucketed.append({**row, "score_decile": f"{decile}-{decile + 9}", "close": value})
    buckets: dict[str, list[float]] = {}
    for row in bucketed:
        buckets.setdefault(str(row["score_decile"]), []).append(float(row["close"]))
    return [
        {
            "bucket": bucket,
            "sample_size": len(values),
            "avg_close_return_pct": _average(values),
            "median_close_return_pct": round(median(values), 2) if values else 0.0,
        }
        for bucket, values in sorted(buckets.items())
    ]


def _outlier_dependency_warning(values: list[float]) -> str:
    if len(values) < 5:
        return "insufficient sample size."
    total = sum(values)
    if total == 0:
        return ""
    largest = max(values, key=abs)
    if abs(largest) / max(abs(total), 0.01) >= 0.5:
        return "Results depend heavily on one outlier."
    return ""


def _numeric_values(rows: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        parsed = _number_or_none(row.get(key))
        if parsed is not None:
            values.append(parsed)
    return values


def _number(value: Any) -> float:
    parsed = _number_or_none(value)
    return 0.0 if parsed is None else parsed


def _number_or_none(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_audited(row: dict[str, Any]) -> bool:
    return row.get("audit_status", "audited") == "audited"
