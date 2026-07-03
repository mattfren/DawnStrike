from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.v2.data import DEFAULT_YAHOO_CHART_SYMBOLS, MarketBar, MarketDataset
from intraday_scanner.v2.data.yahoo_chart import YahooChartFetchResult
from intraday_scanner.v2.historical_backtest import core


def test_six_month_historical_workflow_generates_safe_artifacts(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    repo_root = tmp_path / "repo"
    output_root = repo_root / "data/v2_historical_backtests/six_month"
    _write_shadow_registries(repo_root)
    dataset = _fixture_dataset(end=date(2026, 7, 6))

    def fake_fetch(**kwargs: Any) -> YahooChartFetchResult:
        cache_dir = Path(kwargs["cache_dir"])
        cache_dir.mkdir(parents=True, exist_ok=True)
        raw_path = cache_dir / "fixture_chart.json"
        raw_path.write_text('{"fixture": true}\n', encoding="utf-8")
        return YahooChartFetchResult(
            dataset=dataset,
            raw_payload_paths=(raw_path,),
            warnings=("public_fixture: deterministic OHLCV for tests",),
        )

    monkeypatch.setattr(core, "fetch_yahoo_chart_daily_dataset", fake_fetch)

    core.init(output_root=output_root, repo_root=repo_root)
    imported = core.import_data(
        months=6,
        asof="2026-07-03",
        output_root=output_root,
        repo_root=repo_root,
    )
    snapshot = core.build_snapshot(
        months=6,
        asof="2026-07-03",
        output_root=output_root,
        repo_root=repo_root,
    )
    champions = core.run(
        months=6,
        asof="2026-07-03",
        output_root=output_root,
        repo_root=repo_root,
        include_champions=True,
        include_benchmarks=True,
    )
    shadow = core.run(
        months=6,
        asof="2026-07-03",
        output_root=output_root,
        repo_root=repo_root,
        include_shadow_challengers=True,
    )
    comparison = core.compare(
        months=6,
        asof="2026-07-03",
        output_root=output_root,
        repo_root=repo_root,
    )
    report = core.report(
        months=6,
        asof="2026-07-03",
        output_root=output_root,
        repo_root=repo_root,
    )
    verified = core.verify(output_root=output_root, repo_root=repo_root)

    assert imported["data_quality"]["accepted_end"] == "2026-07-03"
    assert imported["data_quality"]["accepted_start"] >= "2026-01-03"
    assert imported["data_quality"]["total_bars"] > 600
    assert snapshot["immutable_snapshot"] is True
    assert champions["strategy_count"] == 9
    assert shadow["shadow_challenger_count"] == 2
    assert comparison["comparison_rows"] == 11
    assert report["quality_score"] == 100
    assert verified["status"] == "passed"
    assert verified["final_status"] == "COMPLETE_WITH_DATA_LIMITATIONS"

    strategy_set = core._read_json(output_root / "reports/strategy_set.json", {})
    assert strategy_set["strategy_count"] == 9
    assert strategy_set["shadow_challenger_count"] == 2
    assert all(
        row["validation_status"] == "not_validated_historical_backtest_only"
        for row in strategy_set["strategies"]
    )

    learning_sync = core._read_json(
        repo_root / "data/v2_learning_foundry/reports/six_month_historical_backtest_sync.json",
        {},
    )
    market_sync = core._read_json(
        repo_root / "data/v2_market_masters/reports/six_month_historical_backtest_sync.json",
        {},
    )
    assert learning_sync["evidence_mode"] == "historical_backtest"
    assert market_sync["evidence_mode"] == "historical_backtest"
    assert learning_sync["validation_triggered"] is False
    assert market_sync["promotion_triggered"] is False

    x2_page = (
        repo_root / "data/v2_command_center_x2/pages/six_month_backtest.html"
    ).read_text(encoding="utf-8")
    assert core.BOUNDARY_TEXT in x2_page
    assert "Live trading disabled" in x2_page
    assert "metadata_only_not_mechanically_replayed" in x2_page
    assert "place order" not in x2_page.lower()


def test_historical_backtest_package_source_safety_scan() -> None:
    scan = core._source_safety_scan(Path("intraday_scanner/v2/historical_backtest"))

    assert scan["status"] == "passed"
    assert scan["failures"] == []


def _fixture_dataset(*, end: date) -> MarketDataset:
    symbols = tuple(DEFAULT_YAHOO_CHART_SYMBOLS)
    bars_by_symbol: dict[str, tuple[MarketBar, ...]] = {}
    for symbol_index, symbol in enumerate(symbols):
        bars: list[MarketBar] = []
        cursor = date(2025, 12, 1)
        bar_index = 0
        while cursor <= end:
            if cursor.weekday() < 5:
                base = 80.0 + symbol_index * 18.0 + bar_index * (0.16 + symbol_index * 0.01)
                wave = ((bar_index + symbol_index) % 9 - 4) * 0.18
                close = base + wave
                open_price = close * (0.997 + (symbol_index % 3) * 0.001)
                high = max(open_price, close) * 1.012
                low = min(open_price, close) * 0.988
                bars.append(
                    MarketBar(
                        symbol=symbol,
                        timestamp=datetime.combine(cursor, time(13, 30), tzinfo=timezone.utc),
                        open=round(open_price, 4),
                        high=round(high, 4),
                        low=round(low, 4),
                        close=round(close, 4),
                        volume=1_000_000 + symbol_index * 10_000 + bar_index * 100,
                    )
                )
                bar_index += 1
            cursor = date.fromordinal(cursor.toordinal() + 1)
        bars_by_symbol[symbol] = tuple(bars)
    return MarketDataset(
        dataset_id="fixture_public_ohlcv",
        source_kind="public_fixture",
        timeframe="1d",
        bars_by_symbol=bars_by_symbol,
        warnings=("fixture source; not market evidence",),
    )


def _write_shadow_registries(repo_root: Path) -> None:
    learning_path = repo_root / "data/v2_learning_foundry/candidates/challenger_registry.json"
    market_path = repo_root / "data/v2_market_masters/candidates/challenger_registry.json"
    learning_path.parent.mkdir(parents=True, exist_ok=True)
    market_path.parent.mkdir(parents=True, exist_ok=True)
    learning_path.write_text(
        """{
  "status": "passed",
  "candidates": [
    {
      "candidate_id": "lf_fixture_shadow_v1",
      "parent_strategy_id": "ts_momentum_sma_atr",
      "status": "shadow",
      "rule_description": "Fixture shadow challenger.",
      "validation_status": "not_validated"
    }
  ]
}
""",
        encoding="utf-8",
    )
    market_path.write_text(
        """{
  "status": "passed",
  "challengers": [
    {
      "challenger_id": "mm_fixture_shadow_v1",
      "parent_strategy_ids": ["donchian_breakout_20_10"],
      "status": "shadow",
      "rule_description": "Fixture market master shadow challenger.",
      "validation_status": "not_validated"
    }
  ]
}
""",
        encoding="utf-8",
    )
