from __future__ import annotations

import ast
import hashlib
import json
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from intraday_scanner.v2.alpha_lab import run_demo
from intraday_scanner.v2.alpha_lab import runner as alpha_lab_runner
from intraday_scanner.v2.backtest import BacktestEngine, BacktestSettings
from intraday_scanner.v2.data import (
    MarketBar,
    MarketDataset,
    filter_incomplete_daily_bars,
    timestamp_alignment_issues,
    validate_dataset,
)
from intraday_scanner.v2.data.synthetic import build_synthetic_ohlcv_dataset
from intraday_scanner.v2.data.yahoo_chart import (
    YahooChartFetchResult,
    dataset_from_yahoo_chart_payloads,
)
from intraday_scanner.v2.indicators import atr, bollinger_bands, donchian_high, rsi, sma
from intraday_scanner.v2.paper import PaperLifecycleSettings, run_paper_lifecycle
from intraday_scanner.v2.risk import RiskSettings
from intraday_scanner.v2.scanner import run_latest_scan
from intraday_scanner.v2.strategies import (
    Direction,
    StrategySignal,
    StrategySpec,
    build_strategy_catalog,
)

NOW = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)


def _bar(
    symbol: str, index: int, open_price: float, high: float, low: float, close: float
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=datetime.combine(date(2026, 1, 1), time(21, 0), tzinfo=timezone.utc).replace(
            day=min(index + 1, 28)
        ),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=100_000 + index,
    )


def test_v2_indicators_known_examples() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sma(values, 3) == [None, None, 2.0, 3.0, 4.0]
    assert rsi([1.0, 2.0, 3.0, 2.0, 1.0], 2)[2] == 100.0
    bars = (
        _bar("TST", 0, 10.0, 12.0, 9.0, 11.0),
        _bar("TST", 1, 11.0, 13.0, 10.0, 12.0),
        _bar("TST", 2, 12.0, 15.0, 11.0, 14.0),
    )
    assert atr(bars, 2)[1] == 3.0
    assert donchian_high(bars, 2, 2) == 13.0
    middle, upper, lower = bollinger_bands(values, 3)
    assert middle[2] == 2.0
    assert upper[0] is None
    assert lower[4] is not None


def test_strategy_signal_has_no_future_dependency_and_explicit_risk() -> None:
    dataset = build_synthetic_ohlcv_dataset(end_date=date(2026, 6, 29), trading_days=120)
    strategy = next(
        item for item in build_strategy_catalog() if item.strategy_id == "ts_momentum_sma_atr"
    )
    symbol = "NOVA"
    bars = dataset.bars_by_symbol[symbol]
    signal_index = next(
        index for index in range(len(bars)) if strategy.signal(dataset, symbol, bars, index)
    )
    full_signal = strategy.signal(dataset, symbol, bars, signal_index)
    truncated_bars = bars[: signal_index + 1]
    truncated_dataset = MarketDataset(
        dataset_id="truncated",
        source_kind="synthetic",
        timeframe="1d",
        bars_by_symbol={symbol: truncated_bars},
    )
    truncated_signal = strategy.signal(truncated_dataset, symbol, truncated_bars, signal_index)

    assert full_signal is not None
    assert truncated_signal is not None
    assert round(full_signal.stop, 6) == round(truncated_signal.stop, 6)
    assert full_signal.target is not None
    assert full_signal.stop < full_signal.entry_reference < full_signal.target


def test_backtest_stop_first_and_determinism() -> None:
    bars = (
        _bar("TST", 0, 10.0, 10.5, 9.5, 10.0),
        _bar("TST", 1, 10.0, 12.5, 8.5, 11.0),
        _bar("TST", 2, 11.0, 11.5, 10.5, 11.2),
    )
    dataset = MarketDataset(
        dataset_id="same_bar_fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"TST": bars},
    )

    def signal(
        spec: StrategySpec,
        data: MarketDataset,
        symbol: str,
        symbol_bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        del data, symbol_bars
        if index != 0:
            return None
        return StrategySignal(
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=10.0,
            stop=9.0,
            target=12.0,
            score=80.0,
            evidence=("test signal",),
            invalidation="stop hit",
        )

    strategy = StrategySpec(
        strategy_id="test_same_bar",
        version="v1",
        status="experimental",
        description="test",
        compatible_timeframe="1d",
        required_data_fields=("open", "high", "low", "close", "volume"),
        parameters={},
        indicators=(),
        entry_logic="test",
        exit_logic="test",
        stop_logic="test",
        target_logic="test",
        position_sizing_assumption="test",
        known_failure_modes=(),
        validation_status="test",
        generate_signal=signal,
    )
    result_a = BacktestEngine().run(strategy, dataset)
    result_b = BacktestEngine().run(strategy, dataset)
    assert result_a.trades[0].exit_reason == "stop"
    assert result_a.trades == result_b.trades


def test_paper_lifecycle_stop_first_and_audit_log() -> None:
    bars = (
        _bar("TST", 0, 10.0, 10.5, 9.5, 10.0),
        _bar("TST", 1, 10.0, 12.5, 8.5, 11.0),
    )
    dataset = MarketDataset(
        dataset_id="paper_stop_first",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"TST": bars},
    )

    def signal(
        spec: StrategySpec,
        data: MarketDataset,
        symbol: str,
        symbol_bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        del data, symbol_bars
        if index != 0:
            return None
        return StrategySignal(
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=10.0,
            stop=9.0,
            target=12.0,
            score=90.0,
            evidence=("stop-first fixture",),
            invalidation="stop hit",
        )

    strategy = _test_strategy("paper_stop_first_strategy", signal)
    validation = validate_dataset(dataset, min_bars_per_symbol=1, max_staleness_days=999, as_of=NOW)
    result = run_paper_lifecycle(
        dataset,
        validation,
        (strategy,),
        run_id="paper-run",
        data_snapshot_id="snapshot-1",
        settings=PaperLifecycleSettings(max_picks_per_day=1),
    )

    assert len(result.picks) == 1
    assert len(result.entries) == 1
    assert result.checks[0].decision == "stop_first"
    assert result.exits[0].exit_reason == "stop"
    assert result.exits[0].net_pnl < 0
    assert result.calendar_returns[0].exit_count == 1
    assert any(event.event_type == "intraday_check_evaluated" for event in result.audit_events)


def test_paper_lifecycle_eod_close_strategy_pnl_and_calendar() -> None:
    bars = (
        _bar("TST", 0, 10.0, 10.5, 9.5, 10.0),
        _bar("TST", 1, 10.0, 11.0, 9.6, 10.8),
    )
    dataset = MarketDataset(
        dataset_id="paper_eod",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"TST": bars},
    )

    def signal(
        spec: StrategySpec,
        data: MarketDataset,
        symbol: str,
        symbol_bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        del data, symbol_bars
        if index != 0:
            return None
        return StrategySignal(
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=10.0,
            stop=9.0,
            target=20.0,
            score=90.0,
            evidence=("eod fixture",),
            invalidation="stop hit",
        )

    strategy = _test_strategy("paper_eod_strategy", signal)
    validation = validate_dataset(dataset, min_bars_per_symbol=1, max_staleness_days=999, as_of=NOW)
    result = run_paper_lifecycle(
        dataset,
        validation,
        (strategy,),
        run_id="paper-run-eod",
        data_snapshot_id="snapshot-1",
        settings=PaperLifecycleSettings(max_picks_per_day=1),
    )

    assert result.checks[0].decision == "eod_close"
    assert result.exits[0].exit_reason == "eod_close"
    assert result.exits[0].total_fees == result.exits[0].entry_fee + result.exits[0].exit_fee
    assert (
        result.exits[0].total_slippage
        == result.exits[0].entry_slippage + result.exits[0].exit_slippage
    )
    assert result.strategy_pnl[0].strategy_id == "paper_eod_strategy"
    assert result.strategy_pnl[0].trade_count == 1
    assert result.strategy_pnl[0].fees_paid == result.exits[0].total_fees
    assert result.strategy_pnl[0].slippage_paid == result.exits[0].total_slippage
    assert result.calendar_returns[0].net_pnl == result.exits[0].net_pnl


def test_fees_and_slippage_reduce_returns() -> None:
    dataset = build_synthetic_ohlcv_dataset(end_date=date(2026, 6, 29), trading_days=90)
    strategy = next(
        item for item in build_strategy_catalog() if item.strategy_id == "ts_momentum_sma_atr"
    )
    clean = BacktestEngine(
        BacktestSettings(fee_bps=0.0, slippage_bps=0.0, risk=RiskSettings(account_equity=100_000.0))
    ).run(strategy, dataset)
    costly = BacktestEngine(
        BacktestSettings(
            fee_bps=10.0, slippage_bps=20.0, risk=RiskSettings(account_equity=100_000.0)
        )
    ).run(strategy, dataset)
    assert float(costly.metrics["final_equity"]) < float(clean.metrics["final_equity"])


def test_latest_scan_cards_are_deterministic_and_complete() -> None:
    dataset = build_synthetic_ohlcv_dataset(end_date=date(2026, 6, 29), trading_days=140)
    strategies = build_strategy_catalog()
    engine = BacktestEngine()
    results = {strategy.strategy_id: engine.run(strategy, dataset) for strategy in strategies}
    scan_a = run_latest_scan(
        dataset,
        tuple(strategy for strategy in strategies if strategy.status == "experimental"),
        results,
        risk_settings=RiskSettings(),
        data_snapshot_id="snapshot-1",
        run_manifest_id="run-1",
    )
    scan_b = run_latest_scan(
        dataset,
        tuple(strategy for strategy in strategies if strategy.status == "experimental"),
        results,
        risk_settings=RiskSettings(),
        data_snapshot_id="snapshot-1",
        run_manifest_id="run-1",
    )
    assert [card.to_dict() for card in scan_a.cards] == [card.to_dict() for card in scan_b.cards]
    assert scan_a.cards
    first = scan_a.cards[0]
    assert first.stop is not None
    assert first.risk_per_share is not None
    assert first.reward_risk is not None
    assert first.research_only is True


def test_insufficient_data_validation_warns() -> None:
    dataset = MarketDataset(
        dataset_id="tiny",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", 0, 1.0, 1.1, 0.9, 1.0),)},
    )
    validation = validate_dataset(
        dataset,
        min_bars_per_symbol=10,
        max_staleness_days=1,
        as_of=NOW,
    )
    assert validation.passed is True
    assert any("only 1 bars" in warning for warning in validation.warnings)


def test_multi_symbol_backtests_reject_unaligned_timestamps() -> None:
    day_one = datetime(2026, 1, 1, 21, 0, tzinfo=timezone.utc)
    day_two = datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc)
    day_three = datetime(2026, 1, 3, 21, 0, tzinfo=timezone.utc)
    dataset = MarketDataset(
        dataset_id="unaligned",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "AAA": (
                MarketBar("AAA", day_one, 10.0, 11.0, 9.5, 10.5, 1000),
                MarketBar("AAA", day_two, 10.5, 11.5, 10.0, 11.0, 1100),
            ),
            "BBB": (
                MarketBar("BBB", day_one, 20.0, 21.0, 19.5, 20.5, 1000),
                MarketBar("BBB", day_three, 20.5, 21.5, 20.0, 21.0, 1100),
            ),
        },
    )

    validation = validate_dataset(dataset, min_bars_per_symbol=1, max_staleness_days=999, as_of=NOW)
    strategy = next(
        item for item in build_strategy_catalog() if item.strategy_id == "ts_momentum_sma_atr"
    )

    assert timestamp_alignment_issues(dataset)
    assert validation.passed is False
    with pytest.raises(ValueError, match="timestamp-aligned"):
        BacktestEngine().run(strategy, dataset)


def test_yahoo_chart_payload_parser_builds_valid_market_dataset() -> None:
    timestamp_a = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    timestamp_b = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())
    payload = {
        "chart": {
            "error": None,
            "result": [
                {
                    "timestamp": [timestamp_a, timestamp_b, timestamp_b + 86400],
                    "indicators": {
                        "quote": [
                            {
                                "open": [100.0, 101.0, 100.0],
                                "high": [102.0, 103.0, 99.0],
                                "low": [99.0, 100.0, 101.0],
                                "close": [101.0, 102.0, 100.5],
                                "volume": [1000, 1100, 1200],
                            }
                        ]
                    },
                }
            ],
        }
    }
    dataset = dataset_from_yahoo_chart_payloads(
        {"TST": payload},
        dataset_id="public_fixture",
        source_kind="public_yahoo_chart",
        source_refs=("https://query1.finance.yahoo.com/v8/finance/chart/TST",),
    )
    validation = validate_dataset(dataset, min_bars_per_symbol=2, max_staleness_days=999, as_of=NOW)

    assert dataset.source_kind == "public_yahoo_chart"
    assert dataset.total_bars == 2
    assert dataset.bars_by_symbol["TST"][0].open == 100.0
    assert any("invalid high" in warning for warning in dataset.warnings)
    assert dataset.source_refs
    assert validation.passed is True


def test_public_daily_filter_excludes_incomplete_current_session_bar() -> None:
    yesterday = MarketBar(
        symbol="SPY",
        timestamp=datetime(2026, 6, 29, 13, 30, tzinfo=timezone.utc),
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=1000,
    )
    partial_today = MarketBar(
        symbol="SPY",
        timestamp=datetime(2026, 6, 30, 13, 30, tzinfo=timezone.utc),
        open=101.0,
        high=103.0,
        low=100.0,
        close=102.0,
        volume=1200,
    )
    dataset = MarketDataset(
        dataset_id="public_daily",
        source_kind="public_yahoo_chart",
        timeframe="1d",
        bars_by_symbol={"SPY": (yesterday, partial_today)},
    )

    filtered = filter_incomplete_daily_bars(
        dataset,
        as_of=datetime(2026, 6, 30, 16, 46, tzinfo=timezone.utc),
    )

    assert filtered.bars_by_symbol["SPY"] == (yesterday,)
    assert filtered.latest_timestamp == yesterday.timestamp
    assert any("incomplete or future daily bar" in warning for warning in filtered.warnings)


def test_alpha_lab_demo_prefers_public_dataset_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic = build_synthetic_ohlcv_dataset(end_date=date(2026, 6, 30), trading_days=130)
    public_dataset = MarketDataset(
        dataset_id="public_yahoo_chart_test",
        source_kind="public_yahoo_chart",
        timeframe="1d",
        bars_by_symbol=synthetic.bars_by_symbol,
        source_path="data/v2_alpha_lab/fixtures/public_yahoo/public_yahoo_ohlcv.csv",
        warnings=("public_yahoo_chart: test cache",),
        source_refs=("https://query1.finance.yahoo.com/v8/finance/chart/SPY",),
    )

    def fake_fetch(*, cache_dir: Path) -> YahooChartFetchResult:
        assert cache_dir.name == "public_yahoo"
        return YahooChartFetchResult(
            dataset=public_dataset,
            raw_payload_paths=(),
            warnings=("test public fetch",),
        )

    monkeypatch.setattr(alpha_lab_runner, "fetch_yahoo_chart_daily_dataset", fake_fetch)
    result = run_demo(output_root=tmp_path / "v2_alpha_lab", created_at=NOW)

    assert result.dataset.source_kind == "public_yahoo_chart"
    assert any("using public Yahoo Finance chart" in warning for warning in result.warnings)
    latest = result.dataset.latest_timestamp
    assert latest is not None
    assert latest.date() <= date(2026, 6, 26)
    manifest = json.loads(
        (tmp_path / "v2_alpha_lab" / "manifests" / f"{result.run_id}.json").read_text()
    )
    source = manifest["source_data"][0]
    assert source["rows_rejected"] > 0
    assert source["rows_read"] == source["rows_accepted"] + source["rows_rejected"]


def test_alpha_lab_demo_generates_required_artifacts(tmp_path: Path) -> None:
    output_root = tmp_path / "v2_alpha_lab"
    result = run_demo(output_root=output_root, created_at=NOW, allow_public_data=False)
    assert result.dataset.source_kind == "synthetic"
    required = [
        output_root / "reports" / "alpha_lab_summary.md",
        output_root / "reports" / "strategy_comparison.csv",
        output_root / "reports" / "strategy_comparison.json",
        output_root / "reports" / "robustness_summary.csv",
        output_root / "reports" / "robustness_summary.json",
        output_root / "reports" / "robustness_summary.md",
        output_root / "scans" / "latest_scan.json",
        output_root / "scans" / "latest_decision_cards.json",
        output_root / "manifests" / f"{result.run_id}.json",
        output_root / "paper" / "paper_picks.json",
        output_root / "paper" / "paper_entries.csv",
        output_root / "paper" / "paper_checks.csv",
        output_root / "paper" / "paper_exits.csv",
        output_root / "paper" / "strategy_pnl.csv",
        output_root / "paper" / "strategy_pnl.json",
        output_root / "paper" / "calendar_returns.csv",
        output_root / "paper" / "calendar_returns.json",
        output_root / "paper" / "paper_audit_log.jsonl",
        output_root / "paper" / "paper_lifecycle_summary.json",
    ]
    for path in required:
        assert path.exists(), path
    for strategy_id in result.backtest_results:
        assert (output_root / "backtests" / f"{strategy_id}_summary.json").exists()
        assert (output_root / "backtests" / f"{strategy_id}_trades.csv").exists()
        assert (output_root / "backtests" / f"{strategy_id}_equity_curve.csv").exists()

    manifest = json.loads((output_root / "manifests" / f"{result.run_id}.json").read_text())
    run_prefix = f"runs/{result.run_id}/"
    for artifact in manifest["output_artifacts"]:
        uri = artifact["uri"]
        assert uri.startswith(run_prefix), uri
        artifact_path = output_root / uri
        assert artifact_path.exists(), uri
        assert _sha256(artifact_path) == artifact["sha256"]

    assert result.paper_lifecycle.picks
    assert result.paper_lifecycle.entries
    assert result.paper_lifecycle.checks
    assert result.paper_lifecycle.exits
    assert result.paper_lifecycle.strategy_pnl
    assert result.paper_lifecycle.calendar_returns


def test_v2_alpha_lab_modules_avoid_runtime_execution_paths() -> None:
    forbidden_import_roots = {
        "app",
        "sqlite3",
        "streamlit",
    }
    forbidden_import_prefixes = {
        "intraday_scanner.cli",
        "intraday_scanner.storage",
        "intraday_scanner.services",
        "intraday_scanner.scoring",
        "intraday_scanner.formula",
        "intraday_scanner.integrations",
        "intraday_scanner.providers",
    }
    forbidden_calls = {
        "connect",
        "execute",
        "executemany",
        "initialize",
        "persist_scan_result",
        "run_trade_watcher",
        "score_universe",
        "submit" + "_order",
    }

    for path in Path("intraday_scanner/v2").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_import_roots, path
                    assert not any(
                        alias.name.startswith(prefix) for prefix in forbidden_import_prefixes
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_import_roots, path
                assert not any(
                    node.module.startswith(prefix) for prefix in forbidden_import_prefixes
                )
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path


def _test_strategy(strategy_id: str, signal_function) -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        version="v1",
        status="experimental",
        description="test",
        compatible_timeframe="1d",
        required_data_fields=("open", "high", "low", "close", "volume"),
        parameters={},
        indicators=(),
        entry_logic="test",
        exit_logic="test",
        stop_logic="test",
        target_logic="test",
        position_sizing_assumption="test",
        known_failure_modes=(),
        validation_status="test",
        generate_signal=signal_function,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()
