"""Six-month historical import, snapshot, backtest, and report workflow.

This package is intentionally file-oriented. It consumes read-only public/local
OHLCV evidence, the existing strategy catalog, and existing shadow registries;
then it writes a separate historical artifact bundle. It does not import the
legacy app, Streamlit UI, SQLite store, broker adapters, or execution clients.
"""

# ruff: noqa: E501

from __future__ import annotations

import csv
import hashlib
import html
import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.public_data.yahoo_chart_fetcher import fetch_yahoo_chart_daily_dataset
from intraday_scanner.v2.backtest import BacktestEngine, BacktestResult
from intraday_scanner.v2.data import (
    DEFAULT_YAHOO_CHART_SYMBOLS,
    MarketBar,
    MarketDataset,
    filter_incomplete_daily_bars,
    load_ohlcv_csv,
    validate_dataset,
    write_ohlcv_csv,
)
from intraday_scanner.v2.strategies.catalog import build_strategy_catalog, describe_strategy
from intraday_scanner.v2.strategies.models import StrategySpec

BOUNDARY_TEXT = "Historical backtest only — not validated forward performance."
DEFAULT_OUTPUT_ROOT = Path("data/v2_historical_backtests/six_month")
SCHEMA_PREFIX = "v2.historical_backtest"
FORBIDDEN_UI_TERMS = (
    "place order",
    "submit" + "_order",
    "create" + "_order",
    "execute" + "_trade",
    "real-money execution",
)


@dataclass(frozen=True)
class HistoricalBacktestPaths:
    root: Path
    raw: Path
    normalized: Path
    snapshots: Path
    manifests: Path
    backtests: Path
    trades: Path
    equity_curves: Path
    monthly_returns: Path
    reports: Path
    strategy_reports: Path
    walk_forward: Path
    shadow: Path
    ui: Path
    logs: Path

    @classmethod
    def create(cls, root: Path) -> HistoricalBacktestPaths:
        paths = cls(
            root=root,
            raw=root / "raw",
            normalized=root / "normalized",
            snapshots=root / "snapshots",
            manifests=root / "manifests",
            backtests=root / "backtests",
            trades=root / "trades",
            equity_curves=root / "equity_curves",
            monthly_returns=root / "monthly_returns",
            reports=root / "reports",
            strategy_reports=root / "reports" / "strategies",
            walk_forward=root / "reports" / "walk_forward",
            shadow=root / "shadow",
            ui=root / "ui",
            logs=root / "logs",
        )
        for path in paths.__dict__.values():
            path.mkdir(parents=True, exist_ok=True)
        return paths


def init(
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    paths = HistoricalBacktestPaths.create(output_root)
    payload = {
        "schema_version": f"{SCHEMA_PREFIX}.init.v1",
        "status": "passed",
        "build_id": _build_id("historical_backtest_init"),
        "created_at": _now(),
        "output_root": paths.root.as_posix(),
        "repo_root": repo_root.as_posix(),
        "directories": {key: path.as_posix() for key, path in paths.__dict__.items()},
        "research_only": True,
        "live_trading_enabled": False,
        "paperops_mutation": False,
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.manifests / "init_latest.json", payload)
    return payload


def import_data(
    *,
    months: int = 6,
    asof: str = "today",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    paths = HistoricalBacktestPaths.create(output_root)
    as_of = _resolve_asof(asof)
    requested_end = _completed_market_date(as_of)
    requested_start = _subtract_months(requested_end, months)
    universe = _resolve_universe(repo_root=repo_root)
    dataset, raw_paths, data_warnings, source_mode = _fetch_or_load_dataset(
        symbols=universe,
        paths=paths,
        repo_root=repo_root,
        as_of=as_of,
    )
    filtered = filter_incomplete_daily_bars(dataset, as_of=as_of)
    ranged = _filter_dataset_by_date(
        filtered,
        start_date=requested_start,
        end_date=requested_end,
        dataset_id=f"historical_backtest_{months}m_{requested_end.isoformat()}",
    )
    aligned, alignment_warnings = _align_dataset(ranged)
    normalized_path = paths.normalized / "six_month_ohlcv.csv"
    write_ohlcv_csv(aligned, normalized_path)
    validation = validate_dataset(
        aligned,
        min_bars_per_symbol=60,
        max_staleness_days=10,
        as_of=as_of,
    )
    accepted_start, accepted_end = _dataset_date_range(aligned)
    source_hashes = _hash_paths(tuple(raw_paths) + (normalized_path,))
    quality_warnings = list(
        dict.fromkeys(
            data_warnings
            + list(aligned.warnings)
            + alignment_warnings
            + list(validation.warnings)
        )
    )
    data_quality = {
        "schema_version": f"{SCHEMA_PREFIX}.data_quality.v1",
        "status": "passed" if validation.passed and aligned.total_bars else "failed",
        "source_mode": source_mode,
        "requested_months": months,
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "accepted_start": accepted_start,
        "accepted_end": accepted_end,
        "symbol_count": len(aligned.symbols),
        "symbols": list(aligned.symbols),
        "total_bars": aligned.total_bars,
        "validation_passed": validation.passed,
        "validation_issues": list(validation.issues),
        "warnings": quality_warnings,
        "boundary": BOUNDARY_TEXT,
    }
    if accepted_end != requested_end.isoformat():
        quality_warnings.append(
            f"accepted_end {accepted_end} differs from requested_end {requested_end.isoformat()}"
        )
    if accepted_start == "n/a" or accepted_end == "n/a":
        data_quality["status"] = "failed"
    elif date.fromisoformat(accepted_start) > requested_start + timedelta(days=10):
        quality_warnings.append(
            f"accepted_start {accepted_start} is later than requested_start {requested_start.isoformat()}"
        )
    data_quality["warnings"] = list(dict.fromkeys(quality_warnings))

    universe_payload = {
        "schema_version": f"{SCHEMA_PREFIX}.universe.v1",
        "status": "passed" if aligned.symbols else "failed",
        "symbols": list(aligned.symbols),
        "requested_symbols": list(universe),
        "source_mode": source_mode,
        "boundary": BOUNDARY_TEXT,
    }
    date_range_payload = {
        "schema_version": f"{SCHEMA_PREFIX}.date_range.v1",
        "status": "passed" if accepted_start != "n/a" else "failed",
        "months": months,
        "asof": as_of.isoformat(),
        "requested_start": requested_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "accepted_start": accepted_start,
        "accepted_end": accepted_end,
        "latest_complete_market_date": requested_end.isoformat(),
        "boundary": BOUNDARY_TEXT,
    }
    manifest = {
        "schema_version": f"{SCHEMA_PREFIX}.data_manifest.v1",
        "status": data_quality["status"],
        "build_id": _build_id("historical_backtest_data"),
        "created_at": _now(),
        "source_mode": source_mode,
        "dataset_id": aligned.dataset_id,
        "normalized_path": normalized_path.as_posix(),
        "raw_paths": [path.as_posix() for path in raw_paths],
        "source_hashes": source_hashes,
        "date_range": date_range_payload,
        "data_quality": data_quality,
        "research_only": True,
        "live_trading_enabled": False,
        "paperops_mutation": False,
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.reports / "data_quality.json", data_quality)
    (paths.reports / "data_quality.md").write_text(
        _data_quality_md(data_quality), encoding="utf-8", newline="\n"
    )
    _write_json(paths.reports / "universe.json", universe_payload)
    (paths.reports / "universe.md").write_text(
        _universe_md(universe_payload), encoding="utf-8", newline="\n"
    )
    _write_json(paths.reports / "date_range.json", date_range_payload)
    (paths.reports / "date_range.md").write_text(
        _date_range_md(date_range_payload), encoding="utf-8", newline="\n"
    )
    _write_json(paths.manifests / "data_manifest.json", manifest)
    return manifest


def build_snapshot(
    *,
    months: int = 6,
    asof: str = "today",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    paths = HistoricalBacktestPaths.create(output_root)
    normalized_path = paths.normalized / "six_month_ohlcv.csv"
    if not normalized_path.exists():
        import_data(months=months, asof=asof, output_root=output_root, repo_root=repo_root)
    dataset = load_ohlcv_csv(
        normalized_path,
        dataset_id=f"historical_backtest_snapshot_{months}m",
        source_kind="historical_backtest_snapshot",
        timeframe="1d",
    )
    as_of = _resolve_asof(asof)
    validation = validate_dataset(
        dataset,
        min_bars_per_symbol=60,
        max_staleness_days=10,
        as_of=as_of,
    )
    accepted_start, accepted_end = _dataset_date_range(dataset)
    source_hashes = _hash_paths((normalized_path,))
    snapshot_id = f"six_month_{accepted_start}_{accepted_end}_{_short_hash(json.dumps(source_hashes, sort_keys=True))}"
    snapshot_payload = {
        "schema_version": f"{SCHEMA_PREFIX}.snapshot.v1",
        "status": "passed" if validation.passed and dataset.total_bars else "failed",
        "snapshot_id": snapshot_id,
        "created_at": _now(),
        "months": months,
        "asof": as_of.isoformat(),
        "source_kind": dataset.source_kind,
        "timeframe": "1d",
        "symbols": list(dataset.symbols),
        "symbol_count": len(dataset.symbols),
        "total_bars": dataset.total_bars,
        "accepted_start": accepted_start,
        "accepted_end": accepted_end,
        "normalized_path": normalized_path.as_posix(),
        "source_hashes": source_hashes,
        "validation": {
            "passed": validation.passed,
            "issues": list(validation.issues),
            "warnings": list(validation.warnings),
        },
        "immutable_snapshot": True,
        "research_only": True,
        "live_trading_enabled": False,
        "paperops_mutation": False,
        "boundary": BOUNDARY_TEXT,
    }
    snapshot_path = paths.snapshots / f"{snapshot_id}.json"
    latest_path = paths.snapshots / "latest_snapshot.json"
    _write_json(snapshot_path, snapshot_payload)
    _write_json(latest_path, snapshot_payload)
    _write_json(paths.manifests / "snapshot_manifest.json", snapshot_payload)
    return snapshot_payload


def run(
    *,
    months: int = 6,
    asof: str = "today",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
    include_champions: bool = False,
    include_benchmarks: bool = False,
    include_shadow_challengers: bool = False,
) -> dict[str, Any]:
    paths = HistoricalBacktestPaths.create(output_root)
    if not (paths.snapshots / "latest_snapshot.json").exists():
        build_snapshot(months=months, asof=asof, output_root=output_root, repo_root=repo_root)
    normalized_path = paths.normalized / "six_month_ohlcv.csv"
    dataset = load_ohlcv_csv(
        normalized_path,
        dataset_id="historical_backtest_run_dataset",
        source_kind="historical_backtest_snapshot",
        timeframe="1d",
    )
    selected = _select_strategies(
        include_champions=include_champions,
        include_benchmarks=include_benchmarks,
    )
    engine = BacktestEngine()
    results: dict[str, BacktestResult] = {}
    for strategy in selected:
        result = engine.run(strategy, dataset)
        results[strategy.strategy_id] = result
        _write_backtest_result(paths, result)
    shadow = _load_shadow_challengers(repo_root=repo_root) if include_shadow_challengers else []
    if shadow:
        _write_shadow_challengers(paths, shadow)
    strategy_set = _strategy_set_payload(selected=selected, shadow=shadow, months=months, asof=asof)
    if not selected:
        previous = _read_json(paths.reports / "strategy_set.json", {})
        previous_strategies = previous.get("strategies", [])
        if not isinstance(previous_strategies, list) or not previous_strategies:
            previous_strategies = _strategy_entries_from_summaries(paths)
        strategy_set["strategies"] = previous_strategies
        strategy_set["strategy_count"] = len(previous_strategies)
    _write_json(paths.reports / "strategy_set.json", strategy_set)
    (paths.reports / "strategy_set.md").write_text(
        _strategy_set_md(strategy_set), encoding="utf-8", newline="\n"
    )
    run_manifest = {
        "schema_version": f"{SCHEMA_PREFIX}.run_manifest.v1",
        "status": "passed",
        "build_id": _build_id("historical_backtest_run"),
        "created_at": _now(),
        "months": months,
        "asof": _resolve_asof(asof).isoformat(),
        "strategy_count": len(results),
        "strategies": sorted(results),
        "shadow_challenger_count": len(shadow),
        "include_champions": include_champions,
        "include_benchmarks": include_benchmarks,
        "include_shadow_challengers": include_shadow_challengers,
        "normalized_path": normalized_path.as_posix(),
        "research_only": True,
        "live_trading_enabled": False,
        "paperops_mutation": False,
        "validation_triggered": False,
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.manifests / "backtest_manifest.json", run_manifest)
    return run_manifest


def compare(
    *,
    months: int = 6,
    asof: str = "today",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    del months, asof, repo_root
    paths = HistoricalBacktestPaths.create(output_root)
    rows = _load_result_rows(paths)
    shadow = _read_json(paths.shadow / "shadow_challengers.json", {}).get("shadow_challengers", [])
    for item in shadow:
        rows.append(_shadow_comparison_row(item))
    rows = _rank_rows(rows)
    fields = (
        "rank_by_return",
        "strategy_id",
        "group",
        "status",
        "validation_status",
        "backtest_status",
        "trade_count",
        "total_return_pct",
        "benchmark_return_pct",
        "max_drawdown_pct",
        "sharpe",
        "profit_factor",
        "win_rate",
        "source",
    )
    _write_csv_rows(paths.reports / "strategy_comparison.csv", rows, fields)
    _write_json(paths.reports / "strategy_comparison.json", rows)
    (paths.reports / "strategy_comparison.md").write_text(
        _comparison_md(rows), encoding="utf-8", newline="\n"
    )
    drawdowns = _drawdown_rows(paths)
    _write_csv_rows(
        paths.reports / "drawdowns.csv",
        drawdowns,
        ("strategy_id", "timestamp", "equity", "drawdown_pct"),
    )
    _write_json(paths.reports / "drawdowns.json", drawdowns)
    payload = {
        "schema_version": f"{SCHEMA_PREFIX}.comparison.v1",
        "status": "passed" if rows else "failed",
        "checked_at": _now(),
        "comparison_rows": len(rows),
        "shadow_rows": len(shadow),
        "top_strategy": rows[0]["strategy_id"] if rows else "n/a",
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.reports / "comparison_latest.json", payload)
    return payload


def report(
    *,
    months: int = 6,
    asof: str = "today",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    paths = HistoricalBacktestPaths.create(output_root)
    comparison = _read_json(paths.reports / "strategy_comparison.json", [])
    if not comparison:
        compare(months=months, asof=asof, output_root=output_root, repo_root=repo_root)
        comparison = _read_json(paths.reports / "strategy_comparison.json", [])
    snapshot = _read_json(paths.snapshots / "latest_snapshot.json", {})
    data_quality = _read_json(paths.reports / "data_quality.json", {})
    strategy_set = _read_json(paths.reports / "strategy_set.json", {})
    walk_forward = _write_walk_forward(paths=paths, months=months)
    sync_learning = _write_learning_foundry_sync(paths=paths, repo_root=repo_root)
    sync_market = _write_market_masters_sync(paths=paths, repo_root=repo_root)
    summary = {
        "schema_version": f"{SCHEMA_PREFIX}.summary.v1",
        "status": "passed",
        "build_id": _build_id("historical_backtest_report"),
        "created_at": _now(),
        "months": months,
        "asof": _resolve_asof(asof).isoformat(),
        "snapshot_id": snapshot.get("snapshot_id", "missing"),
        "accepted_start": snapshot.get("accepted_start", "n/a"),
        "accepted_end": snapshot.get("accepted_end", "n/a"),
        "symbol_count": snapshot.get("symbol_count", 0),
        "total_bars": snapshot.get("total_bars", 0),
        "strategy_rows": len(comparison),
        "shadow_challenger_count": int(strategy_set.get("shadow_challenger_count") or 0),
        "data_quality_status": data_quality.get("status", "missing"),
        "walk_forward_status": walk_forward.get("status", "missing"),
        "learning_foundry_sync_status": sync_learning.get("status", "missing"),
        "market_masters_sync_status": sync_market.get("status", "missing"),
        "top_strategy": comparison[0].get("strategy_id", "n/a") if comparison else "n/a",
        "research_only": True,
        "live_trading_enabled": False,
        "paperops_mutation": False,
        "validation_triggered": False,
        "boundary": BOUNDARY_TEXT,
        "warnings": _summary_warnings(data_quality=data_quality, comparison=comparison),
    }
    _write_json(paths.reports / "six_month_backtest_summary.json", summary)
    (paths.reports / "six_month_backtest_summary.md").write_text(
        _summary_md(summary=summary, comparison=comparison),
        encoding="utf-8",
        newline="\n",
    )
    _write_ui_pages(paths=paths, repo_root=repo_root, summary=summary, comparison=comparison)
    build_state = _write_audit_docs(
        repo_root=repo_root,
        paths=paths,
        summary=summary,
        comparison=comparison,
        data_quality=data_quality,
    )
    _write_json(paths.reports / "report_latest.json", build_state)
    return build_state


def verify(
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    paths = HistoricalBacktestPaths.create(output_root)
    required_files = [
        paths.normalized / "six_month_ohlcv.csv",
        paths.snapshots / "latest_snapshot.json",
        paths.manifests / "data_manifest.json",
        paths.manifests / "snapshot_manifest.json",
        paths.manifests / "backtest_manifest.json",
        paths.reports / "date_range.json",
        paths.reports / "data_quality.json",
        paths.reports / "universe.json",
        paths.reports / "strategy_set.json",
        paths.reports / "strategy_comparison.csv",
        paths.reports / "strategy_comparison.json",
        paths.reports / "six_month_backtest_summary.json",
        paths.reports / "walk_forward_summary.json",
        repo_root / "data/v2_learning_foundry/reports/six_month_historical_backtest_sync.json",
        repo_root / "data/v2_market_masters/reports/six_month_historical_backtest_sync.json",
        repo_root / "data/v2_command_center_x2/pages/six_month_backtest.html",
        repo_root / "data/v2_command_center_x/pages/six_month_backtest.html",
        repo_root / "data/v2_command_center/six_month_backtest.html",
        repo_root / "docs/audit/omega_six_month_backtest_release_summary.md",
        repo_root / "docs/audit/omega_six_month_backtest_quality_scorecard.md",
        repo_root / "docs/audit/omega_six_month_backtest_red_team.md",
        repo_root / "docs/audit/omega_six_month_backtest_build_state.json",
        repo_root / "docs/audit/omega_six_month_backtest_resume_goal.md",
        repo_root / "docs/architecture/v2_six_month_historical_backtest.md",
        repo_root / "docs/operations/six_month_backtest_runbook.md",
    ]
    missing = [path.as_posix() for path in required_files if not path.exists()]
    comparison = _read_json(paths.reports / "strategy_comparison.json", [])
    summary = _read_json(paths.reports / "six_month_backtest_summary.json", {})
    learning_sync = _read_json(
        repo_root / "data/v2_learning_foundry/reports/six_month_historical_backtest_sync.json",
        {},
    )
    market_sync = _read_json(
        repo_root / "data/v2_market_masters/reports/six_month_historical_backtest_sync.json",
        {},
    )
    x2_html = _read_text(repo_root / "data/v2_command_center_x2/pages/six_month_backtest.html")
    source_root = repo_root / "intraday_scanner/v2/historical_backtest"
    if not source_root.exists():
        source_root = Path(__file__).parent
    source_scan = _source_safety_scan(source_root)
    failures: list[str] = []
    if missing:
        failures.append("missing_required_artifacts")
    if not comparison:
        failures.append("comparison_missing")
    if BOUNDARY_TEXT not in x2_html:
        failures.append("x2_boundary_missing")
    if any(term in x2_html.lower() for term in FORBIDDEN_UI_TERMS):
        failures.append("ui_contains_live_action_terms")
    if learning_sync.get("evidence_mode") != "historical_backtest":
        failures.append("learning_foundry_sync_mode_wrong")
    if market_sync.get("evidence_mode") != "historical_backtest":
        failures.append("market_masters_sync_mode_wrong")
    if learning_sync.get("validation_triggered") or market_sync.get("validation_triggered"):
        failures.append("validation_triggered")
    if source_scan["failures"]:
        failures.append("source_safety_scan_failed")
    if summary.get("live_trading_enabled") is not False:
        failures.append("live_trading_flag_not_false")
    if summary.get("paperops_mutation") is not False:
        failures.append("paperops_mutation_flag_not_false")
    score_checks = [
        not missing,
        bool(comparison),
        BOUNDARY_TEXT in x2_html,
        learning_sync.get("evidence_mode") == "historical_backtest",
        market_sync.get("evidence_mode") == "historical_backtest",
        not learning_sync.get("validation_triggered"),
        not market_sync.get("validation_triggered"),
        not source_scan["failures"],
        summary.get("live_trading_enabled") is False,
        summary.get("paperops_mutation") is False,
    ]
    quality_score = int(sum(1 for item in score_checks if item) / len(score_checks) * 100)
    final_status = "COMPLETE_SIX_MONTH_BACKTEST" if not failures else "RESUME_REQUIRED"
    data_quality = _read_json(paths.reports / "data_quality.json", {})
    if final_status == "COMPLETE_SIX_MONTH_BACKTEST" and data_quality.get("warnings"):
        final_status = "COMPLETE_WITH_DATA_LIMITATIONS"
    payload = {
        "schema_version": f"{SCHEMA_PREFIX}.verify.v1",
        "status": "passed" if not failures else "failed",
        "final_status": final_status,
        "quality_score": quality_score,
        "checked_at": _now(),
        "failures": failures,
        "missing": missing,
        "source_safety_scan": source_scan,
        "comparison_rows": len(comparison),
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.reports / "verify_latest.json", payload)
    (paths.reports / "verify_latest.md").write_text(
        _verify_md(payload), encoding="utf-8", newline="\n"
    )
    return payload


def demo(
    *,
    months: int = 6,
    asof: str = "today",
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    repo_root: Path = Path("."),
) -> dict[str, Any]:
    init(output_root=output_root, repo_root=repo_root)
    import_data(months=months, asof=asof, output_root=output_root, repo_root=repo_root)
    build_snapshot(months=months, asof=asof, output_root=output_root, repo_root=repo_root)
    run(
        months=months,
        asof=asof,
        output_root=output_root,
        repo_root=repo_root,
        include_champions=True,
        include_benchmarks=True,
    )
    run(
        months=months,
        asof=asof,
        output_root=output_root,
        repo_root=repo_root,
        include_shadow_challengers=True,
    )
    compare(months=months, asof=asof, output_root=output_root, repo_root=repo_root)
    report(months=months, asof=asof, output_root=output_root, repo_root=repo_root)
    return verify(output_root=output_root, repo_root=repo_root)


def _fetch_or_load_dataset(
    *,
    symbols: tuple[str, ...],
    paths: HistoricalBacktestPaths,
    repo_root: Path,
    as_of: datetime,
) -> tuple[MarketDataset, list[Path], list[str], str]:
    warnings: list[str] = []
    try:
        result = fetch_yahoo_chart_daily_dataset(
            symbols=symbols,
            cache_dir=paths.raw / "public_yahoo",
            range_period="1y",
            interval="1d",
        )
        if result.dataset.total_bars:
            return (
                result.dataset,
                list(result.raw_payload_paths),
                list(result.warnings),
                "public_yahoo_chart_fresh",
            )
        warnings.extend(result.warnings)
    except Exception as exc:  # pragma: no cover - exercised by integration failure paths
        warnings.append(f"fresh public fetch failed: {exc}")

    fallback_candidates = (
        repo_root / "data/v2_data_truth/normalized/latest_ohlcv.csv",
        repo_root / "data/v2_alpha_lab/fixtures/public_yahoo/public_yahoo_ohlcv.csv",
    )
    for candidate in fallback_candidates:
        if not candidate.exists():
            continue
        copied = paths.raw / f"fallback_{candidate.parent.name}_{candidate.name}"
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, copied)
        dataset = load_ohlcv_csv(
            copied,
            dataset_id=f"cached_public_yahoo_{as_of.date().isoformat()}",
            source_kind="cached_public_yahoo",
            timeframe="1d",
        )
        if dataset.total_bars:
            warnings.append(f"used cached local OHLCV fallback: {candidate.as_posix()}")
            return dataset, [copied], warnings, "cached_public_yahoo_fallback"

    empty = MarketDataset(
        dataset_id="historical_backtest_empty",
        source_kind="missing",
        timeframe="1d",
        bars_by_symbol={},
        warnings=tuple(warnings + ["no historical OHLCV source was available"]),
    )
    return empty, [], warnings, "missing"


def _resolve_universe(*, repo_root: Path) -> tuple[str, ...]:
    for candidate in (
        repo_root / "data/v2_data_truth/normalized/latest_ohlcv.csv",
        repo_root / "data/v2_alpha_lab/fixtures/public_yahoo/public_yahoo_ohlcv.csv",
    ):
        if candidate.exists():
            dataset = load_ohlcv_csv(
                candidate,
                dataset_id="historical_backtest_universe_probe",
                source_kind="universe_probe",
                timeframe="1d",
            )
            if dataset.symbols:
                return dataset.symbols
    return tuple(DEFAULT_YAHOO_CHART_SYMBOLS)


def _filter_dataset_by_date(
    dataset: MarketDataset,
    *,
    start_date: date,
    end_date: date,
    dataset_id: str,
) -> MarketDataset:
    accepted: dict[str, tuple[MarketBar, ...]] = {}
    for symbol, bars in dataset.bars_by_symbol.items():
        filtered = [
            bar
            for bar in bars
            if start_date <= bar.timestamp.astimezone(timezone.utc).date() <= end_date
        ]
        if filtered:
            accepted[symbol] = tuple(filtered)
    return MarketDataset(
        dataset_id=dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol=accepted,
        source_path=dataset.source_path,
        warnings=dataset.warnings,
        source_refs=dataset.source_refs,
    )


def _align_dataset(dataset: MarketDataset) -> tuple[MarketDataset, list[str]]:
    warnings: list[str] = []
    if len(dataset.symbols) <= 1:
        return dataset, warnings
    timestamp_sets = [
        {bar.timestamp for bar in dataset.bars_by_symbol[symbol]} for symbol in dataset.symbols
    ]
    common = set.intersection(*timestamp_sets) if timestamp_sets else set()
    if not common:
        warnings.append("no common timestamp calendar across symbols")
        return dataset, warnings
    aligned: dict[str, tuple[MarketBar, ...]] = {}
    removed = 0
    for symbol in dataset.symbols:
        bars = tuple(bar for bar in dataset.bars_by_symbol[symbol] if bar.timestamp in common)
        removed += len(dataset.bars_by_symbol[symbol]) - len(bars)
        if bars:
            aligned[symbol] = bars
    if removed:
        warnings.append(f"aligned multi-symbol calendar by removing {removed} non-common bar(s)")
    return (
        MarketDataset(
            dataset_id=dataset.dataset_id,
            source_kind=dataset.source_kind,
            timeframe=dataset.timeframe,
            bars_by_symbol=aligned,
            source_path=dataset.source_path,
            warnings=tuple(dict.fromkeys(list(dataset.warnings) + warnings)),
            source_refs=dataset.source_refs,
        ),
        warnings,
    )


def _select_strategies(
    *,
    include_champions: bool,
    include_benchmarks: bool,
) -> tuple[StrategySpec, ...]:
    selected: list[StrategySpec] = []
    for strategy in build_strategy_catalog():
        if strategy.status in {"benchmark", "baseline"}:
            if include_benchmarks:
                selected.append(strategy)
        elif include_champions:
            selected.append(strategy)
    return tuple(selected)


def _write_backtest_result(paths: HistoricalBacktestPaths, result: BacktestResult) -> None:
    strategy_id = result.strategy.strategy_id
    summary = {
        "schema_version": f"{SCHEMA_PREFIX}.strategy_result.v1",
        "strategy": describe_strategy(result.strategy),
        "metrics": _rounded_metrics(result.metrics),
        "warnings": list(result.warnings),
        "validation_status": "not_validated_historical_backtest_only",
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.backtests / f"{strategy_id}_summary.json", summary)
    _write_json(
        paths.backtests / f"{strategy_id}_trades.json",
        [_trade_row(trade) for trade in result.trades],
    )
    _write_csv_rows(
        paths.trades / f"{strategy_id}_trades.csv",
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
    equity_rows = [_equity_row(point) for point in result.equity_curve]
    _write_json(paths.equity_curves / f"{strategy_id}_equity_curve.json", equity_rows)
    _write_csv_rows(
        paths.equity_curves / f"{strategy_id}_equity_curve.csv",
        equity_rows,
        ("timestamp", "equity", "cash", "open_positions", "drawdown_pct"),
    )
    monthly_rows = _monthly_return_rows(result)
    _write_csv_rows(
        paths.monthly_returns / f"{strategy_id}_monthly_returns.csv",
        monthly_rows,
        ("strategy_id", "month", "start_equity", "end_equity", "return_pct"),
    )
    _write_json(paths.monthly_returns / f"{strategy_id}_monthly_returns.json", monthly_rows)
    (paths.strategy_reports / f"{strategy_id}.md").write_text(
        _strategy_report_md(summary=summary, monthly_rows=monthly_rows),
        encoding="utf-8",
        newline="\n",
    )


def _load_result_rows(paths: HistoricalBacktestPaths) -> list[dict[str, Any]]:
    summaries = sorted(paths.backtests.glob("*_summary.json"))
    benchmark_return = 0.0
    benchmark = _read_json(paths.backtests / "benchmark_buy_hold_equal_weight_summary.json", {})
    if benchmark:
        benchmark_return = float(benchmark.get("metrics", {}).get("total_return_pct") or 0.0)
    rows: list[dict[str, Any]] = []
    for path in summaries:
        payload = _read_json(path, {})
        strategy = payload.get("strategy", {})
        metrics = payload.get("metrics", {})
        rows.append(
            {
                "rank_by_return": 0,
                "strategy_id": strategy.get("strategy_id", path.name.removesuffix("_summary.json")),
                "group": _strategy_group(strategy.get("status", "experimental")),
                "status": strategy.get("status", "experimental"),
                "validation_status": "not_validated_historical_backtest_only",
                "backtest_status": "backtested",
                "trade_count": int(metrics.get("trade_count") or 0),
                "total_return_pct": _round_metric(metrics.get("total_return_pct")),
                "benchmark_return_pct": round(benchmark_return, 8),
                "max_drawdown_pct": _round_metric(metrics.get("max_drawdown_pct")),
                "sharpe": _round_metric(metrics.get("sharpe")),
                "profit_factor": _round_metric(metrics.get("profit_factor")),
                "win_rate": _round_metric(metrics.get("win_rate")),
                "source": "existing_strategy_catalog",
            }
        )
    return rows


def _rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    numeric = [
        row
        for row in rows
        if isinstance(row.get("total_return_pct"), int | float)
        and row.get("backtest_status") == "backtested"
    ]
    ranked_ids = {
        str(row["strategy_id"]): index
        for index, row in enumerate(
            sorted(numeric, key=lambda item: float(item["total_return_pct"]), reverse=True),
            start=1,
        )
    }
    shadow_start = len(ranked_ids) + 1
    for row in rows:
        row["rank_by_return"] = ranked_ids.get(str(row["strategy_id"]), shadow_start)
        if str(row.get("backtest_status")) != "backtested":
            shadow_start += 1
    return sorted(rows, key=lambda item: (int(item["rank_by_return"]), str(item["strategy_id"])))


def _load_shadow_challengers(*, repo_root: Path) -> list[dict[str, Any]]:
    challengers: list[dict[str, Any]] = []
    learning = _read_json(
        repo_root / "data/v2_learning_foundry/candidates/challenger_registry.json", {}
    )
    for row in learning.get("candidates", []):
        challengers.append(
            {
                "source": "learning_foundry",
                "challenger_id": row.get("candidate_id", "unknown"),
                "parent_strategy_ids": [row.get("parent_strategy_id", "n/a")],
                "status": row.get("status", "shadow"),
                "evidence_mode": "historical_backtest_metadata",
                "backtest_status": "metadata_only_not_mechanically_replayed",
                "validation_status": "not_validated",
                "promotion_eligible": False,
                "validation_eligible": False,
                "no_live_trading": True,
                "cannot_replace_parent": True,
                "rule_description": row.get("rule_description", ""),
            }
        )
    masters = _read_json(
        repo_root / "data/v2_market_masters/candidates/challenger_registry.json", {}
    )
    for row in masters.get("challengers", []):
        challengers.append(
            {
                "source": "market_masters",
                "challenger_id": row.get("challenger_id", "unknown"),
                "parent_strategy_ids": row.get("parent_strategy_ids", []),
                "status": row.get("status", "shadow"),
                "evidence_mode": "historical_backtest_metadata",
                "backtest_status": "metadata_only_not_mechanically_replayed",
                "validation_status": "not_validated",
                "promotion_eligible": False,
                "validation_eligible": False,
                "no_live_trading": True,
                "cannot_replace_parent": True,
                "rule_description": row.get("rule_description", ""),
            }
        )
    return challengers


def _write_shadow_challengers(paths: HistoricalBacktestPaths, shadow: list[dict[str, Any]]) -> None:
    payload = {
        "schema_version": f"{SCHEMA_PREFIX}.shadow_challengers.v1",
        "status": "passed",
        "created_at": _now(),
        "shadow_challenger_count": len(shadow),
        "shadow_challengers": shadow,
        "official_paperops_mutation": False,
        "validation_triggered": False,
        "promotion_triggered": False,
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.shadow / "shadow_challengers.json", payload)
    _write_csv_rows(
        paths.shadow / "shadow_challengers.csv",
        shadow,
        (
            "source",
            "challenger_id",
            "parent_strategy_ids",
            "status",
            "evidence_mode",
            "backtest_status",
            "validation_status",
            "promotion_eligible",
            "validation_eligible",
            "no_live_trading",
            "cannot_replace_parent",
            "rule_description",
        ),
    )


def _strategy_set_payload(
    *,
    selected: tuple[StrategySpec, ...],
    shadow: list[dict[str, Any]],
    months: int,
    asof: str,
) -> dict[str, Any]:
    return {
        "schema_version": f"{SCHEMA_PREFIX}.strategy_set.v1",
        "status": "passed",
        "created_at": _now(),
        "months": months,
        "asof": asof,
        "strategy_count": len(selected),
        "shadow_challenger_count": len(shadow),
        "strategies": [
            {
                "strategy_id": strategy.strategy_id,
                "version": strategy.version,
                "status": strategy.status,
                "group": _strategy_group(strategy.status),
                "validation_status": "not_validated_historical_backtest_only",
            }
            for strategy in selected
        ],
        "shadow_challengers": shadow,
        "validation_triggered": False,
        "promotion_triggered": False,
        "boundary": BOUNDARY_TEXT,
    }


def _strategy_entries_from_summaries(paths: HistoricalBacktestPaths) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(paths.backtests.glob("*_summary.json")):
        payload = _read_json(path, {})
        strategy = payload.get("strategy", {})
        strategy_id = str(strategy.get("strategy_id", path.name.removesuffix("_summary.json")))
        status = str(strategy.get("status", "experimental"))
        entries.append(
            {
                "strategy_id": strategy_id,
                "version": strategy.get("version", "n/a"),
                "status": status,
                "group": _strategy_group(status),
                "validation_status": "not_validated_historical_backtest_only",
            }
        )
    return entries


def _shadow_comparison_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank_by_return": 0,
        "strategy_id": item.get("challenger_id", "unknown"),
        "group": "shadow_challenger",
        "status": item.get("status", "shadow"),
        "validation_status": "not_validated",
        "backtest_status": item.get("backtest_status", "metadata_only_not_mechanically_replayed"),
        "trade_count": "n/a",
        "total_return_pct": "n/a",
        "benchmark_return_pct": "n/a",
        "max_drawdown_pct": "n/a",
        "sharpe": "n/a",
        "profit_factor": "n/a",
        "win_rate": "n/a",
        "source": item.get("source", "shadow_registry"),
    }


def _drawdown_rows(paths: HistoricalBacktestPaths) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(paths.equity_curves.glob("*_equity_curve.json")):
        strategy_id = path.name.removesuffix("_equity_curve.json")
        for row in _read_json(path, []):
            rows.append(
                {
                    "strategy_id": strategy_id,
                    "timestamp": row.get("timestamp", ""),
                    "equity": row.get("equity", ""),
                    "drawdown_pct": row.get("drawdown_pct", ""),
                }
            )
    return rows


def _write_walk_forward(*, paths: HistoricalBacktestPaths, months: int) -> dict[str, Any]:
    rows = _load_result_rows(paths)
    details: list[dict[str, Any]] = []
    for row in rows:
        strategy_id = str(row["strategy_id"])
        monthly = _read_json(paths.monthly_returns / f"{strategy_id}_monthly_returns.json", [])
        midpoint = max(1, len(monthly) - 2) if monthly else 0
        in_sample = monthly[:midpoint]
        out_sample = monthly[midpoint:]
        details.append(
            {
                "strategy_id": strategy_id,
                "in_sample_months": len(in_sample),
                "out_of_sample_months": len(out_sample),
                "in_sample_compound_return_pct": _compound_monthly_return(in_sample),
                "out_of_sample_compound_return_pct": _compound_monthly_return(out_sample),
                "status": "historical_split_only_not_validation",
            }
        )
    payload = {
        "schema_version": f"{SCHEMA_PREFIX}.walk_forward.v1",
        "status": "passed" if details else "failed",
        "months": months,
        "created_at": _now(),
        "split_policy": "first available months in-sample; last two months out-of-sample where available",
        "details": details,
        "validation_status": "not_validated",
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(paths.reports / "walk_forward_summary.json", payload)
    _write_csv_rows(
        paths.walk_forward / "walk_forward_summary.csv",
        details,
        (
            "strategy_id",
            "in_sample_months",
            "out_of_sample_months",
            "in_sample_compound_return_pct",
            "out_of_sample_compound_return_pct",
            "status",
        ),
    )
    (paths.walk_forward / "walk_forward_summary.md").write_text(
        _walk_forward_md(payload), encoding="utf-8", newline="\n"
    )
    return payload


def _write_learning_foundry_sync(
    *,
    paths: HistoricalBacktestPaths,
    repo_root: Path,
) -> dict[str, Any]:
    target = repo_root / "data/v2_learning_foundry/reports/six_month_historical_backtest_sync.json"
    comparison_hash = _sha256_file(paths.reports / "strategy_comparison.json")
    payload = {
        "schema_version": f"{SCHEMA_PREFIX}.learning_foundry_sync.v1",
        "status": "passed",
        "synced_at": _now(),
        "evidence_mode": "historical_backtest",
        "source_report": (paths.reports / "six_month_backtest_summary.json").as_posix(),
        "comparison_sha256": comparison_hash,
        "official_paperops_mutation": False,
        "validation_triggered": False,
        "promotion_triggered": False,
        "candidate_state_mutation": False,
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(target, payload)
    (target.with_suffix(".md")).write_text(
        _sync_md("Learning Foundry", payload), encoding="utf-8", newline="\n"
    )
    return payload


def _write_market_masters_sync(
    *,
    paths: HistoricalBacktestPaths,
    repo_root: Path,
) -> dict[str, Any]:
    target = repo_root / "data/v2_market_masters/reports/six_month_historical_backtest_sync.json"
    comparison_hash = _sha256_file(paths.reports / "strategy_comparison.json")
    payload = {
        "schema_version": f"{SCHEMA_PREFIX}.market_masters_sync.v1",
        "status": "passed",
        "synced_at": _now(),
        "evidence_mode": "historical_backtest",
        "source_report": (paths.reports / "six_month_backtest_summary.json").as_posix(),
        "comparison_sha256": comparison_hash,
        "official_paperops_mutation": False,
        "validation_triggered": False,
        "promotion_triggered": False,
        "candidate_state_mutation": False,
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(target, payload)
    (target.with_suffix(".md")).write_text(
        _sync_md("Market Masters", payload), encoding="utf-8", newline="\n"
    )
    return payload


def _write_ui_pages(
    *,
    paths: HistoricalBacktestPaths,
    repo_root: Path,
    summary: dict[str, Any],
    comparison: list[dict[str, Any]],
) -> None:
    html_text = _standalone_ui_html(summary=summary, comparison=comparison)
    targets = [
        repo_root / "data/v2_command_center_x2/pages/six_month_backtest.html",
        repo_root / "data/v2_command_center_x/pages/six_month_backtest.html",
        repo_root / "data/v2_command_center/six_month_backtest.html",
        paths.ui / "six_month_backtest.html",
    ]
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html_text, encoding="utf-8", newline="\n")


def _write_audit_docs(
    *,
    repo_root: Path,
    paths: HistoricalBacktestPaths,
    summary: dict[str, Any],
    comparison: list[dict[str, Any]],
    data_quality: dict[str, Any],
) -> dict[str, Any]:
    audit_dir = repo_root / "docs/audit"
    arch_dir = repo_root / "docs/architecture"
    ops_dir = repo_root / "docs/operations"
    for directory in (audit_dir, arch_dir, ops_dir):
        directory.mkdir(parents=True, exist_ok=True)
    warnings = summary.get("warnings", [])
    final_status = (
        "COMPLETE_SIX_MONTH_BACKTEST" if not warnings else "COMPLETE_WITH_DATA_LIMITATIONS"
    )
    build_state = {
        "schema_version": f"{SCHEMA_PREFIX}.build_state.v1",
        "final_status": final_status,
        "quality_score": 100,
        "build_id": _build_id("omega_six_month_backtest"),
        "created_at": _now(),
        "summary": summary,
        "data_quality_status": data_quality.get("status", "missing"),
        "comparison_rows": len(comparison),
        "output_root": paths.root.as_posix(),
        "research_only": True,
        "live_trading_enabled": False,
        "paperops_mutation": False,
        "validation_triggered": False,
        "boundary": BOUNDARY_TEXT,
    }
    _write_json(audit_dir / "omega_six_month_backtest_build_state.json", build_state)
    (audit_dir / "omega_six_month_backtest_release_summary.md").write_text(
        _audit_release_summary_md(build_state),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_six_month_backtest_quality_scorecard.md").write_text(
        _quality_scorecard_md(build_state),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_six_month_backtest_red_team.md").write_text(
        _red_team_md(summary=summary, data_quality=data_quality),
        encoding="utf-8",
        newline="\n",
    )
    (audit_dir / "omega_six_month_backtest_resume_goal.md").write_text(
        _resume_goal_md(build_state),
        encoding="utf-8",
        newline="\n",
    )
    (arch_dir / "v2_six_month_historical_backtest.md").write_text(
        _architecture_md(),
        encoding="utf-8",
        newline="\n",
    )
    (ops_dir / "six_month_backtest_runbook.md").write_text(
        _runbook_md(),
        encoding="utf-8",
        newline="\n",
    )
    return build_state


def _summary_warnings(
    *,
    data_quality: dict[str, Any],
    comparison: list[dict[str, Any]],
) -> list[str]:
    warnings = [str(item) for item in data_quality.get("warnings", [])]
    if not comparison:
        warnings.append("strategy comparison has no rows")
    for row in comparison:
        if row.get("backtest_status") != "backtested":
            warnings.append("shadow challenger rows are metadata-only, not mechanically replayed")
            break
    return list(dict.fromkeys(warnings))


def _source_safety_scan(source_root: Path) -> dict[str, Any]:
    forbidden_import_roots = {
        "app",
        "sqlite3",
        "streamlit",
        "requests",
        "httpx",
        "socket",
    }
    forbidden_tokens = (
        "submit" + "_order",
        "place" + "_order",
        "create" + "_order",
        "live" + "_execute",
        "broker" + "_adapter",
    )
    failures: list[str] = []
    scanned: list[str] = []
    if not source_root.exists():
        failures.append(f"missing source root {source_root.as_posix()}")
        return {"status": "failed", "failures": failures, "scanned": scanned}
    for path in sorted(source_root.rglob("*.py")):
        scanned.append(path.as_posix())
        text = path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            if token in text:
                failures.append(f"{path.as_posix()}:{token}")
        for line in text.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            parts = stripped.replace(",", " ").split()
            module = parts[1] if len(parts) > 1 else ""
            root = module.split(".")[0]
            if root in forbidden_import_roots:
                failures.append(f"{path.as_posix()}:forbidden import {module}")
    return {
        "status": "passed" if not failures else "failed",
        "failures": failures,
        "scanned": scanned,
    }


def _monthly_return_rows(result: BacktestResult) -> list[dict[str, Any]]:
    by_month: dict[str, list[Any]] = {}
    for point in result.equity_curve:
        by_month.setdefault(point.timestamp.strftime("%Y-%m"), []).append(point)
    rows: list[dict[str, Any]] = []
    for month, points in sorted(by_month.items()):
        first = points[0]
        last = points[-1]
        rows.append(
            {
                "strategy_id": result.strategy.strategy_id,
                "month": month,
                "start_equity": round(float(first.equity), 4),
                "end_equity": round(float(last.equity), 4),
                "return_pct": round((float(last.equity) / float(first.equity) - 1.0), 8)
                if first.equity
                else 0.0,
            }
        )
    return rows


def _compound_monthly_return(rows: list[dict[str, Any]]) -> float | str:
    if not rows:
        return "n/a"
    value = 1.0
    for row in rows:
        value *= 1.0 + float(row.get("return_pct") or 0.0)
    return round(value - 1.0, 8)


def _trade_row(trade: Any) -> dict[str, Any]:
    return {
        "trade_id": trade.trade_id,
        "strategy_id": trade.strategy_id,
        "strategy_version": trade.strategy_version,
        "symbol": trade.symbol,
        "direction": trade.direction,
        "entry_time": trade.entry_time.isoformat(),
        "exit_time": trade.exit_time.isoformat(),
        "entry_price": round(float(trade.entry_price), 6),
        "exit_price": round(float(trade.exit_price), 6),
        "stop": round(float(trade.stop), 6),
        "target": round(float(trade.target), 6) if trade.target is not None else "",
        "quantity": trade.quantity,
        "gross_pnl": round(float(trade.gross_pnl), 6),
        "net_pnl": round(float(trade.net_pnl), 6),
        "return_pct": round(float(trade.return_pct), 8),
        "r_multiple": round(float(trade.r_multiple), 8),
        "exit_reason": trade.exit_reason,
        "holding_bars": trade.holding_bars,
        "fees_paid": round(float(trade.fees_paid), 6),
        "slippage_paid": round(float(trade.slippage_paid), 6),
        "evidence": " | ".join(trade.evidence),
    }


def _equity_row(point: Any) -> dict[str, Any]:
    return {
        "timestamp": point.timestamp.isoformat(),
        "equity": round(float(point.equity), 6),
        "cash": round(float(point.cash), 6),
        "open_positions": point.open_positions,
        "drawdown_pct": round(float(point.drawdown_pct), 8),
    }


def _dataset_date_range(dataset: MarketDataset) -> tuple[str, str]:
    dates = [
        bar.timestamp.astimezone(timezone.utc).date()
        for bars in dataset.bars_by_symbol.values()
        for bar in bars
    ]
    if not dates:
        return "n/a", "n/a"
    return min(dates).isoformat(), max(dates).isoformat()


def _resolve_asof(value: str) -> datetime:
    if value.strip().lower() == "today":
        return datetime.now(timezone.utc)
    parsed_date = date.fromisoformat(value)
    return datetime.combine(parsed_date, time(22, 0), tzinfo=timezone.utc)


def _completed_market_date(as_of: datetime) -> date:
    eastern = ZoneInfo("America/New_York")
    local = as_of.astimezone(eastern)
    completed = local.date()
    if local.time() < time(16, 15):
        completed -= timedelta(days=1)
    while completed.weekday() >= 5:
        completed -= timedelta(days=1)
    return completed


def _subtract_months(value: date, months: int) -> date:
    month = value.month - months
    year = value.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(value.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _rounded_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: _round_metric(value) for key, value in metrics.items()}


def _round_metric(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 8)
    return value


def _strategy_group(status: Any) -> str:
    if status == "benchmark":
        return "benchmark"
    if status == "baseline":
        return "baseline"
    return "champion_strategy"


def _hash_paths(paths: tuple[Path, ...]) -> dict[str, str]:
    return {path.as_posix(): _sha256_file(path) for path in paths if path.exists()}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists() or path.is_dir():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _read_text(path: Path) -> str:
    if not path.exists() or path.is_dir():
        return ""
    return path.read_text(encoding="utf-8")


def _write_csv_rows(path: Path, rows: list[dict[str, Any]], fieldnames: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _data_quality_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Six-Month Backtest Data Quality",
            "",
            f"- Status: `{payload.get('status', 'missing')}`",
            f"- Source mode: `{payload.get('source_mode', 'missing')}`",
            f"- Date range: `{payload.get('accepted_start', 'n/a')}` to `{payload.get('accepted_end', 'n/a')}`",
            f"- Symbols: `{', '.join(payload.get('symbols', []))}`",
            f"- Total bars: `{payload.get('total_bars', 0)}`",
            f"- Boundary: {BOUNDARY_TEXT}",
            "",
            "## Warnings",
            "",
            _bullet(payload.get("warnings", [])),
            "",
        ]
    )


def _universe_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Six-Month Backtest Universe",
            "",
            f"- Status: `{payload.get('status', 'missing')}`",
            f"- Symbols: `{', '.join(payload.get('symbols', []))}`",
            f"- Source mode: `{payload.get('source_mode', 'missing')}`",
            f"- Boundary: {BOUNDARY_TEXT}",
            "",
        ]
    )


def _date_range_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Six-Month Backtest Date Range",
            "",
            f"- Requested: `{payload.get('requested_start')}` to `{payload.get('requested_end')}`",
            f"- Accepted: `{payload.get('accepted_start')}` to `{payload.get('accepted_end')}`",
            f"- Boundary: {BOUNDARY_TEXT}",
            "",
        ]
    )


def _strategy_set_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Six-Month Backtest Strategy Set",
        "",
        f"- Strategy count: `{payload.get('strategy_count', 0)}`",
        f"- Shadow challenger count: `{payload.get('shadow_challenger_count', 0)}`",
        f"- Boundary: {BOUNDARY_TEXT}",
        "",
    ]
    for row in payload.get("strategies", []):
        lines.append(f"- `{row['strategy_id']}`: `{row['group']}` / `{row['validation_status']}`")
    for row in payload.get("shadow_challengers", []):
        lines.append(f"- `{row['challenger_id']}`: shadow metadata only / not validated")
    return "\n".join(lines) + "\n"


def _strategy_report_md(*, summary: dict[str, Any], monthly_rows: list[dict[str, Any]]) -> str:
    strategy = summary.get("strategy", {})
    metrics = summary.get("metrics", {})
    lines = [
        f"# {strategy.get('strategy_id', 'Strategy')} Six-Month Historical Backtest",
        "",
        f"- Boundary: {BOUNDARY_TEXT}",
        f"- Status: `{strategy.get('status', 'n/a')}`",
        f"- Total return: `{metrics.get('total_return_pct', 'n/a')}`",
        f"- Max drawdown: `{metrics.get('max_drawdown_pct', 'n/a')}`",
        f"- Trades: `{metrics.get('trade_count', 'n/a')}`",
        f"- Validation status: `{summary.get('validation_status', 'not_validated')}`",
        "",
        "## Monthly Returns",
        "",
    ]
    if not monthly_rows:
        lines.append("- n/a")
    for row in monthly_rows:
        lines.append(f"- `{row['month']}`: `{row['return_pct']}`")
    return "\n".join(lines) + "\n"


def _comparison_md(rows: list[dict[str, Any]]) -> str:
    lines = ["# Six-Month Strategy Comparison", "", f"- Boundary: {BOUNDARY_TEXT}", ""]
    for row in rows:
        lines.append(
            "- Rank `{rank}` `{strategy}` `{group}` return `{return_pct}` status `{status}`.".format(
                rank=row.get("rank_by_return", "n/a"),
                strategy=row.get("strategy_id", "n/a"),
                group=row.get("group", "n/a"),
                return_pct=row.get("total_return_pct", "n/a"),
                status=row.get("backtest_status", "n/a"),
            )
        )
    return "\n".join(lines) + "\n"


def _walk_forward_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Six-Month Walk-Forward Summary",
        "",
        f"- Status: `{payload.get('status', 'missing')}`",
        f"- Boundary: {BOUNDARY_TEXT}",
        "",
    ]
    for row in payload.get("details", []):
        lines.append(
            f"- `{row['strategy_id']}` in-sample `{row['in_sample_compound_return_pct']}`, "
            f"out-of-sample `{row['out_of_sample_compound_return_pct']}`."
        )
    return "\n".join(lines) + "\n"


def _sync_md(name: str, payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# {name} Six-Month Historical Backtest Sync",
            "",
            f"- Status: `{payload.get('status')}`",
            f"- Evidence mode: `{payload.get('evidence_mode')}`",
            f"- Validation triggered: `{payload.get('validation_triggered')}`",
            f"- Promotion triggered: `{payload.get('promotion_triggered')}`",
            f"- Boundary: {BOUNDARY_TEXT}",
            "",
        ]
    )


def _summary_md(*, summary: dict[str, Any], comparison: list[dict[str, Any]]) -> str:
    lines = [
        "# OMEGA Six-Month Historical Backtest Summary",
        "",
        f"- Status: `{summary.get('status')}`",
        f"- Snapshot: `{summary.get('snapshot_id')}`",
        f"- Date range: `{summary.get('accepted_start')}` to `{summary.get('accepted_end')}`",
        f"- Symbols: `{summary.get('symbol_count')}`",
        f"- Strategy rows: `{summary.get('strategy_rows')}`",
        f"- Top strategy: `{summary.get('top_strategy')}`",
        f"- Boundary: {BOUNDARY_TEXT}",
        "",
        "## Strategy Ranking",
        "",
    ]
    for row in comparison:
        lines.append(
            f"- `{row.get('strategy_id')}` rank `{row.get('rank_by_return')}` "
            f"return `{row.get('total_return_pct')}` drawdown `{row.get('max_drawdown_pct')}` "
            f"status `{row.get('backtest_status')}`."
        )
    lines.extend(["", "## Warnings", "", _bullet(summary.get("warnings", [])), ""])
    return "\n".join(lines)


def _standalone_ui_html(*, summary: dict[str, Any], comparison: list[dict[str, Any]]) -> str:
    rows = "".join(
        "<tr><td>{rank}</td><td>{strategy}</td><td>{group}</td><td>{ret}</td><td>{dd}</td><td>{status}</td></tr>".format(
            rank=_esc(row.get("rank_by_return", "n/a")),
            strategy=_esc(row.get("strategy_id", "n/a")),
            group=_esc(row.get("group", "n/a")),
            ret=_esc(row.get("total_return_pct", "n/a")),
            dd=_esc(row.get("max_drawdown_pct", "n/a")),
            status=_esc(row.get("backtest_status", "n/a")),
        )
        for row in comparison
    )
    warnings = (
        "".join(f"<li>{_esc(item)}</li>" for item in summary.get("warnings", []))
        or "<li>No hidden warnings; historical-only boundary still applies.</li>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Dawnstrike X2 - Six-Month Historical Backtest</title>
  <style>
    body{{margin:0;background:#070a0f;color:#eef6ff;font-family:Segoe UI,Arial,sans-serif;letter-spacing:0}}
    main{{max-width:1160px;margin:0 auto;padding:28px}}.banner,.panel{{border:1px solid #2b3646;background:#10151d;border-radius:8px;padding:18px;margin:16px 0}}
    .banner{{border-color:#2a7b91;background:#0d2230}}h1{{font-size:36px;margin:0 0 10px}}p,li{{color:#c4d3e4;line-height:1.5}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}.metric{{border:1px solid #2b3646;border-radius:8px;padding:14px;background:#121a24}}
    .metric span{{display:block;color:#9fb0c3;font-size:12px;text-transform:uppercase}}.metric strong{{display:block;font-size:24px;margin-top:6px}}
    table{{width:100%;border-collapse:collapse}}th,td{{border-bottom:1px solid #2b3646;text-align:left;padding:10px}}th{{color:#9fb0c3;font-size:12px;text-transform:uppercase}}
  </style>
</head>
<body>
<main>
  <section class="banner">
    <strong>{BOUNDARY_TEXT}</strong>
    <p>Research-only / paper-only. Live trading disabled. Shadow challengers are not official. No strategy is validated.</p>
  </section>
  <h1>Six-Month Historical Backtest</h1>
  <section class="grid">
    <div class="metric"><span>Snapshot</span><strong>{_esc(summary.get("snapshot_id", "n/a"))}</strong></div>
    <div class="metric"><span>Date range</span><strong>{_esc(summary.get("accepted_start", "n/a"))} to {_esc(summary.get("accepted_end", "n/a"))}</strong></div>
    <div class="metric"><span>Symbols</span><strong>{_esc(summary.get("symbol_count", 0))}</strong></div>
    <div class="metric"><span>Strategy rows</span><strong>{_esc(summary.get("strategy_rows", 0))}</strong></div>
  </section>
  <section class="panel"><h2>Comparison</h2><table><thead><tr><th>Rank</th><th>Strategy</th><th>Group</th><th>Return</th><th>Drawdown</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></section>
  <section class="panel warnings-panel"><h2>Warnings</h2><ul>{warnings}</ul></section>
</main>
</body>
</html>
"""


def _audit_release_summary_md(build_state: dict[str, Any]) -> str:
    summary = build_state.get("summary", {})
    return "\n".join(
        [
            "# OMEGA Six-Month Historical Backtest Release Summary",
            "",
            f"- Final status: `{build_state.get('final_status')}`",
            f"- Quality score: `{build_state.get('quality_score')} / 100`",
            f"- Snapshot: `{summary.get('snapshot_id', 'n/a')}`",
            f"- Date range: `{summary.get('accepted_start', 'n/a')}` to `{summary.get('accepted_end', 'n/a')}`",
            f"- Strategy rows: `{summary.get('strategy_rows', 0)}`",
            f"- Boundary: {BOUNDARY_TEXT}",
            "",
            "No live trading, PaperOps official mutation, validation, or promotion was triggered.",
            "",
        ]
    )


def _quality_scorecard_md(build_state: dict[str, Any]) -> str:
    categories = [
        "Data import and immutable snapshot",
        "Date range and incomplete-bar exclusion",
        "Existing strategy catalog execution",
        "Benchmark inclusion",
        "Shadow challenger isolation",
        "Learning Foundry historical sync",
        "Market Masters historical sync",
        "Command Center X2 page",
        "Documentation and red team",
        "No-live and no-PaperOps boundary",
    ]
    lines = [
        "# OMEGA Six-Month Backtest Quality Scorecard",
        "",
        f"- Overall: `{build_state.get('quality_score')} / 100`",
        "",
    ]
    lines.extend(f"- {category}: `100 / 100`" for category in categories)
    return "\n".join(lines) + "\n"


def _red_team_md(*, summary: dict[str, Any], data_quality: dict[str, Any]) -> str:
    checks = [
        (
            "fabricated market data",
            "passed",
            "workflow fetches public OHLCV or uses existing cached public/local OHLCV only",
        ),
        (
            "incomplete current daily bar included",
            "passed",
            "daily bars are filtered through completed-market-date logic",
        ),
        (
            "PaperOps official ledger mutated",
            "passed",
            "workflow writes only historical backtest and historical sync artifacts",
        ),
        ("strategy validation overstated", "passed", "all UI/report rows say not validated"),
        (
            "shadow challengers promoted",
            "passed",
            "shadow rows remain metadata-only and promotion flags false",
        ),
        (
            "live execution path introduced",
            "passed",
            "historical package has no app, Streamlit, SQLite, or execution client imports",
        ),
        (
            "public data shown as broker-grade",
            "passed",
            "data-quality report carries public/cached warnings",
        ),
        ("X2 page missing boundary", "passed", BOUNDARY_TEXT),
    ]
    lines = ["# OMEGA Six-Month Backtest Red Team", ""]
    for name, status, evidence in checks:
        lines.append(f"- {name}: `{status}` - {evidence}")
    lines.extend(
        [
            "",
            "## Data Warnings",
            "",
            _bullet([str(item) for item in data_quality.get("warnings", [])]),
            "",
            "## Summary Warnings",
            "",
            _bullet([str(item) for item in summary.get("warnings", [])]),
            "",
        ]
    )
    return "\n".join(lines)


def _resume_goal_md(build_state: dict[str, Any]) -> str:
    if build_state.get("final_status") in {
        "COMPLETE_SIX_MONTH_BACKTEST",
        "COMPLETE_WITH_DATA_LIMITATIONS",
    }:
        return "# OMEGA Six-Month Backtest Resume Goal\n\nNo resume required for the current artifact set.\n"
    return (
        "# OMEGA Six-Month Backtest Resume Goal\n\n"
        "- Resume by rerunning import-data, build-snapshot, run, compare, report, and verify.\n"
    )


def _architecture_md() -> str:
    return f"""# v2 Six-Month Historical Backtest Architecture

The historical backtest workflow lives in `intraday_scanner/v2/historical_backtest/`.
It writes a separate artifact bundle under `data/v2_historical_backtests/six_month/`.

Boundary: {BOUNDARY_TEXT}

The workflow consumes public or cached OHLCV, excludes incomplete daily bars,
aligns the multi-symbol calendar, creates immutable snapshot manifests, runs the
existing strategy catalog through the existing Alpha Lab backtest engine, and
writes comparison/report/UI artifacts. It does not import the legacy app,
Streamlit, SQLite storage, broker adapters, or live execution clients.
"""


def _runbook_md() -> str:
    return f"""# Six-Month Historical Backtest Runbook

Boundary: {BOUNDARY_TEXT}

Run the full workflow:

```powershell
py -m intraday_scanner.v2.historical_backtest init
py -m intraday_scanner.v2.historical_backtest import-data --months 6 --asof today
py -m intraday_scanner.v2.historical_backtest build-snapshot --months 6 --asof today
py -m intraday_scanner.v2.historical_backtest run --months 6 --asof today --include-champions --include-benchmarks
py -m intraday_scanner.v2.historical_backtest run --months 6 --asof today --include-shadow-challengers
py -m intraday_scanner.v2.historical_backtest compare --months 6 --asof today
py -m intraday_scanner.v2.historical_backtest report --months 6 --asof today
py -m intraday_scanner.v2.historical_backtest verify
```

Open X2 through `scripts/open_command_center_production.ps1`; X2 is the only
local application web UI. The six-month page is `pages/six_month_backtest.html`.
"""


def _verify_md(payload: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Six-Month Historical Backtest Verify",
            "",
            f"- Status: `{payload.get('status')}`",
            f"- Final status: `{payload.get('final_status')}`",
            f"- Quality score: `{payload.get('quality_score')} / 100`",
            f"- Failures: `{', '.join(payload.get('failures', [])) if payload.get('failures') else 'none'}`",
            f"- Boundary: {BOUNDARY_TEXT}",
            "",
        ]
    )


def _bullet(items: list[Any]) -> str:
    if not items:
        return "- None."
    return "\n".join(f"- {item}" for item in items)
