"""Deterministic empirical R-metric builders with explicit sample insufficiency."""

from __future__ import annotations

from decimal import Decimal

from intraday_scanner.v2.opportunity.models import (
    Availability,
    EvidenceKind,
    ExpectancyEvidence,
    RegimeState,
    stable_identity,
)


def build_expectancy_evidence(
    r_values: tuple[Decimal, ...],
    *,
    cohort_id: str,
    min_sample_size: int,
    mfe_r_values: tuple[Decimal, ...] = (),
    mae_r_values: tuple[Decimal, ...] = (),
    holding_minutes: tuple[int, ...] = (),
    regime: RegimeState | None = None,
) -> ExpectancyEvidence:
    """Build empirical R metrics; insufficient cohorts carry no fabricated metrics."""

    if min_sample_size < 1:
        raise ValueError("min_sample_size must be positive")
    _validate_decimals(r_values, "r_values")
    _validate_optional_series(mfe_r_values, len(r_values), "mfe_r_values")
    _validate_optional_series(mae_r_values, len(r_values), "mae_r_values")
    if holding_minutes and len(holding_minutes) != len(r_values):
        raise ValueError("holding_minutes must align one-to-one with r_values")
    if any(value < 0 for value in holding_minutes):
        raise ValueError("holding_minutes cannot be negative")

    winners = tuple(value for value in r_values if value > 0)
    losers = tuple(-value for value in r_values if value < 0)
    limitations: list[str] = []
    if len(r_values) < min_sample_size:
        limitations.append(f"minimum_sample_not_met:{len(r_values)}<{min_sample_size}")
    if not winners:
        limitations.append("winner_cohort_absent")
    if not losers:
        limitations.append("loser_cohort_absent")
    if limitations:
        return _insufficient(
            cohort_id=cohort_id,
            sample_size=len(r_values),
            regime=regime,
            limitations=tuple(limitations),
        )

    sample_size = len(r_values)
    probability = Decimal(len(winners)) / Decimal(sample_size)
    loss_probability = Decimal(len(losers)) / Decimal(sample_size)
    average_winner = _mean(winners)
    average_loser = _mean(losers)
    expectancy = probability * average_winner - loss_probability * average_loser
    gross_wins = sum(winners, Decimal("0"))
    gross_losses = sum(losers, Decimal("0"))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else None
    average = _mean(r_values)
    variance = sum((value - average) ** 2 for value in r_values) / Decimal(sample_size)
    standard_error = variance.sqrt() / Decimal(sample_size).sqrt()
    half_width = Decimal("1.96") * standard_error
    stability = _stability_score(r_values)
    payload = {
        "cohort_id": cohort_id,
        "sample_size": sample_size,
        "r_values": r_values,
        "regime": regime,
    }
    return ExpectancyEvidence(
        evidence_id=stable_identity("expectancy", payload),
        cohort_id=cohort_id,
        availability=Availability.AVAILABLE,
        evidence_kind=EvidenceKind.EMPIRICAL,
        sample_size=sample_size,
        effective_sample_size=Decimal(sample_size),
        win_probability=probability,
        average_winner_r=average_winner,
        average_loser_r=average_loser,
        expectancy_r=expectancy,
        profit_factor=profit_factor,
        average_mfe_r=_mean(mfe_r_values) if mfe_r_values else None,
        average_mae_r=_mean(mae_r_values) if mae_r_values else None,
        average_holding_minutes=(
            _mean(tuple(Decimal(value) for value in holding_minutes)) if holding_minutes else None
        ),
        confidence_interval_low_r=average - half_width,
        confidence_interval_high_r=average + half_width,
        uncertainty_half_width_r=half_width,
        stability_score=stability,
        regime=regime,
        limitations=(),
    )


def unavailable_expectancy(
    *,
    cohort_id: str,
    reason: str,
    regime: RegimeState | None = None,
) -> ExpectancyEvidence:
    """Return an explicit unavailable evidence receipt without zero metrics."""

    payload = {"cohort_id": cohort_id, "reason": reason, "regime": regime}
    return ExpectancyEvidence(
        evidence_id=stable_identity("expectancy", payload),
        cohort_id=cohort_id,
        availability=Availability.UNAVAILABLE,
        evidence_kind=EvidenceKind.EMPIRICAL,
        sample_size=0,
        effective_sample_size=None,
        win_probability=None,
        average_winner_r=None,
        average_loser_r=None,
        expectancy_r=None,
        profit_factor=None,
        average_mfe_r=None,
        average_mae_r=None,
        average_holding_minutes=None,
        confidence_interval_low_r=None,
        confidence_interval_high_r=None,
        uncertainty_half_width_r=None,
        stability_score=None,
        regime=regime,
        limitations=(reason,),
    )


def _insufficient(
    *,
    cohort_id: str,
    sample_size: int,
    regime: RegimeState | None,
    limitations: tuple[str, ...],
) -> ExpectancyEvidence:
    payload = {
        "cohort_id": cohort_id,
        "sample_size": sample_size,
        "regime": regime,
        "limitations": limitations,
    }
    return ExpectancyEvidence(
        evidence_id=stable_identity("expectancy", payload),
        cohort_id=cohort_id,
        availability=Availability.INSUFFICIENT_DATA,
        evidence_kind=EvidenceKind.EMPIRICAL,
        sample_size=sample_size,
        effective_sample_size=None,
        win_probability=None,
        average_winner_r=None,
        average_loser_r=None,
        expectancy_r=None,
        profit_factor=None,
        average_mfe_r=None,
        average_mae_r=None,
        average_holding_minutes=None,
        confidence_interval_low_r=None,
        confidence_interval_high_r=None,
        uncertainty_half_width_r=None,
        stability_score=None,
        regime=regime,
        limitations=limitations,
    )


def _validate_decimals(values: tuple[Decimal, ...], field_name: str) -> None:
    if any(not value.is_finite() for value in values):
        raise ValueError(f"{field_name} must contain only finite values")


def _validate_optional_series(
    values: tuple[Decimal, ...],
    expected_size: int,
    field_name: str,
) -> None:
    _validate_decimals(values, field_name)
    if values and len(values) != expected_size:
        raise ValueError(f"{field_name} must align one-to-one with r_values")


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _stability_score(values: tuple[Decimal, ...]) -> Decimal:
    midpoint = len(values) // 2
    if midpoint < 1 or midpoint == len(values):
        return Decimal("0")
    first = _mean(values[:midpoint])
    second = _mean(values[midpoint:])
    denominator = max(abs(_mean(values)), Decimal("0.25"))
    return max(Decimal("0"), Decimal("1") - abs(first - second) / denominator)


__all__ = ["build_expectancy_evidence", "unavailable_expectancy"]
