"""Fresh-context Decimal arithmetic for validation trading metrics."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Protocol

METRIC_DECIMAL_PRECISION = 64
METRIC_DECIMAL_SCALE = 12
METRIC_DECIMAL_EMIN = -999999
METRIC_DECIMAL_EMAX = 999999


class _MetricDecimalPolicy(Protocol):
    @property
    def decimal_precision(self) -> int: ...

    @property
    def decimal_scale(self) -> int: ...

    @property
    def rounding_mode(self) -> str: ...


def _new_decimal_context(*, precision: int = METRIC_DECIMAL_PRECISION) -> Context:
    """Return a fully specified context independent of caller Decimal state."""

    return Context(
        prec=precision,
        rounding=ROUND_HALF_EVEN,
        Emin=METRIC_DECIMAL_EMIN,
        Emax=METRIC_DECIMAL_EMAX,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


@contextmanager
def _fresh_decimal_context(
    *, precision: int = METRIC_DECIMAL_PRECISION
) -> Iterator[Context]:
    with localcontext(_new_decimal_context(precision=precision)) as context:
        context.clear_flags()
        yield context


@contextmanager
def _metric_decimal_context(policy: _MetricDecimalPolicy) -> Iterator[Context]:
    if (
        policy.decimal_precision != METRIC_DECIMAL_PRECISION
        or policy.decimal_scale != METRIC_DECIMAL_SCALE
        or policy.rounding_mode != "ROUND_HALF_EVEN"
    ):
        raise ValueError("validation metric policy Decimal context is not canonical")
    with _fresh_decimal_context() as context:
        yield context


def _quantize_metric_decimal(
    value: Decimal,
    policy: _MetricDecimalPolicy,
) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("validation metric value must be a finite Decimal")
    with _metric_decimal_context(policy) as context:
        quantum = Decimal(1).scaleb(-policy.decimal_scale, context=context)
        return value.quantize(quantum, context=context)


def _timedelta_decimal_seconds(
    value: timedelta,
    policy: _MetricDecimalPolicy,
) -> Decimal:
    total_microseconds = (
        (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds
    )
    with _metric_decimal_context(policy):
        return Decimal(total_microseconds) / Decimal(1_000_000)


def _decimal_sum(
    values: tuple[Decimal, ...],
    policy: _MetricDecimalPolicy,
) -> Decimal:
    with _metric_decimal_context(policy):
        total = Decimal("0")
        for value in values:
            total += value
        return total


def _decimal_mean(
    values: tuple[Decimal, ...],
    policy: _MetricDecimalPolicy,
) -> Decimal:
    if not values:
        raise ValueError("validation metric mean population cannot be empty")
    with _metric_decimal_context(policy):
        return _decimal_sum(values, policy) / Decimal(len(values))


def _decimal_ratio(
    numerator: Decimal,
    denominator: Decimal,
    policy: _MetricDecimalPolicy,
) -> Decimal:
    if denominator == 0:
        raise ValueError("validation metric denominator cannot be zero")
    with _metric_decimal_context(policy):
        return _quantize_metric_decimal(numerator / denominator, policy)


def _population_standard_deviation(
    values: tuple[Decimal, ...],
    policy: _MetricDecimalPolicy,
) -> Decimal:
    with _metric_decimal_context(policy) as context:
        mean = _decimal_sum(values, policy) / Decimal(len(values))
        squared = tuple((item - mean) ** 2 for item in values)
        variance = _decimal_sum(squared, policy) / Decimal(len(values))
        return variance.sqrt(context=context)


def _downside_deviation(
    values: tuple[Decimal, ...],
    target: Decimal,
    policy: _MetricDecimalPolicy,
) -> Decimal:
    with _metric_decimal_context(policy) as context:
        squared = tuple(min(item - target, Decimal("0")) ** 2 for item in values)
        lower_partial = _decimal_sum(squared, policy) / Decimal(len(values))
        return lower_partial.sqrt(context=context)


def _session_drawdown(
    values: tuple[Decimal, ...],
    policy: _MetricDecimalPolicy,
) -> tuple[Decimal, int]:
    with _metric_decimal_context(policy):
        cumulative = Decimal("0")
        peak = Decimal("0")
        minimum = Decimal("0")
        duration = 0
        current_duration = 0
        for value in values:
            cumulative += value
            if cumulative >= peak:
                peak = cumulative
                current_duration = 0
            else:
                current_duration += 1
            drawdown = cumulative - peak
            if drawdown < minimum:
                minimum = drawdown
            duration = max(duration, current_duration)
        return minimum, duration


__all__ = [
    "METRIC_DECIMAL_EMAX",
    "METRIC_DECIMAL_EMIN",
    "METRIC_DECIMAL_PRECISION",
    "METRIC_DECIMAL_SCALE",
    "_decimal_mean",
    "_decimal_ratio",
    "_decimal_sum",
    "_downside_deviation",
    "_fresh_decimal_context",
    "_metric_decimal_context",
    "_new_decimal_context",
    "_population_standard_deviation",
    "_quantize_metric_decimal",
    "_session_drawdown",
    "_timedelta_decimal_seconds",
]
