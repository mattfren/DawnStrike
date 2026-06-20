"""US market-calendar helpers with static holiday and early-close fallback."""

from __future__ import annotations

from datetime import date, timedelta

US_MARKET_HOLIDAYS_2026 = {
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
}

US_MARKET_EARLY_CLOSES_2026 = {
    date(2026, 11, 27): "12:00",
    date(2026, 12, 24): "12:00",
}


def is_weekday_market_day(value: date) -> bool:
    return value.weekday() < 5


def is_market_holiday(value: date) -> bool:
    return value in US_MARKET_HOLIDAYS_2026


def is_market_day(value: date) -> bool:
    return is_weekday_market_day(value) and not is_market_holiday(value)


def early_close_time_ct(value: date) -> str | None:
    return US_MARKET_EARLY_CLOSES_2026.get(value)


def next_market_day(value: date) -> date:
    current = value
    while not is_market_day(current):
        current += timedelta(days=1)
    return current
