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
    price = _float(row.get("premarket_price"))
    high = _float(row.get("premarket_high"))
    low = _float(row.get("premarket_low"))
    range_width = _range_width(high, low, price)
    return {
        "premarket_price": price,
        "previous_close": _float(row.get("previous_close")),
        "premarket_high": high,
        "premarket_low": low,
        "price_bucket": _price_bucket(price),
        "gap_pct": gap,
        "gap_bucket": _bucket(
            gap,
            ((15, "low_gap"), (50, "clean_gap"), (140, "hot_gap"), (300, "extreme_gap")),
            "mega_gap",
        ),
        "mega_gap_flag": bool(gap is not None and gap >= 300),
        "price_near_high": _near_high(price, high),
        "premarket_range_position": _float(row.get("range_position_pct")),
        "premarket_range_width": range_width,
        "range_position_pct": _float(row.get("range_position_pct")),
        "trend_into_scan": _text(row.get("trend_into_scan"), "missing"),
        "prior_day_continuation": _text(row.get("prior_day_continuation"), "missing"),
        "volatility_signature": _text(row.get("volatility_signature")),
    }


def _liquidity_execution(row: dict[str, Any]) -> dict[str, Any]:
    dollar_volume = _float(row.get("dollar_volume"))
    spread = _float(row.get("spread_pct"))
    volume = _float(row.get("premarket_volume"))
    tradability = _float(row.get("tradability_score"), 0.0)
    return {
        "premarket_volume": volume,
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
        "liquidity_bucket": _bucket(
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
        "spread_pct": spread,
        "estimated_slippage_bucket": _slippage_bucket(spread, dollar_volume),
        "tradability_score": tradability,
        "untradeable_flag": bool(
            (spread is not None and spread >= 8.0)
            or (volume is not None and volume <= 0)
            or (dollar_volume is not None and dollar_volume < 250_000)
        ),
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
    source_confidence = _float(row.get("source_confidence"), 0.0)
    data_kind = _text(row.get("data_source_kind"))
    coverage_warning = _text(row.get("coverage_warning"))
    conflict_flags = _text(row.get("conflict_flags"))
    return {
        "source": source,
        "preferred_source": source,
        "source_url": _text(row.get("source_url")),
        "source_confidence": source_confidence,
        "source_confidence_bucket": _bucket(
            source_confidence,
            ((35, "low"), (60, "mixed"), (80, "usable"), (95, "strong")),
            "verified",
        ),
        "source_count": _float(row.get("source_count"), 0.0),
        "stale_data_flag": _bool(row.get("stale_data_flag")),
        "stale_source_flag": _bool(row.get("stale_data_flag")),
        "extraction_mode": _text(row.get("extraction_mode")),
        "missing_field_count": _missing_field_count(row),
        "conflict_flags": conflict_flags,
        "conflicting_source_flag": bool(conflict_flags),
        "coverage_warning": coverage_warning,
        "data_source_kind": data_kind,
        "public_url_unverified_flag": (
            data_kind in {"web_url", "browser_url"} or "url_table_unverified" in coverage_warning
        ),
        "source_rows_normalized": _float(source_summary.get("rows_normalized"), 0.0),
        "source_reliability_score": _float(reliability.get("reliability_score"), 50.0),
        "source_reliability_prior": _float(reliability.get("reliability_score"), 50.0),
    }


def _catalyst(row: dict[str, Any]) -> dict[str, Any]:
    headline = _text(row.get("catalyst_headline") or row.get("catalyst_summary"))
    category = _text(row.get("catalyst_category"))
    catalyst_url = _text(row.get("catalyst_url"))
    combined = f"{headline} {category}".lower()
    has_news = bool(headline and headline.lower() not in {"none", "no clear catalyst"})
    return {
        "has_news": has_news,
        "catalyst_headline": headline,
        "catalyst_url": catalyst_url,
        "catalyst_tier": _text(row.get("catalyst_tier")),
        "catalyst_category": category,
        "catalyst_confidence": _float(row.get("catalyst_confidence"), 0.0),
        "catalyst_strength": _float(row.get("catalyst_confidence"), 0.0),
        "catalyst_source_quality": "linked" if catalyst_url else "missing",
        "catalyst_risk_flags": _text(row.get("catalyst_risk_flags")),
        "fda_biotech_flag": _contains(combined, ("fda", "phase", "trial", "biotech")),
        "earnings_guidance_flag": _contains(combined, ("earnings", "guidance", "revenue")),
        "m_and_a_strategic_flag": _contains(
            combined, ("merger", "acquisition", "strategic", "takeover")
        ),
        "contract_government_award_flag": _contains(
            combined, ("contract", "award", "government", "defense department")
        ),
        "ai_semis_crypto_nuclear_defense_quantum_robotics_flag": _contains(
            combined,
            (
                " ai ",
                "artificial intelligence",
                "semi",
                "crypto",
                "bitcoin",
                "nuclear",
                "defense",
                "quantum",
                "robot",
            ),
        ),
        "no_clear_catalyst_flag": not has_news or category == "no_clear_catalyst",
    }


def _risk(row: dict[str, Any]) -> dict[str, Any]:
    risk_flags = _tokens(row.get("risk_flags"))
    avoid_reasons = _tokens(row.get("avoid_reasons"))
    risk_text = " ".join(risk_flags + avoid_reasons).lower()
    price = _float(row.get("premarket_price"))
    volume = _float(row.get("premarket_volume"))
    return {
        "risk_flags": risk_flags,
        "avoid_reasons": avoid_reasons,
        "risk_flag_count": len(risk_flags),
        "avoid_reason_count": len(avoid_reasons),
        "current_halt": "current_halt" in risk_flags or _bool(row.get("current_halt")),
        "halt_flag": "current_halt" in risk_flags or _bool(row.get("current_halt")),
        "recent_offering": "recent_offering" in risk_flags or _bool(row.get("recent_offering")),
        "recent_offering_flag": "recent_offering" in risk_flags
        or _bool(row.get("recent_offering")),
        "shelf_atm_warrant_language": _contains(risk_text, ("shelf", "atm", "warrant")),
        "reverse_split_flag": "reverse_split_90d" in risk_flags
        or _bool(row.get("reverse_split_90d")),
        "wide_spread": "wide_spread" in risk_flags,
        "wide_spread_flag": "wide_spread" in risk_flags,
        "sub_min_price": "sub_min_price" in risk_flags or "price_below_min" in avoid_reasons,
        "sub_dollar_flag": bool(price is not None and price < 1.0),
        "dilution_risk": _contains(risk_text, ("offering", "dilution", "atm", "warrant")),
        "legal_regulatory_risk": _contains(risk_text, ("sec", "nasdaq", "lawsuit", "delist")),
        "low_volume_risk": "low_share_volume" in avoid_reasons
        or bool(volume is not None and volume < 100_000),
        "known_trap_pattern_flag": _contains(
            risk_text, ("gap_and_crap", "trap", "offering", "halt")
        ),
        "unknown_float": "unknown_float" in risk_flags,
        "score_risk": _float(row.get("risk_score"), 0.0),
    }


def _structure(row: dict[str, Any]) -> dict[str, Any]:
    float_shares = _float(row.get("float_shares"))
    market_cap = _float(row.get("market_cap"))
    short_float = _float(row.get("short_float_pct"))
    float_rotation = _float(row.get("float_rotation_pct"))
    return {
        "float_shares": float_shares,
        "unknown_float_flag": float_shares in {None, 0.0},
        "float_rotation": float_rotation,
        "float_rotation_pct": float_rotation,
        "market_cap": market_cap,
        "short_float_pct": short_float,
        "low_float_flag": bool(float_shares is not None and 0 < float_shares <= 20_000_000),
        "squeeze_structure_score": _squeeze_score(float_shares, float_rotation, short_float),
        "premarket_structure": _text(row.get("premarket_structure")),
        "structure_notes": _text(row.get("structure_notes")),
        "pullback_zone": _text(row.get("pullback_zone")),
        "entry_trigger": _text(row.get("entry_trigger")),
        "confirmation_needed": _bool(row.get("confirmation_needed")),
        "do_not_enter_if": _text(row.get("do_not_enter_if")),
    }


def _playbook(row: dict[str, Any]) -> dict[str, Any]:
    score = _float(row.get("score") or row.get("total_score"), 0.0)
    setup = _primary_setup(row)
    warnings = _tokens(row.get("risk_flags")) + _tokens(row.get("avoid_reasons"))
    return {
        "rank": _float(row.get("rank")),
        "score": score,
        "primary_setup": setup,
        "setup_score": score,
        "score_bucket": _bucket(
            score,
            ((40, "low"), (60, "review"), (75, "watch"), (88, "prime")),
            "elite",
        ),
        "setup_grade": _text(row.get("setup_grade")),
        "setup_reasons": _text(row.get("why_this_matters") or row.get("structure_notes")),
        "setup_warnings": warnings,
        "entry_condition": _text(row.get("entry_trigger") or row.get("breakout_trigger")),
        "confirmation_condition": _text(row.get("confirmation_needed"), "missing"),
        "failure_condition": _text(row.get("do_not_enter_if") or row.get("invalidation")),
        "review_bucket": _review_bucket(score, warnings),
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


def _price_bucket(price: float | None) -> str:
    return _bucket(
        price,
        (
            (1, "sub_dollar"),
            (3, "low_price"),
            (10, "small_cap_range"),
            (25, "upper_small_cap"),
        ),
        "high_price",
    )


def _range_width(high: float | None, low: float | None, price: float | None) -> float | None:
    if high is None or low is None or price is None or price == 0.0:
        return None
    return round(((high - low) / price) * 100.0, 4)


def _near_high(price: float | None, high: float | None) -> bool | None:
    if price is None or high is None or high == 0.0:
        return None
    return price >= high * 0.98


def _slippage_bucket(spread: float | None, dollar_volume: float | None) -> str:
    if spread is None:
        return "missing"
    if spread >= 8 or (dollar_volume is not None and dollar_volume < 250_000):
        return "high"
    if spread >= 4:
        return "medium"
    if spread >= 1.5:
        return "low"
    return "tight"


def _missing_field_count(row: dict[str, Any]) -> int:
    critical = (
        "ticker",
        "premarket_price",
        "previous_close",
        "premarket_high",
        "premarket_low",
        "premarket_volume",
        "dollar_volume",
        "source_confidence",
    )
    return sum(1 for key in critical if row.get(key) in {None, "", 0})


def _squeeze_score(
    float_shares: float | None,
    float_rotation: float | None,
    short_float: float | None,
) -> float:
    score = 0.0
    if float_shares is not None and 0 < float_shares <= 20_000_000:
        score += 35.0
    if float_rotation is not None:
        score += min(40.0, max(0.0, float_rotation))
    if short_float is not None:
        score += min(25.0, max(0.0, short_float))
    return round(min(100.0, score), 2)


def _primary_setup(row: dict[str, Any]) -> str:
    category = _text(row.get("catalyst_category"))
    gap = _float(row.get("gap_pct"), 0.0) or 0.0
    if category and category != "no_clear_catalyst":
        return f"{category}_catalyst"
    if gap >= 50:
        return "gap_and_go"
    return "watch_only"


def _review_bucket(score: float | None, warnings: list[str]) -> str:
    score = score or 0.0
    if warnings:
        return "watch_only"
    if score >= 78:
        return "priority_review"
    if score >= 55:
        return "standard_review"
    return "low_edge_review"


def _contains(text: str, needles: tuple[str, ...]) -> bool:
    haystack = f" {text.lower()} "
    return any(needle.lower() in haystack for needle in needles)


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
