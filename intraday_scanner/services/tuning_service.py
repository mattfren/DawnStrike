"""Offline strategy tuning against point-in-time snapshots and later minute bars."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.models import SnapshotRow, utc_now_iso
from intraday_scanner.scoring import score_universe
from intraday_scanner.services.audit_service import calculate_audit

TUNING_COLUMNS = [
    "scenario",
    "fixture_only",
    "top_1_close_return_pct",
    "top_3_close_return_pct",
    "top_5_close_return_pct",
    "top_1_high_return_pct",
    "top_3_high_return_pct",
    "top_5_high_return_pct",
    "top_3_1m_return_pct",
    "top_3_5m_return_pct",
    "top_3_15m_return_pct",
    "top_3_lunch_return_pct",
    "hit_rate_close_pct",
    "avg_close_return_pct",
    "median_close_return_pct",
    "max_drawdown_pct",
    "best_pick",
    "worst_pick",
    "params_json",
]


@dataclass(frozen=True)
class TuningScenario:
    name: str
    overrides: dict[str, Any]


def default_tuning_scenarios() -> list[TuningScenario]:
    return [
        TuningScenario("base", {}),
        TuningScenario("gap_plus", {"score_weight_gap": 1.15}),
        TuningScenario("liquidity_plus", {"score_weight_liquidity": 1.2}),
        TuningScenario("float_rotation_plus", {"score_weight_float_rotation": 1.2}),
        TuningScenario("range_plus", {"score_weight_range": 1.15}),
        TuningScenario("catalyst_plus", {"score_weight_catalyst": 1.25}),
        TuningScenario("execution_plus", {"score_weight_execution": 1.2}),
        TuningScenario("risk_penalty_plus", {"score_weight_risk_penalty": 1.25}),
        TuningScenario("risk_penalty_minus", {"score_weight_risk_penalty": 0.85}),
        TuningScenario("aggressive_gap_floor", {"min_gap_pct": 25.0}),
        TuningScenario("lower_gap_floor", {"min_gap_pct": 10.0}),
        TuningScenario("high_liquidity_only", {"min_premarket_dollar_volume": 1_000_000.0}),
    ]


def run_strategy_tuning(
    *,
    snapshots: list[SnapshotRow],
    minute_bars: list[dict[str, Any]],
    base_config: ScannerConfig,
    fixture_only: bool,
    top_n: int = 5,
) -> dict[str, Any]:
    rows = []
    for scenario in default_tuning_scenarios():
        config = base_config.with_overrides(**scenario.overrides, top_n=max(top_n, 5))
        result = score_universe(snapshots, config)
        ranked = [candidate.to_dict() for candidate in result.ranked_candidates]
        audit = calculate_audit(ranked, minute_bars, config, top_n=top_n)
        rows.append(_scenario_row(scenario, audit.trades, fixture_only))
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            _number(row["top_3_close_return_pct"]),
            _number(row["top_3_high_return_pct"]),
            _number(row["hit_rate_close_pct"]),
        ),
        reverse=True,
    )
    return {
        "created_at": utc_now_iso(),
        "fixture_only": fixture_only,
        "scenario_count": len(sorted_rows),
        "ranked_results": sorted_rows,
        "best": sorted_rows[0] if sorted_rows else {},
    }


def write_tuning_outputs(report: dict[str, Any], out_dir: str | Path) -> dict[str, Path]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "strategy_tuning_results.csv"
    json_path = output_dir / "strategy_tuning_summary.json"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=TUNING_COLUMNS)
        writer.writeheader()
        for row in report.get("ranked_results", []):
            writer.writerow({column: row.get(column, "") for column in TUNING_COLUMNS})
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return {"csv": csv_path, "summary": json_path}


def _scenario_row(
    scenario: TuningScenario, trades: list[dict[str, Any]], fixture_only: bool
) -> dict[str, Any]:
    return {
        "scenario": scenario.name,
        "fixture_only": fixture_only,
        "top_1_close_return_pct": _portfolio(trades, "close_return_pct", 1),
        "top_3_close_return_pct": _portfolio(trades, "close_return_pct", 3),
        "top_5_close_return_pct": _portfolio(trades, "close_return_pct", 5),
        "top_1_high_return_pct": _portfolio(trades, "high_return_pct", 1),
        "top_3_high_return_pct": _portfolio(trades, "high_return_pct", 3),
        "top_5_high_return_pct": _portfolio(trades, "high_return_pct", 5),
        "top_3_1m_return_pct": _portfolio(trades, "return_1m_pct", 3),
        "top_3_5m_return_pct": _portfolio(trades, "return_5m_pct", 3),
        "top_3_15m_return_pct": _portfolio(trades, "return_15m_pct", 3),
        "top_3_lunch_return_pct": _portfolio(trades, "lunch_return_pct", 3),
        "hit_rate_close_pct": _hit_rate(trades, "close_return_pct"),
        "avg_close_return_pct": _average(trades, "close_return_pct"),
        "median_close_return_pct": _median(trades, "close_return_pct"),
        "max_drawdown_pct": _minimum(trades, "low_drawdown_pct"),
        "best_pick": _pick(trades, "close_return_pct", best=True),
        "worst_pick": _pick(trades, "close_return_pct", best=False),
        "params_json": json.dumps(scenario.overrides, sort_keys=True),
    }


def _portfolio(rows: list[dict[str, Any]], key: str, count: int) -> float:
    selected = rows[:count]
    return _average(selected, key)


def _average(rows: list[dict[str, Any]], key: str) -> float:
    values = [_number(row.get(key)) for row in rows]
    return round(sum(values) / len(values), 2) if values else 0.0


def _median(rows: list[dict[str, Any]], key: str) -> float:
    values = sorted(_number(row.get(key)) for row in rows)
    if not values:
        return 0.0
    midpoint = len(values) // 2
    if len(values) % 2:
        return round(values[midpoint], 2)
    return round((values[midpoint - 1] + values[midpoint]) / 2, 2)


def _minimum(rows: list[dict[str, Any]], key: str) -> float:
    values = [_number(row.get(key)) for row in rows]
    return round(min(values), 2) if values else 0.0


def _hit_rate(rows: list[dict[str, Any]], key: str) -> float:
    values = [_number(row.get(key)) for row in rows]
    if not values:
        return 0.0
    return round((sum(1 for value in values if value > 0) / len(values)) * 100, 2)


def _pick(rows: list[dict[str, Any]], key: str, *, best: bool) -> str:
    if not rows:
        return ""
    row = max(rows, key=lambda item: _number(item.get(key))) if best else min(
        rows, key=lambda item: _number(item.get(key))
    )
    return f"{row.get('ticker')}:{row.get(key)}"


def _number(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
