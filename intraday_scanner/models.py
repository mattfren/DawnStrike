"""Canonical data models and validation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.errors import SnapshotValidationError

SNAPSHOT_COLUMNS = [
    "ticker",
    "company",
    "previous_close",
    "premarket_price",
    "premarket_high",
    "premarket_low",
    "premarket_volume",
    "dollar_volume",
    "gap_pct",
    "float_shares",
    "market_cap",
    "spread_pct",
    "short_float_pct",
    "has_news",
    "catalyst_headline",
    "catalyst_url",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
    "source",
    "as_of_timestamp",
    "data_source_kind",
    "shadow_mode",
    "paid_data",
    "fixture_only",
    "manual_uploaded_data",
    "data_quality_score",
    "coverage_warning",
    "missing_enrichment_count",
    "raw_file_path",
    "imported_at",
]

SNAPSHOT_REQUIRED_COLUMNS = [
    "ticker",
    "company",
    "previous_close",
    "premarket_price",
    "premarket_high",
    "premarket_low",
    "premarket_volume",
    "dollar_volume",
    "gap_pct",
    "float_shares",
    "market_cap",
    "spread_pct",
    "short_float_pct",
    "has_news",
    "catalyst_headline",
    "catalyst_url",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
    "source",
    "as_of_timestamp",
]

CANDIDATE_COLUMNS = [
    "rank",
    "ticker",
    "company",
    "score",
    "gap_pct",
    "dollar_volume",
    "float_rotation_pct",
    "range_position_pct",
    "data_quality_score",
    "liquidity_tier",
    "setup_grade",
    "volatility_signature",
    "equation_version",
    "premarket_price",
    "previous_close",
    "premarket_high",
    "premarket_low",
    "premarket_volume",
    "catalyst_headline",
    "catalyst_url",
    "breakout_trigger",
    "pullback_zone",
    "invalidation_level",
    "first_target",
    "stretch_target",
    "risk_flags",
    "best_exit_bias",
    "action",
    "classification",
    "predicted_action",
    "catalyst_tier",
    "catalyst_summary",
    "catalyst_confidence",
    "catalyst_risk_flags",
    "premarket_structure",
    "structure_notes",
    "float_rotation",
    "float_rotation_label",
    "entry_trigger",
    "confirmation_needed",
    "invalidation",
    "target_1",
    "target_2",
    "risk_level",
    "why_this_matters",
    "do_not_buy_if",
    "data_confidence_score",
    "data_warnings",
    "field_sources",
    "historical_win_rate",
    "average_max_gain",
    "average_drawdown",
    "similar_setup_count",
    "probability_note",
    "score_breakdown",
    "avoid_reasons",
    "source",
    "as_of_timestamp",
    "data_source_kind",
    "shadow_mode",
    "paid_data",
    "fixture_only",
    "manual_uploaded_data",
    "coverage_warning",
    "missing_enrichment_count",
    "raw_file_path",
    "imported_at",
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"true", "t", "1", "yes", "y"}:
        return True
    if normalized in {"false", "f", "0", "no", "n", ""}:
        return False
    raise SnapshotValidationError(f"Cannot parse boolean value {value!r}")


def parse_float(value: Any, column: str, *, default: float | None = None) -> float:
    if value is None or value == "":
        if default is not None:
            return default
        raise SnapshotValidationError(f"{column} is required")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"{column} must be numeric, got {value!r}") from exc


def parse_int(value: Any, column: str, *, default: int | None = None) -> int:
    if value is None or value == "":
        if default is not None:
            return default
        raise SnapshotValidationError(f"{column} is required")
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(f"{column} must be an integer, got {value!r}") from exc


def validate_required_columns(columns: set[str], required: list[str], source: str) -> None:
    missing = [column for column in required if column not in columns]
    if missing:
        raise SnapshotValidationError(
            f"{source} is missing required column(s): {', '.join(missing)}"
        )


def _gap_pct(price: float, previous_close: float) -> float:
    if previous_close <= 0:
        return 0.0
    return ((price - previous_close) / previous_close) * 100


@dataclass(frozen=True)
class SnapshotRow:
    ticker: str
    company: str
    premarket_price: float
    previous_close: float
    premarket_high: float
    premarket_low: float
    premarket_volume: int
    float_shares: float | None
    market_cap: float | None
    spread_pct: float
    short_float_pct: float | None
    has_news: bool
    current_halt: bool
    recent_offering: bool
    reverse_split_90d: bool
    source: str
    as_of_timestamp: str
    dollar_volume: float = 0.0
    gap_pct: float = 0.0
    catalyst_headline: str = ""
    catalyst_url: str = ""
    data_source_kind: str = ""
    shadow_mode: bool = False
    paid_data: bool = False
    fixture_only: bool = False
    manual_uploaded_data: bool = False
    coverage_warning: str = ""
    missing_enrichment_count: int = 0
    raw_file_path: str = ""
    imported_at: str = ""

    @classmethod
    def from_mapping(cls, row: dict[str, Any], source: str = "snapshot") -> SnapshotRow:
        validate_required_columns(set(row), SNAPSHOT_REQUIRED_COLUMNS, source)
        ticker = str(row["ticker"]).strip().upper()
        if not ticker:
            raise SnapshotValidationError("ticker is required")
        premarket_price = parse_float(row.get("premarket_price"), "premarket_price")
        previous_close = parse_float(row.get("previous_close"), "previous_close", default=0.0)
        premarket_volume = parse_int(row.get("premarket_volume"), "premarket_volume")
        dollar_volume = (
            parse_float(row.get("dollar_volume"), "dollar_volume")
            if row.get("dollar_volume") not in {None, ""}
            else premarket_price * premarket_volume
        )
        gap_pct = (
            parse_float(row.get("gap_pct"), "gap_pct")
            if row.get("gap_pct") not in {None, ""}
            else _gap_pct(premarket_price, previous_close)
        )
        snapshot = cls(
            ticker=ticker,
            company=str(row.get("company") or ticker).strip(),
            premarket_price=premarket_price,
            previous_close=previous_close,
            premarket_high=parse_float(row.get("premarket_high"), "premarket_high"),
            premarket_low=parse_float(row.get("premarket_low"), "premarket_low"),
            premarket_volume=premarket_volume,
            float_shares=(
                None
                if row.get("float_shares") in {None, ""}
                else parse_float(row.get("float_shares"), "float_shares")
            ),
            market_cap=(
                None
                if row.get("market_cap") in {None, ""}
                else parse_float(row.get("market_cap"), "market_cap")
            ),
            spread_pct=parse_float(row.get("spread_pct"), "spread_pct", default=0.0),
            short_float_pct=(
                None
                if row.get("short_float_pct") in {None, ""}
                else parse_float(row.get("short_float_pct"), "short_float_pct")
            ),
            has_news=parse_bool(row.get("has_news")),
            current_halt=parse_bool(row.get("current_halt")),
            recent_offering=parse_bool(row.get("recent_offering")),
            reverse_split_90d=parse_bool(row.get("reverse_split_90d")),
            source=str(row.get("source") or "unknown").strip(),
            as_of_timestamp=str(row.get("as_of_timestamp") or utc_now_iso()).strip(),
            dollar_volume=dollar_volume,
            gap_pct=gap_pct,
            catalyst_headline=str(row.get("catalyst_headline") or "").strip(),
            catalyst_url=str(row.get("catalyst_url") or "").strip(),
            data_source_kind=str(row.get("data_source_kind") or "").strip(),
            shadow_mode=parse_bool(row.get("shadow_mode")),
            paid_data=parse_bool(row.get("paid_data")),
            fixture_only=parse_bool(row.get("fixture_only")),
            manual_uploaded_data=parse_bool(row.get("manual_uploaded_data")),
            coverage_warning=str(row.get("coverage_warning") or "").strip(),
            missing_enrichment_count=parse_int(
                row.get("missing_enrichment_count"), "missing_enrichment_count", default=0
            ),
            raw_file_path=str(row.get("raw_file_path") or "").strip(),
            imported_at=str(row.get("imported_at") or "").strip(),
        )
        snapshot.validate()
        return snapshot

    def validate(self) -> None:
        if self.premarket_price < 0:
            raise SnapshotValidationError(f"{self.ticker}: premarket_price must be non-negative")
        if self.previous_close < 0:
            raise SnapshotValidationError(f"{self.ticker}: previous_close must be non-negative")
        if self.premarket_high < 0 or self.premarket_low < 0:
            raise SnapshotValidationError(f"{self.ticker}: premarket high/low must be non-negative")
        if self.premarket_high < self.premarket_low:
            raise SnapshotValidationError(
                f"{self.ticker}: premarket_high cannot be below premarket_low"
            )
        if self.premarket_volume < 0:
            raise SnapshotValidationError(f"{self.ticker}: premarket_volume must be non-negative")
        if self.spread_pct < 0:
            raise SnapshotValidationError(f"{self.ticker}: spread_pct must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "company": self.company,
            "previous_close": self.previous_close,
            "premarket_price": self.premarket_price,
            "premarket_high": self.premarket_high,
            "premarket_low": self.premarket_low,
            "premarket_volume": self.premarket_volume,
            "dollar_volume": self.dollar_volume,
            "gap_pct": self.gap_pct,
            "float_shares": self.float_shares,
            "market_cap": self.market_cap,
            "spread_pct": self.spread_pct,
            "short_float_pct": self.short_float_pct,
            "has_news": self.has_news,
            "catalyst_headline": self.catalyst_headline,
            "catalyst_url": self.catalyst_url,
            "current_halt": self.current_halt,
            "recent_offering": self.recent_offering,
            "reverse_split_90d": self.reverse_split_90d,
            "source": self.source,
            "as_of_timestamp": self.as_of_timestamp,
            "data_source_kind": self.data_source_kind,
            "shadow_mode": self.shadow_mode,
            "paid_data": self.paid_data,
            "fixture_only": self.fixture_only,
            "manual_uploaded_data": self.manual_uploaded_data,
            "coverage_warning": self.coverage_warning,
            "missing_enrichment_count": self.missing_enrichment_count,
            "raw_file_path": self.raw_file_path,
            "imported_at": self.imported_at,
        }


@dataclass(frozen=True)
class ScoredCandidate:
    rank: int
    snapshot: SnapshotRow
    score: float
    gap_pct: float
    dollar_volume: float
    float_rotation_pct: float
    range_position_pct: float
    data_quality_score: float
    liquidity_tier: str
    setup_grade: str
    volatility_signature: str
    equation_version: str
    breakout_trigger: float
    pullback_zone: str
    invalidation_level: float
    first_target: float
    stretch_target: float
    risk_flags: list[str]
    best_exit_bias: str
    score_breakdown: dict[str, float]
    is_avoid: bool
    avoid_reasons: list[str] = field(default_factory=list)
    intelligence: dict[str, Any] = field(default_factory=dict)

    @property
    def ticker(self) -> str:
        return self.snapshot.ticker

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "rank": self.rank,
            "ticker": self.snapshot.ticker,
            "company": self.snapshot.company,
            "score": self.score,
            "gap_pct": self.gap_pct,
            "dollar_volume": self.dollar_volume,
            "float_rotation_pct": self.float_rotation_pct,
            "range_position_pct": self.range_position_pct,
            "data_quality_score": self.data_quality_score,
            "liquidity_tier": self.liquidity_tier,
            "setup_grade": self.setup_grade,
            "volatility_signature": self.volatility_signature,
            "equation_version": self.equation_version,
            "premarket_price": self.snapshot.premarket_price,
            "previous_close": self.snapshot.previous_close,
            "premarket_high": self.snapshot.premarket_high,
            "premarket_low": self.snapshot.premarket_low,
            "premarket_volume": self.snapshot.premarket_volume,
            "catalyst_headline": self.snapshot.catalyst_headline,
            "catalyst_url": self.snapshot.catalyst_url,
            "breakout_trigger": self.breakout_trigger,
            "pullback_zone": self.pullback_zone,
            "invalidation_level": self.invalidation_level,
            "first_target": self.first_target,
            "stretch_target": self.stretch_target,
            "risk_flags": ";".join(self.risk_flags),
            "best_exit_bias": self.best_exit_bias,
            "score_breakdown": json.dumps(self.score_breakdown, sort_keys=True),
            "avoid_reasons": ";".join(self.avoid_reasons),
            "source": self.snapshot.source,
            "as_of_timestamp": self.snapshot.as_of_timestamp,
            "data_source_kind": self.snapshot.data_source_kind,
            "shadow_mode": self.snapshot.shadow_mode,
            "paid_data": self.snapshot.paid_data,
            "fixture_only": self.snapshot.fixture_only,
            "manual_uploaded_data": self.snapshot.manual_uploaded_data,
            "coverage_warning": self.snapshot.coverage_warning,
            "missing_enrichment_count": self.snapshot.missing_enrichment_count,
            "raw_file_path": self.snapshot.raw_file_path,
            "imported_at": self.snapshot.imported_at,
        }
        payload.update(self.intelligence)
        return payload


@dataclass(frozen=True)
class ScanResult:
    run_id: str
    created_at: str
    all_candidates: list[ScoredCandidate]
    ranked_candidates: list[ScoredCandidate]
    top_explosive: list[ScoredCandidate]
    avoid_list: list[ScoredCandidate]
    config: dict[str, Any]

    def summary(self) -> dict[str, Any]:
        summary = {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "candidate_count": len(self.all_candidates),
            "ranked_count": len(self.ranked_candidates),
            "top_explosive_count": len(self.top_explosive),
            "avoid_count": len(self.avoid_list),
            "top_ticker": self.ranked_candidates[0].ticker if self.ranked_candidates else None,
        }
        for key in (
            "data_source_kind",
            "shadow_mode",
            "paid_data",
            "fixture_only",
            "manual_uploaded_data",
        ):
            if key in self.config:
                summary[key] = self.config[key]
        return summary
