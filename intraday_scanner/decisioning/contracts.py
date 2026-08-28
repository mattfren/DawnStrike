"""Immutable contracts for strategy decision evidence and identity."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from enum import StrEnum
from typing import Any, ClassVar
from urllib.parse import urlsplit


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
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


def canonical_json(value: Any) -> str:
    """Serialize JSON without non-finite values or implementation-dependent spacing."""

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    return value


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
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")


def _iso_date(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be an ISO date")
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _public_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _require_string(value: Any, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} is required")
    return value


def _require_string_sequence(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} must contain strings")
    return tuple(value)


def _require_number(value: Any, field_name: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")


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
        object.__setattr__(self, "required_source_types", _tuple(self.required_source_types))
        if not self.condition_id.strip() or not self.strategy_id.strip():
            raise ValueError("condition_id and strategy_id are required")
        if self.freshness_limit_seconds is not None and self.freshness_limit_seconds < 0:
            raise ValueError("freshness_limit_seconds cannot be negative")
        if not self.resolver_id.strip() or not self.missing_policy.strip():
            raise ValueError("resolver_id and missing_policy are required")
        if not self.policy_version.strip():
            raise ValueError("policy_version is required")

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
        _require_string(self.condition_id, "condition_id")
        if not isinstance(self.status, (str, ConditionStatus)):
            raise ValueError("status must be a condition-status string")
        for field_name in (
            "reason",
            "resolver_id",
            "resolution_method",
            "requested_model",
            "actual_model",
        ):
            _require_string(getattr(self, field_name), field_name, allow_empty=True)
        for field_name in ("observed_at", "effective_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_string(value, field_name)
        for field_name in (
            "source_urls",
            "source_hashes",
            "contradictions",
            "unresolved_unknowns",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_string_sequence(getattr(self, field_name), field_name),
            )
        if self.confidence is not None:
            _require_number(self.confidence, "confidence")
        object.__setattr__(self, "status", ConditionStatus(self.status))
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
        object.__setattr__(self, "source_urls", _tuple(self.source_urls))
        object.__setattr__(self, "source_hashes", _tuple(self.source_hashes))
        if (
            not self.claim_id.strip()
            or not self.condition_id.strip()
            or not self.claim_type.strip()
        ):
            raise ValueError("claim identity and type are required")
        if not self.statement.strip():
            raise ValueError("evidence claim statement is required")
        _iso_datetime(self.published_at, "published_at")
        if self.effective_at:
            _iso_datetime(self.effective_at, "effective_at")
        if not self.source_urls:
            raise ValueError("evidence claims require cited source URLs")
        if len(self.source_urls) != len(self.source_hashes):
            raise ValueError("source URL and hash counts must match")
        if any(not _public_url(url) for url in self.source_urls):
            raise ValueError("evidence claims require public HTTP(S) URLs")
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
    started_at: str = ""
    completed_at: str = ""

    def __post_init__(self) -> None:
        _iso_date(self.market_date, "market_date")
        symbol = self.symbol.strip().upper()
        if not _SYMBOL.fullmatch(symbol) or not self.source_identity.strip():
            raise ValueError("symbol and source_identity are required")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "condition_ids", _tuple(self.condition_ids))
        if len(self.condition_ids) != len(set(self.condition_ids)):
            raise ValueError("duplicate condition IDs are not allowed")
        if not self.requested_model.strip() or not self.actual_model.strip():
            raise ValueError("requested and actual model identity are required")
        if self.started_at:
            _iso_datetime(self.started_at, "started_at")
        if self.completed_at:
            _iso_datetime(self.completed_at, "completed_at")
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
    input_payload_json: str = ""
    plan_hash_sha256: str = ""
    gross_reward_risk_ratio: float | None = None
    after_cost_reward_risk_ratio: float | None = None
    stop_distance_pct: float | None = None
    paper_entry_blockers: tuple[str, ...] = ()

    _HASH_FIELDS: ClassVar[set[str]] = {"receipt_hash_sha256", "receipt_id"}

    def __post_init__(self) -> None:
        for field_name in (
            "schema_version",
            "receipt_id",
            "strategy_id",
            "strategy_version",
            "symbol",
            "market_date",
            "decision_at",
            "code_sha",
            "policy_version",
            "source_identity",
            "input_hash_sha256",
            "receipt_hash_sha256",
            "input_payload_json",
            "plan_hash_sha256",
        ):
            _require_string(
                getattr(self, field_name),
                field_name,
                allow_empty=field_name
                in {
                    "receipt_id",
                    "receipt_hash_sha256",
                    "input_payload_json",
                    "plan_hash_sha256",
                },
            )
        if self.first_blocking_failure is not None:
            _require_string(self.first_blocking_failure, "first_blocking_failure")
        if not isinstance(self.pick_tier, (str, PickTier)):
            raise ValueError("pick_tier must be a pick-tier string")
        for field_name in (
            "research_pick_eligible",
            "paper_entry_eligible",
            "research_only",
            "broker_execution_enabled",
        ):
            if type(getattr(self, field_name)) is not bool:
                raise ValueError(f"{field_name} must be a boolean")
        for field_name in (
            "base_strategy_score",
            "score_adjustment",
            "final_score",
        ):
            _require_number(getattr(self, field_name), field_name)
        for field_name in (
            "entry_reference",
            "stop",
            "target",
            "reward_risk_ratio",
            "gross_reward_risk_ratio",
            "after_cost_reward_risk_ratio",
            "stop_distance_pct",
        ):
            _require_number(getattr(self, field_name), field_name, optional=True)
        for field_name in (
            "all_blocking_failures",
            "disclosed_gaps",
            "paper_entry_blockers",
        ):
            object.__setattr__(
                self,
                field_name,
                _require_string_sequence(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "pick_tier", PickTier(self.pick_tier))
        symbol = self.symbol.strip().upper()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("symbol must be a valid uppercase ticker")
        object.__setattr__(self, "symbol", symbol)
        _iso_date(self.market_date, "market_date")
        _iso_datetime(self.decision_at, "decision_at")
        if (
            self.schema_version == "dawnstrike.strategy_decision_receipt.v2"
            and not _GIT_SHA.fullmatch(self.code_sha)
        ):
            raise ValueError("v2 receipt code_sha must be a full lowercase Git SHA")
        if not isinstance(self.condition_results, tuple):
            if not isinstance(self.condition_results, list):
                raise ValueError("condition_results must be a list or tuple")
            object.__setattr__(self, "condition_results", tuple(self.condition_results))
        if any(not isinstance(item, ConditionResult) for item in self.condition_results):
            raise ValueError("condition_results must contain typed ConditionResult values")
        if not _SHA256.fullmatch(self.input_hash_sha256):
            raise ValueError("input_hash_sha256 must be a SHA-256 hex digest")
        if self.schema_version == "dawnstrike.strategy_decision_receipt.v2":
            if not self.input_payload_json:
                raise ValueError("v2 receipt requires canonical input_payload_json")
            try:
                input_payload = json.loads(self.input_payload_json)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("input_payload_json must be valid JSON") from exc
            if canonical_json(input_payload) != self.input_payload_json:
                raise ValueError("input_payload_json must be canonical JSON")
            expected_input_hash = hashlib.sha256(
                self.input_payload_json.encode("utf-8")
            ).hexdigest()
            if self.input_hash_sha256 != expected_input_hash:
                raise ValueError("input_hash_sha256 does not match input_payload_json")
        if self.plan_hash_sha256 and not _SHA256.fullmatch(self.plan_hash_sha256):
            raise ValueError("plan_hash_sha256 must be a SHA-256 hex digest")
        if self.paper_entry_eligible and self.paper_entry_blockers:
            raise ValueError("paper-entry eligible receipt cannot carry paper blockers")
        if self.broker_execution_enabled:
            raise ValueError("broker execution must remain disabled")
        if not self.research_only:
            raise ValueError("strategy decision receipts must remain research-only")
        if self.paper_entry_eligible and not self.research_pick_eligible:
            raise ValueError("paper entry cannot be eligible when research pick is not eligible")
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
        if self.schema_version == "dawnstrike.strategy_decision_receipt.v1":
            # Preserve the canonical identity of historical v1 receipts while
            # v2 carries replayable input and explicit frozen-plan cost truth.
            for key in (
                "input_payload_json",
                "plan_hash_sha256",
                "gross_reward_risk_ratio",
                "after_cost_reward_risk_ratio",
                "stop_distance_pct",
                "paper_entry_blockers",
            ):
                result.pop(key, None)
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


def parse_strategy_decision_receipt(
    payload: Any,
    *,
    require_v2: bool = False,
) -> StrategyDecisionReceipt:
    """Rebuild and exactly validate a serialized strategy decision receipt.

    Reconstructing the frozen dataclass is the single canonical validation
    boundary for hashes, IDs, finite numerics, research-only safety, and v2
    input replay.  The final canonical equality check rejects both omitted and
    unrecognized fields instead of silently accepting a partial projection.
    """

    if not isinstance(payload, dict):
        raise ValueError("strategy decision receipt must be a JSON object")
    receipt_payload = {
        contract_field.name: payload[contract_field.name]
        for contract_field in fields(StrategyDecisionReceipt)
        if contract_field.name in payload
    }
    condition_results = receipt_payload.get("condition_results") or []
    if not isinstance(condition_results, (list, tuple)):
        raise ValueError("strategy decision receipt condition_results must be a list")
    try:
        receipt_payload["condition_results"] = tuple(
            item if isinstance(item, ConditionResult) else ConditionResult(**item)
            for item in condition_results
        )
        receipt = StrategyDecisionReceipt(**receipt_payload)
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        raise ValueError("strategy decision receipt typed schema is invalid") from exc
    if require_v2 and receipt.schema_version != "dawnstrike.strategy_decision_receipt.v2":
        raise ValueError("strategy decision receipt must use replayable v2 schema")
    if canonical_json(receipt.to_dict()) != canonical_json(payload):
        raise ValueError("strategy decision receipt payload is not exact")
    return receipt


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
    "parse_strategy_decision_receipt",
]
