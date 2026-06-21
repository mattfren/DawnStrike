"""Build persisted AlphaOps feature vectors from existing scanner candidates."""

from __future__ import annotations

import hashlib
import json
from typing import Any

FEATURE_MODEL_VERSION = "dawnstrike-alphaops-v4-feature-factory"


def build_feature_vector(
    candidate: dict[str, Any],
    *,
    scan_id: str,
    timestamp: str,
    source_summary: dict[str, Any] | None = None,
    source_reliability: dict[str, Any] | None = None,
    model_version: str = FEATURE_MODEL_VERSION,
) -> dict[str, Any]:
    """Return a full feature-vector record suitable for SQLite persistence."""

    source_summary = dict(source_summary or {})
    source_reliability = dict(source_reliability or {})
    feature_json = {
        "price_momentum": _price_momentum(candidate),
        "liquidity_execution": _liquidity_execution(candidate),
        "source_data_quality": _source_quality(candidate, source_summary, source_reliability),
        "catalyst": _catalyst(candidate),
        "risk": _risk(candidate),
        "structure": _structure(candidate),
        "playbook_setup": _playbook(candidate),
    }
    config_hash = _config_hash(feature_json)
    return {
        "scan_id": scan_id,
        "ticker": _text(candidate.get("ticker")),
        "timestamp": timestamp,
        "model_version": model_version,
        "config_hash": config_hash,
        "feature_json": feature_json,
        "feature_count": sum(len(group) for group in feature_json.values()),
    }


def feature_for_model(feature_record: dict[str, Any]) -> dict[str, Any]:
    """Flatten a persisted feature vector into the compact model input surface."""

    raw = feature_record.get("feature_json") or {}
    if isinstance(raw, str):
        raw = json.loads(raw)
    feature_json = dict(raw or {})
    merged: dict[str, Any] = {}
    for group in feature_json.values():
        if isinstance(group, dict):
            merged.update(group)
    return merged


def _price_momentum(row: dict[str, Any]) -> dict[str, Any]:
    gap = _float(row.get("gap_pct"))
    return {
        "premarket_price": _float(row.get("premarket_price")),
        "previous_close": _float(row.get("previous_close")),
        "premarket_high": _float(row.get("premarket_high")),
        "premarket_low": _float(row.get("premarket_low")),
        "gap_pct": gap,
        "gap_bucket": _bucket(
            gap,
            ((15, "low_gap"), (50, "clean_gap"), (140, "hot_gap"), (300, "extreme_gap")),
            "mega_gap",
        ),
        "range_position_pct": _float(row.get("range_position_pct")),
        "volatility_signature": _text(row.get("volatility_signature")),
    }


def _liquidity_execution(row: dict[str, Any]) -> dict[str, Any]:
    dollar_volume = _float(row.get("dollar_volume"))
    return {
        "premarket_volume": _float(row.get("premarket_volume")),
        "dollar_volume": dollar_volume,
        "dollar_volume_bucket": _bucket(
            dollar_volume,
            (
                (250_000, "thin"),
                (1_000_000, "building"),
                (5_000_000, "liquid"),
                (20_000_000, "crowded"),
            ),
            "very_crowded",
        ),
        "float_rotation_pct": _float(row.get("float_rotation_pct")),
        "liquidity_tier": _text(row.get("liquidity_tier")),
        "spread_pct": _float(row.get("spread_pct")),
        "breakout_trigger": _float(row.get("breakout_trigger") or row.get("target_1")),
        "invalidation_level": _float(row.get("invalidation_level") or row.get("invalidation")),
        "first_target": _float(row.get("first_target") or row.get("target_1")),
    }


def _source_quality(
    row: dict[str, Any],
    source_summary: dict[str, Any],
    source_reliability: dict[str, Any],
) -> dict[str, Any]:
    source = _text(row.get("preferred_source") or row.get("source"))
    reliability = source_reliability.get(source) if source else None
    if not isinstance(reliability, dict):
        reliability = source_reliability
    return {
        "source": source,
        "source_url": _text(row.get("source_url")),
        "source_confidence": _float(row.get("source_confidence"), 0.0),
        "source_confidence_bucket": _bucket(
            _float(row.get("source_confidence"), 0.0),
            ((35, "low"), (60, "mixed"), (80, "usable"), (95, "strong")),
            "verified",
        ),
        "source_count": _float(row.get("source_count"), 0.0),
        "stale_data_flag": _bool(row.get("stale_data_flag")),
        "conflict_flags": _text(row.get("conflict_flags")),
        "coverage_warning": _text(row.get("coverage_warning")),
        "data_source_kind": _text(row.get("data_source_kind")),
        "source_rows_normalized": _float(source_summary.get("rows_normalized"), 0.0),
        "source_reliability_score": _float(reliability.get("reliability_score"), 50.0),
    }


def _catalyst(row: dict[str, Any]) -> dict[str, Any]:
    headline = _text(row.get("catalyst_headline") or row.get("catalyst_summary"))
    return {
        "has_news": bool(headline and headline.lower() not in {"none", "no clear catalyst"}),
        "catalyst_headline": headline,
        "catalyst_tier": _text(row.get("catalyst_tier")),
        "catalyst_category": _text(row.get("catalyst_category")),
        "catalyst_confidence": _float(row.get("catalyst_confidence"), 0.0),
        "catalyst_risk_flags": _text(row.get("catalyst_risk_flags")),
    }


def _risk(row: dict[str, Any]) -> dict[str, Any]:
    risk_flags = _tokens(row.get("risk_flags"))
    avoid_reasons = _tokens(row.get("avoid_reasons"))
    return {
        "risk_flags": risk_flags,
        "avoid_reasons": avoid_reasons,
        "risk_flag_count": len(risk_flags),
        "avoid_reason_count": len(avoid_reasons),
        "current_halt": "current_halt" in risk_flags or _bool(row.get("current_halt")),
        "recent_offering": "recent_offering" in risk_flags or _bool(row.get("recent_offering")),
        "wide_spread": "wide_spread" in risk_flags,
        "sub_min_price": "sub_min_price" in risk_flags or "price_below_min" in avoid_reasons,
        "unknown_float": "unknown_float" in risk_flags,
        "score_risk": _float(row.get("risk_score"), 0.0),
    }


def _structure(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "premarket_structure": _text(row.get("premarket_structure")),
        "structure_notes": _text(row.get("structure_notes")),
        "pullback_zone": _text(row.get("pullback_zone")),
        "entry_trigger": _text(row.get("entry_trigger")),
        "confirmation_needed": _bool(row.get("confirmation_needed")),
        "do_not_enter_if": _text(row.get("do_not_enter_if")),
    }


def _playbook(row: dict[str, Any]) -> dict[str, Any]:
    score = _float(row.get("score") or row.get("total_score"), 0.0)
    return {
        "rank": _float(row.get("rank")),
        "score": score,
        "score_bucket": _bucket(
            score,
            ((40, "low"), (60, "review"), (75, "watch"), (88, "prime")),
            "elite",
        ),
        "setup_grade": _text(row.get("setup_grade")),
        "classification": _text(row.get("classification")),
        "predicted_action": _text(row.get("predicted_action") or row.get("action")),
        "exit_bias": _text(row.get("best_exit_bias") or row.get("exit_bias")),
        "expected_return_bucket": _text(row.get("expected_return_bucket")),
        "confidence_bucket": _text(row.get("confidence_bucket")),
    }


def _config_hash(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def _bucket(
    value: float | None,
    thresholds: tuple[tuple[float, str], ...],
    fallback: str,
) -> str:
    if value is None:
        return "missing"
    for maximum, label in thresholds:
        if value < maximum:
            return label
    return fallback


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").replace(",", ";")
    return [part.strip() for part in text.split(";") if part.strip()]


def _float(value: Any, default: float | None = None) -> float | None:
    if value in {None, ""}:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _text(value: Any, default: str = "") -> str:
    text = str(value or "").strip()
    return text if text else default
