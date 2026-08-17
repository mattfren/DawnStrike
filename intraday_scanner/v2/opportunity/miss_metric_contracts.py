"""Immutable definitions for downstream discovery metrics.

Metric definitions are deliberately fixed rather than caller-authored.  Exact
integer populations are primary; the Decimal fraction is a definition-bound,
quantized projection.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import Enum
from typing import Any

from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    MissQualificationPolicy,
    QualificationClaimKind,
    QualificationHorizonKind,
    identity_payload,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
)
from intraday_scanner.v2.opportunity.models import stable_identity


class DiscoveryMetricName(str, Enum):
    DAILY_OPPORTUNITY_RECALL = "daily_opportunity_recall"
    TOP_1_RECALL = "top_1_recall"
    TOP_3_RECALL = "top_3_recall"
    TOP_5_RECALL = "top_5_recall"
    PRECISION_AT_1 = "precision_at_1"
    PRECISION_AT_3 = "precision_at_3"
    PRECISION_AT_5 = "precision_at_5"
    FALSE_POSITIVE_RATE = "false_positive_rate"
    NO_TRADE_ACCURACY = "no_trade_accuracy"


class DiscoveryMetricStatus(str, Enum):
    AVAILABLE = "available"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"


class DiscoveryMetricScope(str, Enum):
    SESSION = "session"
    MULTI_SESSION = "multi_session"


class DiscoveryMetricUnit(str, Enum):
    FRACTION = "fraction"


class DiscoveryMetricRoundingMode(str, Enum):
    HALF_EVEN = "ROUND_HALF_EVEN"


METRIC_DEFINITION_VERSION = "discovery-metric-definition-v1"
METRIC_MATCHING_POLICY_VERSION = "strategy-agnostic-session-symbol-direction-v1"
METRIC_DECIMAL_PRECISION = 64
METRIC_FRACTION_SCALE = 12
METRIC_FRACTION_QUANTIZER = Decimal("0.000000000001")
METRIC_ROUNDING_MODE = DiscoveryMetricRoundingMode.HALF_EVEN
METRIC_CLAIM_KINDS = (
    QualificationClaimKind.EXECUTABLE_TRADE,
    QualificationClaimKind.PRICE_MOVE_PROXY,
)


@dataclass(frozen=True)
class DiscoveryMetricHorizonDefinition(MissContract):
    horizon_definition_id: str
    kind: QualificationHorizonKind
    elapsed_seconds: int | None
    schema_version: str = "v2.opportunity.discovery_metric_horizon_definition.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.discovery_metric_horizon_definition.v1",
        )
        require_identity(self.horizon_definition_id, "horizon_definition_id")
        if self.kind is QualificationHorizonKind.ELAPSED_FROM_ENTRY:
            if (
                isinstance(self.elapsed_seconds, bool)
                or not isinstance(self.elapsed_seconds, int)
                or self.elapsed_seconds <= 0
            ):
                raise ValueError("elapsed metric horizon requires positive integral seconds")
        elif self.elapsed_seconds is not None:
            raise ValueError("session-close metric horizon cannot carry elapsed seconds")
        expected = stable_identity(
            "discovery-metric-horizon-definition",
            identity_payload(self, "horizon_definition_id"),
        )
        if self.horizon_definition_id != expected:
            raise ValueError("metric horizon definition identity does not match content")


@dataclass(frozen=True)
class DiscoveryMetricDefinition(MissContract):
    definition_id: str
    name: DiscoveryMetricName
    definition_version: str
    matching_policy_version: str
    numerator_population: str
    denominator_population: str
    top_k: int | None
    unit: DiscoveryMetricUnit
    decimal_precision: int
    fraction_scale: int
    fraction_quantizer: Decimal
    rounding_mode: DiscoveryMetricRoundingMode
    schema_version: str = "v2.opportunity.discovery_metric_definition.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.discovery_metric_definition.v1",
        )
        require_identity(self.definition_id, "definition_id")
        for value, name in (
            (self.definition_version, "definition_version"),
            (self.matching_policy_version, "matching_policy_version"),
            (self.numerator_population, "numerator_population"),
            (self.denominator_population, "denominator_population"),
        ):
            require_sanitized(value, name)
        expected_values = _definition_values(self.name)
        actual_values = {
            "name": self.name,
            "definition_version": self.definition_version,
            "matching_policy_version": self.matching_policy_version,
            "numerator_population": self.numerator_population,
            "denominator_population": self.denominator_population,
            "top_k": self.top_k,
            "unit": self.unit,
            "decimal_precision": self.decimal_precision,
            "fraction_scale": self.fraction_scale,
            "fraction_quantizer": self.fraction_quantizer,
            "rounding_mode": self.rounding_mode,
            "schema_version": self.schema_version,
        }
        if actual_values != expected_values:
            raise ValueError("metric definition does not match canonical v1 semantics")
        expected = stable_identity("discovery-metric-definition", expected_values)
        if self.definition_id != expected:
            raise ValueError("metric definition identity does not match content")


@dataclass(frozen=True)
class DiscoveryMetricPolicy(MissContract):
    metric_policy_id: str
    policy_version: str
    qualification_policy_id: str
    qualification_policy_content_hash_sha256: str
    qualification_policy: MissQualificationPolicy
    horizon_definition: DiscoveryMetricHorizonDefinition
    definitions: tuple[DiscoveryMetricDefinition, ...]
    accepted_claim_kinds: tuple[QualificationClaimKind, ...]
    strict_cutoff: bool = True
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.discovery_metric_policy.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.discovery_metric_policy.v1")
        require_identity(self.metric_policy_id, "metric_policy_id")
        require_identity(self.qualification_policy_id, "qualification_policy_id")
        require_hash(
            self.qualification_policy_content_hash_sha256,
            "qualification_policy_content_hash_sha256",
        )
        require_sanitized(self.policy_version, "metric policy version")
        if (
            self.qualification_policy_id != self.qualification_policy.policy_id
            or self.qualification_policy_content_hash_sha256
            != self.qualification_policy.content_hash()
        ):
            raise ValueError("metric policy does not bind exact qualification policy")
        if self.definitions != canonical_metric_definitions():
            raise ValueError("metric policy requires exact canonical metric definitions")
        require_unique(
            tuple(item.definition_id for item in self.definitions),
            "metric definition",
        )
        if self.accepted_claim_kinds != METRIC_CLAIM_KINDS:
            raise ValueError("metric policy must include both accepted qualification claims")
        if not self.strict_cutoff:
            raise ValueError("metric policy requires strict cutoff semantics")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("metric policy must remain research-only")
        expected = stable_identity(
            "discovery-metric-policy",
            identity_payload(self, "metric_policy_id"),
        )
        if self.metric_policy_id != expected:
            raise ValueError("metric policy identity does not match content")


def build_discovery_metric_horizon_definition(
    *,
    kind: QualificationHorizonKind,
    elapsed_seconds: int | None,
) -> DiscoveryMetricHorizonDefinition:
    values: dict[str, Any] = {
        "kind": kind,
        "elapsed_seconds": elapsed_seconds,
        "schema_version": "v2.opportunity.discovery_metric_horizon_definition.v1",
    }
    return DiscoveryMetricHorizonDefinition(
        horizon_definition_id=stable_identity(
            "discovery-metric-horizon-definition",
            values,
        ),
        **values,
    )


def build_discovery_metric_policy(
    *,
    policy_version: str,
    qualification_policy: MissQualificationPolicy,
    horizon_kind: QualificationHorizonKind,
    elapsed_seconds: int | None,
) -> DiscoveryMetricPolicy:
    horizon = build_discovery_metric_horizon_definition(
        kind=horizon_kind,
        elapsed_seconds=elapsed_seconds,
    )
    values: dict[str, Any] = {
        "policy_version": policy_version,
        "qualification_policy_id": qualification_policy.policy_id,
        "qualification_policy_content_hash_sha256": qualification_policy.content_hash(),
        "qualification_policy": qualification_policy,
        "horizon_definition": horizon,
        "definitions": canonical_metric_definitions(),
        "accepted_claim_kinds": METRIC_CLAIM_KINDS,
        "strict_cutoff": True,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.discovery_metric_policy.v1",
    }
    return DiscoveryMetricPolicy(
        metric_policy_id=stable_identity("discovery-metric-policy", values),
        **values,
    )


def canonical_metric_definitions() -> tuple[DiscoveryMetricDefinition, ...]:
    return tuple(_build_metric_definition(name) for name in DiscoveryMetricName)


def quantize_metric_fraction(numerator: int, denominator: int) -> Decimal:
    _require_nonnegative_int(numerator, "metric numerator")
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator <= 0:
        raise ValueError("metric denominator must be a positive integer")
    if numerator > denominator:
        raise ValueError("metric numerator cannot exceed denominator")
    with localcontext() as context:
        context.prec = METRIC_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return (Decimal(numerator) / Decimal(denominator)).quantize(
            METRIC_FRACTION_QUANTIZER,
            rounding=ROUND_HALF_EVEN,
        )


def _build_metric_definition(name: DiscoveryMetricName) -> DiscoveryMetricDefinition:
    values = _definition_values(name)
    return DiscoveryMetricDefinition(
        definition_id=stable_identity("discovery-metric-definition", values),
        **values,
    )


def _definition_values(name: DiscoveryMetricName) -> dict[str, Any]:
    populations = {
        DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL: (
            "qualified_with_on_time_watch_or_take",
            "qualified_opportunity_units",
            None,
        ),
        DiscoveryMetricName.TOP_1_RECALL: (
            "qualified_with_on_time_top_1_rank",
            "qualified_opportunity_units",
            1,
        ),
        DiscoveryMetricName.TOP_3_RECALL: (
            "qualified_with_on_time_top_3_rank",
            "qualified_opportunity_units",
            3,
        ),
        DiscoveryMetricName.TOP_5_RECALL: (
            "qualified_with_on_time_top_5_rank",
            "qualified_opportunity_units",
            5,
        ),
        DiscoveryMetricName.PRECISION_AT_1: (
            "qualified_on_time_top_1_predictions",
            "on_time_top_1_prediction_units",
            1,
        ),
        DiscoveryMetricName.PRECISION_AT_3: (
            "qualified_on_time_top_3_predictions",
            "on_time_top_3_prediction_units",
            3,
        ),
        DiscoveryMetricName.PRECISION_AT_5: (
            "qualified_on_time_top_5_predictions",
            "on_time_top_5_prediction_units",
            5,
        ),
        DiscoveryMetricName.FALSE_POSITIVE_RATE: (
            "not_qualified_with_on_time_watch_or_take",
            "not_qualified_assessment_units",
            None,
        ),
        DiscoveryMetricName.NO_TRADE_ACCURACY: (
            "correct_complete_no_trade_sessions",
            "complete_all_session_no_trade_predictions",
            None,
        ),
    }
    numerator, denominator, top_k = populations[name]
    return {
        "name": name,
        "definition_version": METRIC_DEFINITION_VERSION,
        "matching_policy_version": METRIC_MATCHING_POLICY_VERSION,
        "numerator_population": numerator,
        "denominator_population": denominator,
        "top_k": top_k,
        "unit": DiscoveryMetricUnit.FRACTION,
        "decimal_precision": METRIC_DECIMAL_PRECISION,
        "fraction_scale": METRIC_FRACTION_SCALE,
        "fraction_quantizer": METRIC_FRACTION_QUANTIZER,
        "rounding_mode": METRIC_ROUNDING_MODE,
        "schema_version": "v2.opportunity.discovery_metric_definition.v1",
    }


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")


__all__ = [
    "DiscoveryMetricDefinition",
    "DiscoveryMetricHorizonDefinition",
    "DiscoveryMetricName",
    "DiscoveryMetricPolicy",
    "DiscoveryMetricRoundingMode",
    "DiscoveryMetricScope",
    "DiscoveryMetricStatus",
    "DiscoveryMetricUnit",
    "build_discovery_metric_horizon_definition",
    "build_discovery_metric_policy",
    "canonical_metric_definitions",
    "quantize_metric_fraction",
]
