"""Causal, identical-data research comparison for all strategy challengers."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from intraday_scanner.v2.backtest import BacktestEngine, BacktestResult
from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.data_truth import load_datatruth_dataset
from intraday_scanner.v2.strategies import (
    build_challenger_catalog,
    build_strategy_catalog,
    evaluate_challenger_gates,
)

SCHEMA_VERSION = "dawnstrike.strategy_challenger_backtest.v1"
GATE_TELEMETRY_WINDOW_BARS = 60
_STATIC_UNAVAILABLE = {
    "cross_sectional_relative_strength": "sector_concentration",
    "failed_breakout_reversal_short": "borrow_evidence",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _result_payload(result: BacktestResult) -> dict[str, Any]:
    return {
        "strategy_id": result.strategy.strategy_id,
        "strategy_version": result.strategy.version,
        "status": result.strategy.status,
        "validation_status": result.strategy.validation_status,
        "metrics": dict(result.metrics),
        "trade_count": len(result.trades),
        "warning_count": len(result.warnings),
        "warning_sample": list(result.warnings[:20]),
    }


def _gate_telemetry(
    strategy_id: str,
    candidate_version: str,
    dataset: MarketDataset,
    *,
    window_bars: int = GATE_TELEMETRY_WINDOW_BARS,
) -> dict[str, Any]:
    statuses: Counter[str] = Counter()
    failures: Counter[str] = Counter()
    first_failures: Counter[str] = Counter()
    eligible = 0
    evaluated = 0
    for symbol in dataset.symbols:
        bars = dataset.bars_by_symbol[symbol]
        start_index = max(0, len(bars) - window_bars)
        for index in range(start_index, len(bars)):
            evaluation = evaluate_challenger_gates(
                strategy_id,
                dataset,
                symbol,
                bars,
                index,
                candidate_version=candidate_version,
            )
            evaluated += 1
            eligible += int(evaluation.eligible)
            for gate in evaluation.gates:
                statuses[gate.status] += 1
            for gate in evaluation.failures:
                failures[gate.name] += 1
            if evaluation.first_failure is not None:
                first_failures[evaluation.first_failure.name] += 1
    return {
        "window_bars_per_symbol": window_bars,
        "evaluated_strategy_symbol_timestamps": evaluated,
        "eligible_count": eligible,
        "ineligible_count": evaluated - eligible,
        "gate_status_counts": dict(sorted(statuses.items())),
        "failure_counts": dict(sorted(failures.items())),
        "first_failure_counts": dict(sorted(first_failures.items())),
    }


def build_strategy_challenger_backtest_report(
    dataset: MarketDataset,
    *,
    source_manifest: Mapping[str, Any],
    code_sha: str,
) -> dict[str, Any]:
    """Compare every catalog entry and each additive challenger on one dataset."""

    if not code_sha.strip():
        raise ValueError("code_sha is required")
    engine = BacktestEngine()
    champions = build_strategy_catalog()
    challengers = {item.strategy_id: item for item in build_challenger_catalog()}
    rows: list[dict[str, Any]] = []
    for champion in champions:
        champion_result = engine.run(champion, dataset)
        challenger = challengers.get(champion.strategy_id)
        row: dict[str, Any] = {
            "strategy_id": champion.strategy_id,
            "champion": _result_payload(champion_result),
            "challenger": None,
            "comparison_status": "COMPARATOR_ONLY",
            "metric_delta": None,
            "gate_telemetry": None,
        }
        if challenger is not None:
            champion_trades = int(champion_result.metrics.get("trade_count") or 0)
            static_gate = _STATIC_UNAVAILABLE.get(champion.strategy_id)
            if static_gate is not None:
                challenger_payload = {
                    "strategy_id": challenger.strategy_id,
                    "strategy_version": challenger.version,
                    "status": challenger.status,
                    "validation_status": challenger.validation_status,
                    "metrics": None,
                    "trade_count": 0,
                    "warning_count": 1,
                    "warning_sample": [
                        f"UNAVAILABLE_REQUIRED_DATA:{static_gate}; backtest not run"
                    ],
                }
                comparison_status = "NOT_EVALUABLE_UNAVAILABLE_REQUIRED_DATA"
                metric_delta = None
                telemetry_window = 1
            else:
                challenger_result = engine.run(challenger, dataset)
                challenger_payload = _result_payload(challenger_result)
                challenger_trades = int(challenger_result.metrics.get("trade_count") or 0)
                telemetry_window = GATE_TELEMETRY_WINDOW_BARS
                if challenger_trades == 0:
                    comparison_status = "NOT_EVALUABLE_NO_CHALLENGER_TRADES"
                    metric_delta = None
                elif champion_trades == 0:
                    comparison_status = "NOT_EVALUABLE_NO_PARENT_TRADES"
                    metric_delta = None
                else:
                    comparison_status = "RESEARCH_COMPARABLE"
                    metric_delta = {
                        key: float(challenger_result.metrics[key])
                        - float(champion_result.metrics[key])
                        for key in (
                            "total_return_pct",
                            "max_drawdown_pct",
                            "win_rate",
                            "expectancy",
                        )
                        if champion_result.metrics.get(key) is not None
                        and challenger_result.metrics.get(key) is not None
                    }
                    metric_delta["trade_count"] = challenger_trades - champion_trades
            if static_gate is None and challenger_payload["trade_count"] == 0:
                comparison_status = "NOT_EVALUABLE_NO_CHALLENGER_TRADES"
            row.update(
                {
                    "challenger": challenger_payload,
                    "comparison_status": comparison_status,
                    "metric_delta": metric_delta,
                    "gate_telemetry": _gate_telemetry(
                        challenger.strategy_id,
                        challenger.version,
                        dataset,
                        window_bars=telemetry_window,
                    ),
                }
            )
        rows.append(row)
    report = {
        "schema_version": SCHEMA_VERSION,
        "code_sha": code_sha,
        "dataset_id": dataset.dataset_id,
        "dataset_source_kind": dataset.source_kind,
        "dataset_timeframe": dataset.timeframe,
        "dataset_symbol_count": len(dataset.symbols),
        "dataset_bar_count": sum(len(bars) for bars in dataset.bars_by_symbol.values()),
        "source_manifest": dict(source_manifest),
        "strategy_count": len(champions),
        "challenger_count": len(challengers),
        "strategies": rows,
        "research_only": True,
        "promotion_eligible": False,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
        "evidence_boundary": "latest_snapshot_retrospective_not_forward",
        "gate_telemetry_boundary": "latest_60_bars_per_symbol",
    }
    report["report_sha256"] = _sha256(report)
    return report


def run_strategy_challenger_backtest(
    *,
    data_truth_root: str | Path,
    out_path: str | Path,
    code_sha: str,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    dataset, manifest = load_datatruth_dataset(
        output_root=Path(data_truth_root),
        snapshot_id=snapshot_id,
    )
    report = build_strategy_challenger_backtest_report(
        dataset,
        source_manifest=manifest.to_dict(),
        code_sha=code_sha,
    )
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical_json(report) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"immutable challenger backtest changed: {path}")
        reused = True
    else:
        path.write_text(encoded, encoding="utf-8")
        reused = False
    return {
        "status": "complete",
        "out_path": str(path),
        "report_sha256": report["report_sha256"],
        "strategy_count": report["strategy_count"],
        "challenger_count": report["challenger_count"],
        "idempotent_reused": reused,
        "research_only": True,
        "promotion_eligible": False,
        "broker_execution_enabled": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "GATE_TELEMETRY_WINDOW_BARS",
    "build_strategy_challenger_backtest_report",
    "run_strategy_challenger_backtest",
]
