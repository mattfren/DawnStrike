"""Timezone helpers for operator-facing automation dates."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_OPERATOR_TIMEZONE = "America/Chicago"


def get_operator_now(
    timezone: str = DEFAULT_OPERATOR_TIMEZONE,
    *,
    now: datetime | None = None,
) -> datetime:
    zone = _zone(timezone)
    current = now or datetime.now(tz=ZoneInfo("UTC"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("UTC"))
    return current.astimezone(zone)


def get_operator_date(
    timezone: str = DEFAULT_OPERATOR_TIMEZONE,
    *,
    now: datetime | None = None,
) -> str:
    return get_operator_now(timezone, now=now).date().isoformat()


def get_market_date(
    timezone: str = DEFAULT_OPERATOR_TIMEZONE,
    *,
    now: datetime | None = None,
) -> str:
    return get_operator_date(timezone, now=now)


def get_operator_time_label(
    timezone: str = DEFAULT_OPERATOR_TIMEZONE,
    *,
    now: datetime | None = None,
) -> str:
    local = get_operator_now(timezone, now=now)
    hour = local.strftime("%I").lstrip("0") or "0"
    return f"{hour}:{local.strftime('%M')} CT"


def _zone(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone or DEFAULT_OPERATOR_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_OPERATOR_TIMEZONE)
