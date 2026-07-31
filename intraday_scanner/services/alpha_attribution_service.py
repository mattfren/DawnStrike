"""Causal AlphaOps attribution from sourced, reconciled paper evidence.

The report is descriptive research evidence. It never scores a live order,
changes a strategy, or converts missing outcomes into zero-return observations.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any

from intraday_scanner.storage.sqlite_store import SQLiteScanStore

ALPHA_STRATEGIES = frozenset({"alphaops_v4", "alphaops_v5"})
OFFICIAL_COHORT = "official_telegram"
ATTRIBUTION_VERSION = "alphaops-causal-attribution-v1"


def generate_alpha_attribution_report(
    *,
    db_path: str | Path,
    out_dir: str | Path = "outputs/alpha_attribution",
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    signals = store.load_historical_signals(start=start, end=end, limit=50_000)
    selections = [
        row
        for row in store.load_signal_selections(limit=50_000)
        if str(row.get("strategy_id") or "") in ALPHA_STRATEGIES
        and _within(str(row.get("selected_at") or "")[:10], start, end)
    ]
    evaluations = [
        row
        for row in store.load_strategy_evaluations(
            start=start,
            end=end,
            limit=50_000,
        )
        if str(row.get("strategy_id") or "") in ALPHA_STRATEGIES
    ]
    trades = [
        row
        for row in store.load_strategy_paper_trades(
            start=start,
            end=end,
            limit=50_000,
        )
        if str(row.get("strategy_id") or "") in ALPHA_STRATEGIES
    ]
    attempts = [
        row
        for row in store.load_outcome_capture_attempts(limit=50_000)
        if _within(str(row.get("market_date") or "")[:10], start, end)
    ]
    intents = [
        row
        for row in store.load_trade_intents(limit=50_000)
        if _within(str(row.get("market_date") or "")[:10], start, end)
    ]
    report = build_alpha_attribution_report(
        signals=signals,
        selections=selections,
        evaluations=evaluations,
        trades=trades,
        attempts=attempts,
        intents=intents,
        generated_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    _atomic_json(output / "alpha_attribution.json", report)
    _atomic_text(output / "alpha_attribution.md", _markdown(report))
    return {
        **report,
        "artifacts": {
            "json": str(output / "alpha_attribution.json"),
            "markdown": str(output / "alpha_attribution.md"),
        },
    }


def build_alpha_attribution_report(
    *,
    signals: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    signal_by_id = {
        str(row.get("signal_id") or ""): row
        for row in signals
        if str(row.get("signal_id") or "")
    }
    selection_by_signal = {
        str(row.get("signal_id") or ""): row
        for row in selections
        if str(row.get("signal_id") or "")
    }
    enriched = [
        _enriched_trade(row, signal_by_id, selection_by_signal)
        for row in trades
    ]
    official = [
        row for row in enriched if str(row.get("cohort") or "") == OFFICIAL_COHORT
    ]
    dates = sorted({
        _row_date(row)
        for row in [*selections, *evaluations, *trades, *attempts]
        if _row_date(row)
    })
    daily = [
        _daily_row(
            day,
            selections=selections,
            evaluations=evaluations,
            trades=enriched,
            attempts=attempts,
        )
        for day in dates
    ]
    buckets = {
        "cohort": _bucket_summaries(enriched, "cohort"),
        "strategy": _bucket_summaries(enriched, "strategy_id"),
        "selection_decision": _bucket_summaries(enriched, "selection_decision"),
        "setup": _bucket_summaries(enriched, "setup_key"),
        "gap_bucket": _bucket_summaries(enriched, "gap_bucket"),
        "catalyst_class": _bucket_summaries(enriched, "catalyst_class"),
        "float_bucket": _bucket_summaries(enriched, "float_bucket"),
        "liquidity_bucket": _bucket_summaries(enriched, "liquidity_bucket"),
        "market_regime": _bucket_summaries(enriched, "market_regime"),
        "sector_regime": _bucket_summaries(enriched, "sector_regime"),
        "source_confidence_bucket": _bucket_summaries(
            enriched,
            "source_confidence_bucket",
        ),
    }
    terminal_missing = [
        row for row in attempts if str(row.get("status") or "") == "terminal_missing"
    ]
    report: dict[str, Any] = {
        "schema_version": "dawnstrike.alpha_attribution.v1",
        "attribution_version": ATTRIBUTION_VERSION,
        "generated_at": generated_at,
        "status": "complete" if evaluations or trades or attempts else "no_evidence",
        "evidence_cutoff": max(dates, default=None),
        "official": _trade_summary(official),
        "all_research_cohorts": _trade_summary(enriched),
        "daily": daily,
        "buckets": buckets,
        "loss_concentration": _concentration(official),
        "symbol_concentration": _symbol_concentration(official),
        "decision_gate_effectiveness": _gate_effectiveness(
            evaluations,
            intents,
            trades,
        ),
        "entry_failure_modes": _entry_failure_modes(evaluations, intents),
        "exit_modes": _exit_modes(official),
        "outcome_coverage": {
            "attempt_count": len(attempts),
            "resolved_count": len(attempts) - len(terminal_missing),
            "terminal_missing_count": len(terminal_missing),
            "coverage_pct": (
                round(
                    ((len(attempts) - len(terminal_missing)) / len(attempts)) * 100.0,
                    4,
                )
                if attempts
                else None
            ),
            "missing_is_zero": False,
        },
        "sample_warning": (
            "insufficient_forward_sample"
            if len(official) < 100
            else "forward_sample_size_gate_met"
        ),
        "promotion_status": "operator_review_required_not_promoted",
        "research_only": True,
        "broker_execution_enabled": False,
        "personalized_advice": False,
        "limitations": [
            "Attribution is observational and does not prove a strategy is profitable.",
            "Expected metrics remain null where the original signal stored no expectation.",
            "Missing outcomes and no-trade decisions are excluded from return denominators.",
            "Promotion still requires the separately versioned strict forward-evidence gate.",
        ],
    }
    report["input_hash_sha256"] = _hash({
        "signals": signals,
        "selections": selections,
        "evaluations": evaluations,
        "trades": trades,
        "attempts": attempts,
        "intents": intents,
    })
    report["payload_hash_sha256"] = _hash(report)
    return report


def _daily_row(
    day: str,
    *,
    selections: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    day_selections = [
        row for row in selections if str(row.get("selected_at") or "")[:10] == day
    ]
    day_evaluations = [
        row for row in evaluations if str(row.get("market_date") or "")[:10] == day
    ]
    day_trades = [
        row for row in trades if str(row.get("market_date") or "")[:10] == day
    ]
    day_attempts = [
        row for row in attempts if str(row.get("market_date") or "")[:10] == day
    ]
    explicit_no_trade = any(
        str(row.get("decision") or "").lower() == "no_trade"
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
        for row in day_selections
    )
    missing = sum(
        1 for row in day_attempts if str(row.get("status") or "") == "terminal_missing"
    )
    if missing:
        status = "MISSING"
    elif day_trades:
        status = "COMPLETE"
    elif explicit_no_trade:
        status = "NO_TRADE"
    elif day_evaluations:
        status = "COMPLETE_NO_FILL"
    else:
        status = "PENDING"
    summary = _trade_summary(day_trades)
    return {
        "market_date": day,
        "status": status,
        "selection_count": len(day_selections),
        "evaluation_count": len(day_evaluations),
        "terminal_missing_count": missing,
        **summary,
    }


def _enriched_trade(
    trade: dict[str, Any],
    signal_by_id: dict[str, dict[str, Any]],
    selection_by_signal: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    signal = signal_by_id.get(str(trade.get("signal_id") or ""), {})
    selection = selection_by_signal.get(str(trade.get("signal_id") or ""), {})
    raw = signal.get("raw_payload_json")
    facts = dict(raw) if isinstance(raw, dict) else {}
    merged = {**facts, **signal}
    gap = _number(merged.get("gap_pct"))
    float_shares = _number(merged.get("float_shares"))
    liquidity = _first_number(
        merged,
        "dollar_volume",
        "premarket_dollar_volume",
    )
    confidence = _first_number(
        merged,
        "source_confidence",
        "data_confidence",
    )
    expected_hit = _first_number(
        merged,
        "expected_win_probability_pct",
        "calibrated_probability_pct",
        "predicted_probability_pct",
        "probability_pct",
    )
    if expected_hit is None:
        probability = _first_number(
            merged,
            "expected_win_probability",
            "calibrated_probability",
            "predicted_probability",
            "probability",
        )
        expected_hit = (
            probability * 100.0
            if probability is not None and probability <= 1
            else probability
        )
    return {
        **trade,
        "selection_decision": selection.get("decision") or "unlinked",
        "setup_key": (
            merged.get("setup_key")
            or merged.get("primary_setup")
            or "unknown"
        ),
        "gap_bucket": _gap_bucket(gap),
        "catalyst_class": (
            merged.get("catalyst_category")
            or merged.get("catalyst_class")
            or "unknown"
        ),
        "float_bucket": _float_bucket(float_shares),
        "liquidity_bucket": _liquidity_bucket(liquidity),
        "market_regime": merged.get("market_regime") or merged.get("regime") or "unknown",
        "sector_regime": merged.get("sector_regime") or "unknown",
        "source_confidence_bucket": _confidence_bucket(confidence),
        "expected_hit_rate_pct": expected_hit,
        "expected_r_multiple": _first_number(
            merged,
            "expected_r_multiple",
            "actual_after_cost_reward_risk",
            "after_cost_reward_risk",
            "reward_risk_ratio",
        ),
    }


def _trade_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = _numbers(rows, "net_return_pct")
    r_values = _numbers(rows, "r_multiple")
    pnl_values = _numbers(rows, "net_pnl")
    expected_hits = _numbers(rows, "expected_hit_rate_pct")
    expected_rs = _numbers(rows, "expected_r_multiple")
    wins = sum(1 for value in pnl_values if value > 0)
    losses = sum(1 for value in pnl_values if value < 0)
    observed_hit = round((wins / len(pnl_values)) * 100.0, 4) if pnl_values else None
    expected_hit = round(mean(expected_hits), 4) if expected_hits else None
    expected_r = round(mean(expected_rs), 4) if expected_rs else None
    observed_r = round(mean(r_values), 4) if r_values else None
    gross_values = _numbers(rows, "gross_pnl")
    return {
        "trade_count": len(rows),
        "wins": wins,
        "losses": losses,
        "flats": len(pnl_values) - wins - losses,
        "net_pnl": round(sum(pnl_values), 4) if pnl_values else None,
        "gross_pnl": round(sum(gross_values), 4) if gross_values else None,
        "average_net_return_pct": round(mean(returns), 4) if returns else None,
        "median_net_return_pct": round(median(returns), 4) if returns else None,
        "expected_hit_rate_pct": expected_hit,
        "observed_hit_rate_pct": observed_hit,
        "hit_rate_delta_pct": (
            round(observed_hit - expected_hit, 4)
            if observed_hit is not None and expected_hit is not None
            else None
        ),
        "expected_r_multiple": expected_r,
        "observed_r_multiple": observed_r,
        "r_delta": (
            round(observed_r - expected_r, 4)
            if observed_r is not None and expected_r is not None
            else None
        ),
        "missing_is_zero": False,
    }


def _bucket_summaries(
    rows: list[dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(field) or "unknown"), []).append(row)
    return [
        {"bucket": key, **_trade_summary(grouped[key])}
        for key in sorted(grouped)
    ]


def _concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    gains = sorted(
        (value for value in _numbers(rows, "net_pnl") if value > 0),
        reverse=True,
    )
    losses = sorted(
        (abs(value) for value in _numbers(rows, "net_pnl") if value < 0),
        reverse=True,
    )
    return {
        "largest_gain_share_pct": (
            round((gains[0] / sum(gains)) * 100.0, 4) if gains else None
        ),
        "largest_loss_share_pct": (
            round((losses[0] / sum(losses)) * 100.0, 4) if losses else None
        ),
        "gain_count": len(gains),
        "loss_count": len(losses),
    }


def _symbol_concentration(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl_by_symbol: dict[str, float] = {}
    count_by_symbol: Counter[str] = Counter()
    for row in rows:
        symbol = str(row.get("ticker") or "UNKNOWN").upper()
        pnl = _number(row.get("net_pnl"))
        if pnl is not None:
            pnl_by_symbol[symbol] = pnl_by_symbol.get(symbol, 0.0) + pnl
        count_by_symbol[symbol] += 1
    total_abs = sum(abs(value) for value in pnl_by_symbol.values())
    dominant = (
        max(pnl_by_symbol, key=lambda key: abs(pnl_by_symbol[key]))
        if pnl_by_symbol
        else None
    )
    most_frequent = count_by_symbol.most_common(1)[0] if count_by_symbol else None
    return {
        "dominant_pnl_symbol": dominant,
        "dominant_absolute_pnl_share_pct": (
            round((abs(pnl_by_symbol[dominant]) / total_abs) * 100.0, 4)
            if dominant is not None and total_abs > 0
            else None
        ),
        "most_frequent_symbol": most_frequent[0] if most_frequent else None,
        "most_frequent_trade_share_pct": (
            round((most_frequent[1] / len(rows)) * 100.0, 4)
            if most_frequent and rows
            else None
        ),
    }


def _gate_effectiveness(
    evaluations: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trade_by_signal = {
        str(row.get("signal_id") or ""): row
        for row in trades
        if str(row.get("signal_id") or "")
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in evaluations:
        key = str(row.get("terminal_state") or row.get("reconciliation_status") or "unknown")
        grouped.setdefault(key, []).append(row)
    output = []
    for key in sorted(grouped):
        group = grouped[key]
        linked = [
            trade_by_signal[str(row.get("signal_id") or "")]
            for row in group
            if str(row.get("signal_id") or "") in trade_by_signal
        ]
        output.append({
            "gate_outcome": key,
            "evaluation_count": len(group),
            "closed_trade_count": len(linked),
            "conversion_pct": round((len(linked) / len(group)) * 100.0, 4),
            "closed_trade_performance": _trade_summary(linked),
        })
    actions = Counter(str(row.get("action") or "unknown") for row in intents)
    output.append({
        "gate_outcome": "intent_actions",
        "evaluation_count": len(intents),
        "closed_trade_count": actions.get("ENTER_LONG", 0),
        "conversion_pct": (
            round((actions.get("ENTER_LONG", 0) / len(intents)) * 100.0, 4)
            if intents
            else None
        ),
        "action_counts": dict(sorted(actions.items())),
    })
    return output


def _entry_failure_modes(
    evaluations: list[dict[str, Any]],
    intents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for row in evaluations:
        if row.get("filled") is not True:
            counts[str(row.get("terminal_state") or "evaluation_not_filled")] += 1
    for row in intents:
        if str(row.get("action") or "").upper() != "ENTER_LONG":
            counts[
                str(row.get("blocked_reason") or row.get("reason") or "intent_blocked")
            ] += 1
    total = sum(counts.values())
    return [
        {
            "mode": key,
            "count": counts[key],
            "share_pct": round((counts[key] / total) * 100.0, 4) if total else None,
        }
        for key in sorted(counts, key=lambda item: (-counts[item], item))
    ]


def _exit_modes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("exit_reason") or "unknown"), []).append(row)
    return [
        {"exit_reason": key, **_trade_summary(grouped[key])}
        for key in sorted(grouped)
    ]


def _numbers(rows: list[dict[str, Any]], field: str) -> list[float]:
    return [
        value
        for row in rows
        if (value := _number(row.get(field))) is not None
    ]


def _first_number(row: dict[str, Any], *fields: str) -> float | None:
    for field in fields:
        value = _number(row.get(field))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _gap_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 15:
        return "under_15"
    if value <= 30:
        return "15_to_30"
    if value <= 50:
        return "30_to_50"
    return "over_50"


def _float_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 5_000_000:
        return "under_5m"
    if value < 20_000_000:
        return "5m_to_20m"
    return "over_20m"


def _liquidity_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 1_000_000:
        return "under_1m"
    if value < 5_000_000:
        return "1m_to_5m"
    return "over_5m"


def _confidence_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 50:
        return "under_50"
    if value < 80:
        return "50_to_79"
    return "80_plus"


def _within(value: str, start: str | None, end: str | None) -> bool:
    if not value:
        return False
    return (start is None or value >= start[:10]) and (
        end is None or value <= end[:10]
    )


def _row_date(row: dict[str, Any]) -> str:
    return str(
        row.get("market_date")
        or row.get("selected_at")
        or row.get("decision_time")
        or ""
    )[:10]


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str))


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _markdown(report: dict[str, Any]) -> str:
    official = dict(report.get("official") or {})
    coverage = dict(report.get("outcome_coverage") or {})
    concentration = dict(report.get("loss_concentration") or {})
    lines = [
        "# Dawnstrike AlphaOps causal attribution",
        "",
        f"- Generated: {report.get('generated_at')}",
        f"- Evidence cutoff: {report.get('evidence_cutoff') or 'not available'}",
        f"- Official closed trades: {official.get('trade_count')}",
        f"- Official net P&L: {_display(official.get('net_pnl'))}",
        f"- Observed hit rate: {_display(official.get('observed_hit_rate_pct'), '%')}",
        f"- Expected hit rate: {_display(official.get('expected_hit_rate_pct'), '%')}",
        f"- Observed average R: {_display(official.get('observed_r_multiple'))}",
        f"- Expected average R: {_display(official.get('expected_r_multiple'))}",
        f"- Outcome coverage: {_display(coverage.get('coverage_pct'), '%')}",
        f"- Largest loss concentration: "
        f"{_display(concentration.get('largest_loss_share_pct'), '%')}",
        "",
        "Missing values are not zero. This report is research-only and does not "
        "establish profitability or authorize promotion.",
    ]
    return "\n".join(lines) + "\n"


def _display(value: Any, suffix: str = "") -> str:
    parsed = _number(value)
    return f"{parsed:.4f}{suffix}" if parsed is not None else "not available"


__all__ = [
    "ATTRIBUTION_VERSION",
    "build_alpha_attribution_report",
    "generate_alpha_attribution_report",
]
