from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from intraday_scanner.dashboard.static_dashboard import (
    StaticDashboardError,
    build_dashboard_payload,
    render_dashboard_json,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "data" / "static_dashboard_source_2026-07-16.json"
OUTPUT_PATH = ROOT / "assets" / "dashboard-data.json"


def _source() -> dict[str, object]:
    return json.loads(SOURCE_PATH.read_text(encoding="utf-8"))


def _payload() -> dict[str, object]:
    return build_dashboard_payload(_source())


def test_committed_dashboard_data_is_the_deterministic_build() -> None:
    rendered = render_dashboard_json(_source())

    assert OUTPUT_PATH.read_text(encoding="utf-8") == rendered
    assert render_dashboard_json(_source()) == rendered


def test_july_16_returns_are_scaled_once_and_keep_unrealized_basis() -> None:
    payload = _payload()
    metrics = {row["label"]: row for row in payload["topMetrics"]}
    strategies = {row["id"]: row for row in payload["strategies"]}

    assert metrics["PaperOps Fleet"]["value"] == "-0.008466%"
    assert metrics["PaperOps Fleet"]["context"].startswith("-$59.26")
    assert metrics["Best Strategy"]["value"] == "+0.021323%"
    assert "unrealized" in metrics["Best Strategy"]["context"]
    assert strategies["ts_momentum_sma_atr"]["return"] == "+0.021323%"
    assert strategies["cross_sectional_relative_strength"]["return"] == "-0.034782%"
    assert strategies["donchian_breakout_20_10"]["return"] == "-0.045804%"
    assert all(row["winRate"] is None for row in payload["strategies"])


def test_latest_calendar_day_lists_all_nine_registered_strategies() -> None:
    payload = _payload()
    tiles = {row["date"]: row for row in payload["calendar"]["tiles"]}
    july_16 = tiles["2026-07-16"]
    returns = {row["id"]: row for row in july_16["strategyReturns"]}

    assert july_16["dailyReturn"] == "-0.008466%"
    assert july_16["activity"] == "5 opened · 0 closed"
    assert len(returns) == 9
    assert returns["ts_momentum_sma_atr"]["return"] == "+0.021323%"
    for strategy_id in ("gap_up_continuation", "gap_up_continuation_atr"):
        assert returns[strategy_id]["return"] is None
        assert returns[strategy_id]["pnl"] is None
        assert returns[strategy_id]["activity"] == "not yet eligible"
        assert returns[strategy_id]["status"] == "Registered · starts 2026-07-17"


def test_unobserved_dates_are_not_fabricated_as_no_trade_days() -> None:
    payload = _payload()
    tiles = {row["date"]: row for row in payload["calendar"]["tiles"]}

    assert tiles["2026-07-17"]["observed"] is False
    assert tiles["2026-07-17"]["noTrade"] is False
    assert tiles["2026-07-17"]["dailyReturn"] is None
    assert tiles["2026-07-17"]["tradeCount"] is None
    assert tiles["2026-07-15"]["observed"] is True
    assert tiles["2026-07-15"]["noTrade"] is True
    assert payload["calendar"]["summary"]["observedDays"] == 2
    assert payload["calendar"]["summary"]["noTradeDays"] == 1


def test_new_strategy_cards_are_pending_na_not_zero() -> None:
    strategies = {row["id"]: row for row in _payload()["strategies"]}

    for strategy_id in ("gap_up_continuation", "gap_up_continuation_atr"):
        row = strategies[strategy_id]
        assert row["status"] == "registered / not yet eligible"
        assert row["trades"] is None
        assert row["winRate"] is None
        assert row["return"] is None
        assert row["drawdown"] is None
        assert "2026-07-17" in row["validation"]


def test_builder_fails_closed_on_incomplete_latest_coverage() -> None:
    source = copy.deepcopy(_source())
    latest = source["paperOps"]["days"][-1]
    latest["strategies"] = latest["strategies"][:-1]

    with pytest.raises(StaticDashboardError, match="coverage mismatch"):
        build_dashboard_payload(source)


def test_builder_fails_closed_on_fleet_math_mismatch() -> None:
    source = copy.deepcopy(_source())
    latest = source["paperOps"]["days"][-1]
    latest["fleetDailyPnl"] = "0"

    with pytest.raises(StaticDashboardError, match="daily P&L disagrees"):
        build_dashboard_payload(source)


def test_restored_renderer_contains_truth_safe_interaction_hooks() -> None:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "assets" / "dashboard.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "assets" / "dashboard.css").read_text(encoding="utf-8")

    assert 'id="calendar-selected-strategies"' in html
    assert 'id="calendar-selected-note"' in html
    assert "strategyRows.forEach" in javascript
    assert 'tile.observed === false ? "not observed"' in javascript
    assert 'bar.classList.add("unavailable")' in javascript
    assert "Math.max(5" not in javascript
    assert 'deployment.className = "status-pill bad"' in javascript
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in stylesheet
    assert ".calendar-day small {\n    display: none;" in stylesheet
