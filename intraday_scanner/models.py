"""Canonical data models and validation helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.errors import SnapshotValidationError

EVIDENCE_CONFIDENCE_VERSION = "evidence-confidence-v1"

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
    "catalyst_summary",
    "catalyst_tier",
    "catalyst_category",
    "catalyst_confidence",
    "catalyst_status",
    "catalyst_risk_flags",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
    "halt_status",
    "sec_risk_status",
    "corporate_action_status",
    "source_quality_status",
    "source",
    "source_url",
    "extraction_mode",
    "source_timestamp",
    "extracted_at",
    "stale_data_flag",
    "source_confidence",
    "field_completeness_score",
    "source_reliability_prior",
    "reconciliation_status",
    "reconciliation_confidence_score",
    "evidence_confidence_version",
    "source_count",
    "score_consensus",
    "conflict_flags",
    "preferred_source",
    "row_merge_reason",
    "discovery_context",
    "universe_lane",
    "core_universe_memberships",
    "core_lane_score",
    "core_lane_eligible",
    "as_of_timestamp",
    "data_source_kind",
    "shadow_mode",
    "paid_data",
    "fixture_only",
    "manual_uploaded_data",
    "data_quality_score",
    "coverage_warning",
    "missing_enrichment_count",
    "premarket_range_source",
    "premarket_range_source_url",
    "premarket_price_source",
    "previous_close_source",
    "premarket_high_source",
    "premarket_low_source",
    "premarket_volume_source",
    "gap_pct_source",
    "dollar_volume_source",
    "enrichment_status",
    "enrichment_primary_source",
    "enrichment_fallback_status",
    "enrichment_fallback_source",
    "enrichment_was_fallback",
    "enrichment_observed_at",
    "enrichment_bar_completed_at",
    "enrichment_is_complete",
    "enrichment_observation_sha256",
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
    "total_score",
    "score",
    "explosive_score",
    "tradability_score",
    "catalyst_score",
    "risk_score",
    "gap_pct",
    "dollar_volume",
    "float_rotation_pct",
    "range_position_pct",
    "data_quality_score",
    "liquidity_tier",
    "setup_grade",
    "expected_return_bucket",
    "confidence_bucket",
    "volatility_signature",
    "equation_version",
    "model_version",
    "config_hash",
    "premarket_price",
    "previous_close",
    "premarket_high",
    "premarket_low",
    "premarket_volume",
    "float_shares",
    "market_cap",
    "spread_pct",
    "short_float_pct",
    "has_news",
    "current_halt",
    "recent_offering",
    "reverse_split_90d",
    "catalyst_headline",
    "catalyst_url",
    "breakout_trigger",
    "pullback_zone",
    "invalidation_level",
    "first_target",
    "stretch_target",
    "target_basis_kind",
    "target_basis_value",
    "target_basis_source",
    "target_basis_observed_high",
    "target_basis_observed_low",
    "target_basis_range",
    "target_basis_extension",
    "target_policy_version",
    "target_derived_from_risk",
    "risk_flags",
    "exit_bias",
    "best_exit_bias",
    "action",
    "classification",
    "predicted_action",
    "catalyst_tier",
    "catalyst_category",
    "catalyst_summary",
    "catalyst_confidence",
    "catalyst_status",
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
    "do_not_enter_if",
    "data_confidence_score",
    "data_warnings",
    "field_sources",
    "historical_win_rate",
    "average_max_gain",
    "average_drawdown",
    "similar_setup_count",
    "probability_note",
    "sample_size",
    "uncertainty_bucket",
    "score_breakdown",
    "avoid_reasons",
    "source_lineage",
    "source",
    "source_url",
    "extraction_mode",
    "source_timestamp",
    "extracted_at",
    "stale_data_flag",
    "source_confidence",
    "field_completeness_score",
    "source_reliability_prior",
    "reconciliation_status",
    "reconciliation_confidence_score",
    "evidence_confidence_version",
    "source_count",
    "score_consensus",
    "conflict_flags",
    "preferred_source",
    "row_merge_reason",
    "discovery_context",
    "universe_lane",
    "core_universe_memberships",
    "core_lane_score",
    "core_lane_eligible",
    "halt_status",
    "sec_risk_status",
    "corporate_action_status",
    "source_quality_status",
    "as_of_timestamp",
    "data_source_kind",
    "shadow_mode",
    "paid_data",
    "fixture_only",
    "manual_uploaded_data",
    "coverage_warning",
    "missing_enrichment_count",
    "premarket_range_source",
    "premarket_range_source_url",
    "premarket_price_source",
    "previous_close_source",
    "premarket_high_source",
    "premarket_low_source",
    "premarket_volume_source",
    "gap_pct_source",
    "dollar_volume_source",
    "enrichment_status",
    "enrichment_primary_source",
    "enrichment_fallback_status",
    "enrichment_fallback_source",
    "enrichment_was_fallback",
    "enrichment_observed_at",
    "enrichment_bar_completed_at",
    "enrichment_is_complete",
    "enrichment_observation_sha256",
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
    source_url: str = ""
    extraction_mode: str = ""
    source_timestamp: str = ""
    extracted_at: str = ""
    stale_data_flag: bool = False
    source_confidence: float = 0.0
    field_completeness_score: float | None = None
    source_reliability_prior: float | None = None
    reconciliation_status: str = ""
    reconciliation_confidence_score: float | None = None
    evidence_confidence_version: str = ""
    source_count: int = 1
    score_consensus: str = "single_source"
    conflict_flags: str = ""
    preferred_source: str = ""
    row_merge_reason: str = "single_source"
    dollar_volume: float = 0.0
    gap_pct: float = 0.0
    catalyst_headline: str = ""
    catalyst_url: str = ""
    catalyst_summary: str = ""
    catalyst_tier: str = ""
    catalyst_category: str = ""
    catalyst_confidence: float | None = None
    catalyst_status: str = ""
    catalyst_risk_flags: str = ""
    halt_status: str = ""
    sec_risk_status: str = ""
    corporate_action_status: str = ""
    source_quality_status: str = ""
    discovery_context: str = ""
    universe_lane: str = "mover"
    core_universe_memberships: str = ""
    core_lane_score: float | None = None
    core_lane_eligible: bool = False
    data_source_kind: str = ""
    shadow_mode: bool = False
    paid_data: bool = False
    fixture_only: bool = False
    manual_uploaded_data: bool = False
    coverage_warning: str = ""
    missing_enrichment_count: int = 0
    raw_file_path: str = ""
    imported_at: str = ""
    premarket_range_source: str = ""
    premarket_range_source_url: str = ""
    premarket_price_source: str = ""
    previous_close_source: str = ""
    premarket_high_source: str = ""
    premarket_low_source: str = ""
    premarket_volume_source: str = ""
    gap_pct_source: str = ""
    dollar_volume_source: str = ""
    enrichment_status: str = ""
    enrichment_primary_source: str = ""
    enrichment_fallback_status: str = ""
    enrichment_fallback_source: str = ""
    enrichment_was_fallback: bool = False
    enrichment_observed_at: str = ""
    enrichment_bar_completed_at: str = ""
    enrichment_is_complete: bool = False
    enrichment_observation_sha256: str = ""

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
            source_url=str(row.get("source_url") or "").strip(),
            extraction_mode=str(
                row.get("extraction_mode") or row.get("data_source_kind") or ""
            ).strip(),
            source_timestamp=str(
                row.get("source_timestamp") or row.get("as_of_timestamp") or ""
            ).strip(),
            extracted_at=str(row.get("extracted_at") or row.get("imported_at") or "").strip(),
            stale_data_flag=parse_bool(row.get("stale_data_flag")),
            source_confidence=parse_float(
                row.get("source_confidence"), "source_confidence", default=0.0
            ),
            field_completeness_score=(
                None
                if row.get("field_completeness_score") in {None, ""}
                else parse_float(row.get("field_completeness_score"), "field_completeness_score")
            ),
            source_reliability_prior=(
                None
                if row.get("source_reliability_prior") in {None, ""}
                else parse_float(row.get("source_reliability_prior"), "source_reliability_prior")
            ),
            reconciliation_status=str(row.get("reconciliation_status") or "").strip(),
            reconciliation_confidence_score=(
                None
                if row.get("reconciliation_confidence_score") in {None, ""}
                else parse_float(
                    row.get("reconciliation_confidence_score"),
                    "reconciliation_confidence_score",
                )
            ),
            evidence_confidence_version=str(
                row.get("evidence_confidence_version") or ""
            ).strip(),
            source_count=parse_int(row.get("source_count"), "source_count", default=1),
            score_consensus=str(row.get("score_consensus") or "single_source").strip(),
            conflict_flags=str(row.get("conflict_flags") or "").strip(),
            preferred_source=str(row.get("preferred_source") or "").strip(),
            row_merge_reason=str(row.get("row_merge_reason") or "single_source").strip(),
            dollar_volume=dollar_volume,
            gap_pct=gap_pct,
            catalyst_headline=str(row.get("catalyst_headline") or "").strip(),
            catalyst_url=str(row.get("catalyst_url") or "").strip(),
            catalyst_summary=str(row.get("catalyst_summary") or "").strip(),
            catalyst_tier=str(row.get("catalyst_tier") or "").strip(),
            catalyst_category=str(row.get("catalyst_category") or "").strip(),
            catalyst_confidence=(
                None
                if row.get("catalyst_confidence") in {None, ""}
                else parse_float(row.get("catalyst_confidence"), "catalyst_confidence")
            ),
            catalyst_status=str(row.get("catalyst_status") or "").strip(),
            catalyst_risk_flags=str(row.get("catalyst_risk_flags") or "").strip(),
            halt_status=str(row.get("halt_status") or "").strip(),
            sec_risk_status=str(row.get("sec_risk_status") or "").strip(),
            corporate_action_status=str(row.get("corporate_action_status") or "").strip(),
            source_quality_status=str(row.get("source_quality_status") or "").strip(),
            discovery_context=str(row.get("discovery_context") or "").strip(),
            universe_lane=str(row.get("universe_lane") or "mover").strip(),
            core_universe_memberships=str(row.get("core_universe_memberships") or "").strip(),
            core_lane_score=(
                None
                if row.get("core_lane_score") in {None, ""}
                else parse_float(row.get("core_lane_score"), "core_lane_score")
            ),
            core_lane_eligible=parse_bool(row.get("core_lane_eligible")),
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
            premarket_range_source=str(row.get("premarket_range_source") or "").strip(),
            premarket_range_source_url=str(
                row.get("premarket_range_source_url") or ""
            ).strip(),
            premarket_price_source=str(row.get("premarket_price_source") or "").strip(),
            previous_close_source=str(row.get("previous_close_source") or "").strip(),
            premarket_high_source=str(row.get("premarket_high_source") or "").strip(),
            premarket_low_source=str(row.get("premarket_low_source") or "").strip(),
            premarket_volume_source=str(
                row.get("premarket_volume_source") or ""
            ).strip(),
            gap_pct_source=str(row.get("gap_pct_source") or "").strip(),
            dollar_volume_source=str(row.get("dollar_volume_source") or "").strip(),
            enrichment_status=str(row.get("enrichment_status") or "").strip(),
            enrichment_primary_source=str(
                row.get("enrichment_primary_source") or ""
            ).strip(),
            enrichment_fallback_status=str(
                row.get("enrichment_fallback_status") or ""
            ).strip(),
            enrichment_fallback_source=str(
                row.get("enrichment_fallback_source") or ""
            ).strip(),
            enrichment_was_fallback=parse_bool(row.get("enrichment_was_fallback")),
            enrichment_observed_at=str(
                row.get("enrichment_observed_at") or ""
            ).strip(),
            enrichment_bar_completed_at=str(
                row.get("enrichment_bar_completed_at") or ""
            ).strip(),
            enrichment_is_complete=parse_bool(row.get("enrichment_is_complete")),
            enrichment_observation_sha256=str(
                row.get("enrichment_observation_sha256") or ""
            ).strip(),
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
        payload = {
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
            "catalyst_summary": self.catalyst_summary,
            "catalyst_tier": self.catalyst_tier,
            "catalyst_category": self.catalyst_category,
            "catalyst_confidence": self.catalyst_confidence,
            "catalyst_status": self.catalyst_status,
            "catalyst_risk_flags": self.catalyst_risk_flags,
            "current_halt": self.current_halt,
            "recent_offering": self.recent_offering,
            "reverse_split_90d": self.reverse_split_90d,
            "halt_status": self.halt_status,
            "sec_risk_status": self.sec_risk_status,
            "corporate_action_status": self.corporate_action_status,
            "source_quality_status": self.source_quality_status,
            "source": self.source,
            "source_url": self.source_url,
            "extraction_mode": self.extraction_mode,
            "source_timestamp": self.source_timestamp,
            "extracted_at": self.extracted_at,
            "stale_data_flag": self.stale_data_flag,
            "source_confidence": self.source_confidence,
            "source_count": self.source_count,
            "score_consensus": self.score_consensus,
            "conflict_flags": self.conflict_flags,
            "preferred_source": self.preferred_source,
            "row_merge_reason": self.row_merge_reason,
            "discovery_context": self.discovery_context,
            "universe_lane": self.universe_lane,
            "core_universe_memberships": self.core_universe_memberships,
            "core_lane_score": self.core_lane_score,
            "core_lane_eligible": self.core_lane_eligible,
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
            "premarket_range_source": self.premarket_range_source,
            "premarket_range_source_url": self.premarket_range_source_url,
            "premarket_price_source": self.premarket_price_source,
            "previous_close_source": self.previous_close_source,
            "premarket_high_source": self.premarket_high_source,
            "premarket_low_source": self.premarket_low_source,
            "premarket_volume_source": self.premarket_volume_source,
            "gap_pct_source": self.gap_pct_source,
            "dollar_volume_source": self.dollar_volume_source,
            "enrichment_status": self.enrichment_status,
            "enrichment_primary_source": self.enrichment_primary_source,
            "enrichment_fallback_status": self.enrichment_fallback_status,
            "enrichment_fallback_source": self.enrichment_fallback_source,
            "enrichment_was_fallback": self.enrichment_was_fallback,
            "enrichment_observed_at": self.enrichment_observed_at,
            "enrichment_bar_completed_at": self.enrichment_bar_completed_at,
            "enrichment_is_complete": self.enrichment_is_complete,
            "enrichment_observation_sha256": self.enrichment_observation_sha256,
        }
        if self.field_completeness_score is not None:
            payload["field_completeness_score"] = self.field_completeness_score
        if self.source_reliability_prior is not None:
            payload["source_reliability_prior"] = self.source_reliability_prior
        if self.reconciliation_status:
            payload["reconciliation_status"] = self.reconciliation_status
        if self.reconciliation_confidence_score is not None:
            payload["reconciliation_confidence_score"] = self.reconciliation_confidence_score
        if self.evidence_confidence_version:
            payload["evidence_confidence_version"] = self.evidence_confidence_version
        return payload


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
        source_lineage = self.intelligence.get("source_lineage") or {
            "source": self.snapshot.source,
            "source_url": self.snapshot.source_url,
            "extraction_mode": self.snapshot.extraction_mode,
            "source_timestamp": self.snapshot.source_timestamp or self.snapshot.as_of_timestamp,
            "extracted_at": self.snapshot.extracted_at or self.snapshot.imported_at,
            "stale_data_flag": self.snapshot.stale_data_flag,
            "source_confidence": self.snapshot.source_confidence,
            "source_count": self.snapshot.source_count,
            "preferred_source": self.snapshot.preferred_source or self.snapshot.source,
            "conflict_flags": self.snapshot.conflict_flags,
        }
        source_lineage = dict(source_lineage)
        if self.snapshot.field_completeness_score is not None:
            source_lineage["field_completeness_score"] = self.snapshot.field_completeness_score
        if self.snapshot.source_reliability_prior is not None:
            source_lineage["source_reliability_prior"] = self.snapshot.source_reliability_prior
        if self.snapshot.reconciliation_status:
            source_lineage["reconciliation_status"] = self.snapshot.reconciliation_status
        if self.snapshot.reconciliation_confidence_score is not None:
            source_lineage["reconciliation_confidence_score"] = (
                self.snapshot.reconciliation_confidence_score
            )
        if self.snapshot.evidence_confidence_version:
            source_lineage["evidence_confidence_version"] = (
                self.snapshot.evidence_confidence_version
            )
        source_lineage["premarket_observation"] = {
            "source": self.snapshot.premarket_range_source,
            "source_url": self.snapshot.premarket_range_source_url,
            "status": self.snapshot.enrichment_status,
            "observed_at": self.snapshot.enrichment_observed_at,
            "bar_completed_at": self.snapshot.enrichment_bar_completed_at,
            "is_complete": self.snapshot.enrichment_is_complete,
            "fallback_status": self.snapshot.enrichment_fallback_status,
            "fallback_source": self.snapshot.enrichment_fallback_source,
            "was_fallback": self.snapshot.enrichment_was_fallback,
            "observation_sha256": self.snapshot.enrichment_observation_sha256,
        }
        payload = {
            "rank": self.rank,
            "ticker": self.snapshot.ticker,
            "company": self.snapshot.company,
            "total_score": self.score,
            "score": self.score,
            "explosive_score": self.intelligence.get("explosive_score", ""),
            "tradability_score": self.intelligence.get("tradability_score", ""),
            "catalyst_score": self.intelligence.get("catalyst_score", ""),
            "risk_score": self.intelligence.get("risk_score", ""),
            "gap_pct": self.gap_pct,
            "dollar_volume": self.dollar_volume,
            "float_rotation_pct": self.float_rotation_pct,
            "range_position_pct": self.range_position_pct,
            "data_quality_score": self.data_quality_score,
            "liquidity_tier": self.liquidity_tier,
            "setup_grade": self.setup_grade,
            "expected_return_bucket": self.intelligence.get("expected_return_bucket", ""),
            "confidence_bucket": self.intelligence.get("confidence_bucket", ""),
            "volatility_signature": self.volatility_signature,
            "equation_version": self.equation_version,
            "model_version": self.equation_version,
            "config_hash": self.intelligence.get("config_hash", ""),
            "premarket_price": self.snapshot.premarket_price,
            "previous_close": self.snapshot.previous_close,
            "premarket_high": self.snapshot.premarket_high,
            "premarket_low": self.snapshot.premarket_low,
            "premarket_volume": self.snapshot.premarket_volume,
            "float_shares": self.snapshot.float_shares,
            "market_cap": self.snapshot.market_cap,
            "spread_pct": self.snapshot.spread_pct,
            "short_float_pct": self.snapshot.short_float_pct,
            "has_news": self.snapshot.has_news,
            "catalyst_headline": self.snapshot.catalyst_headline,
            "catalyst_url": self.snapshot.catalyst_url,
            "catalyst_summary": self.snapshot.catalyst_summary,
            "catalyst_tier": (
                self.snapshot.catalyst_tier
                or self.intelligence.get("catalyst_tier", "")
            ),
            "catalyst_category": (
                self.snapshot.catalyst_category
                or self.intelligence.get("catalyst_category", "")
            ),
            "catalyst_confidence": (
                self.snapshot.catalyst_confidence
                if self.snapshot.catalyst_confidence is not None
                else self.intelligence.get("catalyst_confidence")
            ),
            "catalyst_status": self.snapshot.catalyst_status,
            "catalyst_risk_flags": (
                self.snapshot.catalyst_risk_flags
                or self.intelligence.get("catalyst_risk_flags", "")
            ),
            "current_halt": self.snapshot.current_halt,
            "recent_offering": self.snapshot.recent_offering,
            "reverse_split_90d": self.snapshot.reverse_split_90d,
            "breakout_trigger": self.breakout_trigger,
            "pullback_zone": self.pullback_zone,
            "invalidation_level": self.invalidation_level,
            "first_target": self.first_target,
            "stretch_target": self.stretch_target,
            "risk_flags": ";".join(self.risk_flags),
            "exit_bias": self.best_exit_bias,
            "best_exit_bias": self.best_exit_bias,
            "score_breakdown": json.dumps(self.score_breakdown, sort_keys=True),
            "avoid_reasons": ";".join(self.avoid_reasons),
            "sample_size": self.intelligence.get(
                "sample_size", self.intelligence.get("similar_setup_count", 0)
            ),
            "uncertainty_bucket": self.intelligence.get("uncertainty_bucket", ""),
            "source_lineage": json.dumps(source_lineage, sort_keys=True),
            "source": self.snapshot.source,
            "source_url": self.snapshot.source_url,
            "extraction_mode": self.snapshot.extraction_mode,
            "source_timestamp": self.snapshot.source_timestamp or self.snapshot.as_of_timestamp,
            "extracted_at": self.snapshot.extracted_at or self.snapshot.imported_at,
            "stale_data_flag": self.snapshot.stale_data_flag,
            "source_confidence": self.snapshot.source_confidence,
            "source_count": self.snapshot.source_count,
            "score_consensus": self.snapshot.score_consensus,
            "conflict_flags": self.snapshot.conflict_flags,
            "preferred_source": self.snapshot.preferred_source or self.snapshot.source,
            "row_merge_reason": self.snapshot.row_merge_reason,
            "discovery_context": self.snapshot.discovery_context,
            "universe_lane": self.snapshot.universe_lane,
            "core_universe_memberships": self.snapshot.core_universe_memberships,
            "core_lane_score": self.snapshot.core_lane_score,
            "core_lane_eligible": self.snapshot.core_lane_eligible,
            "halt_status": self.snapshot.halt_status,
            "sec_risk_status": self.snapshot.sec_risk_status,
            "corporate_action_status": self.snapshot.corporate_action_status,
            "source_quality_status": self.snapshot.source_quality_status,
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
            "premarket_range_source": self.snapshot.premarket_range_source,
            "premarket_range_source_url": self.snapshot.premarket_range_source_url,
            "premarket_price_source": self.snapshot.premarket_price_source,
            "previous_close_source": self.snapshot.previous_close_source,
            "premarket_high_source": self.snapshot.premarket_high_source,
            "premarket_low_source": self.snapshot.premarket_low_source,
            "premarket_volume_source": self.snapshot.premarket_volume_source,
            "gap_pct_source": self.snapshot.gap_pct_source,
            "dollar_volume_source": self.snapshot.dollar_volume_source,
            "enrichment_status": self.snapshot.enrichment_status,
            "enrichment_primary_source": self.snapshot.enrichment_primary_source,
            "enrichment_fallback_status": self.snapshot.enrichment_fallback_status,
            "enrichment_fallback_source": self.snapshot.enrichment_fallback_source,
            "enrichment_was_fallback": self.snapshot.enrichment_was_fallback,
            "enrichment_observed_at": self.snapshot.enrichment_observed_at,
            "enrichment_bar_completed_at": self.snapshot.enrichment_bar_completed_at,
            "enrichment_is_complete": self.snapshot.enrichment_is_complete,
            "enrichment_observation_sha256": self.snapshot.enrichment_observation_sha256,
        }
        if self.snapshot.field_completeness_score is not None:
            payload["field_completeness_score"] = self.snapshot.field_completeness_score
        if self.snapshot.source_reliability_prior is not None:
            payload["source_reliability_prior"] = self.snapshot.source_reliability_prior
        if self.snapshot.reconciliation_status:
            payload["reconciliation_status"] = self.snapshot.reconciliation_status
        if self.snapshot.reconciliation_confidence_score is not None:
            payload["reconciliation_confidence_score"] = (
                self.snapshot.reconciliation_confidence_score
            )
        if self.snapshot.evidence_confidence_version:
            payload["evidence_confidence_version"] = self.snapshot.evidence_confidence_version
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
