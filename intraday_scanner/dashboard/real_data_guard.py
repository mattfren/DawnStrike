"""Guards that keep dashboard return displays tied to real persisted prices."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

NA = "n/a"
OUTCOME_NEEDED = "Outcome needed"
NOT_ENOUGH_HISTORY = "Not enough history"
NO_CURRENT_PRICE_SOURCE = "No current price source"

REAL = "REAL"
SCENARIO = "SCENARIO"
OPPORTUNITY = "OPPORTUNITY"
UNAVAILABLE = "UNAVAILABLE"
PENDING = "PENDING"


@dataclass(frozen=True)
class ReturnDisplay:
    label: str
    proof_flag: str
    source: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def parse_price(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    text = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def real_return_pct(entry_price: Any, exit_or_current_price: Any) -> float | None:
    entry = parse_price(entry_price)
    exit_price = parse_price(exit_or_current_price)
    if entry is None or exit_price is None or entry <= 0:
        return None
    return round(((exit_price - entry) / entry) * 100.0, 4)


def display_price(value: Any) -> str:
    price = parse_price(value)
    if price is None:
        return NA
    return f"${price:.4f}".rstrip("0").rstrip(".")


def display_return(value: Any) -> str:
    number = parse_price(value)
    if number is None:
        return NA
    sign = "+" if number > 0 else ""
    return f"{sign}{number:.2f}%"


def display_real_return(
    entry_price: Any,
    exit_or_current_price: Any,
    *,
    source: str,
) -> ReturnDisplay:
    value = real_return_pct(entry_price, exit_or_current_price)
    if value is None:
        return ReturnDisplay(NA, UNAVAILABLE, source or NO_CURRENT_PRICE_SOURCE)
    return ReturnDisplay(display_return(value), REAL, source)


def display_scenario_return(
    entry_price: Any,
    scenario_price: Any,
    *,
    source: str,
) -> ReturnDisplay:
    value = real_return_pct(entry_price, scenario_price)
    if value is None:
        return ReturnDisplay(NA, UNAVAILABLE, source)
    return ReturnDisplay(display_return(value), SCENARIO, source)


def display_opportunity_return(
    entry_price: Any,
    high_after_entry: Any,
    *,
    source: str,
) -> ReturnDisplay:
    value = real_return_pct(entry_price, high_after_entry)
    if value is None:
        return ReturnDisplay(NA, UNAVAILABLE, source)
    return ReturnDisplay(display_return(value), OPPORTUNITY, source)


def latest_outcome_price(outcome: dict[str, Any] | None) -> tuple[float | None, str]:
    """Return the latest sourced outcome price, excluding opportunity fields."""

    row = dict(outcome or {})
    for field in ("close_price", "lunch_price", "price_15m", "price_5m", "price_1m"):
        price = parse_price(row.get(field))
        if price is not None:
            return price, field
    return None, NO_CURRENT_PRICE_SOURCE


def safe_return_label(entry_price: Any, current_price: Any) -> str:
    return display_return(real_return_pct(entry_price, current_price))


def copied_current_price_is_unsafe(
    open_price: Any,
    current_price: Any,
    source: str,
) -> bool:
    open_number = parse_price(open_price)
    current_number = parse_price(current_price)
    if open_number is None or current_number is None:
        return False
    if source != NO_CURRENT_PRICE_SOURCE:
        return False
    return abs(open_number - current_number) < 0.000001
