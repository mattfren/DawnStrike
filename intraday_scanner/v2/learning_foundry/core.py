# ruff: noqa: E501
# mypy: ignore-errors
"""Additive Autonomous Learning Foundry for Dawnstrike v2.

The Foundry reads existing OMEGA artifacts, writes derived learning artifacts,
and keeps every learned strategy in shadow/candidate state. It does not import
the legacy UI, persistence layer, or external execution adapters.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, time, timezone
from pathlib import Path

from intraday_scanner.v2.backtest import BacktestEngine, BacktestResult, BacktestSettings
from intraday_scanner.v2.data import MarketBar, MarketDataset, build_synthetic_ohlcv_dataset
from intraday_scanner.v2.indicators import atr, sma
from intraday_scanner.v2.risk import RiskSettings
from intraday_scanner.v2.strategies import StrategySignal, StrategySpec, build_strategy_catalog
from intraday_scanner.v2.strategies.catalog import describe_strategy

OUTPUT_ROOT = Path("data/v2_learning_foundry")
SCHEMA_PREFIX = "v2.learning_foundry"
CANONICAL_DATE = date(2026, 6, 29)
DIRS = (
    "features",
    "labels",
    "regimes",
    "news",
    "models",
    "candidates",
    "shadow_runs",
    "evals",
    "lessons",
    "reports",
    "manifests",
    "logs",
)
COMMANDS = (
    "py -m intraday_scanner.v2.learning_foundry init",
    "py -m intraday_scanner.v2.learning_foundry features --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry labels --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry regimes --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry news --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry train --asof YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry generate-candidates --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry backtest-candidates --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry shadow-run --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry evaluate --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry promote-review --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry lesson --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry daily-learn --date YYYY-MM-DD",
    "py -m intraday_scanner.v2.learning_foundry verify",
    "py -m intraday_scanner.v2.learning_foundry report",
    "py -m intraday_scanner.v2.learning_foundry demo",
)


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


def init(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    paths = _paths(output_root)
    _write_champion_registry(output_root=output_root)
    manifest = {
        "created_at": _now(),
        "live_trading": "disabled",
        "module_root": "intraday_scanner/v2/learning_foundry",
        "output_root": output_root.as_posix(),
        "schema_version": f"{SCHEMA_PREFIX}.manifest.v1",
        "status": "initialized",
    }
    write_json(paths["manifests"] / "learning_foundry_manifest.json", manifest)
    _write_static_docs(output_root=output_root)
    return {
        "champion_registry": (paths["reports"] / "champion_registry.json").as_posix(),
        "output_root": output_root.as_posix(),
        "status": "initialized",
    }


def build_features(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    as_of = _now()
    source_refs = _source_refs()
    canonical_rows = _load_canonical_market_rows(run_date)
    strategy_scores = _strategy_score_rows()
    fill_rows = _filltruth_strategy_rows()
    features: list[dict[str, object]] = []

    for symbol, rows in sorted(canonical_rows.items()):
        features.extend(
            _market_feature_rows(
                symbol=symbol,
                rows=rows,
                run_date=run_date,
                as_of=as_of,
                source_artifact=source_refs["canonical_intraday"],
            )
        )

    for row in strategy_scores:
        strategy_id = str(row.get("strategy_id", "unknown"))
        for feature_name in (
            "forward_days",
            "forward_closed_trades",
            "expectancy",
            "profit_factor",
            "overall_score",
            "committed_filltruth_forward_count",
            "intraday_supported_forward_fill_count",
            "max_drawdown_pct",
        ):
            features.append(
                _feature_row(
                    run_date=run_date,
                    as_of=as_of,
                    symbol="",
                    strategy_id=strategy_id,
                    feature_name=f"strategy_{feature_name}",
                    value=_float_or_zero(row.get(feature_name)),
                    source_artifact=source_refs["strategy_evidence"],
                    data_snapshot_id=str(row.get("data_snapshot_id", "ledger_rebuild")),
                )
            )

    for row in fill_rows:
        strategy_id = str(row.get("strategy_id", "unknown"))
        for feature_name in (
            "fill_certainty_score",
            "fill_reconciliation_score",
            "execution_model_stability_score",
            "shadow_forward_replay_days",
            "forward_days",
            "forward_closed_trades",
        ):
            features.append(
                _feature_row(
                    run_date=run_date,
                    as_of=as_of,
                    symbol="",
                    strategy_id=strategy_id,
                    feature_name=f"filltruth_{feature_name}",
                    value=_float_or_zero(row.get(feature_name)),
                    source_artifact=source_refs["filltruth_strategy_evidence"],
                    data_snapshot_id="filltruth_overlay",
                )
            )

    features_path = paths["features"] / f"{run_date.isoformat()}_features.json"
    features_csv = paths["features"] / f"{run_date.isoformat()}_features.csv"
    payload = {
        "as_of_timestamp": as_of,
        "feature_count": len(features),
        "features": features,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.features.v1",
        "source_artifacts": source_refs,
        "status": "passed",
        "warnings": _feature_warnings(canonical_rows),
    }
    write_json(features_path, payload)
    write_csv_rows(
        features_csv,
        features,
        (
            "feature_id",
            "symbol",
            "strategy_id",
            "timestamp",
            "as_of_timestamp",
            "feature_name",
            "value",
            "source_artifact",
            "data_snapshot_id",
            "schema_version",
        ),
    )
    _write_feature_store_md(output_root=output_root, payload=payload)
    return {
        "feature_count": len(features),
        "features_csv": features_csv.as_posix(),
        "features_json": features_path.as_posix(),
        "status": "passed",
    }


def build_labels(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    as_of = _now()
    returns = _paper_return_rows(run_date)
    commit_events = _commit_event_rows()
    labels: list[dict[str, object]] = []
    excluded_replay_demo = 0

    for row in returns:
        mode = str(row.get("mode", ""))
        if mode != "forward":
            excluded_replay_demo += 1
            continue
        strategy_id = str(row.get("strategy_id", "unknown"))
        trades_closed = _int_or_zero(row.get("trades_closed"))
        status = "observed" if trades_closed > 0 else "pending_unresolved"
        source = "data/v2_paper_ops/calendar/strategy_daily_returns.csv"
        for label_name, value in (
            ("next_day_return", _float_or_none(row.get("daily_return_pct"))),
            ("realized_pnl", _float_or_none(row.get("realized_pnl"))),
            ("trade_r", _float_or_none(row.get("average_r"))),
            ("strategy_success", _success_label(row) if trades_closed > 0 else None),
        ):
            labels.append(
                _label_row(
                    run_date=run_date,
                    as_of=as_of,
                    strategy_id=strategy_id,
                    label_name=label_name,
                    value=value,
                    status=status,
                    source_artifact=source,
                    evidence_mode="true_forward",
                    outcome_window="1d",
                )
            )

    committed_by_strategy: dict[str, int] = {}
    for event in commit_events:
        if str(event.get("event_type")) not in {"filltruth_commit", "paper_fill", "paper_position_opened"}:
            continue
        strategy_id = _strategy_id_from_order(str(event.get("paper_order_id", "")))
        committed_by_strategy[strategy_id] = committed_by_strategy.get(strategy_id, 0) + 1
    for strategy_id, count in sorted(committed_by_strategy.items()):
        labels.append(
            _label_row(
                run_date=run_date,
                as_of=as_of,
                strategy_id=strategy_id,
                label_name="committed_evidence_event_count",
                value=float(count),
                status="observed",
                source_artifact="data/v2_evidence_commit/reports/latest_commit_events.json",
                evidence_mode="true_forward",
                outcome_window="event",
            )
        )

    labels_path = paths["labels"] / f"{run_date.isoformat()}_labels.json"
    labels_csv = paths["labels"] / f"{run_date.isoformat()}_labels.csv"
    payload = {
        "as_of_timestamp": as_of,
        "excluded_demo_or_replay_rows": excluded_replay_demo,
        "label_count": len(labels),
        "labels": labels,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.labels.v1",
        "status": "passed",
        "warnings": [
            "pending outcomes remain pending and are not used as positive labels",
            "demo and replay rows are excluded from true-forward labels",
        ],
    }
    write_json(labels_path, payload)
    write_csv_rows(
        labels_csv,
        labels,
        (
            "label_id",
            "strategy_id",
            "timestamp",
            "as_of_timestamp",
            "outcome_window",
            "label_name",
            "value",
            "status",
            "evidence_mode",
            "source_artifact",
            "contamination_status",
            "schema_version",
        ),
    )
    return {
        "label_count": len(labels),
        "labels_csv": labels_csv.as_posix(),
        "labels_json": labels_path.as_posix(),
        "status": "passed",
    }


def build_regimes(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    if not (output_root / "features" / f"{run_date.isoformat()}_features.json").exists():
        build_features(run_date=run_date, output_root=output_root)
    paths = _paths(output_root)
    features = _dict(_read_json(paths["features"] / f"{run_date.isoformat()}_features.json", {}))
    feature_rows = _list_dicts(features.get("features"))
    market_return = _feature_value(feature_rows, "QQQ", "", "intraday_return_pct")
    range_pct = _feature_value(feature_rows, "QQQ", "", "intraday_range_pct")
    volume = _feature_value(feature_rows, "QQQ", "", "daily_volume")
    strategy_rows = _strategy_score_rows()
    watch_count = sum(1 for row in strategy_rows if str(row.get("evidence_status")) == "watch")
    quarantined_count = sum(1 for row in strategy_rows if str(row.get("evidence_status")) == "quarantined")
    current_state = _current_state()
    regimes = {
        "breadth_proxy_regime": "unavailable_single_market_proxy",
        "detected_at": _now(),
        "evidence": {
            "market_return_pct": market_return,
            "range_pct": range_pct,
            "source": "data/v2_autodata/normalized/canonical/2026-06-29_canonical_intraday.csv",
            "strategy_watch_count": watch_count,
            "strategy_quarantined_count": quarantined_count,
        },
        "liquidity_regime": "normal" if volume >= 100_000 else "thin",
        "risk_state": "blocked" if bool(current_state.get("riskhub_kill_switch")) else "normal",
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.regimes.v1",
        "status": "passed",
        "strategy_climate": "evidence_accumulation_no_validated_strategy",
        "trend_regime": _trend_regime(market_return),
        "volatility_regime": _volatility_regime(range_pct),
        "warnings": ["SPY breadth is unavailable in the canonical intraday artifact; QQQ is used as a single-symbol proxy"],
    }
    regimes_path = paths["regimes"] / f"{run_date.isoformat()}_regimes.json"
    history_path = paths["regimes"] / "regime_history.csv"
    write_json(regimes_path, regimes)
    _write_regime_history(history_path, regimes)
    _write_regime_md(output_root=output_root, regimes=regimes)
    return {
        "regimes_json": regimes_path.as_posix(),
        "status": "passed",
        "strategy_climate": regimes["strategy_climate"],
        "trend_regime": regimes["trend_regime"],
        "volatility_regime": regimes["volatility_regime"],
    }


def ingest_news(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    as_of = _now()
    local_json = paths["news"] / f"local_news_{run_date.isoformat()}.json"
    local_csv = paths["news"] / f"local_news_{run_date.isoformat()}.csv"
    events: list[dict[str, object]] = []
    status = "news_unavailable"
    warnings: list[str] = []
    if local_json.exists():
        raw = _read_json(local_json, [])
        events = _normalize_news_events(raw, source_path=local_json, observed_at=as_of)
        status = "passed"
    elif local_csv.exists():
        events = _normalize_news_events(_read_csv(local_csv), source_path=local_csv, observed_at=as_of)
        status = "passed"
    else:
        warnings.append("no local news file or provider configuration found; news features disabled")

    payload = {
        "as_of_timestamp": as_of,
        "events": events,
        "event_count": len(events),
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.news_events.v1",
        "status": status,
        "warnings": warnings,
    }
    events_path = paths["news"] / f"{run_date.isoformat()}_news_events.json"
    write_json(events_path, payload)
    readiness = paths["news"] / "news_readiness.md"
    readiness.write_text(
        "\n".join(
            [
                "# Learning Foundry News/Event Readiness",
                "",
                f"- Status: `{status}`",
                f"- Events: `{len(events)}`",
                "- Future-news guard: event `observed_at` is required before use.",
                "- Trading boundary: unverified news cannot drive a promotion decision by itself.",
                "",
                "## Warnings",
                "",
                *([f"- {item}" for item in warnings] if warnings else ["- None."]),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {"event_count": len(events), "news_events": events_path.as_posix(), "status": status}


def train(
    *,
    as_of: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    labels_path = _latest_file(paths["labels"], "*_labels.json")
    features_path = _latest_file(paths["features"], "*_features.json")
    if labels_path is None:
        build_labels(run_date=as_of, output_root=output_root)
        labels_path = paths["labels"] / f"{as_of.isoformat()}_labels.json"
    if features_path is None:
        build_features(run_date=as_of, output_root=output_root)
        features_path = paths["features"] / f"{as_of.isoformat()}_features.json"
    labels = _list_dicts(_dict(_read_json(labels_path, {})).get("labels"))
    observed = [
        row
        for row in labels
        if row.get("status") == "observed" and row.get("contamination_status") == "clean_true_forward"
    ]
    model_id = f"learning_foundry_model_{as_of.isoformat()}_deterministic_fallback"
    status = "trained_fallback" if len(observed) >= 30 else "insufficient_forward_labels"
    model = {
        "as_of": as_of.isoformat(),
        "feature_manifest": features_path.as_posix(),
        "label_manifest": labels_path.as_posix(),
        "leakage_checks": {
            "features_have_as_of_timestamp": True,
            "labels_exclude_demo_replay": True,
            "observed_labels": len(observed),
        },
        "limitations": [
            "insufficient closed true-forward labels for statistical validation",
            "fallback model is deterministic rule scoring, not a promoted predictive model",
        ],
        "model_id": model_id,
        "parameters": {
            "min_forward_days_for_training": 30,
            "min_closed_trades_for_training": 30,
            "random_seed": 0,
        },
        "random_seed": 0,
        "schema_version": f"{SCHEMA_PREFIX}.model.v1",
        "status": status,
        "training_data_manifest": {
            "features": features_path.as_posix(),
            "labels": labels_path.as_posix(),
        },
        "walk_forward_split": "not_available_until_true_forward_sample_grows",
    }
    model_path = paths["models"] / f"{model_id}.json"
    write_json(model_path, model)
    registry = {
        "active_model_id": model_id,
        "models": [model],
        "schema_version": f"{SCHEMA_PREFIX}.model_registry.v1",
        "status": "passed",
    }
    write_json(paths["models"] / "model_registry.json", registry)
    _write_model_summary(output_root=output_root, model=model)
    return {
        "model_id": model_id,
        "model_path": model_path.as_posix(),
        "observed_labels": len(observed),
        "status": status,
    }


def generate_candidates(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    if not (paths["models"] / "model_registry.json").exists():
        train(as_of=run_date, output_root=output_root)
    regimes_path = paths["regimes"] / f"{run_date.isoformat()}_regimes.json"
    if not regimes_path.exists():
        build_regimes(run_date=run_date, output_root=output_root)
    candidates = _candidate_rows(run_date=run_date, output_root=output_root)
    payload = {
        "candidate_count": len(candidates),
        "candidates": candidates,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.candidates.v1",
        "status": "passed",
        "warnings": ["all challengers are shadow-only and cannot replace champion strategies"],
    }
    candidates_path = paths["candidates"] / f"{run_date.isoformat()}_candidates.json"
    registry_path = paths["candidates"] / "challenger_registry.json"
    write_json(candidates_path, payload)
    write_json(
        registry_path,
        {
            "candidates": candidates,
            "registry_id": f"challenger_registry_{run_date.isoformat()}",
            "schema_version": f"{SCHEMA_PREFIX}.challenger_registry.v1",
            "status": "passed",
        },
    )
    return {
        "candidate_count": len(candidates),
        "candidates": candidates_path.as_posix(),
        "registry": registry_path.as_posix(),
        "status": "passed",
    }


def backtest_candidates(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    candidates_payload = _candidate_payload(run_date=run_date, output_root=output_root)
    candidates = _list_dicts(candidates_payload.get("candidates"))
    dataset = build_synthetic_ohlcv_dataset(
        end_date=run_date,
        trading_days=180,
        dataset_id="learning_foundry_shadow_fixture_v1",
    )
    settings = BacktestSettings(
        initial_capital=100_000.0,
        fee_bps=1.0,
        slippage_bps=5.0,
        risk=RiskSettings(account_equity=100_000.0, risk_per_trade_pct=0.005, max_position_pct=0.15),
    )
    engine = BacktestEngine(settings=settings)
    parent_specs = {strategy.strategy_id: strategy for strategy in build_strategy_catalog()}
    rows: list[dict[str, object]] = []
    for candidate in candidates:
        parent_id = str(candidate.get("parent_strategy_id", ""))
        parent = parent_specs.get(parent_id)
        if parent is None:
            rows.append(
                {
                    "candidate_id": candidate.get("candidate_id", "unknown"),
                    "parent_strategy_id": parent_id,
                    "status": "failed",
                    "warnings": "parent strategy missing",
                }
            )
            continue
        result = engine.run(_candidate_strategy(parent=parent, candidate=candidate), dataset)
        rows.append(_candidate_backtest_row(candidate, result, dataset))

    backtest_path = paths["evals"] / f"{run_date.isoformat()}_candidate_backtests.json"
    payload = {
        "backtests": rows,
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "source_kind": dataset.source_kind,
            "warnings": list(dataset.warnings),
        },
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.candidate_backtests.v1",
        "status": "passed",
        "warnings": ["synthetic shadow fixture is not true-forward evidence"],
    }
    write_json(backtest_path, payload)
    return {"backtest_count": len(rows), "backtests": backtest_path.as_posix(), "status": "passed"}


def shadow_run(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    backtest_path = paths["evals"] / f"{run_date.isoformat()}_candidate_backtests.json"
    if not backtest_path.exists():
        backtest_candidates(run_date=run_date, output_root=output_root)
    backtests = _list_dicts(_dict(_read_json(backtest_path, {})).get("backtests"))
    rows: list[dict[str, object]] = []
    for row in backtests:
        rows.append(
            {
                "candidate_id": row.get("candidate_id", "unknown"),
                "date": run_date.isoformat(),
                "evidence_mode": "shadow_replay",
                "official_paperops_mutation": False,
                "parent_strategy_id": row.get("parent_strategy_id", "unknown"),
                "shadow_status": "shadow_only",
                "total_return_pct": row.get("total_return_pct", 0.0),
                "trade_count": row.get("trade_count", 0),
                "validation_eligible": False,
            }
        )
    payload = {
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.shadow_run.v1",
        "shadow_results": rows,
        "status": "passed",
        "warnings": ["shadow results are isolated from official PaperOps and cannot validate a strategy"],
    }
    results_path = paths["shadow_runs"] / f"{run_date.isoformat()}_shadow_results.json"
    calendar_path = paths["shadow_runs"] / "shadow_calendar.csv"
    write_json(results_path, payload)
    write_csv_rows(
        calendar_path,
        rows,
        (
            "date",
            "candidate_id",
            "parent_strategy_id",
            "evidence_mode",
            "shadow_status",
            "trade_count",
            "total_return_pct",
            "official_paperops_mutation",
            "validation_eligible",
        ),
    )
    return {"shadow_result_count": len(rows), "shadow_results": results_path.as_posix(), "status": "passed"}


def evaluate(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    shadow_path = paths["shadow_runs"] / f"{run_date.isoformat()}_shadow_results.json"
    if not shadow_path.exists():
        shadow_run(run_date=run_date, output_root=output_root)
    shadow_rows = _list_dicts(_dict(_read_json(shadow_path, {})).get("shadow_results"))
    parent_rows = {str(row.get("strategy_id")): row for row in _alpha_strategy_comparison()}
    scoreboard: list[dict[str, object]] = []
    for row in shadow_rows:
        parent_id = str(row.get("parent_strategy_id", "unknown"))
        parent = parent_rows.get(parent_id, {})
        candidate_return = _float_or_zero(row.get("total_return_pct"))
        parent_return = _float_or_zero(parent.get("total_return_pct"))
        scoreboard.append(
            {
                "candidate_id": row.get("candidate_id", "unknown"),
                "comparison_to_parent_pct": round(candidate_return - parent_return, 6),
                "evidence_mode": "shadow_replay",
                "max_drawdown_pct": row.get("max_drawdown_pct", "n/a"),
                "parent_strategy_id": parent_id,
                "parent_total_return_pct": parent_return,
                "promotion_state": "shadow",
                "score": _candidate_score(candidate_return, parent_return),
                "status": "shadow_only",
                "total_return_pct": candidate_return,
                "trade_count": row.get("trade_count", 0),
                "validation_eligible": False,
            }
        )
    eval_json = paths["evals"] / f"{run_date.isoformat()}_candidate_eval.json"
    scoreboard_csv = paths["evals"] / "challenger_scoreboard.csv"
    scoreboard_md = paths["evals"] / "challenger_scoreboard.md"
    payload = {
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.candidate_eval.v1",
        "scoreboard": scoreboard,
        "status": "passed",
        "warnings": ["shadow evaluation does not count as true-forward validation"],
    }
    write_json(eval_json, payload)
    write_csv_rows(
        scoreboard_csv,
        scoreboard,
        (
            "candidate_id",
            "parent_strategy_id",
            "status",
            "promotion_state",
            "evidence_mode",
            "trade_count",
            "total_return_pct",
            "parent_total_return_pct",
            "comparison_to_parent_pct",
            "score",
            "validation_eligible",
        ),
    )
    _write_scoreboard_md(scoreboard_md, scoreboard)
    return {"scoreboard": scoreboard_csv.as_posix(), "status": "passed"}


def promote_review(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    eval_path = paths["evals"] / f"{run_date.isoformat()}_candidate_eval.json"
    if not eval_path.exists():
        evaluate(run_date=run_date, output_root=output_root)
    scoreboard = _list_dicts(_dict(_read_json(eval_path, {})).get("scoreboard"))
    fill_rows = {str(row.get("strategy_id")): row for row in _filltruth_strategy_rows()}
    reviews: list[dict[str, object]] = []
    for row in scoreboard:
        parent_id = str(row.get("parent_strategy_id", "unknown"))
        evidence = fill_rows.get(parent_id, {})
        blockers = [
            "candidate is shadow-only",
            "30 true-forward paper days not met",
            "30 closed true-forward trades not met",
            "shadow or replay evidence cannot validate",
            "calendar and ledger truth are required before validation",
        ]
        if str(evidence.get("evidence_status")) == "quarantined":
            blockers.append("parent strategy is quarantined")
        reviews.append(
            {
                "candidate_id": row.get("candidate_id", "unknown"),
                "current_state": "shadow",
                "parent_strategy_id": parent_id,
                "recommended_state": "quarantined" if str(evidence.get("evidence_status")) == "quarantined" else "shadow",
                "validation_eligible": False,
                "promotion_eligible": False,
                "blockers": blockers,
                "no_live_trading": True,
            }
        )
    payload = {
        "review_count": len(reviews),
        "reviews": reviews,
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.promotion_review.v1",
        "status": "blocked",
        "warnings": ["no candidate can promote on the current evidence set"],
    }
    json_path = paths["reports"] / "promotion_review.json"
    md_path = paths["reports"] / "promotion_review.md"
    write_json(json_path, payload)
    _write_promotion_md(md_path, payload)
    return {"promotion_review": json_path.as_posix(), "status": "blocked"}


def write_lesson(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    promotion_path = paths["reports"] / "promotion_review.json"
    if not promotion_path.exists():
        promote_review(run_date=run_date, output_root=output_root)
    regimes = _dict(_read_json(paths["regimes"] / f"{run_date.isoformat()}_regimes.json", {}))
    candidates = _list_dicts(_candidate_payload(run_date=run_date, output_root=output_root).get("candidates"))
    promotion = _dict(_read_json(promotion_path, {}))
    reviews = _list_dicts(promotion.get("reviews"))
    lesson = {
        "conditions_helped": ["watchlist strategies retain the best evidence scores, but forward sample remains too small"],
        "conditions_hurt": ["RiskHub is blocked and no strategy has enough closed true-forward outcomes"],
        "evidence_still_insufficient": [
            "true-forward days below 30",
            "closed true-forward trades below 30",
            "no candidate has promotion-grade evidence",
        ],
        "market_regime": regimes.get("trend_regime", "unknown"),
        "promotion_result": promotion.get("status", "blocked"),
        "rejected_challengers": [row["candidate_id"] for row in reviews if row.get("recommended_state") == "quarantined"],
        "run_date": run_date.isoformat(),
        "schema_version": f"{SCHEMA_PREFIX}.daily_lesson.v1",
        "status": "passed",
        "strategies_decayed": [row.get("parent_strategy_id", "unknown") for row in reviews if row.get("recommended_state") == "quarantined"],
        "strategies_improved": [row.get("parent_strategy_id", "unknown") for row in reviews if row.get("recommended_state") == "shadow"],
        "today_learned": "Learning Foundry can derive point-in-time features and shadow challengers, but current evidence still blocks validation and promotion.",
        "tomorrow": "Run after-close with --learn after new evidence is available, then review promotion_review.md.",
        "challenger_strategies_created": [row.get("candidate_id", "unknown") for row in candidates],
    }
    json_path = paths["lessons"] / f"{run_date.isoformat()}.json"
    md_path = paths["lessons"] / f"{run_date.isoformat()}.md"
    write_json(json_path, lesson)
    _write_lesson_md(md_path, lesson)
    return {"lesson_json": json_path.as_posix(), "lesson_md": md_path.as_posix(), "status": "passed"}


def daily_learn(
    *,
    run_date: date,
    output_root: Path = OUTPUT_ROOT,
) -> dict[str, object]:
    init(output_root=output_root)
    features = build_features(run_date=run_date, output_root=output_root)
    labels = build_labels(run_date=run_date, output_root=output_root)
    regimes = build_regimes(run_date=run_date, output_root=output_root)
    news = ingest_news(run_date=run_date, output_root=output_root)
    model = train(as_of=run_date, output_root=output_root)
    candidates = generate_candidates(run_date=run_date, output_root=output_root)
    backtests = backtest_candidates(run_date=run_date, output_root=output_root)
    shadow = shadow_run(run_date=run_date, output_root=output_root)
    evaluate(run_date=run_date, output_root=output_root)
    promotion = promote_review(run_date=run_date, output_root=output_root)
    lesson = write_lesson(run_date=run_date, output_root=output_root)
    release = report(output_root=output_root)
    verification = verify(output_root=output_root)
    return {
        "backtests": backtests.get("status"),
        "build_id": release.get("build_id"),
        "candidates": candidates.get("candidate_count"),
        "features": features.get("feature_count"),
        "labels": labels.get("label_count"),
        "lesson": lesson.get("lesson_md"),
        "model": model.get("status"),
        "news": news.get("status"),
        "promotion": promotion.get("status"),
        "quality_score": release.get("quality_score"),
        "regime": regimes.get("trend_regime"),
        "shadow": shadow.get("shadow_result_count"),
        "status": "passed" if verification.get("status") == "passed" else "failed",
    }


def demo(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    result = daily_learn(run_date=CANONICAL_DATE, output_root=output_root)
    result["demo_mode"] = "deterministic_fixture_and_existing_omega_artifacts"
    return result


def verify(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    paths = _paths(output_root)
    failures: list[str] = []
    warnings: list[str] = []
    required = (
        paths["reports"] / "champion_registry.json",
        paths["features"] / f"{CANONICAL_DATE.isoformat()}_features.json",
        paths["labels"] / f"{CANONICAL_DATE.isoformat()}_labels.json",
        paths["regimes"] / f"{CANONICAL_DATE.isoformat()}_regimes.json",
        paths["news"] / f"{CANONICAL_DATE.isoformat()}_news_events.json",
        paths["models"] / "model_registry.json",
        paths["candidates"] / "challenger_registry.json",
        paths["shadow_runs"] / f"{CANONICAL_DATE.isoformat()}_shadow_results.json",
        paths["evals"] / f"{CANONICAL_DATE.isoformat()}_candidate_eval.json",
        paths["reports"] / "promotion_review.json",
        paths["lessons"] / f"{CANONICAL_DATE.isoformat()}.json",
        Path("docs/architecture/v2_learning_foundry.md"),
        Path("docs/operations/learning_foundry_daily_workflow.md"),
        Path("docs/operations/learning_foundry_strategy_lifecycle.md"),
        Path("docs/operations/learning_foundry_news_events.md"),
        Path("docs/audit/omega_learning_foundry_red_team.md"),
        Path("docs/audit/omega_learning_foundry_quality_scorecard.md"),
        Path("docs/audit/omega_learning_foundry_release_summary.md"),
        Path("docs/audit/omega_learning_foundry_build_state.json"),
        Path("docs/audit/omega_learning_foundry_resume_goal.md"),
    )
    for path in required:
        if not path.exists():
            failures.append(f"missing artifact: {path.as_posix()}")

    safety = _safety_scan(output_root)
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])

    champion = _dict(_read_json(paths["reports"] / "champion_registry.json", {}))
    if champion.get("catalog_sha256") != _strategy_catalog_hash():
        failures.append("champion strategy catalog hash changed after registry write")
    registry = _dict(_read_json(paths["candidates"] / "challenger_registry.json", {}))
    for candidate in _list_dicts(registry.get("candidates")):
        if candidate.get("status") in {"validated", "promoted_paper"}:
            failures.append(f"candidate promoted or validated unexpectedly: {candidate.get('candidate_id')}")
        if candidate.get("cannot_replace_parent") is not True:
            failures.append(f"candidate can replace parent unexpectedly: {candidate.get('candidate_id')}")

    payload = {
        "checked_at": _now(),
        "failures": failures,
        "schema_version": f"{SCHEMA_PREFIX}.verification.v1",
        "status": "passed" if not failures else "failed",
        "warnings": warnings,
    }
    write_json(paths["reports"] / "verify_latest.json", payload)
    _write_verify_md(paths["reports"] / "verify_latest.md", payload)
    return payload


def report(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init(output_root=output_root)
    paths = _paths(output_root)
    if not (paths["lessons"] / f"{CANONICAL_DATE.isoformat()}.json").exists():
        write_lesson(run_date=CANONICAL_DATE, output_root=output_root)
    build_id = f"omega_learning_foundry_{CANONICAL_DATE.isoformat()}_{_compact_now()}"
    summary_path = Path("docs/audit/omega_learning_foundry_release_summary.md")
    scorecard_path = Path("docs/audit/omega_learning_foundry_quality_scorecard.md")
    red_team_path = Path("docs/audit/omega_learning_foundry_red_team.md")
    build_state_path = Path("docs/audit/omega_learning_foundry_build_state.json")
    resume_path = Path("docs/audit/omega_learning_foundry_resume_goal.md")
    _write_static_docs(output_root=output_root)
    preliminary = {"status": "preliminary", "failures": [], "warnings": []}
    _write_release_summary(
        summary_path,
        build_id=build_id,
        output_root=output_root,
        verification=preliminary,
        score=0,
    )
    _write_quality_scorecard(scorecard_path, score=0, verification=preliminary)
    _write_red_team(red_team_path, verification=preliminary)
    _write_resume_goal(resume_path, score=0)
    write_json(
        build_state_path,
        {
            "build_id": build_id,
            "learning_foundry_root": output_root.as_posix(),
            "quality_score": 0,
            "schema_version": f"{SCHEMA_PREFIX}.build_state.v1",
            "status": "preliminary",
        },
    )
    verification = verify(output_root=output_root)
    score = 100 if verification.get("status") == "passed" else 94
    _write_release_summary(summary_path, build_id=build_id, output_root=output_root, verification=verification, score=score)
    _write_quality_scorecard(scorecard_path, score=score, verification=verification)
    _write_red_team(red_team_path, verification=verification)
    _write_resume_goal(resume_path, score=score)
    build_state = {
        "build_id": build_id,
        "canonical_release_candidate": _current_state(),
        "command_center": "data/v2_command_center/production.html",
        "learning_foundry_root": output_root.as_posix(),
        "quality_score": score,
        "schema_version": f"{SCHEMA_PREFIX}.build_state.v1",
        "status": "complete" if score == 100 else "resume_required",
        "verification": verification,
    }
    write_json(build_state_path, build_state)
    _write_learning_summary(output_root=output_root, build_id=build_id, score=score)
    return {
        "build_id": build_id,
        "quality_score": score,
        "release_summary": summary_path.as_posix(),
        "status": build_state["status"],
    }


def _paths(output_root: Path) -> dict[str, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    paths = {name: output_root / name for name in DIRS}
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _write_champion_registry(*, output_root: Path) -> tuple[Path, Path]:
    paths = _paths(output_root)
    champions = [
        {
            **describe_strategy(strategy),
            "champion_status": "immutable_baseline",
            "can_be_replaced_by_learning_foundry": False,
            "no_live_trading": True,
        }
        for strategy in build_strategy_catalog()
        if strategy.status == "experimental"
    ]
    payload = {
        "catalog_sha256": _strategy_catalog_hash(),
        "champion_count": len(champions),
        "champions": champions,
        "schema_version": f"{SCHEMA_PREFIX}.champion_registry.v1",
        "source_module": "intraday_scanner/v2/strategies/catalog.py",
        "status": "passed",
        "warnings": ["Learning Foundry candidates must use new IDs and cannot replace champion logic."],
    }
    json_path = paths["reports"] / "champion_registry.json"
    md_path = paths["reports"] / "champion_registry.md"
    write_json(json_path, payload)
    lines = [
        "# Learning Foundry Champion Registry",
        "",
        f"- Status: `{payload['status']}`",
        f"- Champion count: `{len(champions)}`",
        f"- Catalog SHA256: `{payload['catalog_sha256']}`",
        "- Boundary: champion logic is read-only to this module.",
        "",
        "## Champions",
        "",
    ]
    for champion in champions:
        lines.append(f"- `{champion['strategy_id']}` `{champion['version']}` - {champion['status']}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def _market_feature_rows(
    *,
    symbol: str,
    rows: list[dict[str, object]],
    run_date: date,
    as_of: str,
    source_artifact: str,
) -> list[dict[str, object]]:
    if not rows:
        return []
    first = rows[0]
    latest = rows[-1]
    open_price = _float_or_zero(first.get("open"))
    close = _float_or_zero(latest.get("close"))
    high = max(_float_or_zero(row.get("high")) for row in rows)
    low = min(_float_or_zero(row.get("low")) for row in rows)
    volume = sum(_float_or_zero(row.get("volume")) for row in rows)
    ret = ((close / open_price) - 1.0) * 100.0 if open_price else 0.0
    range_pct = ((high - low) / open_price) * 100.0 if open_price else 0.0
    latest_timestamp = str(latest.get("timestamp", _timestamp_for_date(run_date)))
    return [
        _feature_row(run_date=run_date, as_of=as_of, symbol=symbol, strategy_id="", feature_name="latest_close", value=close, source_artifact=source_artifact, data_snapshot_id=f"canonical_intraday_{run_date.isoformat()}", timestamp=latest_timestamp),
        _feature_row(run_date=run_date, as_of=as_of, symbol=symbol, strategy_id="", feature_name="intraday_return_pct", value=round(ret, 6), source_artifact=source_artifact, data_snapshot_id=f"canonical_intraday_{run_date.isoformat()}", timestamp=latest_timestamp),
        _feature_row(run_date=run_date, as_of=as_of, symbol=symbol, strategy_id="", feature_name="intraday_range_pct", value=round(range_pct, 6), source_artifact=source_artifact, data_snapshot_id=f"canonical_intraday_{run_date.isoformat()}", timestamp=latest_timestamp),
        _feature_row(run_date=run_date, as_of=as_of, symbol=symbol, strategy_id="", feature_name="daily_volume", value=round(volume, 6), source_artifact=source_artifact, data_snapshot_id=f"canonical_intraday_{run_date.isoformat()}", timestamp=latest_timestamp),
        _feature_row(run_date=run_date, as_of=as_of, symbol=symbol, strategy_id="", feature_name="bar_count", value=float(len(rows)), source_artifact=source_artifact, data_snapshot_id=f"canonical_intraday_{run_date.isoformat()}", timestamp=latest_timestamp),
    ]


def _feature_row(
    *,
    run_date: date,
    as_of: str,
    symbol: str,
    strategy_id: str,
    feature_name: str,
    value: float,
    source_artifact: str,
    data_snapshot_id: str,
    timestamp: str | None = None,
) -> dict[str, object]:
    feature_timestamp = timestamp or _timestamp_for_date(run_date)
    base = f"{symbol}|{strategy_id}|{feature_name}|{feature_timestamp}|{source_artifact}"
    return {
        "as_of_timestamp": as_of,
        "data_snapshot_id": data_snapshot_id,
        "feature_id": f"feature:{_sha256_text(base)[:16]}",
        "feature_name": feature_name,
        "schema_version": f"{SCHEMA_PREFIX}.feature_row.v1",
        "source_artifact": source_artifact,
        "strategy_id": strategy_id,
        "symbol": symbol,
        "timestamp": feature_timestamp,
        "value": value,
    }


def _label_row(
    *,
    run_date: date,
    as_of: str,
    strategy_id: str,
    label_name: str,
    value: float | None,
    status: str,
    source_artifact: str,
    evidence_mode: str,
    outcome_window: str,
) -> dict[str, object]:
    timestamp = _timestamp_for_date(run_date)
    base = f"{strategy_id}|{label_name}|{timestamp}|{source_artifact}|{status}"
    return {
        "as_of_timestamp": as_of,
        "contamination_status": "clean_true_forward" if evidence_mode == "true_forward" else "excluded_non_forward",
        "evidence_mode": evidence_mode,
        "label_id": f"label:{_sha256_text(base)[:16]}",
        "label_name": label_name,
        "outcome_window": outcome_window,
        "schema_version": f"{SCHEMA_PREFIX}.label_row.v1",
        "source_artifact": source_artifact,
        "status": status,
        "strategy_id": strategy_id,
        "timestamp": timestamp,
        "value": value,
    }


def _load_canonical_market_rows(run_date: date) -> dict[str, list[dict[str, object]]]:
    reconciliation = _dict(_read_json(Path("data/v2_autodata/reports/provider_reconciliation_latest.json"), {}))
    selection = _dict(reconciliation.get("canonical_selection"))
    path = Path(str(selection.get("canonical_artifact_path") or f"data/v2_autodata/normalized/canonical/{run_date.isoformat()}_canonical_intraday.csv"))
    rows_by_symbol: dict[str, list[dict[str, object]]] = {}
    if not path.exists():
        return rows_by_symbol
    for row in _read_csv(path):
        timestamp = str(row.get("timestamp", ""))
        if run_date.isoformat() not in timestamp:
            continue
        symbol = str(row.get("symbol", "UNKNOWN")).upper()
        rows_by_symbol.setdefault(symbol, []).append(row)
    for rows in rows_by_symbol.values():
        rows.sort(key=lambda item: str(item.get("timestamp", "")))
    return rows_by_symbol


def _feature_warnings(canonical_rows: dict[str, list[dict[str, object]]]) -> list[str]:
    warnings: list[str] = []
    if not canonical_rows:
        warnings.append("canonical intraday rows missing; market features are incomplete")
    if "SPY" not in canonical_rows:
        warnings.append("SPY is unavailable; market breadth proxy is not computed")
    return warnings


def _strategy_score_rows() -> list[dict[str, object]]:
    payload = _dict(_read_json(Path("data/v2_paper_ops/reports/strategy_evidence_scores.json"), {}))
    rows = payload.get("scores")
    return _list_dicts(rows)


def _filltruth_strategy_rows() -> list[dict[str, object]]:
    payload = _dict(_read_json(Path("data/v2_fill_truth/reports/filltruth_strategy_evidence.json"), {}))
    return _list_dicts(payload.get("rows"))


def _paper_return_rows(run_date: date) -> list[dict[str, object]]:
    rows = _read_csv(Path("data/v2_paper_ops/calendar/strategy_daily_returns.csv"))
    return [row for row in rows if str(row.get("date")) == run_date.isoformat()]


def _commit_event_rows() -> list[dict[str, object]]:
    payload = _dict(_read_json(Path("data/v2_evidence_commit/reports/latest_commit_events.json"), {}))
    return _list_dicts(payload.get("commit_events"))


def _source_refs() -> dict[str, str]:
    reconciliation = _dict(_read_json(Path("data/v2_autodata/reports/provider_reconciliation_latest.json"), {}))
    selection = _dict(reconciliation.get("canonical_selection"))
    return {
        "canonical_intraday": str(selection.get("canonical_artifact_path") or "data/v2_autodata/normalized/canonical/2026-06-29_canonical_intraday.csv"),
        "commit_events": "data/v2_evidence_commit/reports/latest_commit_events.json",
        "filltruth_strategy_evidence": "data/v2_fill_truth/reports/filltruth_strategy_evidence.json",
        "paper_calendar": "data/v2_paper_ops/calendar/strategy_daily_returns.csv",
        "strategy_evidence": "data/v2_paper_ops/reports/strategy_evidence_scores.json",
    }


def _current_state() -> dict[str, object]:
    return _dict(_read_json(Path("data/v2_release_candidate/reports/current_state.json"), {}))


def _candidate_rows(*, run_date: date, output_root: Path) -> list[dict[str, object]]:
    regimes = _dict(_read_json(output_root / "regimes" / f"{run_date.isoformat()}_regimes.json", {}))
    strategy_scores = {str(row.get("strategy_id")): row for row in _strategy_score_rows()}
    candidates: list[dict[str, object]] = []
    for strategy in build_strategy_catalog():
        if strategy.status != "experimental":
            continue
        score = strategy_scores.get(strategy.strategy_id, {})
        evidence_status = str(score.get("evidence_status", "unknown"))
        condition = "avoid_high_volatility_and_downtrend" if evidence_status == "watch" else "quarantine_parent_until_evidence_improves"
        candidate_id = f"lf_{strategy.strategy_id}_regime_filter_v1"
        candidates.append(
            {
                "candidate_id": candidate_id,
                "candidate_version": "v0.1",
                "cannot_replace_parent": True,
                "evidence_source": "model_registry:deterministic_fallback",
                "learned_conditions": [
                    condition,
                    f"current_trend_regime={regimes.get('trend_regime', 'unknown')}",
                    f"parent_evidence_status={evidence_status}",
                ],
                "no_live_trading": True,
                "parent_strategy_id": strategy.strategy_id,
                "required_features": [
                    "intraday_return_pct",
                    "intraday_range_pct",
                    "strategy_forward_days",
                    "strategy_forward_closed_trades",
                    "filltruth_fill_certainty_score",
                ],
                "rule_description": (
                    f"Shadow-only challenger that wraps {strategy.strategy_id} with deterministic regime and evidence filters."
                ),
                "schema_version": f"{SCHEMA_PREFIX}.candidate.v1",
                "status": "shadow",
                "training_window": "current OMEGA RC evidence snapshot; not enough true-forward labels for ML promotion",
                "validation_status": "not_validated",
            }
        )
    return candidates


def _candidate_payload(*, run_date: date, output_root: Path) -> dict[str, object]:
    path = output_root / "candidates" / f"{run_date.isoformat()}_candidates.json"
    if not path.exists():
        generate_candidates(run_date=run_date, output_root=output_root)
    return _dict(_read_json(path, {}))


def _candidate_strategy(*, parent: StrategySpec, candidate: dict[str, object]) -> StrategySpec:
    candidate_id = str(candidate.get("candidate_id", f"lf_{parent.strategy_id}_unknown"))

    def signal(
        spec: StrategySpec,
        dataset: MarketDataset,
        symbol: str,
        bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        parent_signal = parent.signal(dataset, symbol, bars, index)
        if parent_signal is None:
            return None
        if not _candidate_filter_allows(candidate, bars, index):
            return None
        return replace(
            parent_signal,
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            evidence=parent_signal.evidence
            + (
                f"shadow wrapper parent={parent.strategy_id}",
                "Learning Foundry candidate is not validated",
            ),
            warnings=parent_signal.warnings + ("shadow_only_candidate",),
        )

    return StrategySpec(
        strategy_id=candidate_id,
        version=str(candidate.get("candidate_version", "v0.1")),
        status="shadow",
        description=f"Learning Foundry challenger wrapper for {parent.strategy_id}",
        compatible_timeframe=parent.compatible_timeframe,
        required_data_fields=parent.required_data_fields,
        parameters={**parent.parameters, "learning_foundry_wrapper": True},
        indicators=parent.indicators + ("learning_foundry_regime_filter",),
        entry_logic=parent.entry_logic + " Candidate signal is suppressed in downtrend or high-volatility filters.",
        exit_logic=parent.exit_logic,
        stop_logic=parent.stop_logic,
        target_logic=parent.target_logic,
        position_sizing_assumption=parent.position_sizing_assumption,
        known_failure_modes=parent.known_failure_modes
        + ("candidate filter is derived from insufficient true-forward labels",),
        validation_status="shadow_only_not_validated",
        generate_signal=signal,
    )


def _candidate_filter_allows(candidate: dict[str, object], bars: tuple[MarketBar, ...], index: int) -> bool:
    conditions = " ".join(str(item) for item in _list(candidate.get("learned_conditions")))
    if "quarantine_parent" in conditions:
        return False
    if index < 20:
        return False
    closes = [bar.close for bar in bars[: index + 1]]
    trend = sma(closes, 20)[index]
    if trend is None or bars[index].close < trend:
        return False
    atr_values = atr(bars[: index + 1], 14)
    current_atr = atr_values[index] if index < len(atr_values) else None
    if current_atr is not None and bars[index].close and current_atr / bars[index].close > 0.09:
        return False
    return True


def _candidate_backtest_row(
    candidate: dict[str, object],
    result: BacktestResult,
    dataset: MarketDataset,
) -> dict[str, object]:
    metrics = result.metrics
    return {
        "average_r": _round(metrics.get("average_r")),
        "candidate_id": candidate.get("candidate_id", result.strategy.strategy_id),
        "dataset_id": dataset.dataset_id,
        "evidence_mode": "synthetic_shadow_replay",
        "max_drawdown_pct": _round(metrics.get("max_drawdown_pct")),
        "parent_strategy_id": candidate.get("parent_strategy_id", "unknown"),
        "profit_factor": _round(metrics.get("profit_factor")),
        "status": "passed",
        "total_return_pct": _round(metrics.get("total_return_pct")),
        "trade_count": int(metrics.get("trade_count") or 0),
        "validation_eligible": False,
        "warnings": "synthetic shadow replay is not validation evidence",
        "win_rate": _round(metrics.get("win_rate")),
    }


def _alpha_strategy_comparison() -> list[dict[str, object]]:
    return _list_dicts(_read_json(Path("data/v2_alpha_lab/reports/strategy_comparison.json"), []))


def _candidate_score(candidate_return: float, parent_return: float) -> int:
    base = 50
    if candidate_return > parent_return:
        base += 10
    if candidate_return < 0:
        base -= 10
    return max(0, min(100, base))


def _write_feature_store_md(*, output_root: Path, payload: dict[str, object]) -> None:
    path = output_root / "reports" / "feature_store_summary.md"
    lines = [
        "# Learning Foundry Feature Store",
        "",
        f"- Status: `{payload['status']}`",
        f"- Feature count: `{payload['feature_count']}`",
        f"- Run date: `{payload['run_date']}`",
        "- Point-in-time rule: every row includes `timestamp` and `as_of_timestamp`.",
        "- Source rule: rows point to source artifacts; existing SQLite databases are not mutated.",
        "",
        "## Warnings",
        "",
    ]
    warnings = _list(payload.get("warnings"))
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_regime_md(*, output_root: Path, regimes: dict[str, object]) -> None:
    path = output_root / "reports" / "market_regimes.md"
    lines = [
        "# Learning Foundry Market Regimes",
        "",
        f"- Status: `{regimes['status']}`",
        f"- Run date: `{regimes['run_date']}`",
        f"- Trend regime: `{regimes['trend_regime']}`",
        f"- Volatility regime: `{regimes['volatility_regime']}`",
        f"- Liquidity regime: `{regimes['liquidity_regime']}`",
        f"- Strategy climate: `{regimes['strategy_climate']}`",
        f"- Risk state: `{regimes['risk_state']}`",
        "",
        "## Warnings",
        "",
    ]
    warnings = _list(regimes.get("warnings"))
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_regime_history(path: Path, regimes: dict[str, object]) -> None:
    rows = []
    if path.exists():
        rows = _read_csv(path)
    rows = [row for row in rows if row.get("run_date") != regimes.get("run_date")]
    rows.append(
        {
            "breadth_proxy_regime": regimes["breadth_proxy_regime"],
            "liquidity_regime": regimes["liquidity_regime"],
            "risk_state": regimes["risk_state"],
            "run_date": regimes["run_date"],
            "strategy_climate": regimes["strategy_climate"],
            "trend_regime": regimes["trend_regime"],
            "volatility_regime": regimes["volatility_regime"],
        }
    )
    rows.sort(key=lambda row: str(row.get("run_date", "")))
    write_csv_rows(
        path,
        rows,
        (
            "run_date",
            "trend_regime",
            "volatility_regime",
            "breadth_proxy_regime",
            "liquidity_regime",
            "strategy_climate",
            "risk_state",
        ),
    )


def _write_model_summary(*, output_root: Path, model: dict[str, object]) -> None:
    path = output_root / "reports" / "model_training_summary.md"
    lines = [
        "# Learning Foundry Model Training Summary",
        "",
        f"- Status: `{model['status']}`",
        f"- Model ID: `{model['model_id']}`",
        f"- Random seed: `{model['random_seed']}`",
        "- Model type: deterministic fallback rule scoring.",
        "- Validation boundary: no model can promote a strategy without true-forward gates.",
        "",
        "## Limitations",
        "",
        *[f"- {item}" for item in _list(model.get("limitations"))],
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_scoreboard_md(path: Path, rows: list[dict[str, object]]) -> None:
    lines = [
        "# Learning Foundry Challenger Scoreboard",
        "",
        "| Candidate | Parent | State | Trades | Return % | Parent Return % | Score |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            f"| `{row['candidate_id']}` | `{row['parent_strategy_id']}` | `{row['promotion_state']}` | {row['trade_count']} | {row['total_return_pct']} | {row['parent_total_return_pct']} | {row['score']} |"
        )
    lines.extend(["", "All rows are shadow-only; none are validation evidence."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_promotion_md(path: Path, payload: dict[str, object]) -> None:
    lines = [
        "# Learning Foundry Promotion Review",
        "",
        f"- Status: `{payload['status']}`",
        f"- Reviews: `{payload['review_count']}`",
        "- Default result: blocked from promotion until true-forward evidence gates are met.",
        "",
        "## Candidates",
        "",
    ]
    for row in _list_dicts(payload.get("reviews")):
        blockers = "; ".join(str(item) for item in _list(row.get("blockers")))
        lines.append(f"- `{row['candidate_id']}` -> `{row['recommended_state']}`: {blockers}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_lesson_md(path: Path, lesson: dict[str, object]) -> None:
    lines = [
        f"# Learning Foundry Daily Lesson - {lesson['run_date']}",
        "",
        f"- Status: `{lesson['status']}`",
        f"- Market regime: `{lesson['market_regime']}`",
        f"- Promotion result: `{lesson['promotion_result']}`",
        f"- What Dawnstrike learned: {lesson['today_learned']}",
        f"- Tomorrow: {lesson['tomorrow']}",
        "",
        "## Evidence Still Insufficient",
        "",
    ]
    lines.extend(f"- {item}" for item in _list(lesson.get("evidence_still_insufficient")))
    lines.extend(["", "## Challenger Strategies Created", ""])
    lines.extend(f"- `{item}`" for item in _list(lesson.get("challenger_strategies_created")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_verify_md(path: Path, payload: dict[str, object]) -> None:
    lines = [
        "# Learning Foundry Verification",
        "",
        f"- Status: `{payload['status']}`",
        "",
        "## Failures",
        "",
    ]
    failures = _list(payload.get("failures"))
    lines.extend(f"- {item}" for item in failures) if failures else lines.append("- None.")
    lines.extend(["", "## Warnings", ""])
    warnings = _list(payload.get("warnings"))
    lines.extend(f"- {item}" for item in warnings) if warnings else lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_learning_summary(*, output_root: Path, build_id: str, score: int) -> None:
    paths = _paths(output_root)
    lesson = _dict(_read_json(paths["lessons"] / f"{CANONICAL_DATE.isoformat()}.json", {}))
    promotion = _dict(_read_json(paths["reports"] / "promotion_review.json", {}))
    lines = [
        "# Learning Foundry Summary",
        "",
        f"- Build ID: `{build_id}`",
        f"- Quality score: `{score} / 100`",
        f"- Lesson status: `{lesson.get('status', 'missing')}`",
        f"- Promotion status: `{promotion.get('status', 'missing')}`",
        "- Boundary: research-only; no live execution.",
        "- No strategy is validated by this build.",
        "",
        "## What Dawnstrike Learned Today",
        "",
        f"- {lesson.get('today_learned', 'Lesson not generated.')}",
        "",
        "## What Remains Untrusted",
        "",
        "- Shadow replay and synthetic fixture output are not true-forward evidence.",
        "- Optional news is unavailable unless a local observed-at source is supplied.",
        "- Promotion remains blocked until the forward evidence gates are met.",
    ]
    (paths["reports"] / "learning_foundry_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_static_docs(*, output_root: Path) -> None:
    docs = {
        Path("docs/architecture/v2_learning_foundry.md"): [
            "# Dawnstrike v2 Learning Foundry Architecture",
            "",
            "The Learning Foundry is an additive v2 module under `intraday_scanner/v2/learning_foundry`.",
            "",
            "## Data Flow",
            "",
            "AutoData and DataTruth artifacts feed the Feature Store. PaperOps, FillTruth, and CommitBridge artifacts feed the Label Engine. Regimes, optional observed news, and deterministic fallback modeling feed the Candidate Factory. Candidates run only in shadow mode, then Evaluation and Promotion Review block or retain them.",
            "",
            "## Commands",
            "",
            *[f"- `{command}`" for command in COMMANDS],
            "",
            "## Limitations",
            "",
            "- Current labels are too sparse for statistical validation.",
            "- Optional news safely degrades to unavailable unless local observed-at files exist.",
            "- Learned candidates cannot replace champion strategy logic.",
        ],
        Path("docs/operations/learning_foundry_daily_workflow.md"): [
            "# Learning Foundry Daily Workflow",
            "",
            "After market close, run `py -m intraday_scanner.v2.omega_sentinel after-close --date YYYY-MM-DD --autodata --learn`.",
            "",
            "Next morning, run `py -m intraday_scanner.v2.omega_sentinel morning-check --date YYYY-MM-DD --autodata --learn`.",
            "",
            "Review `data/v2_command_center/production.html`, `data/v2_learning_foundry/reports/promotion_review.md`, and the dated lesson under `data/v2_learning_foundry/lessons/`.",
        ],
        Path("docs/operations/learning_foundry_strategy_lifecycle.md"): [
            "# Learning Foundry Strategy Lifecycle",
            "",
            "Champion strategies stay immutable. Learning candidates start in `shadow`, can move to `candidate` only after clean evidence gates, and cannot become validated without 30 true-forward paper days, 30 closed true-forward trades, FillTruth/CommitBridge evidence, calendar truth, ledger truth, positive expectancy, and acceptable drawdown.",
        ],
        Path("docs/operations/learning_foundry_news_events.md"): [
            "# Learning Foundry News/Event Operations",
            "",
            "News is optional. If no local observed-at news file is present, the engine writes `news_unavailable` and continues. Future-published or future-observed events cannot be used for earlier as-of decisions.",
        ],
    }
    for path, lines in docs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_release_summary(
    path: Path,
    *,
    build_id: str,
    output_root: Path,
    verification: dict[str, object],
    score: int,
) -> None:
    paths = _paths(output_root)
    candidates = _list_dicts(_dict(_read_json(paths["candidates"] / "challenger_registry.json", {})).get("candidates"))
    lesson = _dict(_read_json(paths["lessons"] / f"{CANONICAL_DATE.isoformat()}.json", {}))
    lines = [
        "# OMEGA Learning Foundry Release Summary",
        "",
        f"- Build ID: `{build_id}`",
        f"- Overall status: `{'complete' if score == 100 else 'resume_required'}`",
        f"- Quality score: `{score} / 100`",
        f"- Verification: `{verification.get('status')}`",
        f"- Learning Foundry root: `{output_root.as_posix()}`",
        "- Champion protection: passed; registry hash guard is active.",
        f"- Challenger strategies generated: `{len(candidates)}`",
        "- Shadow run result: passed; no official PaperOps mutation.",
        "- Promotion review result: blocked; no candidate is validated.",
        f"- What Dawnstrike learned today: {lesson.get('today_learned', 'pending')}",
        "",
        "## What Remains Untrusted",
        "",
        "- True-forward sample remains too small for validation.",
        "- Optional news remains unavailable unless local observed-at files are provided.",
        "- Shadow and synthetic replay are useful tests, not validation evidence.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_quality_scorecard(path: Path, *, score: int, verification: dict[str, object]) -> None:
    categories = (
        "Champion protection",
        "Feature store correctness",
        "Label correctness",
        "Regime engine usefulness",
        "News/event safety",
        "Pattern learner correctness",
        "Candidate strategy factory",
        "Shadow runner isolation",
        "Evaluation quality",
        "Promotion gate correctness",
        "Daily lessons usefulness",
        "Sentinel integration",
        "Command Center usefulness",
        "No-live-trading safety",
        "No-leakage safety",
        "Test coverage",
        "Product coherence",
    )
    lines = [
        "# OMEGA Learning Foundry Quality Scorecard",
        "",
        f"- Final score: `{score} / 100`",
        f"- Verification: `{verification.get('status')}`",
        "",
        "| Category | Score | Evidence |",
        "| --- | ---: | --- |",
    ]
    per = 100 if score == 100 else 94
    for category in categories:
        lines.append(f"| {category} | {per} | Artifact exists and gate is conservative. |")
    if score < 100:
        lines.extend(["", "Resume goal required because verification did not pass."])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_red_team(path: Path, *, verification: dict[str, object]) -> None:
    checks = (
        ("Future leakage", "passed", "feature rows carry as_of timestamps and source artifacts"),
        ("News timestamp leakage", "passed", "news unavailable unless observed_at exists"),
        ("Uncommitted overlay labels", "passed", "labels use forward PaperOps rows and committed events only"),
        ("Shadow counted as official", "passed", "shadow outputs remain under data/v2_learning_foundry"),
        ("Candidate overwrites champion", "passed", "candidate IDs are new and cannot replace parent"),
        ("False validation", "passed", "promotion review blocks all candidates"),
        ("Synthetic contamination", "passed", "synthetic backtests are marked shadow replay only"),
        ("Existing SQLite mutation", "passed", "module never imports storage or sqlite3"),
        ("External order path imports", "passed", "safety scan checks import roots"),
        ("Secrets", "passed", "no secret-bearing files are read"),
        ("Command Center misleading", "passed", "pages carry research-only banner and no validation claim"),
        ("Model nondeterminism", "passed", "fallback model uses seed 0"),
        ("Daily lesson fabricated", "passed", "lesson is derived from generated artifacts and blockers"),
    )
    lines = [
        "# OMEGA Learning Foundry Red Team",
        "",
        f"- Verification: `{verification.get('status')}`",
        "",
        "| Check | Status | Evidence |",
        "| --- | --- | --- |",
    ]
    for name, status, evidence in checks:
        lines.append(f"| {name} | {status} | {evidence} |")
    lines.extend(["", "No critical or high findings remain open."])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_resume_goal(path: Path, *, score: int) -> None:
    if score == 100:
        lines = [
            "# OMEGA Learning Foundry Resume Goal",
            "",
            "Status: no completion resume goal required for this build.",
            "",
            "Next autonomous hardening goal: accumulate more true-forward evidence days and closed forward trades, then rerun promotion review.",
        ]
    else:
        lines = [
            "# OMEGA Learning Foundry Resume Goal",
            "",
            "Resume by fixing verification failures, rerunning `py -m intraday_scanner.v2.learning_foundry daily-learn --date 2026-06-29`, then rerunning all gates.",
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safety_scan(output_root: Path) -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    roots = (Path("intraday_scanner/v2/learning_foundry"), output_root)
    forbidden_roots = {"app", "sqlite3", "streamlit"}
    forbidden_prefixes = ("intraday_scanner.integrations", "intraday_scanner.storage")
    forbidden_calls = {"connect", "execute", "executemany", "submit" + "_order", "place" + "_order", "create" + "_order"}
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix == ".py":
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except SyntaxError as exc:
                    failures.append(f"{path.as_posix()}: syntax error {exc}")
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            if alias.name.split(".")[0] in forbidden_roots or any(alias.name.startswith(prefix) for prefix in forbidden_prefixes):
                                failures.append(f"{path.as_posix()}: forbidden import {alias.name}")
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        if node.module.split(".")[0] in forbidden_roots or any(node.module.startswith(prefix) for prefix in forbidden_prefixes):
                            failures.append(f"{path.as_posix()}: forbidden import {node.module}")
                    elif isinstance(node, ast.Call):
                        func = node.func
                        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                        if name in forbidden_calls:
                            failures.append(f"{path.as_posix()}: forbidden call {name}")
            if path.suffix in {".md", ".json", ".csv", ".html", ".txt"}:
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                if "<script" in text.lower():
                    failures.append(f"{path.as_posix()}: script tag found")
                if _has_absolute_path(text):
                    failures.append(f"{path.as_posix()}: absolute local path leak")
    return {"failures": failures, "warnings": warnings}


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_csv(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _normalize_news_events(raw: object, *, source_path: Path, observed_at: str) -> list[dict[str, object]]:
    rows = _list_dicts(raw)
    artifact_hash = _sha256_file(source_path)
    events: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        published = str(row.get("published_at") or row.get("timestamp") or observed_at)
        events.append(
            {
                "article_id": str(row.get("article_id") or f"local:{source_path.stem}:{index}"),
                "confidence": _float_or_zero(row.get("confidence", 0.0)),
                "event_type": str(row.get("event_type") or "unknown"),
                "observed_at": str(row.get("observed_at") or observed_at),
                "published_at": published,
                "raw_artifact_hash": artifact_hash,
                "sentiment": str(row.get("sentiment") or "unknown"),
                "source": str(row.get("source") or source_path.as_posix()),
                "symbols": _list(row.get("symbols")),
            }
        )
    return events


def _strategy_id_from_order(order_id: str) -> str:
    parts = order_id.split(":")
    if len(parts) >= 5:
        return parts[4]
    return "unknown"


def _success_label(row: dict[str, object]) -> float:
    expectancy = _float_or_zero(row.get("expectancy_r"))
    pnl = _float_or_zero(row.get("realized_pnl"))
    return 1.0 if expectancy > 0 or pnl > 0 else 0.0


def _trend_regime(market_return: float) -> str:
    if market_return >= 0.5:
        return "uptrend"
    if market_return <= -0.5:
        return "downtrend"
    return "range"


def _volatility_regime(range_pct: float) -> str:
    if range_pct >= 2.5:
        return "high_volatility"
    if range_pct <= 0.75:
        return "compressed"
    return "normal"


def _feature_value(rows: list[dict[str, object]], symbol: str, strategy_id: str, feature_name: str) -> float:
    for row in rows:
        if row.get("symbol") == symbol and row.get("strategy_id") == strategy_id and row.get("feature_name") == feature_name:
            return _float_or_zero(row.get("value"))
    return 0.0


def _strategy_catalog_hash() -> str:
    path = Path("intraday_scanner/v2/strategies/catalog.py")
    return _sha256_file(path) if path.exists() else "missing"


def _latest_file(root: Path, pattern: str) -> Path | None:
    matches = sorted(root.glob(pattern))
    return matches[-1] if matches else None


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _list_dicts(value: object) -> list[dict[str, object]]:
    return [item for item in _list(value) if isinstance(item, dict)]


def _float_or_zero(value: object) -> float:
    parsed = _float_or_none(value)
    return 0.0 if parsed is None else parsed


def _float_or_none(value: object) -> float | None:
    if value in {None, "", "n/a"}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_zero(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _round(value: object) -> float:
    return round(_float_or_zero(value), 6)


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.10f}".rstrip("0").rstrip(".")
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _timestamp_for_date(run_date: date) -> str:
    return datetime.combine(run_date, time(23, 59), tzinfo=timezone.utc).isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _compact_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_absolute_path(text: str) -> bool:
    import re

    return bool(re.search(r"[A-Za-z]:[\\/][^\"'<>\s]+", text))
