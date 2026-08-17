"""Immutable downstream outcome contracts and direct invariants.

This module is intentionally outside the real-time opportunity import graph.
"""

from __future__ import annotations

import hashlib
import json
import re
import types
from copy import deepcopy
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.contracts.serialization import contract_from_dict
from intraday_scanner.v2.opportunity.models import (
    OpportunityContract,
    StrategyDirection,
    stable_identity,
)
from intraday_scanner.v2.opportunity.risk import (
    CapabilityState,
    ExecutionRiskEvidence,
    RiskMetric,
    RiskValueStatus,
)

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
_PRIVATE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|secret|access[_-]?token|token|password|authorization)"
    r"\s*[:=]\s*\S+|\bbearer\s+\S+|https?://[^\s]*(?:@|api[_-]?key=|token=|secret=)"
    r"|(?:[A-Za-z]:[\\/]|\\\\|/Users/|/home/)|(?<![A-Za-z0-9])/(?!/)\S+"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password|authorization)"
)
class OutcomeHorizonKind(str, Enum):
    ELAPSED_SECONDS = "elapsed_seconds"
    SESSION_CLOSE = "session_close"


class OutcomeEntryRule(str, Enum):
    PLANNED_PRICE_TOUCH = "planned_price_touch"


class OutcomeAmbiguityPolicy(str, Enum):
    CENSOR = "censor"


class OutcomeCostSource(str, Enum):
    EXECUTION_RISK_EVIDENCE = "execution_risk_evidence"


class OutcomeCompleteness(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    PENDING = "pending"
    CENSORED = "censored"
    UNAVAILABLE = "unavailable"


class OutcomeEntryStatus(str, Enum):
    FILLED = "filled"
    NO_ENTRY = "no_entry"
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    ENTRY_BAR_AMBIGUOUS = "entry_bar_ambiguous"
    GAP_THROUGH_AMBIGUOUS = "gap_through_ambiguous"
    UNATTAINABLE = "unattainable"
    UNSUPPORTED = "unsupported"


class OutcomePathStatus(str, Enum):
    TARGET_FIRST = "target_first"
    STOP_FIRST = "stop_first"
    HORIZON_EXIT = "horizon_exit"
    NO_ENTRY = "no_entry"
    PENDING_HORIZON = "pending_horizon"
    ENTRY_BAR_AMBIGUOUS = "entry_bar_ambiguous"
    SAME_BAR_AMBIGUOUS = "same_bar_ambiguous"
    GAP_THROUGH_AMBIGUOUS = "gap_through_ambiguous"
    HALT_CENSORED = "halt_censored"
    CORPORATE_ACTION_CENSORED = "corporate_action_censored"
    MISSING_BARS = "missing_bars"
    UNATTAINABLE_FILL = "unattainable_fill"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED_EVIDENCE = "unsupported_evidence"


class OutcomeMetric(str, Enum):
    REFERENCE_HORIZON_RETURN = "reference_horizon_return"
    MAXIMUM_FAVORABLE_EXCURSION_R = "maximum_favorable_excursion_r"
    MAXIMUM_ADVERSE_EXCURSION_R = "maximum_adverse_excursion_r"
    SIMULATED_GROSS_R = "simulated_gross_r"
    SIMULATED_AFTER_COST_R = "simulated_after_cost_r"
    TIME_TO_TARGET_LOWER_BOUND = "time_to_target_lower_bound"
    TIME_TO_TARGET_UPPER_BOUND = "time_to_target_upper_bound"
    TIME_TO_STOP_LOWER_BOUND = "time_to_stop_lower_bound"
    TIME_TO_STOP_UPPER_BOUND = "time_to_stop_upper_bound"


class OutcomeUnit(str, Enum):
    FRACTION = "fraction"
    RATIO = "ratio"
    SECONDS = "seconds"


class OutcomeValueStatus(str, Enum):
    OBSERVED = "observed"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


class OutcomeReferencePriceKind(str, Enum):
    FIRST_POST_DECISION_OPEN = "first_post_decision_open"
    UNAVAILABLE = "unavailable"


class OutcomeMarketStatusKind(str, Enum):
    HALTED = "halted"
    OPEN = "open"
    RESUMED = "resumed"
    CLOSED = "closed"
    AUCTION = "auction"


CANONICAL_OUTCOME_METRICS = tuple(OutcomeMetric)
_METRIC_UNITS = {
    OutcomeMetric.REFERENCE_HORIZON_RETURN: OutcomeUnit.FRACTION,
    OutcomeMetric.MAXIMUM_FAVORABLE_EXCURSION_R: OutcomeUnit.RATIO,
    OutcomeMetric.MAXIMUM_ADVERSE_EXCURSION_R: OutcomeUnit.RATIO,
    OutcomeMetric.SIMULATED_GROSS_R: OutcomeUnit.RATIO,
    OutcomeMetric.SIMULATED_AFTER_COST_R: OutcomeUnit.RATIO,
    OutcomeMetric.TIME_TO_TARGET_LOWER_BOUND: OutcomeUnit.SECONDS,
    OutcomeMetric.TIME_TO_TARGET_UPPER_BOUND: OutcomeUnit.SECONDS,
    OutcomeMetric.TIME_TO_STOP_LOWER_BOUND: OutcomeUnit.SECONDS,
    OutcomeMetric.TIME_TO_STOP_UPPER_BOUND: OutcomeUnit.SECONDS,
}


class OutcomeContract(OpportunityContract):
    """Strict downstream boundary that rejects unknown or lossy raw payloads."""

    @classmethod
    def from_dict(cls, payload: dict[str, object]):
        normalized = deepcopy(payload)
        _normalize_coverage_missing_intervals(normalized)
        _strict_payload_prewalk(normalized, cls, cls.__name__)
        return contract_from_dict(cls, normalized)

    @classmethod
    def from_json(cls, payload: str):
        decoded = _strict_json_loads(payload)
        return cls.from_dict(decoded)


@dataclass(frozen=True)
class OutcomeHorizon(OutcomeContract):
    horizon_id: str
    decision_at: datetime
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    kind: OutcomeHorizonKind
    end_at: datetime
    elapsed_seconds: int | None
    schema_version: str = "v2.opportunity.outcome_horizon.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.outcome_horizon.v1")
        _require_identity(self.horizon_id, "horizon_id")
        _require_sanitized_text(self.exchange_session_id, "exchange_session_id")
        _require_aware(self.decision_at, "decision_at")
        for timestamp, name in (
            (self.session_open_at, "session_open_at"),
            (self.session_close_at, "session_close_at"),
            (self.end_at, "end_at"),
        ):
            _require_utc(timestamp, name)
        if not self.session_open_at <= self.decision_at < self.end_at <= self.session_close_at:
            raise ValueError("outcome horizon must lie inside the explicit decision session")
        if self.kind is OutcomeHorizonKind.ELAPSED_SECONDS:
            if (
                isinstance(self.elapsed_seconds, bool)
                or not isinstance(self.elapsed_seconds, int)
                or self.elapsed_seconds <= 0
            ):
                raise ValueError("elapsed horizon requires positive integral seconds")
            if self.end_at != self.decision_at + timedelta(seconds=self.elapsed_seconds):
                raise ValueError("elapsed horizon end does not match elapsed_seconds")
        elif self.elapsed_seconds is not None or self.end_at != self.session_close_at:
            raise ValueError("session-close horizon must end at the explicit session close")
        expected = stable_identity("outcome-horizon", _identity_payload(self, "horizon_id"))
        if self.horizon_id != expected:
            raise ValueError("outcome horizon identity does not match content")


@dataclass(frozen=True)
class OutcomeLabelPolicy(OutcomeContract):
    policy_id: str
    policy_version: str
    entry_rule: OutcomeEntryRule
    expected_bar_interval_seconds: int
    entry_bar_policy: OutcomeAmbiguityPolicy
    same_bar_policy: OutcomeAmbiguityPolicy
    gap_through_policy: OutcomeAmbiguityPolicy
    cost_source: OutcomeCostSource
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.outcome_label_policy.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.outcome_label_policy.v1")
        _require_identity(self.policy_id, "policy_id")
        _require_sanitized_text(self.policy_version, "policy_version")
        if (
            isinstance(self.expected_bar_interval_seconds, bool)
            or not isinstance(self.expected_bar_interval_seconds, int)
            or self.expected_bar_interval_seconds <= 0
        ):
            raise ValueError("expected_bar_interval_seconds must be a positive integer")
        if (
            self.entry_rule is not OutcomeEntryRule.PLANNED_PRICE_TOUCH
            or self.entry_bar_policy is not OutcomeAmbiguityPolicy.CENSOR
            or self.same_bar_policy is not OutcomeAmbiguityPolicy.CENSOR
            or self.gap_through_policy is not OutcomeAmbiguityPolicy.CENSOR
            or self.cost_source is not OutcomeCostSource.EXECUTION_RISK_EVIDENCE
        ):
            raise ValueError("unsupported outcome labeling policy")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("bounded outcome policy must remain research-only and non-promotable")
        expected = stable_identity("outcome-label-policy", _identity_payload(self, "policy_id"))
        if self.policy_id != expected:
            raise ValueError("outcome label policy identity does not match content")


@dataclass(frozen=True)
class OutcomeTouchInterval(OutcomeContract):
    observation_id: str
    observation_content_hash_sha256: str
    interval_start_at: datetime
    interval_end_at: datetime
    schema_version: str = "v2.opportunity.outcome_touch_interval.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(self.schema_version, "v2.opportunity.outcome_touch_interval.v1")
        _require_identity(self.observation_id, "observation_id")
        _require_hash(self.observation_content_hash_sha256, "observation_content_hash_sha256")
        _require_utc(self.interval_start_at, "interval_start_at")
        _require_utc(self.interval_end_at, "interval_end_at")
        if self.interval_start_at >= self.interval_end_at:
            raise ValueError("outcome touch interval is reversed or empty")


@dataclass(frozen=True)
class OutcomeNumericEvidence(OutcomeContract):
    metric: OutcomeMetric
    unit: OutcomeUnit
    value: Decimal | None
    status: OutcomeValueStatus
    observed_at: datetime | None
    source_observation_ids: tuple[str, ...]
    method: str
    reason: str | None = None
    schema_version: str = "v2.opportunity.outcome_numeric_evidence.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.outcome_numeric_evidence.v1",
        )
        if self.unit is not _METRIC_UNITS[self.metric]:
            raise ValueError("outcome metric unit does not match canonical metric")
        _require_sanitized_text(self.method, "outcome metric method")
        _require_unique(list(self.source_observation_ids), "outcome metric source observation")
        for value in self.source_observation_ids:
            _require_identity(value, "source_observation_id")
        if self.status is OutcomeValueStatus.UNAVAILABLE:
            if (
                self.value is not None
                or self.reason is None
                or self.observed_at is not None
                or self.source_observation_ids
            ):
                raise ValueError("unavailable outcome metric requires null value and reason")
            _require_sanitized_text(self.reason, "outcome metric reason")
        else:
            if type(self.value) is not Decimal or not self.value.is_finite():
                raise ValueError("available outcome metric requires a finite Decimal value")
            if self.status is not OutcomeValueStatus.DERIVED:
                raise ValueError("canonical outcome metrics are derived-only")
            if self.observed_at is None or not self.source_observation_ids:
                raise ValueError("available outcome metric requires causal observation lineage")
            if self.reason is not None:
                raise ValueError("available outcome metric cannot carry a reason")
        if self.observed_at is not None:
            _require_utc(self.observed_at, "outcome metric observed_at")




def build_outcome_horizon(
    *,
    decision_at: datetime,
    exchange_session_id: str,
    session_open_at: datetime,
    session_close_at: datetime,
    kind: OutcomeHorizonKind,
    elapsed_seconds: int | None = None,
) -> OutcomeHorizon:
    end_at = (
        decision_at.astimezone(timezone.utc) + timedelta(seconds=elapsed_seconds)
        if kind is OutcomeHorizonKind.ELAPSED_SECONDS and isinstance(elapsed_seconds, int)
        else session_close_at
    )
    values = {
        "decision_at": decision_at,
        "exchange_session_id": exchange_session_id,
        "session_open_at": session_open_at,
        "session_close_at": session_close_at,
        "kind": kind,
        "end_at": end_at,
        "elapsed_seconds": elapsed_seconds,
        "schema_version": "v2.opportunity.outcome_horizon.v1",
    }
    return OutcomeHorizon(
        horizon_id=stable_identity("outcome-horizon", values),
        decision_at=decision_at,
        exchange_session_id=exchange_session_id,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
        kind=kind,
        end_at=end_at,
        elapsed_seconds=elapsed_seconds,
    )


def build_outcome_label_policy(
    *,
    policy_version: str,
    expected_bar_interval_seconds: int,
) -> OutcomeLabelPolicy:
    values = {
        "policy_version": policy_version,
        "entry_rule": OutcomeEntryRule.PLANNED_PRICE_TOUCH,
        "expected_bar_interval_seconds": expected_bar_interval_seconds,
        "entry_bar_policy": OutcomeAmbiguityPolicy.CENSOR,
        "same_bar_policy": OutcomeAmbiguityPolicy.CENSOR,
        "gap_through_policy": OutcomeAmbiguityPolicy.CENSOR,
        "cost_source": OutcomeCostSource.EXECUTION_RISK_EVIDENCE,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.outcome_label_policy.v1",
    }
    return OutcomeLabelPolicy(
        policy_id=stable_identity("outcome-label-policy", values),
        policy_version=policy_version,
        entry_rule=OutcomeEntryRule.PLANNED_PRICE_TOUCH,
        expected_bar_interval_seconds=expected_bar_interval_seconds,
        entry_bar_policy=OutcomeAmbiguityPolicy.CENSOR,
        same_bar_policy=OutcomeAmbiguityPolicy.CENSOR,
        gap_through_policy=OutcomeAmbiguityPolicy.CENSOR,
        cost_source=OutcomeCostSource.EXECUTION_RISK_EVIDENCE,
    )


def _after_cost_value(
    *,
    risk: ExecutionRiskEvidence | None,
    modeled_entry: Decimal,
    modeled_stop: Decimal,
    modeled_exit: Decimal,
    sign: Decimal,
) -> Decimal | None:
    if risk is None:
        return None
    entry = risk.metric(RiskMetric.ENTRY_PRICE)
    stop = risk.metric(RiskMetric.STOP_PRICE)
    quantity = risk.metric(RiskMetric.QUANTITY)
    total_cost = risk.metric(RiskMetric.TOTAL_ROUND_TRIP_COST)
    required = (entry, stop, quantity, total_cost)
    if any(
        item.status not in {RiskValueStatus.OBSERVED, RiskValueStatus.DERIVED}
        or item.capability_state is not CapabilityState.AVAILABLE
        or type(item.value) is not Decimal
        for item in required
    ):
        return None
    if entry.value != modeled_entry or stop.value != modeled_stop:
        return None
    assert quantity.value is not None
    assert total_cost.value is not None
    stop_distance = abs(modeled_entry - modeled_stop)
    return (
        quantity.value * sign * (modeled_exit - modeled_entry) - total_cost.value
    ) / (quantity.value * stop_distance + total_cost.value)


def _direction_sign(direction: StrategyDirection) -> Decimal | None:
    if direction is StrategyDirection.LONG:
        return Decimal("1")
    if direction is StrategyDirection.SHORT:
        return Decimal("-1")
    return None


def _timedelta_decimal_seconds(value: timedelta) -> Decimal:
    return (
        Decimal(value.days * 86400 + value.seconds)
        + Decimal(value.microseconds) / Decimal("1000000")
    )


















def _identity_payload(value: OpportunityContract, identity_field: str) -> dict[str, object]:
    return {
        name: item
        for name, item in value.__dict__.items()
        if name != identity_field
    }


def _normalize_coverage_missing_intervals(value: object) -> None:
    if isinstance(value, dict):
        if "coverage_receipt" in value and isinstance(value["coverage_receipt"], dict):
            receipt = value["coverage_receipt"]
            intervals = receipt.get("missing_intervals")
            if isinstance(intervals, list):
                receipt["missing_intervals"] = [
                    [
                        datetime.fromisoformat(item.replace("Z", "+00:00"))
                        if isinstance(item, str)
                        else item
                        for item in interval
                    ]
                    for interval in intervals
                ]
        for item in value.values():
            _normalize_coverage_missing_intervals(item)
    elif isinstance(value, list):
        for item in value:
            _normalize_coverage_missing_intervals(item)


def _strict_json_loads(payload: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    decoded = json.loads(payload, object_pairs_hook=reject_duplicates)
    if not isinstance(decoded, dict):
        raise ValueError("contract JSON root must be an object")
    return decoded


def _strict_payload_prewalk(value: object, expected: object, path: str) -> None:
    origin = get_origin(expected)
    args = get_args(expected)
    if expected in {Any, object}:
        return
    if origin in {Union, types.UnionType}:
        errors: list[ValueError] = []
        for option in args:
            try:
                _strict_payload_prewalk(value, option, path)
            except ValueError as exc:
                errors.append(exc)
            else:
                return
        raise ValueError(f"{path} does not match its declared union: {errors[0]}")
    if expected is type(None):
        if value is not None:
            raise ValueError(f"{path} must be null")
        return
    if expected is Decimal:
        if not isinstance(value, Decimal | str) or isinstance(value, bool):
            raise ValueError(f"{path} must use an exact Decimal or canonical Decimal string")
        return
    if expected is datetime:
        if not isinstance(value, datetime | str):
            raise ValueError(f"{path} must use a datetime or ISO datetime string")
        return
    if origin is tuple:
        if not isinstance(value, tuple | list):
            raise ValueError(f"{path} must be an array")
        if args and args[-1] is Ellipsis:
            for index, item in enumerate(value):
                _strict_payload_prewalk(item, args[0], f"{path}[{index}]")
        elif args:
            if len(value) != len(args):
                raise ValueError(f"{path} must have exact tuple cardinality")
            for index, (item, item_type) in enumerate(zip(value, args, strict=True)):
                _strict_payload_prewalk(item, item_type, f"{path}[{index}]")
        return
    if origin is list:
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        item_type = args[0] if args else Any
        for index, item in enumerate(value):
            _strict_payload_prewalk(item, item_type, f"{path}[{index}]")
        return
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        key_type = args[0] if args else Any
        item_type = args[1] if len(args) > 1 else Any
        for key, item in value.items():
            _strict_payload_prewalk(key, key_type, f"{path}.<key>")
            _strict_payload_prewalk(item, item_type, f"{path}.{key}")
        return
    if isinstance(expected, type) and is_dataclass(expected):
        if isinstance(value, expected):
            return
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        field_map = {field.name: field for field in fields(expected)}
        unknown = set(value) - set(field_map)
        if unknown:
            raise ValueError(f"{path} contains unknown field(s): {sorted(unknown)}")
        hints = get_type_hints(expected)
        for name, item in value.items():
            _strict_payload_prewalk(item, hints.get(name, Any), f"{path}.{name}")
        return
    if isinstance(expected, type) and issubclass(expected, Enum):
        if not isinstance(value, expected | str):
            raise ValueError(f"{path} must use the declared enum value")
        return
    if isinstance(expected, type) and not isinstance(value, expected):
        raise ValueError(f"{path} has the wrong runtime type")


def _contract_hash(value: object) -> str:
    return hashlib.sha256(contract_to_json(value).encode("utf-8")).hexdigest()


def _require_identity(value: str, field_name: str) -> None:
    if not _IDENTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a sanitized nonblank identity")


def _require_hash(value: str, field_name: str) -> None:
    if not _HASH_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_sanitized_text(value: str, field_name: str) -> None:
    if not value.strip() or _PRIVATE_VALUE.search(value):
        raise ValueError(f"{field_name} must be sanitized nonblank text")


def _validate_safe_nested(value: object, field_name: str) -> None:
    if value is None or isinstance(value, bool | int | Decimal):
        return
    if isinstance(value, float):
        raise ValueError(f"{field_name} cannot contain float values")
    if isinstance(value, str):
        _require_sanitized_text(value, field_name)
        return
    if isinstance(value, tuple | list):
        for item in value:
            _validate_safe_nested(item, field_name)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)):
                raise ValueError(f"{field_name} contains a sensitive key")
            _require_sanitized_text(str(key), field_name)
            _validate_safe_nested(item, field_name)
        return
    raise ValueError(f"{field_name} contains an unsupported value type")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


def _require_schema(value: str, expected: str) -> None:
    if value != expected:
        raise ValueError(f"unsupported schema_version: {value}")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_positive_decimal(value: Decimal, field_name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a finite positive Decimal")


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")
