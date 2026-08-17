"""Deterministic symbol-plus-strategy ranking, separate from quality decisions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from intraday_scanner.v2.opportunity.models import (
    Availability,
    EvaluationStatus,
    EvidenceKind,
    RankComponent,
    RankedOpportunity,
    StrategyEvaluation,
    stable_identity,
)


@dataclass(frozen=True)
class RankingConfig:
    anomaly_weight: Decimal = Decimal("0.35")
    regime_weight: Decimal = Decimal("0.20")
    data_quality_weight: Decimal = Decimal("0.20")
    liquidity_weight: Decimal = Decimal("0.10")
    expectancy_weight: Decimal = Decimal("0.15")
    direction_repeat_penalty: Decimal = Decimal("0.02")
    strategy_family_repeat_penalty: Decimal = Decimal("0.03")
    sector_repeat_penalty: Decimal = Decimal("0.04")
    correlation_repeat_penalty: Decimal = Decimal("0.05")
    max_concentration_penalty: Decimal = Decimal("0.20")
    config_version: str = "opportunity-ranking-heuristic-v1"

    def __post_init__(self) -> None:
        for name in (
            "anomaly_weight",
            "regime_weight",
            "data_quality_weight",
            "liquidity_weight",
            "expectancy_weight",
            "direction_repeat_penalty",
            "strategy_family_repeat_penalty",
            "sector_repeat_penalty",
            "correlation_repeat_penalty",
            "max_concentration_penalty",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name in (
            "anomaly_weight",
            "regime_weight",
            "data_quality_weight",
            "liquidity_weight",
            "expectancy_weight",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


DEFAULT_RANKING_CONFIG = RankingConfig()


@dataclass(frozen=True)
class _ProvisionalRank:
    evaluation: StrategyEvaluation
    components: tuple[RankComponent, ...]
    base_score: Decimal
    labels: tuple[str, ...]
    limitations: tuple[str, ...]
    penalty: Decimal = Decimal("0")


def rank_opportunities(
    evaluations: tuple[StrategyEvaluation, ...],
    *,
    sector_by_symbol: dict[str, str] | None = None,
    correlation_cluster_by_symbol: dict[str, str] | None = None,
    config: RankingConfig = DEFAULT_RANKING_CONFIG,
) -> tuple[RankedOpportunity, ...]:
    """Rank eligible pairs; this function has no decision vocabulary or gate output."""

    sectors = sector_by_symbol or {}
    correlations = correlation_cluster_by_symbol or {}
    provisional: list[_ProvisionalRank] = []
    for evaluation in evaluations:
        if evaluation.status is not EvaluationStatus.ELIGIBLE:
            continue
        components, limitations = _components(evaluation, config)
        total_weight = sum((item.weight for item in components), Decimal("0"))
        total = sum((item.contribution for item in components), Decimal("0"))
        base_score = total / total_weight
        family = evaluation.strategy_id.rsplit("-", 1)[0]
        labels = [f"direction:{evaluation.direction.value}", f"strategy_family:{family}"]
        sector = sectors.get(evaluation.symbol)
        correlation = correlations.get(evaluation.symbol)
        if sector:
            labels.append(f"sector:{sector}")
        else:
            limitations.append("sector_unavailable")
        if correlation:
            labels.append(f"correlation_cluster:{correlation}")
        else:
            limitations.append("correlation_unavailable_no_coefficient_inferred")
        provisional.append(
            _ProvisionalRank(
                evaluation=evaluation,
                components=components,
                base_score=base_score,
                labels=tuple(labels),
                limitations=tuple(dict.fromkeys(limitations)),
            )
        )

    provisional.sort(key=_provisional_sort_key)
    seen: dict[str, int] = {}
    penalized: list[_ProvisionalRank] = []
    for item in provisional:
        penalty = Decimal("0")
        for label in item.labels:
            repeats = seen.get(label, 0)
            if repeats:
                penalty += _label_penalty(label, config)
            seen[label] = repeats + 1
        penalty = min(config.max_concentration_penalty, penalty)
        penalized.append(
            _ProvisionalRank(
                evaluation=item.evaluation,
                components=item.components,
                base_score=item.base_score,
                labels=item.labels,
                limitations=item.limitations,
                penalty=penalty,
            )
        )
    penalized.sort(
        key=lambda item: (
            -(item.base_score - item.penalty),
            item.evaluation.symbol,
            item.evaluation.strategy_id,
            item.evaluation.strategy_version,
            item.evaluation.evaluation_id,
        )
    )

    ranked: list[RankedOpportunity] = []
    for rank, item in enumerate(penalized, start=1):
        final_score = max(Decimal("0"), item.base_score - item.penalty)
        payload = {
            "evaluation_id": item.evaluation.evaluation_id,
            "base_score": item.base_score,
            "penalty": item.penalty,
            "components": item.components,
            "config_version": config.config_version,
        }
        ranked.append(
            RankedOpportunity(
                ranked_id=stable_identity("ranked", payload),
                evaluation_id=item.evaluation.evaluation_id,
                symbol=item.evaluation.symbol,
                strategy_id=item.evaluation.strategy_id,
                strategy_version=item.evaluation.strategy_version,
                direction=item.evaluation.direction,
                relative_rank=rank,
                base_score=item.base_score,
                concentration_penalty=item.penalty,
                final_score=final_score,
                components=item.components,
                concentration_labels=item.labels,
                limitations=item.limitations,
            )
        )
    return tuple(ranked)


def _components(
    evaluation: StrategyEvaluation,
    config: RankingConfig,
) -> tuple[tuple[RankComponent, ...], list[str]]:
    components: list[RankComponent] = []
    limitations: list[str] = []
    for name, value, weight, explanation in (
        (
            "anomaly_strength",
            evaluation.anomaly_strength,
            config.anomaly_weight,
            "maximum normalized triggered anomaly strength",
        ),
        (
            "regime_fit",
            evaluation.regime_fit,
            config.regime_weight,
            "heuristic market and security regime compatibility",
        ),
        (
            "data_quality",
            evaluation.data_quality_score,
            config.data_quality_weight,
            "typed feature snapshot data quality",
        ),
        (
            "liquidity",
            evaluation.liquidity_score,
            config.liquidity_weight,
            "timestamp-aligned cross-section liquidity percentile",
        ),
    ):
        if value is None:
            limitations.append(f"ranking_component_unavailable:{name}")
            continue
        components.append(_component(name, value, weight, EvidenceKind.HEURISTIC, explanation))

    expectancy = evaluation.expectancy
    if (
        expectancy is not None
        and expectancy.availability is Availability.AVAILABLE
        and expectancy.evidence_kind is EvidenceKind.EMPIRICAL
        and expectancy.expectancy_r is not None
    ):
        normalized = _clip01(Decimal("0.5") + expectancy.expectancy_r / Decimal("2"))
        components.append(
            _component(
                "empirical_expectancy_r",
                normalized,
                config.expectancy_weight,
                EvidenceKind.EMPIRICAL,
                "empirical expectancy R normalized only for relative ranking",
            )
        )
    else:
        limitations.append("empirical_expectancy_unavailable_not_zero")
    if not components:
        raise ValueError("eligible evaluation has no available rank components")
    return tuple(components), limitations


def _component(
    name: str,
    value: Decimal,
    weight: Decimal,
    evidence_kind: EvidenceKind,
    explanation: str,
) -> RankComponent:
    normalized = _clip01(value)
    return RankComponent(
        name=name,
        value=normalized,
        weight=weight,
        contribution=normalized * weight,
        evidence_kind=evidence_kind,
        explanation=explanation,
    )


def _label_penalty(label: str, config: RankingConfig) -> Decimal:
    if label.startswith("direction:"):
        return config.direction_repeat_penalty
    if label.startswith("strategy_family:"):
        return config.strategy_family_repeat_penalty
    if label.startswith("sector:"):
        return config.sector_repeat_penalty
    if label.startswith("correlation_cluster:"):
        return config.correlation_repeat_penalty
    raise ValueError(f"unknown concentration label: {label}")


def _provisional_sort_key(item: _ProvisionalRank) -> tuple[Decimal, str, str, str, str]:
    return (
        -item.base_score,
        item.evaluation.symbol,
        item.evaluation.strategy_id,
        item.evaluation.strategy_version,
        item.evaluation.evaluation_id,
    )


def _clip01(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))


__all__ = ["DEFAULT_RANKING_CONFIG", "RankingConfig", "rank_opportunities"]
