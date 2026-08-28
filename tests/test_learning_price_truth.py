from __future__ import annotations

import math

import pytest

from intraday_scanner.alpha.outcome_labeler import label_outcome
from intraday_scanner.dashboard.components import display_pick_from_raw, main_pick_card
from intraday_scanner.services.alpha_cycle_service import _positive_finite_price
from intraday_scanner.services.return_attribution_service import record_monitor_signal_events


class _EventStore:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def load_historical_signals(self, **_kwargs: object) -> list[dict[str, object]]:
        return [{"signal_id": "signal-1", "scan_id": "scan-1", "ticker": "NOVA"}]

    def persist_signal_events(self, rows: list[dict[str, object]]) -> dict[str, int]:
        self.events.extend(rows)
        return {"inserted": len(rows), "skipped": 0}


@pytest.mark.parametrize("invalid", (0.0, -1.0, math.nan, math.inf, -math.inf))
def test_monitor_event_invalid_primary_price_cannot_fall_through(invalid: float) -> None:
    store = _EventStore()

    record_monitor_signal_events(
        store,
        signals=[{"scan_id": "scan-1", "ticker": "NOVA"}],
        monitor_events=[
            {
                "ticker": "NOVA",
                "current_price": invalid,
                "exit_price": 9.0,
                "price": 8.0,
                "label": "EXIT SIGNAL",
            }
        ],
    )

    assert store.events[0]["event_price"] is None


@pytest.mark.parametrize("missing", (None, ""))
def test_monitor_event_blank_primary_price_allows_lower_alias(missing: object) -> None:
    store = _EventStore()
    record_monitor_signal_events(
        store,
        signals=[{"scan_id": "scan-1", "ticker": "NOVA"}],
        monitor_events=[
            {"ticker": "NOVA", "current_price": missing, "exit_price": 9.0}
        ],
    )

    assert store.events[0]["event_price"] == 9.0


def _complete_outcome(**overrides: object) -> dict[str, object]:
    outcome: dict[str, object] = {
        "ticker": "NOVA",
        "entry_price": 10.0,
        "high_after_entry": 11.0,
        "low_after_entry": 9.5,
        "close_price": 10.5,
        "price_1m": 10.2,
        "price_5m": 10.4,
        "price_15m": 10.6,
        "lunch_price": 10.8,
        "source": "test-source",
    }
    outcome.update(overrides)
    return outcome


@pytest.mark.parametrize("invalid", (0.0, -1.0, math.nan, math.inf, -math.inf))
def test_outcome_labeler_invalid_primary_entry_is_not_masked(invalid: float) -> None:
    label = label_outcome(
        {"signal_id": "signal-1", "ticker": "NOVA", "entry_trigger": 9.0},
        _complete_outcome(entry=invalid, entry_price=10.0),
    )

    assert label["entry_price"] is None
    assert label["learning_eligible"] is False
    assert label["close_return_pct"] is None


@pytest.mark.parametrize("invalid", (0.0, -1.0, math.nan, math.inf, -math.inf))
@pytest.mark.parametrize(
    "field",
    ("high", "low", "close", "price_1m", "price_5m", "price_15m", "lunch"),
)
def test_outcome_labeler_invalid_primary_exit_or_interval_is_not_masked(
    field: str, invalid: float
) -> None:
    aliases = {
        "high": "high_after_entry",
        "low": "low_after_entry",
        "close": "close_price",
        "price_1m": "one_minute",
        "price_5m": "five_minute",
        "price_15m": "fifteen_minute",
        "lunch": "lunch_price",
    }
    overrides = {field: invalid, aliases[field]: 10.9}
    label = label_outcome(
        {"signal_id": "signal-1", "ticker": "NOVA", "entry_trigger": 10.0},
        _complete_outcome(**overrides),
    )

    assert label["learning_eligible"] is False
    if field in {"high", "low", "close"}:
        if field == "high":
            assert label["high_after_entry_return"] is None
        elif field == "low":
            assert label["low_after_entry_drawdown"] is None
        else:
            assert label["close_return_pct"] is None
    else:
        winner_key = {
            "price_1m": "winner_1m",
            "price_5m": "winner_5m",
            "price_15m": "winner_15m",
            "lunch": "winner_lunch",
        }[field]
        assert label[winner_key] is None


def test_outcome_labeler_missing_primary_alias_uses_positive_lower_alias() -> None:
    label = label_outcome(
        {"signal_id": "signal-1", "ticker": "NOVA", "entry_trigger": 9.0},
        _complete_outcome(entry=None, entry_price=10.0, close=None, close_price=10.5),
    )

    assert label["entry_price"] == 10.0
    assert label["close_return_pct"] == 5.0
    assert label["learning_eligible"] is True


@pytest.mark.parametrize("invalid", (0.0, -1.0, math.nan, math.inf))
def test_alpha_cycle_current_price_map_rejects_invalid_prices(invalid: float) -> None:
    assert _positive_finite_price(invalid) is None
    assert _positive_finite_price(10.0) == 10.0


@pytest.mark.parametrize("invalid", (0.0, -1.0, math.nan, math.inf, -math.inf, "bad"))
def test_dashboard_does_not_display_lower_alias_when_primary_price_invalid(invalid: object) -> None:
    pick = display_pick_from_raw(
        {"ticker": "NOVA", "premarket_price": invalid, "current_price": 10.0}
    )

    assert pick["price"] is None
    assert "Pending" in main_pick_card(pick)
    assert "$10" not in main_pick_card(pick)


def test_dashboard_missing_primary_price_uses_positive_lower_alias() -> None:
    pick = display_pick_from_raw({"ticker": "NOVA", "current_price": 10.0})
    assert pick["price"] == 10.0
