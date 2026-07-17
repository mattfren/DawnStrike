"""Immutable point-in-time contracts for mover research."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import MappingProxyType
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
RTH_START = time(9, 30)
RTH_END = time(16, 0)
OPENING_RANGE_END = time(9, 45)
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PROSPECTIVE_SNAPSHOT_SCHEMA = "v2.prospective_mover_snapshot.v1"
MOVER_STRATEGY_SCHEMA = "v2.mover_strategy_spec.v1"
MOVER_SIGNAL_SCHEMA = "v2.mover_paper_signal.v1"
EVIDENCE_MODES = frozenset({"forward_observation", "historical_replay"})
MAX_FORWARD_CAPTURE_DELAY = timedelta(minutes=5)
UNIVERSE_SELECTION_METHODS = frozenset(
    {
        "premarket_screen",
        "scheduled_universe",
        "prior_session_watchlist",
        "live_intraday_scan",
    }
)
FORBIDDEN_FUTURE_FIELDS = frozenset(
    {
        "close_return_pct",
        "daily_high",
        "daily_low",
        "eod_rank",
        "final_change_pct",
        "final_return_pct",
        "future_high",
        "future_low",
        "outcome",
        "outcome_return_pct",
    }
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class ProspectiveMoverSnapshot:
    snapshot_id: str
    market_date: str
    symbol: str
    observed_at: datetime
    feature_cutoff_at: datetime
    universe_selected_at: datetime
    universe_source_ref: str
    universe_selection_method: str
    context_observed_at: datetime
    evidence_mode: str
    source_captured_at: datetime | None
    system_received_at: datetime | None
    forward_receipt_ref: str
    price: float
    previous_close: float | None
    session_open: float | None
    opening_range_high: float | None
    opening_range_low: float | None
    opening_range_complete: bool
    running_vwap: float | None
    cumulative_volume: float | None
    cumulative_dollar_volume: float | None
    same_clock_rvol: float | None
    spread_pct: float | None
    split_adjusted: bool | None
    reverse_split_days: int | None
    reverse_split_lookback_clear: bool | None
    recent_offering_days: int | None
    offering_lookback_clear: bool | None
    halt_state: str
    source_conflict: bool | None
    catalyst_verified: bool | None
    catalyst_published_at: datetime | None
    catalyst_source_url: str
    catalyst_source_type: str
    catalyst_artifact_ref: str
    source_refs: tuple[str, ...]
    raw_payload: Mapping[str, Any]
    schema_version: str = PROSPECTIVE_SNAPSHOT_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_refs", _string_tuple(self.source_refs))
        object.__setattr__(self, "raw_payload", _frozen_mapping(self.raw_payload))
        self.validate()

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> ProspectiveMoverSnapshot:
        forbidden = _forbidden_field_names(row)
        if forbidden:
            raise ValueError(
                "prospective snapshot contains future/outcome fields: "
                + ", ".join(sorted(forbidden))
            )
        cutoff = _aware_datetime(row.get("feature_cutoff_at"), "feature_cutoff_at")
        observed = _aware_datetime(row.get("observed_at"), "observed_at")
        universe_selected = _aware_datetime(
            row.get("universe_selected_at"),
            "universe_selected_at",
        )
        context_observed = _aware_datetime(
            row.get("context_observed_at"),
            "context_observed_at",
        )
        evidence_mode = str(
            row.get("evidence_mode") or "historical_replay"
        ).strip().lower()
        source_captured = _optional_datetime(
            row.get("source_captured_at"),
            "source_captured_at",
        )
        system_received = _optional_datetime(
            row.get("system_received_at"),
            "system_received_at",
        )
        published = _optional_datetime(
            row.get("catalyst_published_at"),
            "catalyst_published_at",
        )
        raw_payload = row.get("raw_payload") or {}
        if not isinstance(raw_payload, Mapping):
            raise ValueError("raw_payload must be an object")
        source_refs_raw = row.get("source_refs") or ()
        if isinstance(source_refs_raw, str):
            source_refs_raw = [
                item.strip() for item in source_refs_raw.split(";") if item.strip()
            ]
        snapshot = cls(
            snapshot_id=str(row.get("snapshot_id") or "").strip(),
            market_date=str(row.get("market_date") or "")[:10],
            symbol=str(row.get("symbol") or row.get("ticker") or "").strip().upper(),
            observed_at=observed,
            feature_cutoff_at=cutoff,
            universe_selected_at=universe_selected,
            universe_source_ref=str(row.get("universe_source_ref") or "").strip(),
            universe_selection_method=str(
                row.get("universe_selection_method") or ""
            )
            .strip()
            .lower(),
            context_observed_at=context_observed,
            evidence_mode=evidence_mode,
            source_captured_at=source_captured,
            system_received_at=system_received,
            forward_receipt_ref=str(
                row.get("forward_receipt_ref") or ""
            ).strip(),
            price=_required_float(row.get("price"), "price"),
            previous_close=_optional_float(row.get("previous_close")),
            session_open=_optional_float(row.get("session_open")),
            opening_range_high=_optional_float(row.get("opening_range_high")),
            opening_range_low=_optional_float(row.get("opening_range_low")),
            opening_range_complete=_required_bool(
                row.get("opening_range_complete", False),
                "opening_range_complete",
            ),
            running_vwap=_optional_float(row.get("running_vwap")),
            cumulative_volume=_optional_float(row.get("cumulative_volume")),
            cumulative_dollar_volume=_optional_float(
                row.get("cumulative_dollar_volume")
            ),
            same_clock_rvol=_optional_float(row.get("same_clock_rvol")),
            spread_pct=_optional_float(row.get("spread_pct")),
            split_adjusted=_optional_bool(row.get("split_adjusted")),
            reverse_split_days=_optional_int(row.get("reverse_split_days")),
            reverse_split_lookback_clear=_optional_bool(
                row.get("reverse_split_lookback_clear")
            ),
            recent_offering_days=_optional_int(row.get("recent_offering_days")),
            offering_lookback_clear=_optional_bool(
                row.get("offering_lookback_clear")
            ),
            halt_state=str(row.get("halt_state") or "unknown").strip().lower(),
            source_conflict=_optional_bool(row.get("source_conflict")),
            catalyst_verified=_optional_bool(row.get("catalyst_verified")),
            catalyst_published_at=published,
            catalyst_source_url=str(row.get("catalyst_source_url") or "").strip(),
            catalyst_source_type=str(row.get("catalyst_source_type") or "").strip(),
            catalyst_artifact_ref=str(
                row.get("catalyst_artifact_ref") or ""
            ).strip(),
            source_refs=tuple(str(item) for item in source_refs_raw if str(item)),
            raw_payload=raw_payload,
            schema_version=str(
                row.get("schema_version") or PROSPECTIVE_SNAPSHOT_SCHEMA
            ),
        )
        return snapshot

    @property
    def gap_pct(self) -> float | None:
        if (
            self.previous_close is None
            or self.previous_close <= 0
            or self.session_open is None
        ):
            return None
        return (self.session_open / self.previous_close - 1.0) * 100.0

    def validate(self) -> None:
        if self.schema_version != PROSPECTIVE_SNAPSHOT_SCHEMA:
            raise ValueError(
                f"schema_version must be {PROSPECTIVE_SNAPSHOT_SCHEMA!r}"
            )
        if not self.snapshot_id:
            raise ValueError("snapshot_id is required")
        if not SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError(f"invalid symbol: {self.symbol!r}")
        try:
            market_day = date.fromisoformat(self.market_date)
        except ValueError as exc:
            raise ValueError("market_date must be YYYY-MM-DD") from exc
        cutoff = _datetime_instance(self.feature_cutoff_at, "feature_cutoff_at")
        observed = _datetime_instance(self.observed_at, "observed_at")
        universe_selected = _datetime_instance(
            self.universe_selected_at,
            "universe_selected_at",
        )
        context_observed = _datetime_instance(
            self.context_observed_at,
            "context_observed_at",
        )
        source_captured = (
            _datetime_instance(self.source_captured_at, "source_captured_at")
            if self.source_captured_at is not None
            else None
        )
        system_received = (
            _datetime_instance(self.system_received_at, "system_received_at")
            if self.system_received_at is not None
            else None
        )
        cutoff_local = cutoff.astimezone(MARKET_TZ)
        observed_local = observed.astimezone(MARKET_TZ)
        if cutoff_local.date() != market_day or observed_local.date() != market_day:
            raise ValueError("snapshot timestamps must match market_date in America/New_York")
        if not (RTH_START <= cutoff_local.time() <= RTH_END):
            raise ValueError("feature_cutoff_at must be inside regular trading hours")
        if observed > cutoff:
            raise ValueError("observed_at cannot be after feature_cutoff_at")
        if universe_selected > cutoff:
            raise ValueError("universe_selected_at cannot be after feature_cutoff_at")
        if context_observed > cutoff:
            raise ValueError("context_observed_at cannot be after feature_cutoff_at")
        if self.evidence_mode not in EVIDENCE_MODES:
            allowed_modes = ", ".join(sorted(EVIDENCE_MODES))
            raise ValueError(f"evidence_mode must be one of: {allowed_modes}")
        if self.evidence_mode == "forward_observation":
            if source_captured is None:
                raise ValueError(
                    "forward_observation requires source_captured_at"
                )
            if system_received is None:
                raise ValueError(
                    "forward_observation requires system_received_at"
                )
            if not observed <= source_captured <= cutoff + MAX_FORWARD_CAPTURE_DELAY:
                raise ValueError(
                    "forward source capture must be between the observed bar close "
                    "and five minutes after the feature cutoff"
                )
            if source_captured != system_received:
                raise ValueError(
                    "forward source capture must equal the authoritative system receipt"
                )
            if source_captured.astimezone(MARKET_TZ).date() != market_day:
                raise ValueError(
                    "forward source capture must match market_date in America/New_York"
                )
            if (
                not self.forward_receipt_ref.startswith("sha256:")
                or self.forward_receipt_ref not in self.source_refs
            ):
                raise ValueError(
                    "forward_observation requires a retained sha256 receipt ref"
                )
        elif source_captured is not None and source_captured < observed:
            raise ValueError("source_captured_at cannot precede observed_at")
        elif system_received is not None or self.forward_receipt_ref:
            raise ValueError(
                "historical_replay cannot claim a forward system receipt"
            )
        if self.universe_selection_method not in UNIVERSE_SELECTION_METHODS:
            allowed = ", ".join(sorted(UNIVERSE_SELECTION_METHODS))
            raise ValueError(
                f"universe_selection_method must be one of: {allowed}"
            )
        if not self.universe_source_ref:
            raise ValueError("universe_source_ref is required")
        if (
            self.evidence_mode == "forward_observation"
            and not self.universe_source_ref.startswith("sha256:")
        ):
            raise ValueError(
                "forward universe_source_ref must be an immutable sha256 artifact ref"
            )
        if self.price <= 0:
            raise ValueError("price must be positive")
        for name in (
            "previous_close",
            "session_open",
            "opening_range_high",
            "opening_range_low",
            "running_vwap",
            "cumulative_volume",
            "cumulative_dollar_volume",
            "same_clock_rvol",
            "spread_pct",
        ):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")
        if (
            self.opening_range_high is not None
            and self.opening_range_low is not None
            and self.opening_range_high < self.opening_range_low
        ):
            raise ValueError("opening_range_high cannot be below opening_range_low")
        if self.opening_range_complete and (
            cutoff_local.time() < OPENING_RANGE_END
            or self.opening_range_high is None
            or self.opening_range_low is None
        ):
            raise ValueError("completed opening range requires cutoff >= 09:45 and high/low")
        for name in ("reverse_split_days", "recent_offering_days"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if (
            self.reverse_split_lookback_clear is True
            and self.reverse_split_days is not None
            and self.reverse_split_days < 91
        ):
            raise ValueError(
                "reverse-split lookback cannot be clear when a recent event is present"
            )
        if (
            self.offering_lookback_clear is True
            and self.recent_offering_days is not None
            and self.recent_offering_days < 31
        ):
            raise ValueError(
                "offering lookback cannot be clear when a recent event is present"
            )
        if (
            self.catalyst_published_at is not None
            and self.catalyst_published_at > self.feature_cutoff_at
        ):
            raise ValueError("catalyst publication cannot be after feature cutoff")
        if (
            self.catalyst_published_at is not None
            and self.catalyst_published_at > context_observed
        ):
            raise ValueError(
                "catalyst publication cannot be after the context observation"
            )
        if self.catalyst_verified is True and (
            self.catalyst_published_at is None
            or not self.catalyst_source_url
            or not self.catalyst_source_type
            or not self.catalyst_artifact_ref
        ):
            raise ValueError(
                "verified catalyst requires publication time, URL, source type, "
                "and an immutable artifact ref"
            )
        if not self.source_refs:
            raise ValueError("source_refs must retain at least one immutable source")
        if any(not item.strip() for item in self.source_refs):
            raise ValueError("source_refs cannot contain blank values")
        if self.universe_source_ref not in self.source_refs:
            raise ValueError("universe_source_ref must be retained in source_refs")
        if (
            self.catalyst_verified is True
            and self.catalyst_source_url not in self.source_refs
        ):
            raise ValueError("verified catalyst URL must be retained in source_refs")
        if self.catalyst_verified is True and (
            not self.catalyst_artifact_ref.startswith("sha256:")
            or self.catalyst_artifact_ref not in self.source_refs
        ):
            raise ValueError(
                "verified catalyst artifact must be a retained sha256 source ref"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "market_date": self.market_date,
            "symbol": self.symbol,
            "observed_at": self.observed_at.isoformat(),
            "feature_cutoff_at": self.feature_cutoff_at.isoformat(),
            "universe_selected_at": self.universe_selected_at.isoformat(),
            "universe_source_ref": self.universe_source_ref,
            "universe_selection_method": self.universe_selection_method,
            "context_observed_at": self.context_observed_at.isoformat(),
            "evidence_mode": self.evidence_mode,
            "source_captured_at": (
                self.source_captured_at.isoformat()
                if self.source_captured_at is not None
                else None
            ),
            "system_received_at": (
                self.system_received_at.isoformat()
                if self.system_received_at is not None
                else None
            ),
            "forward_receipt_ref": self.forward_receipt_ref or None,
            "price": self.price,
            "previous_close": self.previous_close,
            "session_open": self.session_open,
            "gap_pct": self.gap_pct,
            "opening_range_high": self.opening_range_high,
            "opening_range_low": self.opening_range_low,
            "opening_range_complete": self.opening_range_complete,
            "running_vwap": self.running_vwap,
            "cumulative_volume": self.cumulative_volume,
            "cumulative_dollar_volume": self.cumulative_dollar_volume,
            "same_clock_rvol": self.same_clock_rvol,
            "spread_pct": self.spread_pct,
            "split_adjusted": self.split_adjusted,
            "reverse_split_days": self.reverse_split_days,
            "reverse_split_lookback_clear": self.reverse_split_lookback_clear,
            "recent_offering_days": self.recent_offering_days,
            "offering_lookback_clear": self.offering_lookback_clear,
            "halt_state": self.halt_state,
            "source_conflict": self.source_conflict,
            "catalyst_verified": self.catalyst_verified,
            "catalyst_published_at": (
                self.catalyst_published_at.isoformat()
                if self.catalyst_published_at
                else None
            ),
            "catalyst_source_url": self.catalyst_source_url,
            "catalyst_source_type": self.catalyst_source_type,
            "catalyst_artifact_ref": self.catalyst_artifact_ref,
            "source_refs": list(self.source_refs),
            "raw_payload": _deep_thaw(self.raw_payload),
        }


@dataclass(frozen=True)
class MoverStrategySpec:
    strategy_id: str
    version: str
    display_name: str
    description: str
    parameters: Mapping[str, Any]
    required_features: tuple[str, ...]
    entry_logic: str
    stop_logic: str
    target_logic: str
    validation_status: str = "forward_observation_required"
    research_only: bool = True
    broker_execution_enabled: bool = False
    schema_version: str = MOVER_STRATEGY_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _frozen_mapping(self.parameters))
        object.__setattr__(
            self,
            "required_features",
            _string_tuple(self.required_features),
        )
        if self.schema_version != MOVER_STRATEGY_SCHEMA:
            raise ValueError(f"schema_version must be {MOVER_STRATEGY_SCHEMA!r}")
        if not self.strategy_id.strip() or not self.version.strip():
            raise ValueError("strategy_id and version are required")
        if not self.research_only or self.broker_execution_enabled:
            raise ValueError("mover strategies must remain research-only")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "strategy_id": self.strategy_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "parameters": _deep_thaw(self.parameters),
            "required_features": list(self.required_features),
            "entry_logic": self.entry_logic,
            "stop_logic": self.stop_logic,
            "target_logic": self.target_logic,
            "validation_status": self.validation_status,
            "research_only": self.research_only,
            "broker_execution_enabled": self.broker_execution_enabled,
        }
        payload["semantics_fingerprint"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return payload


@dataclass(frozen=True)
class MoverPaperSignal:
    signal_id: str
    strategy_id: str
    strategy_version: str
    strategy_semantics_fingerprint: str
    market_date: str
    symbol: str
    signal_at: datetime
    snapshot_id: str
    evidence_mode: str
    source_captured_at: datetime | None
    system_received_at: datetime | None
    forward_receipt_ref: str
    entry_reference: float
    stop: float
    target: float
    score: float
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]
    source_refs: tuple[str, ...]
    features: Mapping[str, Any]
    schema_version: str = MOVER_SIGNAL_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _string_tuple(self.evidence))
        object.__setattr__(self, "warnings", _string_tuple(self.warnings))
        object.__setattr__(self, "source_refs", _string_tuple(self.source_refs))
        object.__setattr__(self, "features", _frozen_mapping(self.features))
        self.validate()

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> MoverPaperSignal:
        schema_version = str(row.get("schema_version") or MOVER_SIGNAL_SCHEMA)
        direction = str(row.get("direction") or "long").strip().lower()
        if direction != "long":
            raise ValueError("mover paper signals must use direction='long'")
        if "research_only" in row and not _required_bool(
            row.get("research_only"),
            "research_only",
        ):
            raise ValueError("mover paper signals must remain research-only")
        if "broker_execution_enabled" in row and _required_bool(
            row.get("broker_execution_enabled"),
            "broker_execution_enabled",
        ):
            raise ValueError("broker execution cannot be enabled")
        features = row.get("features") or {}
        if not isinstance(features, Mapping):
            raise ValueError("features must be an object")
        forbidden = _forbidden_field_names(features)
        if forbidden:
            raise ValueError(
                "paper signal features contain future/outcome fields: "
                + ", ".join(sorted(forbidden))
            )
        return cls(
            signal_id=str(row.get("signal_id") or "").strip(),
            strategy_id=str(row.get("strategy_id") or "").strip(),
            strategy_version=str(row.get("strategy_version") or "").strip(),
            strategy_semantics_fingerprint=str(
                row.get("strategy_semantics_fingerprint") or ""
            )
            .strip()
            .lower(),
            market_date=str(row.get("market_date") or "")[:10],
            symbol=str(row.get("symbol") or row.get("ticker") or "")
            .strip()
            .upper(),
            signal_at=_aware_datetime(row.get("signal_at"), "signal_at"),
            snapshot_id=str(row.get("snapshot_id") or "").strip(),
            evidence_mode=str(
                row.get("evidence_mode") or "historical_replay"
            ).strip().lower(),
            source_captured_at=_optional_datetime(
                row.get("source_captured_at"),
                "source_captured_at",
            ),
            system_received_at=_optional_datetime(
                row.get("system_received_at"),
                "system_received_at",
            ),
            forward_receipt_ref=str(
                row.get("forward_receipt_ref") or ""
            ).strip(),
            entry_reference=_required_float(
                row.get("entry_reference"),
                "entry_reference",
            ),
            stop=_required_float(row.get("stop"), "stop"),
            target=_required_float(row.get("target"), "target"),
            score=_required_float(row.get("score"), "score"),
            evidence=_string_tuple(row.get("evidence") or ()),
            warnings=_string_tuple(row.get("warnings") or ()),
            source_refs=_string_tuple(row.get("source_refs") or ()),
            features=features,
            schema_version=schema_version,
        )

    def validate(self) -> None:
        if self.schema_version != MOVER_SIGNAL_SCHEMA:
            raise ValueError(f"schema_version must be {MOVER_SIGNAL_SCHEMA!r}")
        if not self.signal_id:
            raise ValueError("signal_id is required")
        if not self.strategy_id or not self.strategy_version:
            raise ValueError("strategy_id and strategy_version are required")
        if not SHA256_PATTERN.fullmatch(self.strategy_semantics_fingerprint):
            raise ValueError(
                "strategy_semantics_fingerprint must be a lowercase 64-hex SHA-256"
            )
        if not self.snapshot_id:
            raise ValueError("snapshot_id is required")
        if self.evidence_mode not in EVIDENCE_MODES:
            allowed_modes = ", ".join(sorted(EVIDENCE_MODES))
            raise ValueError(f"evidence_mode must be one of: {allowed_modes}")
        if self.evidence_mode == "forward_observation":
            if self.source_captured_at is None:
                raise ValueError("forward paper signal requires source_captured_at")
            if self.system_received_at is None:
                raise ValueError("forward paper signal requires system_received_at")
            if self.source_captured_at != self.system_received_at:
                raise ValueError(
                    "forward paper signal capture must equal its system receipt"
                )
            if (
                not self.forward_receipt_ref.startswith("sha256:")
                or self.forward_receipt_ref not in self.source_refs
            ):
                raise ValueError(
                    "forward paper signal requires its retained sha256 receipt ref"
                )
        elif self.system_received_at is not None or self.forward_receipt_ref:
            raise ValueError(
                "historical replay signal cannot claim a forward system receipt"
            )
        if not SYMBOL_PATTERN.fullmatch(self.symbol):
            raise ValueError(f"invalid symbol: {self.symbol!r}")
        try:
            market_day = date.fromisoformat(self.market_date)
        except ValueError as exc:
            raise ValueError("market_date must be YYYY-MM-DD") from exc
        signal_at = _datetime_instance(self.signal_at, "signal_at")
        signal_local = signal_at.astimezone(MARKET_TZ)
        if signal_local.date() != market_day:
            raise ValueError("signal_at must match market_date in America/New_York")
        if not (RTH_START <= signal_local.time() <= RTH_END):
            raise ValueError("signal_at must be inside regular trading hours")
        for name in ("entry_reference", "stop", "target", "score"):
            value = getattr(self, name)
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not (0 < self.stop < self.entry_reference < self.target):
            raise ValueError(
                "long signal prices must satisfy 0 < stop < entry_reference < target"
            )
        if not 0 <= self.score <= 1:
            raise ValueError("score must be between 0 and 1")
        if not self.source_refs:
            raise ValueError("source_refs must retain at least one immutable source")
        if any(not item.strip() for item in self.source_refs):
            raise ValueError("source_refs cannot contain blank values")
        forbidden = _forbidden_field_names(self.features)
        if forbidden:
            raise ValueError(
                "paper signal features contain future/outcome fields: "
                + ", ".join(sorted(forbidden))
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_semantics_fingerprint": self.strategy_semantics_fingerprint,
            "market_date": self.market_date,
            "symbol": self.symbol,
            "signal_at": self.signal_at.isoformat(),
            "snapshot_id": self.snapshot_id,
            "evidence_mode": self.evidence_mode,
            "source_captured_at": (
                self.source_captured_at.isoformat()
                if self.source_captured_at is not None
                else None
            ),
            "system_received_at": (
                self.system_received_at.isoformat()
                if self.system_received_at is not None
                else None
            ),
            "forward_receipt_ref": self.forward_receipt_ref or None,
            "direction": "long",
            "entry_reference": self.entry_reference,
            "stop": self.stop,
            "target": self.target,
            "score": self.score,
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
            "source_refs": list(self.source_refs),
            "features": _deep_thaw(self.features),
            "research_only": True,
            "broker_execution_enabled": False,
        }


@dataclass(frozen=True)
class MoverStrategyDecision:
    decision_id: str
    strategy_id: str
    strategy_version: str
    market_date: str
    symbol: str
    snapshot_id: str
    feature_cutoff_at: datetime
    evidence_mode: str
    decision: str
    reason: str
    evidence: tuple[str, ...] = ()
    missing_features: tuple[str, ...] = ()
    vetoes: tuple[str, ...] = ()
    signal: MoverPaperSignal | None = None
    schema_version: str = "v2.mover_strategy_decision.v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "market_date": self.market_date,
            "symbol": self.symbol,
            "snapshot_id": self.snapshot_id,
            "feature_cutoff_at": self.feature_cutoff_at.isoformat(),
            "evidence_mode": self.evidence_mode,
            "decision": self.decision,
            "reason": self.reason,
            "evidence": list(self.evidence),
            "missing_features": list(self.missing_features),
            "vetoes": list(self.vetoes),
            "signal": self.signal.to_dict() if self.signal else None,
            "research_only": True,
            "broker_execution_enabled": False,
        }


def stable_id(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _deep_freeze(value: _T) -> _T:
    """Defensively copy JSON-like data into recursively immutable containers."""

    if isinstance(value, Mapping):
        frozen = {
            key: _deep_freeze(item)
            for key, item in value.items()
        }
        return cast(_T, MappingProxyType(frozen))
    if isinstance(value, (list, tuple)):
        return cast(_T, tuple(_deep_freeze(item) for item in value))
    if isinstance(value, (set, frozenset)):
        return cast(_T, frozenset(_deep_freeze(item) for item in value))
    return value


def _deep_thaw(value: Any) -> Any:
    """Return fresh JSON-serializable containers from a frozen contract value."""

    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return [_deep_thaw(item) for item in sorted(value, key=repr)]
    return value


def _frozen_mapping(value: Mapping[str, _T]) -> Mapping[str, _T]:
    if not isinstance(value, Mapping):
        raise ValueError("expected an object")
    return cast(Mapping[str, _T], _deep_freeze(value))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    try:
        return tuple(str(item).strip() for item in value)
    except TypeError as exc:
        raise ValueError("expected a string sequence") from exc


def _forbidden_field_names(value: Any) -> set[str]:
    """Find outcome-only field names anywhere in a JSON-like payload."""

    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in FORBIDDEN_FUTURE_FIELDS:
                found.add(normalized)
            found.update(_forbidden_field_names(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_forbidden_field_names(item))
    return found


def _aware_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip().replace("Z", "+00:00")
        if not raw:
            raise ValueError(f"{field} is required")
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _datetime_instance(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{field} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value


def _optional_datetime(value: Any, field: str) -> datetime | None:
    return None if value is None or value == "" else _aware_datetime(value, field)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected numeric value, got {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError("numeric values must be finite")
    return number


def _required_float(value: Any, field: str) -> float:
    number = _optional_float(value)
    if number is None:
        raise ValueError(f"{field} is required")
    return number


def _optional_int(value: Any) -> int | None:
    number = _optional_float(value)
    if number is None:
        return None
    if not number.is_integer():
        raise ValueError(f"expected an integer, got {value!r}")
    return int(number)


def _required_bool(value: Any, field: str) -> bool:
    parsed = _optional_bool(value)
    if parsed is None:
        raise ValueError(f"{field} is required")
    return parsed


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"cannot parse boolean value {value!r}")


__all__ = [
    "MoverPaperSignal",
    "MoverStrategyDecision",
    "MoverStrategySpec",
    "ProspectiveMoverSnapshot",
    "stable_id",
]
