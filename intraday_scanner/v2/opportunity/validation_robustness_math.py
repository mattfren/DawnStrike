"""Deterministic session-clustered arithmetic for WP005-C."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_identity,
    _require_schema,
    _require_unique,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _decimal_mean,
    _metric_decimal_context,
    _quantize_metric_decimal,
)
from intraday_scanner.v2.opportunity.validation_robustness_contracts import (
    RobustnessCalibrationPolicy,
)


@dataclass(frozen=True)
class SessionClusterConfidenceInterval(OutcomeContract):
    interval_id: str
    session_ids: tuple[str, ...]
    session_values_r: tuple[Decimal, ...]
    bootstrap_seed: int
    bootstrap_resamples: int
    lower_quantile: Decimal
    upper_quantile: Decimal
    mean_session_r: Decimal
    lower_bound_r: Decimal
    upper_bound_r: Decimal
    schema_version: str = "v2.opportunity.session_cluster_confidence_interval.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.session_cluster_confidence_interval.v1",
        )
        _require_identity(self.interval_id, "interval_id")
        if not self.session_ids or len(self.session_ids) != len(self.session_values_r):
            raise ValueError("confidence interval requires aligned session observations")
        _require_unique(list(self.session_ids), "confidence interval session")
        if self.session_ids != tuple(sorted(self.session_ids)):
            raise ValueError("confidence interval sessions must use canonical order")
        for session_id in self.session_ids:
            _require_identity(session_id, "confidence interval session")
        for value in self.session_values_r:
            if type(value) is not Decimal or not value.is_finite():
                raise ValueError("confidence interval values must be finite Decimals")
        if type(self.bootstrap_seed) is not int or self.bootstrap_seed < 0:
            raise ValueError("bootstrap seed must be a nonnegative integer")
        if type(self.bootstrap_resamples) is not int or self.bootstrap_resamples < 20:
            raise ValueError("bootstrap resamples must be at least 20")
        if not (
            type(self.lower_quantile) is Decimal
            and type(self.upper_quantile) is Decimal
            and Decimal("0") < self.lower_quantile < self.upper_quantile < Decimal("1")
        ):
            raise ValueError("confidence interval quantiles are invalid")
        expected_values = _clustered_bootstrap_values(
            tuple(zip(self.session_ids, self.session_values_r, strict=True)),
            seed=self.bootstrap_seed,
            resamples=self.bootstrap_resamples,
            lower_quantile=self.lower_quantile,
            upper_quantile=self.upper_quantile,
        )
        if (
            self.mean_session_r,
            self.lower_bound_r,
            self.upper_bound_r,
        ) != expected_values:
            raise ValueError("confidence interval values do not recompute")
        expected_id = stable_identity(
            "session-cluster-confidence-interval",
            _identity_payload(self, "interval_id"),
        )
        if self.interval_id != expected_id:
            raise ValueError("confidence interval identity does not match content")


def build_session_clustered_confidence_interval(
    observations: tuple[tuple[str, Decimal], ...],
    *,
    policy: RobustnessCalibrationPolicy,
) -> SessionClusterConfidenceInterval:
    canonical = tuple(sorted(observations, key=lambda item: item[0]))
    values_r = _clustered_bootstrap_values(
        canonical,
        seed=policy.bootstrap_seed,
        resamples=policy.bootstrap_resamples,
        lower_quantile=policy.confidence_lower_quantile,
        upper_quantile=policy.confidence_upper_quantile,
        policy=policy,
    )
    values = {
        "session_ids": tuple(item[0] for item in canonical),
        "session_values_r": tuple(item[1] for item in canonical),
        "bootstrap_seed": policy.bootstrap_seed,
        "bootstrap_resamples": policy.bootstrap_resamples,
        "lower_quantile": policy.confidence_lower_quantile,
        "upper_quantile": policy.confidence_upper_quantile,
        "mean_session_r": values_r[0],
        "lower_bound_r": values_r[1],
        "upper_bound_r": values_r[2],
        "schema_version": "v2.opportunity.session_cluster_confidence_interval.v1",
    }
    return SessionClusterConfidenceInterval(
        interval_id=stable_identity("session-cluster-confidence-interval", values),
        **values,  # type: ignore[arg-type]
    )


def _clustered_bootstrap_values(
    observations: tuple[tuple[str, Decimal], ...],
    *,
    seed: int,
    resamples: int,
    lower_quantile: Decimal,
    upper_quantile: Decimal,
    policy: RobustnessCalibrationPolicy | None = None,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return an order-invariant deterministic whole-session bootstrap interval."""

    canonical = tuple(sorted(observations, key=lambda item: item[0]))
    if not canonical:
        raise ValueError("clustered bootstrap population cannot be empty")
    if len({item[0] for item in canonical}) != len(canonical):
        raise ValueError("clustered bootstrap sessions must be unique")
    values = tuple(item[1] for item in canonical)
    if any(type(value) is not Decimal or not value.is_finite() for value in values):
        raise ValueError("clustered bootstrap values must be finite Decimals")
    if policy is None:
        context_policy: Any = _CanonicalRobustnessDecimalPolicy()
    else:
        context_policy = policy
    with _metric_decimal_context(context_policy):
        means: list[Decimal] = []
        size = len(values)
        for resample_ordinal in range(resamples):
            sample = tuple(
                values[_bootstrap_index(seed, resample_ordinal, draw_ordinal, size)]
                for draw_ordinal in range(size)
            )
            means.append(_decimal_mean(sample, context_policy))
        means.sort()
        lower_index = _quantile_index(lower_quantile, resamples, rounding=ROUND_FLOOR)
        upper_index = _quantile_index(upper_quantile, resamples, rounding=ROUND_CEILING)
        return (
            _quantize_metric_decimal(_decimal_mean(values, context_policy), context_policy),
            _quantize_metric_decimal(means[lower_index], context_policy),
            _quantize_metric_decimal(means[upper_index], context_policy),
        )


@dataclass(frozen=True)
class _CanonicalRobustnessDecimalPolicy:
    decimal_precision: int = 64
    decimal_scale: int = 12
    rounding_mode: str = "ROUND_HALF_EVEN"


def _bootstrap_index(seed: int, resample: int, draw: int, population_size: int) -> int:
    digest = hashlib.sha256(f"{seed}:{resample}:{draw}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % population_size


def _quantile_index(value: Decimal, count: int, *, rounding: str) -> int:
    scaled = value * Decimal(count - 1)
    return int(scaled.to_integral_value(rounding=rounding))


__all__ = [
    "SessionClusterConfidenceInterval",
    "build_session_clustered_confidence_interval",
]
