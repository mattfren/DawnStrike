"""Aggressive intraday scoring logic."""

from __future__ import annotations

import uuid
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.formula import FORMULA_VERSION, evaluate_formula
from intraday_scanner.models import ScanResult, ScoredCandidate, SnapshotRow, utc_now_iso
from intraday_scanner.services.premarket_intelligence import (
    ACTION_AVOID,
    build_premarket_intelligence,
)


def score_universe(
    rows: list[SnapshotRow],
    config: ScannerConfig,
    *,
    historical_outcomes: list[dict[str, Any]] | None = None,
) -> ScanResult:
    scored = [
        score_snapshot(row, config, historical_outcomes=historical_outcomes) for row in rows
    ]
    ranked_candidates = sorted(
        [candidate for candidate in scored if not candidate.is_avoid],
        key=lambda candidate: (candidate.score, candidate.dollar_volume),
        reverse=True,
    )
    avoid_list = sorted(
        [candidate for candidate in scored if candidate.is_avoid],
        key=lambda candidate: (candidate.score, candidate.dollar_volume),
        reverse=True,
    )
    ranked_candidates = _assign_ranks(ranked_candidates)
    avoid_list = _assign_ranks(avoid_list)
    top_explosive = [
        candidate
        for candidate in ranked_candidates
        if candidate.score >= config.explosive_score_threshold
    ][: config.explosive_top_n]
    return ScanResult(
        run_id=str(uuid.uuid4()),
        created_at=utc_now_iso(),
        all_candidates=_assign_ranks(sorted(scored, key=lambda item: item.score, reverse=True)),
        ranked_candidates=ranked_candidates[: config.top_n],
        top_explosive=top_explosive,
        avoid_list=avoid_list,
        config=config.public_dict(),
    )


def score_snapshot(
    row: SnapshotRow,
    config: ScannerConfig,
    *,
    historical_outcomes: list[dict[str, Any]] | None = None,
) -> ScoredCandidate:
    formula = evaluate_formula(row, config)
    breakout_trigger = round(row.premarket_high * 1.005, 4)
    pullback_low = row.premarket_price * 0.94
    pullback_high = row.premarket_price * 0.98
    invalidation = row.premarket_low * 0.985
    first_target = row.premarket_price + max(row.premarket_price - invalidation, 0) * 1.5
    range_stretch_target = (
        row.premarket_price + max(row.premarket_high - row.premarket_low, 0) * 1.25
    )
    stretch_target = max(range_stretch_target, first_target * 1.08)
    intelligence = build_premarket_intelligence(
        row,
        formula,
        config,
        breakout_trigger=breakout_trigger,
        invalidation_level=round(invalidation, 4),
        first_target=round(first_target, 4),
        stretch_target=round(stretch_target, 4),
        historical_outcomes=historical_outcomes,
    )
    intelligence_payload = intelligence.to_dict()
    is_intelligence_avoid = intelligence.action == ACTION_AVOID
    avoid_reasons = list(formula.avoid_reasons)
    if is_intelligence_avoid and "intelligence_gap_and_crap_risk" not in avoid_reasons:
        avoid_reasons.append("intelligence_gap_and_crap_risk")

    return ScoredCandidate(
        rank=0,
        snapshot=row,
        score=formula.score,
        gap_pct=formula.gap_pct,
        dollar_volume=formula.dollar_volume,
        float_rotation_pct=formula.float_rotation_pct,
        range_position_pct=formula.range_position_pct,
        data_quality_score=formula.data_quality_score,
        liquidity_tier=formula.liquidity_tier,
        setup_grade=formula.setup_grade,
        volatility_signature=formula.volatility_signature,
        equation_version=FORMULA_VERSION,
        breakout_trigger=breakout_trigger,
        pullback_zone=f"{pullback_low:.2f}-{pullback_high:.2f}",
        invalidation_level=round(invalidation, 4),
        first_target=round(first_target, 4),
        stretch_target=round(stretch_target, 4),
        risk_flags=formula.risk_flags,
        best_exit_bias=formula.best_exit_bias,
        score_breakdown=formula.score_breakdown,
        is_avoid=bool(avoid_reasons),
        avoid_reasons=avoid_reasons,
        intelligence=intelligence_payload,
    )


def _assign_ranks(candidates: list[ScoredCandidate]) -> list[ScoredCandidate]:
    return [
        ScoredCandidate(
            rank=index,
            snapshot=candidate.snapshot,
            score=candidate.score,
            gap_pct=candidate.gap_pct,
            dollar_volume=candidate.dollar_volume,
            float_rotation_pct=candidate.float_rotation_pct,
            range_position_pct=candidate.range_position_pct,
            data_quality_score=candidate.data_quality_score,
            liquidity_tier=candidate.liquidity_tier,
            setup_grade=candidate.setup_grade,
            volatility_signature=candidate.volatility_signature,
            equation_version=candidate.equation_version,
            breakout_trigger=candidate.breakout_trigger,
            pullback_zone=candidate.pullback_zone,
            invalidation_level=candidate.invalidation_level,
            first_target=candidate.first_target,
            stretch_target=candidate.stretch_target,
            risk_flags=candidate.risk_flags,
            best_exit_bias=candidate.best_exit_bias,
            score_breakdown=candidate.score_breakdown,
            is_avoid=candidate.is_avoid,
            avoid_reasons=candidate.avoid_reasons,
            intelligence=candidate.intelligence,
        )
        for index, candidate in enumerate(candidates, start=1)
    ]
