from datetime import date
from decimal import Decimal

import pytest

from intraday_scanner.dashboard.strategy_calendar_page import (
    _calendar_button_label,
    _kpi_strip,
    _month_calendar_html,
    _resolve_selected_day,
    _selected_day_panel_html,
    _selected_day_strategy_rows,
    _simple_return_text,
    _strategy_day_status,
    _strategy_matrix_html,
)


def test_month_calendar_uses_published_market_holidays() -> None:
    rendered = _month_calendar_html([], 2026, 7)

    assert "Independence Day Observed" in rendered
    assert '<div class="dsx-day-num">03</div>' in rendered
    assert "No retained evidence" in rendered


def test_strategy_matrix_keeps_daily_precision_and_missing_truth_distinct() -> None:
    alpha_key = "replay|alpha|v1|policy|alpha-fingerprint"
    beta_key = "replay|beta|v1|policy|beta-fingerprint"
    rows = [
        {
            "date": "2026-07-13",
            "series_key": alpha_key,
            "series_role": "official",
            "strategy_label": "Alpha",
            "strategy_version": "v1",
            "execution_policy_version": "policy",
            "daily_return_pct": Decimal("-0.00000548"),
        },
        {
            "date": "2026-07-14",
            "series_key": beta_key,
            "series_role": "official",
            "strategy_label": "Beta",
            "strategy_version": "v1",
            "execution_policy_version": "policy",
            "daily_return_pct": Decimal("0.001234"),
        },
    ]
    summaries = [
        {
            "series_key": alpha_key,
            "period_return": None,
            "net_pnl": None,
            "positive_days": 0,
            "negative_days": 1,
            "flat_days": 0,
            "missing_days": 1,
        },
        {
            "series_key": beta_key,
            "period_return": Decimal("0.001234"),
            "net_pnl": Decimal("1.234"),
            "positive_days": 1,
            "negative_days": 0,
            "flat_days": 0,
            "missing_days": 1,
        },
    ]

    rendered = _strategy_matrix_html(
        rows,
        summaries,
        "2026-07",
        show_references=False,
    )

    assert "-0.0005%" in rendered
    assert "+0.1234%" in rendered
    assert "N/A" in rendered
    assert "W/L/F/N/A" in rendered


def test_kpi_strip_does_not_turn_missing_exposure_into_zero() -> None:
    days = [
        {
            "fleet_daily_return": Decimal("0.01"),
            "benchmark_daily_return": Decimal("0.02"),
            "fleet_daily_pnl": Decimal("10"),
        }
    ]
    rows = [
        {
            "date": "2026-07-14",
            "series_role": "official",
            "drawdown_pct": Decimal("-0.01"),
            "trades_closed": 1,
            "exposure_pct": None,
        }
    ]

    rendered = _kpi_strip(days, rows)

    assert "Open exposure</span><strong>N/A</strong>" in rendered
    assert "Closed trades</span><strong>1</strong>" in rendered


def test_selected_day_defaults_to_latest_and_preserves_valid_click() -> None:
    retained = ["2026-07-13", "2026-07-14", "2026-07-15"]

    assert _resolve_selected_day(retained, None) == "2026-07-15"
    assert _resolve_selected_day(retained, "2026-07-13") == "2026-07-13"
    assert _resolve_selected_day(retained, "2026-06-30") == "2026-07-15"
    assert _resolve_selected_day([], "2026-07-15") is None


def test_calendar_button_label_keeps_small_returns_and_flat_distinct() -> None:
    small_loss = _calendar_button_label(
        date(2026, 7, 13),
        {"fleet_daily_return": Decimal("-0.00000548")},
        selected=True,
    )
    flat = _calendar_button_label(
        date(2026, 7, 14),
        {"fleet_daily_return": Decimal("0")},
        selected=False,
    )
    missing = _calendar_button_label(
        date(2026, 7, 15),
        {"fleet_daily_return": None},
        selected=False,
    )

    assert "✓" in small_loss
    assert "↓ -0.0005%" in small_loss
    assert "— Flat" in flat
    assert "N/A" in missing


def test_selected_day_cards_use_clicked_day_and_only_official_strategies() -> None:
    alpha_key = "replay|alpha|v1|policy|alpha"
    beta_key = "replay|beta|v1|policy|beta"
    benchmark_key = "replay|benchmark|v1|policy|benchmark"
    summaries = [
        {
            "series_key": alpha_key,
            "series_role": "official",
            "strategy_label": "Alpha",
        },
        {
            "series_key": beta_key,
            "series_role": "official",
            "strategy_label": "Beta",
        },
        {
            "series_key": benchmark_key,
            "series_role": "benchmark",
            "strategy_label": "Market Benchmark",
        },
    ]
    rows = [
        {
            "date": "2026-07-13",
            "series_key": alpha_key,
            "series_role": "official",
            "strategy_label": "Alpha",
            "daily_return_pct": Decimal("0.01234"),
            "total_pnl": Decimal("12.34"),
        },
        {
            "date": "2026-07-14",
            "series_key": alpha_key,
            "series_role": "official",
            "strategy_label": "Alpha",
            "daily_return_pct": Decimal("-0.5"),
            "total_pnl": Decimal("-500"),
        },
        {
            "date": "2026-07-13",
            "series_key": benchmark_key,
            "series_role": "benchmark",
            "strategy_label": "Market Benchmark",
            "daily_return_pct": Decimal("0.9"),
            "total_pnl": Decimal("900"),
        },
    ]

    selected = _selected_day_strategy_rows(rows, summaries, "2026-07-13")
    rendered = _selected_day_panel_html(
        {"date": "2026-07-13", "fleet_daily_return": Decimal("0.01234")},
        selected,
    )

    assert [row["strategy_label"] for row in selected] == ["Alpha", "Beta"]
    assert rendered.count('class="dsx-result dsx-result--') == 2
    assert "Monday, July 13, 2026" in rendered
    assert "+1.2340%" in rendered
    assert "+$12.34" in rendered
    assert "-50.0000%" not in rendered
    assert "Market Benchmark" not in rendered
    assert "Beta" in rendered
    assert "No verified return" in rendered
    assert "$0.00" not in rendered


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0.01"), "Gain"),
        (Decimal("-0.01"), "Loss"),
        (Decimal("0"), "Flat"),
        (None, "N/A"),
    ],
)
def test_strategy_day_status_uses_return_truth(value: object, expected: str) -> None:
    assert _strategy_day_status(value) == expected


def test_simple_return_text_is_percent_only_and_missing_aware() -> None:
    assert _simple_return_text(Decimal("0")) == "0.0000%"
    assert _simple_return_text(Decimal("0.00000548")) == "+0.0005%"
    assert _simple_return_text(None) == "N/A"
