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
    close_values = [_number(row.get("close_return_pct")) for row in sorted_trades]
    lunch_values = [_number(row.get("lunch_return_pct")) for row in sorted_trades]
    high_values = [_number(row.get("high_return_pct")) for row in sorted_trades]
    low_values = [_number(row.get("low_drawdown_pct")) for row in sorted_trades]
    cumulative = _cumulative_curve(close_values)
    compounded_curve = _compounded_curve(close_values)
    return {
        "created_at": utc_now_iso(),
        "report_date": utc_now_iso()[:10],
        "run_id": latest_summary.get("run_id"),
        "fixture_only": bool(latest_summary.get("fixture_only", False)),
        "trade_count": len(sorted_trades),
        "avg_close_return_pct": _average(close_values),
        "avg_lunch_return_pct": _average(lunch_values),
        "avg_high_return_pct": _average(high_values),
        "median_close_return_pct": round(median(close_values), 2) if close_values else 0.0,
        "hit_rate_close_pct": _hit_rate(close_values),
        "max_drawdown_pct": min(low_values) if low_values else 0.0,
        "best_pick": _extreme_trade(sorted_trades, "close_return_pct", best=True),
        "worst_pick": _extreme_trade(sorted_trades, "close_return_pct", best=False),
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
    if not selected:
        return 0.0
    return round(sum(_number(row.get(key)) for row in selected) / len(selected), 2)


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
    if not rows:
        return None
    return max(rows, key=lambda row: _number(row.get(key))) if best else min(
        rows, key=lambda row: _number(row.get(key))
    )


def _number(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_audited(row: dict[str, Any]) -> bool:
    return row.get("audit_status", "audited") == "audited"
