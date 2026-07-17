"""Fail-closed US equities market-session calendar.

The AlphaOps scheduler needs a common calendar for NYSE- and Nasdaq-listed
equities.  The checked-in calendar is deliberately finite: dates outside the
published, cross-exchange coverage raise instead of silently becoming trading
days.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from intraday_scanner.errors import MarketCalendarCoverageError

MARKET_TIMEZONE = ZoneInfo("America/New_York")
CALENDAR_ID = "us-equities-xnys-xnas-2026-2028.v1"
CALENDAR_PUBLISHED_AS_OF = "2026-07-13"
CALENDAR_COVERAGE_START = date(2026, 1, 1)
CALENDAR_COVERAGE_END = date(2028, 12, 31)
CALENDAR_AUTHORITY = (
    "NYSE published 2026-2028 schedule; Nasdaq published 2026 parity verification"
)
CALENDAR_SOURCE_REFS = (
    "https://www.nyse.com/trade/hours-calendars",
    "https://www.nasdaq.com/market-activity/stock-market-holiday-schedule",
)

REGULAR_OPEN_ET = time(9, 30)
REGULAR_CLOSE_ET = time(16, 0)
EARLY_CLOSE_ET = time(13, 0)
FIRST_ELIGIBLE_ACTIVATION_POLICY = "first_eligible_session"
NEXT_SESSION_ACTIVATION_POLICY = "next_market_session_after_registration"

US_MARKET_HOLIDAYS_2026: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth National Independence Day",
    date(2026, 7, 3): "Independence Day observed",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
}

US_MARKET_EARLY_CLOSES_2026: dict[date, str] = {
    date(2026, 11, 27): "Day after Thanksgiving",
    date(2026, 12, 24): "Christmas Eve",
}

US_MARKET_HOLIDAYS_2027: dict[date, str] = {
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King, Jr. Day",
    date(2027, 2, 15): "Washington's Birthday",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth National Independence Day observed",
    date(2027, 7, 5): "Independence Day observed",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day observed",
}

US_MARKET_EARLY_CLOSES_2027: dict[date, str] = {
    date(2027, 11, 26): "Day after Thanksgiving",
}

US_MARKET_HOLIDAYS_2028: dict[date, str] = {
    # NYSE explicitly publishes no observed New Year's closure for Saturday,
    # January 1, 2028. Friday, December 31, 2027 remains a regular session.
    date(2028, 1, 17): "Martin Luther King, Jr. Day",
    date(2028, 2, 21): "Washington's Birthday",
    date(2028, 4, 14): "Good Friday",
    date(2028, 5, 29): "Memorial Day",
    date(2028, 6, 19): "Juneteenth National Independence Day",
    date(2028, 7, 4): "Independence Day",
    date(2028, 9, 4): "Labor Day",
    date(2028, 11, 23): "Thanksgiving Day",
    date(2028, 12, 25): "Christmas Day",
}

US_MARKET_EARLY_CLOSES_2028: dict[date, str] = {
    date(2028, 7, 3): "Day before Independence Day",
    date(2028, 11, 24): "Day after Thanksgiving",
}

US_MARKET_HOLIDAYS = {
    **US_MARKET_HOLIDAYS_2026,
    **US_MARKET_HOLIDAYS_2027,
    **US_MARKET_HOLIDAYS_2028,
}

US_MARKET_EARLY_CLOSES = {
    **US_MARKET_EARLY_CLOSES_2026,
    **US_MARKET_EARLY_CLOSES_2027,
    **US_MARKET_EARLY_CLOSES_2028,
}


class MarketSessionStatus(str, Enum):
    """Published scheduled state for a US equities session date."""

    OPEN = "open"
    EARLY_CLOSE = "early_close"
    CLOSED = "closed"


@dataclass(frozen=True)
class MarketSessionDecision:
    """Auditable scheduled-session decision shared by NYSE and Nasdaq flows."""

    market_date: str
    status: MarketSessionStatus
    reason: str
    open_time_et: str | None
    close_time_et: str | None
    calendar_id: str = CALENDAR_ID
    calendar_authority: str = CALENDAR_AUTHORITY
    calendar_published_as_of: str = CALENDAR_PUBLISHED_AS_OF
    coverage_start: str = CALENDAR_COVERAGE_START.isoformat()
    coverage_end: str = CALENDAR_COVERAGE_END.isoformat()
    source_refs: tuple[str, ...] = CALENDAR_SOURCE_REFS

    @property
    def is_trading_day(self) -> bool:
        return self.status in {MarketSessionStatus.OPEN, MarketSessionStatus.EARLY_CLOSE}

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["is_trading_day"] = self.is_trading_day
        payload["source_refs"] = list(self.source_refs)
        return payload


def market_session(value: date) -> MarketSessionDecision:
    """Return a published session decision or fail outside calendar coverage."""

    if not CALENDAR_COVERAGE_START <= value <= CALENDAR_COVERAGE_END:
        raise MarketCalendarCoverageError(
            f"US equities calendar {CALENDAR_ID} covers "
            f"{CALENDAR_COVERAGE_START.isoformat()} through "
            f"{CALENDAR_COVERAGE_END.isoformat()}; requested {value.isoformat()}"
        )
    if value.weekday() >= 5:
        return _decision(value, MarketSessionStatus.CLOSED, "weekend")
    holiday = US_MARKET_HOLIDAYS.get(value)
    if holiday:
        return _decision(value, MarketSessionStatus.CLOSED, holiday)
    early_close = US_MARKET_EARLY_CLOSES.get(value)
    if early_close:
        return _decision(
            value,
            MarketSessionStatus.EARLY_CLOSE,
            early_close,
            close_time=EARLY_CLOSE_ET,
        )
    return _decision(value, MarketSessionStatus.OPEN, "regular_session")


def session_for_timestamp(value: datetime | None = None) -> MarketSessionDecision:
    """Resolve a timestamp to its New York market date and scheduled session."""

    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("market-session timestamps must include a timezone")
    return market_session(current.astimezone(MARKET_TIMEZONE).date())


def core_session_phase(value: datetime | None = None) -> str:
    """Classify a timestamp relative to the scheduled core equities session."""

    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("market-session timestamps must include a timezone")
    local = current.astimezone(MARKET_TIMEZONE)
    decision = market_session(local.date())
    if not decision.is_trading_day:
        return "market_closed"
    close_time = (
        EARLY_CLOSE_ET
        if decision.status == MarketSessionStatus.EARLY_CLOSE
        else REGULAR_CLOSE_ET
    )
    if local.time() < REGULAR_OPEN_ET:
        return "before_core_session"
    if local.time() >= close_time:
        return "after_core_session"
    return "core_session_open"


def is_weekday_market_day(value: date) -> bool:
    """Compatibility helper; coverage is still enforced."""

    _require_covered(value)
    return value.weekday() < 5


def is_market_holiday(value: date) -> bool:
    """Return whether the covered date is an exchange holiday."""

    _require_covered(value)
    return value in US_MARKET_HOLIDAYS


def is_market_day(value: date) -> bool:
    """Return whether a covered date has a scheduled core session."""

    return market_session(value).is_trading_day


def early_close_time_ct(value: date) -> str | None:
    """Return the scheduled Central Time close for a covered early-close date."""

    decision = market_session(value)
    return "12:00" if decision.status == MarketSessionStatus.EARLY_CLOSE else None


def next_market_day(value: date) -> date:
    """Return ``value`` or the next covered scheduled trading day."""

    current = value
    while not market_session(current).is_trading_day:
        current += timedelta(days=1)
    return current


def first_eligible_session_date(registered_at: datetime) -> date:
    """Return the first after-close run date available to a registration.

    A registration at or before a session's scheduled close can participate in
    that session's after-close run. A registration after the close, or on a
    closed day, becomes eligible on the next published market session.
    """

    if registered_at.tzinfo is None or registered_at.utcoffset() is None:
        raise ValueError("strategy registration timestamps must include a timezone")
    local = registered_at.astimezone(MARKET_TIMEZONE)
    decision = market_session(local.date())
    if decision.is_trading_day and decision.close_time_et is not None:
        scheduled_close = time.fromisoformat(decision.close_time_et)
        if local.time() <= scheduled_close:
            return local.date()
    return next_market_day(local.date() + timedelta(days=1))


def next_session_after_registration(registered_at: datetime) -> date:
    """Return the first covered market session strictly after registration day.

    New immutable strategy and execution-policy identities use this conservative
    activation boundary.  Waiting until the next session prevents a catalog
    write that straddles the scheduled close from being treated as if it had
    been durably active for that just-completed session.
    """

    if registered_at.tzinfo is None or registered_at.utcoffset() is None:
        raise ValueError("strategy registration timestamps must include a timezone")
    local_date = registered_at.astimezone(MARKET_TIMEZONE).date()
    return next_market_day(local_date + timedelta(days=1))


def registration_coverage_inception_date(
    registered_at: datetime,
    activation_policy: str | None = None,
) -> date:
    """Resolve an immutable registration's declared forward-coverage boundary."""

    policy = (activation_policy or FIRST_ELIGIBLE_ACTIVATION_POLICY).strip()
    if policy == FIRST_ELIGIBLE_ACTIVATION_POLICY:
        return first_eligible_session_date(registered_at)
    if policy == NEXT_SESSION_ACTIVATION_POLICY:
        return next_session_after_registration(registered_at)
    raise ValueError(f"unsupported registration activation policy: {policy or '<blank>'}")


def _decision(
    value: date,
    status: MarketSessionStatus,
    reason: str,
    *,
    close_time: time | None = None,
) -> MarketSessionDecision:
    is_open = status in {MarketSessionStatus.OPEN, MarketSessionStatus.EARLY_CLOSE}
    return MarketSessionDecision(
        market_date=value.isoformat(),
        status=status,
        reason=reason,
        open_time_et=REGULAR_OPEN_ET.strftime("%H:%M") if is_open else None,
        close_time_et=(close_time or REGULAR_CLOSE_ET).strftime("%H:%M") if is_open else None,
    )


def _require_covered(value: date) -> None:
    if not CALENDAR_COVERAGE_START <= value <= CALENDAR_COVERAGE_END:
        market_session(value)


def main(argv: Sequence[str] | None = None) -> int:
    """Expose the calendar gate for scheduled shell entrypoints."""

    parser = argparse.ArgumentParser(description="Check the US equities session calendar")
    parser.add_argument("--date", required=True, help="Market date in YYYY-MM-DD format")
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        value = date.fromisoformat(args.date)
        decision = market_session(value)
    except (ValueError, MarketCalendarCoverageError) as exc:
        print(json.dumps({"status": "calendar_unavailable", "error": str(exc)}, sort_keys=True))
        return 11
    print(json.dumps(decision.to_dict(), sort_keys=True))
    return 0 if decision.is_trading_day else 10


if __name__ == "__main__":
    raise SystemExit(main())
