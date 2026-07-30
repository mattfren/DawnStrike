"""Typed contracts for the public performance read model.

The contracts deliberately keep cohort identity and missing outcomes visible.
They are data contracts, not investment recommendations.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any


class Cohort(str, Enum):
    """Evidence populations that must never be blended in public reporting."""

    OFFICIAL_FORWARD_PAPER = "official_forward_paper"
    ALPHAOPS_RESEARCH = "alphaops_research"
    HISTORICAL_BACKTEST = "historical_backtest"
    SHADOW_CHALLENGER = "shadow_challenger"


class RecordStatus(str, Enum):
    REALIZED = "realized"
    UNREALIZED = "unrealized"
    NO_TRADE = "no_trade"
    MISSING_OUTCOME = "missing_outcome"
    QUARANTINED = "quarantined"


def stable_hash(value: Any) -> str:
    """Return a deterministic SHA-256 for JSON-compatible input."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def money_to_cents(value: Any) -> int | None:
    """Convert a dollar value to integer cents without silently turning missing into zero."""

    decimal = as_decimal(value)
    if decimal is None:
        return None
    return int((decimal * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def as_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def percentage_from_prices(entry_price: Any, exit_price: Any) -> float | None:
    entry = as_decimal(entry_price)
    exit = as_decimal(exit_price)
    if entry is None or exit is None or entry <= 0:
        return None
    return float(((exit - entry) / entry * Decimal("100")).quantize(Decimal("0.0001")))


def normalize_cohort(value: Any, *, default: Cohort) -> Cohort:
    text = str(value or "").strip().lower()
    aliases = {
        "official_telegram": Cohort.OFFICIAL_FORWARD_PAPER,
        "official_forward": Cohort.OFFICIAL_FORWARD_PAPER,
        "official_forward_paper": Cohort.OFFICIAL_FORWARD_PAPER,
        "research": Cohort.ALPHAOPS_RESEARCH,
        "alphaops": Cohort.ALPHAOPS_RESEARCH,
        "alphaops_research": Cohort.ALPHAOPS_RESEARCH,
        "backtest": Cohort.HISTORICAL_BACKTEST,
        "historical_backtest": Cohort.HISTORICAL_BACKTEST,
        "shadow": Cohort.SHADOW_CHALLENGER,
        "shadow_challenger": Cohort.SHADOW_CHALLENGER,
    }
    return aliases.get(text, default)


@dataclass(frozen=True, slots=True)
class PerformanceRow:
    """One auditable observation in exactly one cohort."""

    record_id: str
    market_date: str
    ticker: str
    cohort: Cohort
    strategy_id: str
    strategy_version: str
    signal_id: str | None
    rank: int | None
    record_status: RecordStatus
    entry_price: float | None
    exit_price: float | None
    quantity: float | None
    notional_cents: int | None
    gross_pnl_cents: int | None
    gross_return_pct: float | None
    fees_cents: int | None
    slippage_cents: int | None
    net_pnl_cents: int | None
    return_pct: float | None
    benchmark_return_pct: float | None
    excess_return_pct: float | None
    source_refs: tuple[str, ...]
    source_hash_sha256: str
    input_hash_sha256: str
    observed_at: str | None
    reconciled_at: str
    quarantine_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cohort"] = self.cohort.value
        payload["record_status"] = self.record_status.value
        payload["source_refs"] = list(self.source_refs)
        return payload


def safe_float(value: Any) -> float | None:
    parsed = as_decimal(value)
    if parsed is None:
        return None
    number = float(parsed)
    return number if math.isfinite(number) else None
