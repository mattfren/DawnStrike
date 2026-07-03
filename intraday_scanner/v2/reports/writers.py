"""Filesystem report writers for the v2 Alpha Lab artifact bundle."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.backtest import BacktestResult, EquityPoint, TradeRecord
from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.paper.lifecycle import PaperLifecycleResult
from intraday_scanner.v2.scanner import ScanOutput
from intraday_scanner.v2.strategies.catalog import describe_strategy


@dataclass(frozen=True)
class AlphaLabPaths:
    root: Path
    research: Path
    backtests: Path
    scans: Path
    reports: Path
    fixtures: Path
    manifests: Path
    logs: Path
    paper: Path

    @classmethod
    def create(cls, root: Path) -> AlphaLabPaths:
        paths = cls(
            root=root,
            research=root / "research",
            backtests=root / "backtests",
            scans=root / "scans",
            reports=root / "reports",
            fixtures=root / "fixtures",
            manifests=root / "manifests",
            logs=root / "logs",
            paper=root / "paper",
        )
        for path in (
            paths.root,
            paths.research,
            paths.backtests,
            paths.scans,
            paths.reports,
            paths.fixtures,
            paths.manifests,
            paths.logs,
            paths.paper,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return paths


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


def write_backtest_artifacts(paths: AlphaLabPaths, result: BacktestResult) -> dict[str, Path]:
    strategy_id = result.strategy.strategy_id
    summary_path = paths.backtests / f"{strategy_id}_summary.json"
    trades_path = paths.backtests / f"{strategy_id}_trades.csv"
    equity_path = paths.backtests / f"{strategy_id}_equity_curve.csv"
    summary_payload = {
        "strategy": describe_strategy(result.strategy),
        "metrics": _rounded_metrics(result.metrics),
        "warnings": list(result.warnings),
    }
    write_json(summary_path, summary_payload)
    write_csv_rows(
        trades_path,
        [_trade_row(trade) for trade in result.trades],
        (
            "trade_id",
            "strategy_id",
            "strategy_version",
            "symbol",
            "direction",
            "entry_time",
            "exit_time",
            "entry_price",
            "exit_price",
            "stop",
            "target",
            "quantity",
            "gross_pnl",
            "net_pnl",
            "return_pct",
            "r_multiple",
            "exit_reason",
            "holding_bars",
            "fees_paid",
            "slippage_paid",
            "evidence",
        ),
    )
    write_csv_rows(
        equity_path,
        [_equity_row(point) for point in result.equity_curve],
        ("timestamp", "equity", "cash", "open_positions", "drawdown_pct"),
    )
    return {"summary": summary_path, "trades": trades_path, "equity_curve": equity_path}


def write_strategy_comparison(
    paths: AlphaLabPaths,
    results: dict[str, BacktestResult],
) -> tuple[Path, Path, list[dict[str, object]]]:
    rows = build_comparison_rows(results)
    csv_path = paths.reports / "strategy_comparison.csv"
    json_path = paths.reports / "strategy_comparison.json"
    write_csv_rows(
        csv_path,
        rows,
        (
            "strategy_id",
            "status",
            "trade_count",
            "total_return_pct",
            "benchmark_return_pct",
            "max_drawdown_pct",
            "sharpe",
            "profit_factor",
            "average_r",
            "win_rate",
            "fees_paid",
            "rank_by_return",
        ),
    )
    write_json(json_path, rows)
    return csv_path, json_path, rows


def build_comparison_rows(results: dict[str, BacktestResult]) -> list[dict[str, object]]:
    benchmark_return = 0.0
    benchmark = results.get("benchmark_buy_hold_equal_weight")
    if benchmark:
        benchmark_return = float(benchmark.metrics.get("total_return_pct") or 0.0)
    rows: list[dict[str, object]] = []
    for strategy_id, result in sorted(results.items()):
        metrics = result.metrics
        rows.append(
            {
                "strategy_id": strategy_id,
                "status": result.strategy.status,
                "trade_count": int(metrics.get("trade_count") or 0),
                "total_return_pct": round(float(metrics.get("total_return_pct") or 0.0), 6),
                "benchmark_return_pct": round(benchmark_return, 6),
                "max_drawdown_pct": round(float(metrics.get("max_drawdown_pct") or 0.0), 6),
                "sharpe": _round_metric(metrics.get("sharpe")),
                "profit_factor": _round_metric(metrics.get("profit_factor")),
                "average_r": round(float(metrics.get("average_r") or 0.0), 6),
                "win_rate": round(float(metrics.get("win_rate") or 0.0), 6),
                "fees_paid": round(float(metrics.get("fees_paid") or 0.0), 4),
                "rank_by_return": 0,
            }
        )
    ranked = sorted(rows, key=lambda row: _as_float(row["total_return_pct"]), reverse=True)
    rank_by_id = {str(row["strategy_id"]): rank for rank, row in enumerate(ranked, start=1)}
    for row in rows:
        row["rank_by_return"] = rank_by_id[str(row["strategy_id"])]
    return sorted(rows, key=lambda row: _as_int(row["rank_by_return"]))


def write_alpha_lab_summary(
    paths: AlphaLabPaths,
    *,
    dataset: MarketDataset,
    comparison_rows: list[dict[str, object]],
    scan: ScanOutput,
    paper_lifecycle: PaperLifecycleResult,
    run_id: str,
    assumptions: dict[str, object],
) -> Path:
    path = paths.reports / "alpha_lab_summary.md"
    first_timestamp, last_timestamp = _date_range(dataset)
    best = comparison_rows[0] if comparison_rows else None
    worst = comparison_rows[-1] if comparison_rows else None
    candidates = scan.cards[:10]
    is_synthetic = dataset.source_kind == "synthetic"
    is_public = dataset.source_kind.startswith("public_")
    data_assumption = _data_assumption_line(dataset.source_kind)
    limitation_lines = _limitation_lines(dataset.source_kind)
    next_test_lines = _next_test_lines(dataset.source_kind)
    distrust_lines = _distrust_lines(dataset.source_kind)
    lines = [
        "# Dawnstrike v2 Alpha Lab Summary",
        "",
        f"- Run ID: `{run_id}`",
        f"- Data source: `{dataset.source_kind}`",
        f"- Dataset ID: `{dataset.dataset_id}`",
        f"- Date range: `{first_timestamp}` to `{last_timestamp}`",
        f"- Symbols: {', '.join(dataset.symbols)}",
        f"- Total bars: {dataset.total_bars}",
        "- Research boundary: research-only; no live execution, broker paths, or SQLite writes.",
        "- Boundary note: Alpha Lab artifacts are research evidence only; they do not validate "
        "a strategy, override PaperOps/CommitBridge gates, or authorize live execution.",
        "",
        "## Backtest Assumptions",
        "",
        f"- Initial capital: `{assumptions['initial_capital']}`",
        f"- Fees: `{assumptions['fee_bps']} bps plus commission "
        f"{assumptions['commission_per_trade']}`",
        f"- Slippage: `{assumptions['slippage_bps']} bps per fill`",
        "- Signal timing: signal at bar close, entry no earlier than next bar open.",
        "- Same-bar stop/target: stop-first conservative priority.",
        data_assumption,
        "",
        "## Strategy Ranking",
        "",
    ]
    for row in comparison_rows:
        lines.append(
            "- `{strategy_id}` rank {rank_by_return}: return {return_pct:.2f}%, "
            "max drawdown {drawdown_pct:.2f}%, trades {trade_count}".format(
                strategy_id=row["strategy_id"],
                rank_by_return=row["rank_by_return"],
                return_pct=_as_float(row["total_return_pct"]) * 100,
                drawdown_pct=_as_float(row["max_drawdown_pct"]) * 100,
                trade_count=row["trade_count"],
            )
        )
    lines.extend(["", "## Best And Worst", ""])
    if best:
        lines.append(f"- Best historical performer on this dataset: `{best['strategy_id']}`.")
    if worst:
        lines.append(f"- Weakest historical performer on this dataset: `{worst['strategy_id']}`.")
    lines.extend(["", "## Current Candidates", ""])
    if candidates:
        for card in candidates:
            lines.append(
                f"- `{card.symbol}` `{card.strategy_id}` {card.direction}: "
                f"entry {card.entry_trigger}; "
                f"stop `{_csv_value(card.stop)}`, target `{_csv_value(card.target)}`, "
                f"R:R `{_csv_value(card.reward_risk)}`."
            )
    else:
        lines.append("- No current triggered candidates on the latest available bar.")
    lines.extend(
        [
            "",
            "## Paper Lifecycle",
            "",
            f"- Paper picks: `{len(paper_lifecycle.picks)}`",
            f"- Paper entries: `{len(paper_lifecycle.entries)}`",
            f"- Intraday/current checks: `{len(paper_lifecycle.checks)}`",
            f"- Paper exits: `{len(paper_lifecycle.exits)}`",
            f"- Calendar return days: `{len(paper_lifecycle.calendar_returns)}`",
            f"- Paper lifecycle net P&L: `{_csv_value(paper_lifecycle.summary()['net_pnl'])}`",
            "",
            "Top paper strategy P&L rows:",
            "",
        ]
    )
    ranked_pnl = sorted(paper_lifecycle.strategy_pnl, key=lambda row: row.net_pnl, reverse=True)
    for pnl_row in ranked_pnl[:5]:
        lines.append(
            f"- `{pnl_row.strategy_id}`: trades `{pnl_row.trade_count}`, "
            f"net P&L `{_csv_value(pnl_row.net_pnl)}`, "
            f"return `{_csv_value(pnl_row.return_on_equity)}`."
        )
    lines.extend(
        [
            "",
            "## Warnings And Limitations",
            "",
        ]
    )
    lines.extend(limitation_lines)
    lines.extend(
        [
            "- No strategy is marked validated.",
            "- Corporate actions, borrow costs, liquidity constraints, and survivorship "
            "controls are not complete.",
            "- The buy-and-hold benchmark is a comparator, not a strategy recommendation.",
            "- Latest scan cards are research decision-support artifacts, not trade instructions.",
            "",
            "## What To Test Next",
            "",
        ]
    )
    lines.extend(next_test_lines)
    lines.extend(
        [
            "- Add walk-forward validation and out-of-sample splits.",
            "- Reconcile selected strategies against the existing Dawnstrike paper-audit "
            "path without changing it.",
            "- Expand risk engine to portfolio-level exposure and max-loss controls.",
            "",
            "## What Not To Trust Yet",
            "",
        ]
    )
    lines.extend(distrust_lines)
    if is_synthetic:
        lines.append("- Do not trust current candidates without real market data.")
    elif is_public:
        lines.append(
            "- Do not trust current candidates without source freshness review and manual review."
        )
    else:
        lines.append("- Do not trust current candidates without live-market data freshness review.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _trade_row(trade: TradeRecord) -> dict[str, object]:
    return {
        "trade_id": trade.trade_id,
        "strategy_id": trade.strategy_id,
        "strategy_version": trade.strategy_version,
        "symbol": trade.symbol,
        "direction": trade.direction,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop": trade.stop,
        "target": trade.target,
        "quantity": trade.quantity,
        "gross_pnl": trade.gross_pnl,
        "net_pnl": trade.net_pnl,
        "return_pct": trade.return_pct,
        "r_multiple": trade.r_multiple,
        "exit_reason": trade.exit_reason,
        "holding_bars": trade.holding_bars,
        "fees_paid": trade.fees_paid,
        "slippage_paid": trade.slippage_paid,
        "evidence": " | ".join(trade.evidence),
    }


def _equity_row(point: EquityPoint) -> dict[str, object]:
    return {
        "timestamp": point.timestamp.isoformat(),
        "equity": point.equity,
        "cash": point.cash,
        "open_positions": point.open_positions,
        "drawdown_pct": point.drawdown_pct,
    }


def _rounded_metrics(
    metrics: dict[str, float | int | str | None],
) -> dict[str, float | int | str | None]:
    return {key: _round_metric(value) for key, value in metrics.items()}


def _round_metric(value: float | int | str | None) -> float | int | str | None:
    if isinstance(value, float):
        return round(value, 8)
    return value


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def _as_float(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return float(value)
    raise TypeError(f"expected numeric value, got {type(value).__name__}")


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"expected integer value, got {type(value).__name__}")


def _date_range(dataset: MarketDataset) -> tuple[str, str]:
    timestamps = [bar.timestamp for bars in dataset.bars_by_symbol.values() for bar in bars]
    if not timestamps:
        return "n/a", "n/a"
    return min(timestamps).date().isoformat(), max(timestamps).date().isoformat()


def _data_assumption_line(source_kind: str) -> str:
    if source_kind == "synthetic":
        return (
            "- Synthetic data warning: synthetic returns are engineering evidence only, "
            "not market evidence."
        )
    if source_kind.startswith("public_"):
        return (
            "- Public data warning: free public OHLCV is schema-validated locally but not "
            "independently reconciled against a second market-data source."
        )
    return "- Local data warning: fixture/local OHLCV is schema-validated before use."


def _limitation_lines(source_kind: str) -> list[str]:
    if source_kind == "synthetic":
        return [
            "- This vertical slice uses deterministic synthetic OHLCV when adequate "
            "real local history is unavailable."
        ]
    if source_kind.startswith("public_"):
        return [
            "- This run uses cached free public daily OHLCV; it is better than synthetic "
            "fixture evidence but still not institutional-grade market data."
        ]
    return ["- This run uses local OHLCV fixture/history selected by the data loader."]


def _next_test_lines(source_kind: str) -> list[str]:
    if source_kind == "synthetic":
        return [
            "- Replace synthetic fixture with vetted real historical OHLCV and immutable manifests."
        ]
    if source_kind.startswith("public_"):
        return [
            "- Cross-check the cached public OHLCV against a second provider or broker-grade "
            "historical source.",
            "- Add immutable data-source manifests for each external payload.",
        ]
    return ["- Expand the vetted local OHLCV history and add immutable data-source manifests."]


def _distrust_lines(source_kind: str) -> list[str]:
    if source_kind == "synthetic":
        return ["- Do not trust synthetic return rankings as market edge."]
    if source_kind.startswith("public_"):
        return [
            "- Do not treat public-data return rankings as validated market edge until "
            "cross-source reconciliation and out-of-sample tests pass."
        ]
    return ["- Do not treat local-fixture return rankings as validated market edge yet."]
