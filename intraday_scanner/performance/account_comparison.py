"""Fail-closed V5/V6 account and benchmark comparison contract.

This module intentionally accepts only authoritative paper-account ledger rows
for V5 and V6.  V6 decision or signal-level outcomes are not account returns,
so they are never converted into a synthetic V6 equity curve here.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from intraday_scanner.performance.contracts import safe_float, stable_hash

ACCOUNT_COMPARISON_VERSION = "dawnstrike.account-comparison.v1"
_ACCOUNT_SERIES = ("v5", "v6")
_REFERENCE_SERIES = ("cash", "SPY", "IWM")
_VALID_ACCOUNT_STATUSES = {"COMPLETE", "NO_TRADE"}


def build_account_comparison(
    *,
    v5_ledger: Iterable[dict[str, Any]],
    v6_ledger: Iterable[dict[str, Any]],
    benchmark_rows: Iterable[dict[str, Any]],
    calculated_at: str,
) -> dict[str, Any]:
    """Build one date-aligned comparison or a precise non-publishable receipt.

    The comparison is complete only when both authoritative account ledgers and
    sourced SPY/IWM observations cover every eligible account session.  A cash
    return is an explicit zero-interest reference policy, not a missing-value
    default.  No input is mutated.
    """

    v5_rows = list(v5_ledger)
    v6_rows = list(v6_ledger)
    benchmark_input = list(benchmark_rows)
    v5_by_date, v5_issues = _account_returns(v5_rows)
    v6_by_date, v6_issues = _account_returns(v6_rows)
    benchmark_by_symbol, benchmark_issues = _benchmark_returns(benchmark_input)
    eligible_dates = sorted(set(v5_by_date) | set(v6_by_date))
    values_by_series: dict[str, dict[str, float]] = {
        "v5": v5_by_date,
        "v6": v6_by_date,
        "cash": {day: 0.0 for day in eligible_dates},
        "SPY": benchmark_by_symbol["SPY"],
        "IWM": benchmark_by_symbol["IWM"],
    }
    missing_dates = {
        series: [day for day in eligible_dates if day not in values]
        for series, values in values_by_series.items()
    }
    aligned_dates = [
        day
        for day in eligible_dates
        if all(day not in missing_dates[series] for series in (*_ACCOUNT_SERIES, "SPY", "IWM"))
    ]
    coverage_pct = (
        round(100.0 * len(aligned_dates) / len(eligible_dates), 6) if eligible_dates else None
    )
    blockers: list[str] = []
    v5_identity = _account_identity(v5_rows)
    v6_identity = _account_identity(v6_rows)
    if not v5_by_date:
        blockers.append("missing_authoritative_v5_account_ledger")
    if not v6_by_date:
        blockers.append("missing_authoritative_v6_account_ledger")
    if not benchmark_by_symbol["SPY"]:
        blockers.append("missing_sourced_spy_benchmark")
    if not benchmark_by_symbol["IWM"]:
        blockers.append("missing_sourced_iwm_benchmark")
    if v5_identity["status"] == "CONFLICT":
        blockers.append("conflicting_v5_account_identity")
    if v6_identity["status"] == "CONFLICT":
        blockers.append("conflicting_v6_account_identity")
    if eligible_dates and len(aligned_dates) != len(eligible_dates):
        blockers.append("date_alignment_or_coverage_incomplete")
    blockers.extend(v5_issues)
    blockers.extend(v6_issues)
    blockers.extend(benchmark_issues)
    blockers = list(dict.fromkeys(blockers))
    status = (
        "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
        if eligible_dates and not blockers
        else "WAITING_FOR_AUTHORITATIVE_V6_ACCOUNT_LEDGER"
        if not v6_by_date
        else "NOT_PUBLISHABLE_INCOMPLETE_ACCOUNT_OR_BENCHMARK_TRUTH"
    )
    metrics = {
        "v5": _account_metrics(v5_rows, aligned_dates, v5_by_date)
        if status == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
        else _missing_metrics(),
        "v6": _account_metrics(v6_rows, aligned_dates, v6_by_date)
        if status == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
        else _missing_metrics(),
        "cash": _reference_metrics(aligned_dates, values_by_series["cash"])
        if status == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
        else _missing_metrics(),
        "SPY": _reference_metrics(aligned_dates, values_by_series["SPY"])
        if status == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
        else _missing_metrics(),
        "IWM": _reference_metrics(aligned_dates, values_by_series["IWM"])
        if status == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
        else _missing_metrics(),
    }
    identities = {
        "v5": v5_identity,
        "v6": v6_identity,
        "cash": {
            "reference_policy": "cash_zero_interest_v1",
            "return_source": "explicit_zero_interest_reference_policy",
        },
        "SPY": _benchmark_identity(benchmark_input, "SPY"),
        "IWM": _benchmark_identity(benchmark_input, "IWM"),
    }
    content = {
        "schema_version": ACCOUNT_COMPARISON_VERSION,
        "calculated_at": calculated_at,
        "status": status,
        "promotion_eligible": False,
        "promotion_blockers": blockers,
        "alignment": {
            "eligible_session_count": len(eligible_dates),
            "aligned_session_count": len(aligned_dates),
            "coverage_pct": coverage_pct,
            "missing_session_counts": {series: len(days) for series, days in missing_dates.items()},
            "date_alignment_rule": "same_market_date_complete_v5_v6_spy_iwm",
        },
        "series_identities": identities,
        "series_metrics": metrics,
        "account_truth_rule": (
            "only_authoritative_paper_account_ledger_rows_can_produce_account_returns"
        ),
        "cash_reference_rule": "cash_zero_interest_v1",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    input_hash = stable_hash(
        {
            "v5_ledger": v5_rows,
            "v6_ledger": v6_rows,
            "benchmark_rows": benchmark_input,
            "schema_version": ACCOUNT_COMPARISON_VERSION,
        }
    )
    return {
        **content,
        "input_hash_sha256": input_hash,
        "comparison_id": "acmp-" + stable_hash({**content, "input_hash": input_hash})[:28],
    }


def public_account_comparison(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Expose safe aggregate status without raw ledger, dates, or source rows."""

    if not report:
        return None
    alignment = report.get("alignment")
    metric_data = report.get("series_metrics")
    return {
        "schema_version": report.get("schema_version"),
        "calculated_at": report.get("calculated_at"),
        "status": report.get("status"),
        "promotion_eligible": False,
        "promotion_blockers": list(report.get("promotion_blockers") or []),
        "alignment": alignment if isinstance(alignment, dict) else {},
        "series_metrics": metric_data if isinstance(metric_data, dict) else {},
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _account_returns(rows: list[dict[str, Any]]) -> tuple[dict[str, float], list[str]]:
    output: dict[str, float] = {}
    issues: list[str] = []
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        market_date = str(row.get("market_date") or "")[:10]
        if market_date:
            by_date[market_date].append(row)
    for market_date, values in sorted(by_date.items()):
        if len(values) != 1:
            issues.append("duplicate_authoritative_account_ledger_date")
            continue
        row = values[0]
        status = str(row.get("status") or "").upper()
        value = safe_float(row.get("net_return_pct"))
        if status not in _VALID_ACCOUNT_STATUSES or value is None:
            issues.append("incomplete_authoritative_account_ledger")
            continue
        if status == "NO_TRADE" and value != 0.0:
            issues.append("invalid_no_trade_account_return")
            continue
        if not str(row.get("source_hash_sha256") or "").strip():
            issues.append("missing_account_ledger_source_hash")
            continue
        output[market_date] = value
    return output, issues


def _benchmark_returns(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, float]], list[str]]:
    output: dict[str, dict[str, float]] = {"SPY": {}, "IWM": {}}
    issues: list[str] = []
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        market_date = str(row.get("market_date") or "")[:10]
        if symbol in output and market_date:
            grouped[(symbol, market_date)].append(row)
    for (symbol, market_date), values in sorted(grouped.items()):
        if len(values) != 1:
            issues.append("duplicate_benchmark_observation")
            continue
        value = safe_float(values[0].get("return_close"))
        if value is None or not _benchmark_lineage_hash(values[0]):
            issues.append("incomplete_benchmark_return")
            continue
        output[symbol][market_date] = value
    return output, issues


def _account_metrics(
    rows: list[dict[str, Any]], dates: list[str], returns_by_date: dict[str, float]
) -> dict[str, Any]:
    selected = [returns_by_date[day] for day in dates]
    equity = 1.0
    high = equity
    drawdowns: list[float] = []
    for value in selected:
        equity *= 1.0 + value / 100.0
        high = max(high, equity)
        drawdowns.append((equity / high - 1.0) * 100.0)
    positive = sum(value for value in selected if value > 0)
    negative = abs(sum(value for value in selected if value < 0))
    row_by_date = {str(row.get("market_date") or "")[:10]: row for row in rows}
    pnl_values = [
        abs(safe_float(row_by_date[day].get("realized_net_pnl_cents")) or 0.0) for day in dates
    ]
    pnl_total = sum(pnl_values)
    return {
        "status": "COMPLETE",
        "session_count": len(selected),
        "compounded_net_return_pct": round((equity - 1.0) * 100.0, 6),
        "expectancy_pct": round(sum(selected) / len(selected), 6),
        "maximum_drawdown_pct": round(min(drawdowns), 6) if drawdowns else None,
        "profit_factor": round(positive / negative, 6) if negative else None,
        "turnover_trades_per_session": round(
            sum(int(row_by_date[day].get("trade_count") or 0) for day in dates) / len(selected),
            6,
        ),
        "gain_loss_concentration_pct": (
            round(100.0 * max(pnl_values) / pnl_total, 6) if pnl_total else None
        ),
        "capacity": None,
        "opening_equity_cents": _integer(row_by_date[dates[0]].get("beginning_equity_cents")),
        "ending_equity_cents": _integer(row_by_date[dates[-1]].get("ending_equity_cents")),
        "return_basis": "account_equity_identity_after_external_flows",
    }


def _reference_metrics(dates: list[str], returns_by_date: dict[str, float]) -> dict[str, Any]:
    selected = [returns_by_date[day] for day in dates]
    equity = 1.0
    high = equity
    drawdowns: list[float] = []
    for value in selected:
        equity *= 1.0 + value / 100.0
        high = max(high, equity)
        drawdowns.append((equity / high - 1.0) * 100.0)
    positive = sum(value for value in selected if value > 0)
    negative = abs(sum(value for value in selected if value < 0))
    return {
        "status": "COMPLETE",
        "session_count": len(selected),
        "compounded_net_return_pct": round((equity - 1.0) * 100.0, 6),
        "expectancy_pct": round(sum(selected) / len(selected), 6),
        "maximum_drawdown_pct": round(min(drawdowns), 6) if drawdowns else None,
        "profit_factor": round(positive / negative, 6) if negative else None,
        "turnover_trades_per_session": 0.0,
        "gain_loss_concentration_pct": None,
        "capacity": None,
        "opening_equity_cents": None,
        "ending_equity_cents": None,
        "return_basis": "sourced_reference_return" if selected else None,
    }


def _missing_metrics() -> dict[str, Any]:
    return {
        "status": "NOT_PUBLISHABLE_INCOMPLETE_TRUTH",
        "session_count": 0,
        "compounded_net_return_pct": None,
        "expectancy_pct": None,
        "maximum_drawdown_pct": None,
        "profit_factor": None,
        "turnover_trades_per_session": None,
        "gain_loss_concentration_pct": None,
        "capacity": None,
        "opening_equity_cents": None,
        "ending_equity_cents": None,
        "return_basis": None,
    }


def _account_identity(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"status": "MISSING_AUTHORITATIVE_ACCOUNT_LEDGER"}
    identities = {
        (
            str(row.get("account_id") or ""),
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
            str(row.get("execution_policy_version") or ""),
            str(row.get("cost_model_version") or ""),
        )
        for row in rows
    }
    return {
        "status": "SINGLE_IMMUTABLE_ACCOUNT_IDENTITY" if len(identities) == 1 else "CONFLICT",
        "identity_count": len(identities),
    }


def _benchmark_identity(rows: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    count = sum(1 for row in rows if str(row.get("symbol") or "").upper() == symbol)
    return {"status": "PRESENT" if count else "MISSING", "observation_count": count}


def _benchmark_lineage_hash(row: dict[str, Any]) -> str | None:
    for key in ("source_bar_hash_sha256", "source_hash_sha256"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if isinstance(payload, dict):
        for key in ("source_bar_hash_sha256", "source_hash_sha256"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
    return None


def _integer(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(float(result)) else None


__all__ = [
    "ACCOUNT_COMPARISON_VERSION",
    "build_account_comparison",
    "public_account_comparison",
]
