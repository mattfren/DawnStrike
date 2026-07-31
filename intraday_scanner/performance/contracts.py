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
    ALPHAOPS_SIGNAL_RESEARCH = "alphaops_signal_research"
    # Backward-compatible source-code name; the serialized identifier is the
    # directive's explicit alphaops_signal_research value.
    ALPHAOPS_RESEARCH = "alphaops_signal_research"
    HISTORICAL_BACKTEST = "historical_backtest"
    SHADOW_CHALLENGER = "shadow_challenger"


class RecordStatus(str, Enum):
    REALIZED = "realized"
    UNREALIZED = "unrealized"
    NO_TRADE = "no_trade"
    MISSING_OUTCOME = "missing_outcome"
    QUARANTINED = "quarantined"


class EvidenceState(str, Enum):
    COMPLETE = "complete"
    NO_TRADE = "no_trade"
    PENDING = "pending"
    DEGRADED = "degraded"
    MISSING = "missing"
    NOT_ELIGIBLE = "not_eligible"


PerformanceCohort = Cohort


@dataclass(frozen=True, slots=True)
class ReturnMethodology:
    """Versioned description of how a return is allowed to be calculated."""

    calculation_version: str
    execution_policy_version: str
    portfolio_return_basis: str
    price_basis: str
    fee_policy: str
    slippage_policy: str
    benchmark_policy: str
    timezone: str = "America/Chicago"


@dataclass(frozen=True, slots=True)
class EvidenceCoverage:
    eligible_count: int
    observed_count: int
    missing_count: int
    excluded_count: int
    coverage_pct: float | None


@dataclass(frozen=True, slots=True)
class BenchmarkPerformance:
    market_date: str
    symbol: str
    return_pct: float | None
    source_refs: tuple[str, ...]
    source_hash_sha256: str
    observed_at: str | None


@dataclass(frozen=True, slots=True)
class DailyPortfolioPerformance:
    market_date: str
    cohort: Cohort
    strategy_id: str
    strategy_version: str
    evidence_state: EvidenceState
    opening_equity_cents: int | None
    ending_equity_cents: int | None
    realized_pnl_cents: int | None
    unrealized_pnl_cents: int | None
    fees_cents: int | None
    slippage_cents: int | None
    net_pnl_cents: int | None
    daily_return_pct: float | None
    cumulative_return_pct: float | None
    benchmark_return_pct: float | None
    excess_return_pct: float | None
    drawdown_pct: float | None
    exposure_cents: int | None
    trade_count: int
    coverage: EvidenceCoverage
    methodology: ReturnMethodology
    source_refs: tuple[str, ...]
    input_hash_sha256: str
    generated_at: str


@dataclass(frozen=True, slots=True)
class CanonicalPerformanceSnapshot:
    schema_version: str
    generated_at: str
    as_of_market_date: str | None
    daily: tuple[DailyPortfolioPerformance, ...]
    trades: tuple[TradePerformance, ...]
    benchmarks: tuple[BenchmarkPerformance, ...]
    input_hash_sha256: str


@dataclass(frozen=True, slots=True)
class TradePerformance:
    record_id: str
    market_date: str
    ticker: str
    cohort: Cohort
    evidence_state: EvidenceState
    gross_pnl_cents: int | None
    fees_cents: int | None
    slippage_cents: int | None
    net_pnl_cents: int | None
    return_pct: float | None
    source_refs: tuple[str, ...]
    input_hash_sha256: str


@dataclass(frozen=True, slots=True)
class PublicSnapshotManifest:
    schema_version: str
    manifest_id: str
    source_sha: str | None
    build_id: str | None
    market_date: str
    generated_at: str
    status: str
    input_hash_sha256: str
    payload_sha256: str
    row_count: int
    byte_count: int
    coverage: EvidenceCoverage | None
    research_only: bool
    live_trading_enabled: bool


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
        "research": Cohort.ALPHAOPS_SIGNAL_RESEARCH,
        "alphaops": Cohort.ALPHAOPS_SIGNAL_RESEARCH,
        "alphaops_research": Cohort.ALPHAOPS_SIGNAL_RESEARCH,
        "alphaops_signal_research": Cohort.ALPHAOPS_SIGNAL_RESEARCH,
        "algorithm_selected": Cohort.ALPHAOPS_SIGNAL_RESEARCH,
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
    execution_policy_version: str = "unregistered-policy"
    trade_count: int = 1
    open_position_count: int = 0
    unrealized_pnl_cents: int | None = None
    record_type: str = "trade"

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
