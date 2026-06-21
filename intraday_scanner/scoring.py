"""Aggressive intraday scoring logic."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
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
    intelligence_payload.update(
        _v3_payload(
            row=row,
            formula=formula,
            config=config,
            intelligence_payload=intelligence_payload,
        )
    )
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


def _v3_payload(
    *,
    row: SnapshotRow,
    formula: Any,
    config: ScannerConfig,
    intelligence_payload: dict[str, Any],
) -> dict[str, Any]:
    breakdown = dict(formula.score_breakdown or {})
    risk_penalty = _num(breakdown.get("risk_penalty"))
    data_confidence = _num(intelligence_payload.get("data_confidence_score"))
    source_confidence = _source_confidence(row, data_confidence)
    stale = _stale_data_flag(row, intelligence_payload)
    catalyst_confidence = _num(intelligence_payload.get("catalyst_confidence"))
    sample_size = int(_num(intelligence_payload.get("similar_setup_count")))
    payload = {
        "explosive_score": _explosive_score(breakdown),
        "tradability_score": _tradability_score(breakdown, risk_penalty),
        "catalyst_score": round(_clamp(catalyst_confidence * 100, 0, 100), 2),
        "risk_score": round(_clamp(100 - risk_penalty, 0, 100), 2),
        "source_confidence": source_confidence,
        "stale_data_flag": stale,
        "expected_return_bucket": _expected_return_bucket(
            score=formula.score,
            is_avoid=bool(formula.avoid_reasons),
            data_confidence=data_confidence,
            sample_size=sample_size,
            historical_win_rate=intelligence_payload.get("historical_win_rate"),
            average_max_gain=intelligence_payload.get("average_max_gain"),
        ),
        "confidence_bucket": _confidence_bucket(
            data_confidence=data_confidence,
            source_confidence=source_confidence,
            stale=stale,
            sample_size=sample_size,
        ),
        "sample_size": sample_size,
        "uncertainty_bucket": _uncertainty_bucket(sample_size, data_confidence, source_confidence),
        "source_lineage": _source_lineage(row, source_confidence, stale),
        "config_hash": _config_hash(config),
    }
    return payload


def _explosive_score(breakdown: dict[str, Any]) -> float:
    raw = (
        _num(breakdown.get("gap_curve"))
        + _num(breakdown.get("float_rotation"))
        + _num(breakdown.get("range_control"))
        + _num(breakdown.get("squeeze_catalyst"))
    )
    return round(_clamp((raw / 62.0) * 100, 0, 100), 2)


def _tradability_score(breakdown: dict[str, Any], risk_penalty: float) -> float:
    raw = (
        _num(breakdown.get("liquidity_thrust"))
        + _num(breakdown.get("execution_quality"))
        + _num(breakdown.get("data_quality"))
        - min(risk_penalty, 35)
    )
    return round(_clamp((raw / 40.0) * 100, 0, 100), 2)


def _expected_return_bucket(
    *,
    score: float,
    is_avoid: bool,
    data_confidence: float,
    sample_size: int,
    historical_win_rate: Any,
    average_max_gain: Any,
) -> str:
    if is_avoid or score < 35:
        return "AVOID"
    if sample_size >= 20:
        win_rate = _num(historical_win_rate)
        avg_gain = _num(average_max_gain)
        if win_rate >= 55 and avg_gain >= 6:
            return "HIGH_UPSIDE"
        if win_rate >= 45 and avg_gain >= 3:
            return "MEDIUM_UPSIDE"
        return "LOW_CONFIDENCE"
    if score >= 82 and data_confidence >= 70:
        return "HIGH_UPSIDE"
    if score >= 62 and data_confidence >= 55:
        return "MEDIUM_UPSIDE"
    return "LOW_CONFIDENCE"


def _confidence_bucket(
    *,
    data_confidence: float,
    source_confidence: float,
    stale: bool,
    sample_size: int,
) -> str:
    if stale or data_confidence < 50 or source_confidence < 50:
        return "LOW"
    if sample_size >= 20 and data_confidence >= 80 and source_confidence >= 80:
        return "HIGH"
    if data_confidence >= 65 and source_confidence >= 65:
        return "MEDIUM"
    return "LOW"


def _uncertainty_bucket(sample_size: int, data_confidence: float, source_confidence: float) -> str:
    if sample_size >= 40 and data_confidence >= 80 and source_confidence >= 80:
        return "lower_uncertainty"
    if sample_size >= 20 and data_confidence >= 65 and source_confidence >= 65:
        return "moderate_uncertainty"
    return "high_uncertainty"


def _source_lineage(
    row: SnapshotRow,
    source_confidence: float,
    stale: bool,
) -> dict[str, Any]:
    return {
        "source": row.source,
        "source_url": row.source_url,
        "extraction_mode": row.extraction_mode or row.data_source_kind or "manual",
        "source_timestamp": row.source_timestamp or row.as_of_timestamp,
        "extracted_at": row.extracted_at or row.imported_at,
        "stale_data_flag": stale,
        "source_confidence": source_confidence,
        "source_count": row.source_count,
        "score_consensus": row.score_consensus,
        "conflict_flags": row.conflict_flags,
        "preferred_source": row.preferred_source or row.source,
        "row_merge_reason": row.row_merge_reason,
        "raw_file_path": row.raw_file_path,
        "coverage_warning": row.coverage_warning,
    }


def _source_confidence(row: SnapshotRow, data_confidence: float) -> float:
    if row.source_confidence > 0:
        base = row.source_confidence
    elif row.fixture_only or "fixture" in row.source.lower() or "sample" in row.source.lower():
        base = 45.0
    elif row.shadow_mode or row.data_source_kind in {"web_url", "browser_url"}:
        base = 65.0
    elif row.manual_uploaded_data:
        base = 70.0
    else:
        base = 80.0
    if row.coverage_warning:
        base -= min(len([item for item in row.coverage_warning.split(";") if item]) * 4, 20)
    if row.source_count > 1 and not row.conflict_flags:
        base += 10
    if row.conflict_flags:
        base -= 15
    if row.stale_data_flag:
        base -= 20
    if data_confidence:
        base = (base + data_confidence) / 2
    return round(_clamp(base, 0, 100), 2)


def _stale_data_flag(row: SnapshotRow, intelligence_payload: dict[str, Any]) -> bool:
    if row.stale_data_flag:
        return True
    warnings = str(intelligence_payload.get("data_warnings") or "")
    if "stale_data" in warnings:
        return True
    timestamp = row.source_timestamp or row.as_of_timestamp
    if not timestamp:
        return True
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() > (
        18 * 60 * 60
    )


def _config_hash(config: ScannerConfig) -> str:
    encoded = json.dumps(config.public_dict(), sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _num(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(str(value).replace("%", "").replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))
