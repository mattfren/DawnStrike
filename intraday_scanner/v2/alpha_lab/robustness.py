"""Deterministic robustness checks for the v2 Alpha Lab."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from statistics import median
from typing import Any

from intraday_scanner.v2.backtest import BacktestEngine, BacktestResult, BacktestSettings
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.strategies import StrategySpec


def write_robustness_artifacts(
    paths: Any,
    *,
    dataset: MarketDataset,
    strategies: tuple[StrategySpec, ...],
    settings: BacktestSettings,
    baseline_results: dict[str, BacktestResult],
) -> dict[str, Path]:
    """Write walk-forward, cost-stress, and trade-order Monte Carlo summaries."""

    train_dataset, test_dataset = _split_dataset(dataset)
    base_engine = BacktestEngine(settings=settings)
    stress_engine = BacktestEngine(settings=_stress_settings(settings))
    rows: list[dict[str, object]] = []
    for strategy in strategies:
        full = baseline_results[strategy.strategy_id]
        train = base_engine.run(strategy, train_dataset)
        test = base_engine.run(strategy, test_dataset)
        stress = stress_engine.run(strategy, dataset)
        monte_carlo = _monte_carlo_from_trades(full)
        row = {
            "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.version,
            "status": strategy.status,
            "full_return_pct": _metric(full, "total_return_pct"),
            "train_return_pct": _metric(train, "total_return_pct"),
            "test_return_pct": _metric(test, "total_return_pct"),
            "train_trade_count": int(train.metrics.get("trade_count") or 0),
            "test_trade_count": int(test.metrics.get("trade_count") or 0),
            "cost_stress_return_pct": _metric(stress, "total_return_pct"),
            "cost_stress_delta_pct": _metric(stress, "total_return_pct")
            - _metric(full, "total_return_pct"),
            "monte_carlo_runs": monte_carlo["runs"],
            "monte_carlo_median_return_pct": monte_carlo["median_return_pct"],
            "monte_carlo_worst_return_pct": monte_carlo["worst_return_pct"],
            "monte_carlo_worst_drawdown_pct": monte_carlo["worst_drawdown_pct"],
            "robustness_status": _robustness_status(test, monte_carlo),
            "warnings": _warnings(test, monte_carlo),
        }
        rows.append(row)

    rows = sorted(rows, key=lambda row: str(row["strategy_id"]))
    summary: dict[str, object] = {
        "dataset_id": dataset.dataset_id,
        "methodology": {
            "cost_stress": "fee_bps and slippage_bps doubled from base settings",
            "monte_carlo": "deterministic rotations and sorted trade-order paths",
            "walk_forward": (
                "first 70 percent of each symbol history as train, final 30 percent as test"
            ),
        },
        "rows": rows,
        "schema_version": "v2.alpha_lab_robustness.v1",
    }
    json_path = paths.reports / "robustness_summary.json"
    csv_path = paths.reports / "robustness_summary.csv"
    md_path = paths.reports / "robustness_summary.md"
    write_json(json_path, summary)
    write_csv_rows(
        csv_path,
        rows,
        (
            "strategy_id",
            "strategy_version",
            "status",
            "full_return_pct",
            "train_return_pct",
            "test_return_pct",
            "train_trade_count",
            "test_trade_count",
            "cost_stress_return_pct",
            "cost_stress_delta_pct",
            "monte_carlo_runs",
            "monte_carlo_median_return_pct",
            "monte_carlo_worst_return_pct",
            "monte_carlo_worst_drawdown_pct",
            "robustness_status",
            "warnings",
        ),
    )
    md_path.write_text(_markdown(summary), encoding="utf-8")
    return {"robustness_json": json_path, "robustness_csv": csv_path, "robustness_md": md_path}


def _split_dataset(
    dataset: MarketDataset,
    train_fraction: float = 0.70,
) -> tuple[MarketDataset, MarketDataset]:
    train: dict[str, tuple[MarketBar, ...]] = {}
    test: dict[str, tuple[MarketBar, ...]] = {}
    for symbol, bars in dataset.bars_by_symbol.items():
        split_index = max(1, min(len(bars) - 1, int(len(bars) * train_fraction)))
        train[symbol] = bars[:split_index]
        test[symbol] = bars[split_index:]
    return (
        MarketDataset(
            dataset_id=f"{dataset.dataset_id}:walk_forward_train",
            source_kind=dataset.source_kind,
            timeframe=dataset.timeframe,
            bars_by_symbol=train,
            source_path=dataset.source_path,
            warnings=dataset.warnings,
            source_refs=dataset.source_refs,
        ),
        MarketDataset(
            dataset_id=f"{dataset.dataset_id}:walk_forward_test",
            source_kind=dataset.source_kind,
            timeframe=dataset.timeframe,
            bars_by_symbol=test,
            source_path=dataset.source_path,
            warnings=dataset.warnings,
            source_refs=dataset.source_refs,
        ),
    )


def _stress_settings(settings: BacktestSettings) -> BacktestSettings:
    return BacktestSettings(
        initial_capital=settings.initial_capital,
        fee_bps=settings.fee_bps * 2,
        slippage_bps=settings.slippage_bps * 2,
        commission_per_trade=settings.commission_per_trade,
        risk=settings.risk,
    )


def _monte_carlo_from_trades(result: BacktestResult) -> dict[str, float | int]:
    trade_pnls = [trade.net_pnl for trade in result.trades]
    if not trade_pnls:
        return {
            "runs": 0,
            "median_return_pct": 0.0,
            "worst_drawdown_pct": 0.0,
            "worst_return_pct": 0.0,
        }
    paths = []
    rotations = min(25, len(trade_pnls))
    for offset in range(rotations):
        paths.append(trade_pnls[offset:] + trade_pnls[:offset])
    paths.append(sorted(trade_pnls))
    paths.append(sorted(trade_pnls, reverse=True))
    returns = []
    drawdowns = []
    initial = float(result.metrics.get("initial_capital") or 100_000.0)
    for path in paths:
        equity = initial
        peak = initial
        worst_drawdown = 0.0
        for pnl in path:
            equity += pnl
            peak = max(peak, equity)
            worst_drawdown = min(worst_drawdown, equity / peak - 1.0 if peak else 0.0)
        returns.append(equity / initial - 1.0)
        drawdowns.append(worst_drawdown)
    return {
        "runs": len(paths),
        "median_return_pct": round(float(median(returns)), 6),
        "worst_drawdown_pct": round(min(drawdowns), 6),
        "worst_return_pct": round(min(returns), 6),
    }


def _metric(result: BacktestResult, key: str) -> float:
    value = result.metrics.get(key)
    return round(float(value or 0.0), 6)


def _robustness_status(
    test: BacktestResult,
    monte_carlo: dict[str, float | int],
) -> str:
    test_trades = int(test.metrics.get("trade_count") or 0)
    test_return = float(test.metrics.get("total_return_pct") or 0.0)
    worst_drawdown = float(monte_carlo["worst_drawdown_pct"])
    if test_trades < 3:
        return "insufficient_oos_trades"
    if test_return < 0 or worst_drawdown < -0.20:
        return "fragile"
    return "watch"


def _warnings(
    test: BacktestResult,
    monte_carlo: dict[str, float | int],
) -> str:
    warnings: list[str] = []
    if int(test.metrics.get("trade_count") or 0) < 3:
        warnings.append("insufficient out-of-sample trade count")
    if float(test.metrics.get("total_return_pct") or 0.0) < 0:
        warnings.append("negative out-of-sample return")
    if float(monte_carlo["worst_drawdown_pct"]) < -0.20:
        warnings.append("trade-order drawdown stress exceeds 20 percent")
    return " | ".join(warnings) if warnings else "none"


def _markdown(summary: dict[str, object]) -> str:
    rows = summary["rows"]
    assert isinstance(rows, list)
    lines = [
        "# Alpha Lab Robustness Summary",
        "",
        f"- Dataset: `{summary['dataset_id']}`",
        "- Boundary: research-only robustness diagnostics; no strategy is validated.",
        "",
        "## Methodology",
        "",
        "- Walk-forward: first 70 percent of each symbol history is train, "
        "final 30 percent is test.",
        "- Cost stress: base fee and slippage assumptions are doubled.",
        "- Monte Carlo: deterministic trade-order rotations plus sorted best/worst paths.",
        "",
        "## Results",
        "",
        "| Strategy | Test Return | Test Trades | Cost Stress Delta | MC Worst DD | Status |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {strategy_id} | {test_return_pct:.4f} | {test_trade_count} | "
            "{cost_stress_delta_pct:.4f} | {monte_carlo_worst_drawdown_pct:.4f} | "
            "{robustness_status} |".format(**row)
        )
    lines.extend(
        [
            "",
            "## Warnings",
            "",
        ]
    )
    for row in rows:
        if row["warnings"] != "none":
            lines.append(f"- `{row['strategy_id']}`: {row['warnings']}")
    if not any(row["warnings"] != "none" for row in rows):
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_csv_rows(path: Path, rows: list[dict[str, object]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple | list):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)
