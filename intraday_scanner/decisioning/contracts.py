"""Immutable contracts for strategy decision evidence and identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar


class ConditionCategory(StrEnum):
    HARD_MARKET = "HARD_MARKET"
    HARD_RISK = "HARD_RISK"
    STRATEGY_CORE = "STRATEGY_CORE"
    AI_RESOLVABLE = "AI_RESOLVABLE"
    EXECUTION_ONLY = "EXECUTION_ONLY"
    ADVISORY = "ADVISORY"


class ConditionStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    RESOLVED_FROM_SOURCE = "RESOLVED_FROM_SOURCE"
    MISSING_DISCLOSED = "MISSING_DISCLOSED"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class PickTier(StrEnum):
    QUALIFIED_PICK = "QUALIFIED_PICK"
    PICK_WITH_DISCLOSED_GAPS = "PICK_WITH_DISCLOSED_GAPS"
    CONDITIONAL_PICK = "CONDITIONAL_PICK"
    WATCH_ONLY = "WATCH_ONLY"
    NO_EDGE = "NO_EDGE"
    BLOCKED_SAFETY = "BLOCKED_SAFETY"
    BLOCKED_DATA = "BLOCKED_DATA"


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


def canonical_json(value: Any) -> str:
    """Serialize JSON without non-finite values or implementation-dependent spacing."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _reject_nonfinite(value: Any, path: str = "payload") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} contains a non-finite number")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite(item, f"{path}[{index}]")


def _iso_datetime(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")


def _iso_date(value: str, field_name: str) -> None:
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    return tuple(value)


@dataclass(frozen=True)
class ConditionSpec:
    condition_id: str
    strategy_id: str
    category: ConditionCategory | str
    description: str
    blocking_for_research_pick: bool
    blocking_for_paper_entry: bool
    freshness_limit_seconds: int | None
    required_source_types: tuple[str, ...] = ()
    resolver_id: str = "deterministic"
    threshold_contract: Any = None
    missing_policy: str = "BLOCKED_DATA"
    policy_version: str = "strategy-decision-policy-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "category", ConditionCategory(self.category))
        if not self.condition_id.strip() or not self.strategy_id.strip():
            raise ValueError("condition_id and strategy_id are required")
        if self.freshness_limit_seconds is not None and self.freshness_limit_seconds < 0:
            raise ValueError("freshness_limit_seconds cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"category": getattr(self.category, "value", str(self.category))}


@dataclass(frozen=True)
class ConditionResult:
    condition_id: str
    status: ConditionStatus | str
    observed_value: Any = None
    threshold: Any = None
    reason: str = ""
    source_urls: tuple[str, ...] = ()
    source_hashes: tuple[str, ...] = ()
    observed_at: str | None = None
    effective_at: str | None = None
    resolver_id: str = "deterministic"
    resolution_method: str = "deterministic"
    requested_model: str = ""
    actual_model: str = ""
    confidence: float | None = None
    contradictions: tuple[str, ...] = ()
    unresolved_unknowns: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ConditionStatus(self.status))
        if not self.condition_id.strip():
            raise ValueError("condition_id is required")
        _reject_nonfinite(asdict(self))
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        for field_name in ("observed_at", "effective_at"):
            value = getattr(self, field_name)
            if value:
                _iso_datetime(value, field_name)
        for digest in self.source_hashes:
            if not _SHA256.fullmatch(str(digest)):
                raise ValueError("source_hashes must contain SHA-256 hex digests")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = getattr(self.status, "value", str(self.status))
        return result


@dataclass(frozen=True)
class EvidenceClaim:
    claim_id: str
    symbol: str
    condition_id: str
    claim_type: str
    statement: str
    source_urls: tuple[str, ...]
    source_hashes: tuple[str, ...]
    published_at: str
    effective_at: str | None = None
    authoritative: bool = False
    supported: bool = True

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol must be a valid uppercase ticker")
        object.__setattr__(self, "symbol", symbol)
        _iso_datetime(self.published_at, "published_at")
        if self.effective_at:
            _iso_datetime(self.effective_at, "effective_at")
        if not self.source_urls:
            raise ValueError("evidence claims require cited source URLs")
        if len(self.source_urls) != len(self.source_hashes):
            raise ValueError("source URL and hash counts must match")
        if any(not _SHA256.fullmatch(str(item)) for item in self.source_hashes):
            raise ValueError("source_hashes must contain SHA-256 hex digests")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceResolutionRun:
    run_id: str
    market_date: str
    symbol: str
    condition_ids: tuple[str, ...]
    source_identity: str
    prompt_version: str
    requested_model: str
    actual_model: str
    response_id: str
    request_count: int = 0
    web_search_call_count: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    cache_hits: int = 0
    elapsed_ms: int = 0
    status: str = "completed"

    def __post_init__(self) -> None:
        _iso_date(self.market_date, "market_date")
        if not self.symbol.strip() or not self.source_identity.strip():
            raise ValueError("symbol and source_identity are required")
        if not self.requested_model.strip() or not self.actual_model.strip():
            raise ValueError("requested and actual model identity are required")
        if (
            min(self.request_count, self.web_search_call_count, self.cache_hits, self.elapsed_ms)
            < 0
        ):
            raise ValueError("resolution counters cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyDecisionReceipt:
    schema_version: str
    receipt_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    market_date: str
    decision_at: str
    code_sha: str
    policy_version: str
    condition_results: tuple[ConditionResult, ...]
    first_blocking_failure: str | None
    all_blocking_failures: tuple[str, ...]
    disclosed_gaps: tuple[str, ...]
    research_pick_eligible: bool
    paper_entry_eligible: bool
    pick_tier: PickTier | str
    base_strategy_score: float
    score_adjustment: float
    final_score: float
    entry_reference: float | None
    stop: float | None
    target: float | None
    reward_risk_ratio: float | None
    source_identity: str
    input_hash_sha256: str
    receipt_hash_sha256: str = ""
    research_only: bool = True
    broker_execution_enabled: bool = False

    _HASH_FIELDS: ClassVar[set[str]] = {"receipt_hash_sha256", "receipt_id"}

    def __post_init__(self) -> None:
        object.__setattr__(self, "pick_tier", PickTier(self.pick_tier))
        _iso_date(self.market_date, "market_date")
        _iso_datetime(self.decision_at, "decision_at")
        if not self.code_sha.strip() or not self.source_identity.strip():
            raise ValueError("code_sha and source_identity are required")
        if not _SHA256.fullmatch(self.input_hash_sha256):
            raise ValueError("input_hash_sha256 must be a SHA-256 hex digest")
        if self.broker_execution_enabled:
            raise ValueError("broker execution must remain disabled")
        _reject_nonfinite(self.to_dict(include_hash=False))
        ids = [item.condition_id for item in self.condition_results]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate condition IDs are not allowed")
        if self.reward_risk_ratio is not None and self.reward_risk_ratio < 0:
            raise ValueError("reward_risk_ratio cannot be negative")
        expected = self.compute_hash()
        if self.receipt_hash_sha256 and self.receipt_hash_sha256 != expected:
            raise ValueError("receipt_hash_sha256 does not match canonical payload")
        object.__setattr__(self, "receipt_hash_sha256", expected)
        expected_id = "sdr-" + expected[:24]
        if self.receipt_id and self.receipt_id != expected_id:
            raise ValueError("receipt_id must be derived from receipt hash")
        object.__setattr__(self, "receipt_id", expected_id)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = asdict(self)
        result["condition_results"] = [item.to_dict() for item in self.condition_results]
        result["pick_tier"] = getattr(self.pick_tier, "value", str(self.pick_tier))
        if not include_hash:
            result.pop("receipt_hash_sha256", None)
            result.pop("receipt_id", None)
        return result

    def compute_hash(self) -> str:
        return hashlib.sha256(
            canonical_json(self.to_dict(include_hash=False)).encode("utf-8")
        ).hexdigest()

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())


__all__ = [
    "ConditionCategory",
    "ConditionResult",
    "ConditionSpec",
    "ConditionStatus",
    "EvidenceClaim",
    "EvidenceResolutionRun",
    "PickTier",
    "StrategyDecisionReceipt",
    "canonical_json",
]
