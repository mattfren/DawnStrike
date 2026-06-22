"""Plain-English display text for the Streamlit dashboard."""

from __future__ import annotations

from typing import Any

TECHNICAL_LABELS = {
    "LOW_CONFIDENCE": "Low confidence",
    "INSUFFICIENT_SAMPLE": "Not enough history yet",
    "NO_EDGE": "No clear edge",
    "unknown_float": "Float unknown",
    "no_previous_close": "Previous close missing",
    "url_table_unverified": "Free web source - verify manually",
    "halt_status_unverified": "Halt status not checked",
    "sec_risk_unverified": "SEC risk not checked",
    "previous_close_unavailable": "Previous close missing",
    "premarket_range_unavailable_price_used": "No premarket range; using current price",
    "intelligence_gap_and_crap_risk": "Low-quality gap risk",
    "gap_below_min": "Gap too small",
    "source conflict": "Data sources disagree",
    "source_conflict": "Data sources disagree",
    "Clean": "No hard risk flags",
    "clean": "No hard risk flags",
}

FIELD_LABELS = {
    "breakout_trigger": "Watch Level",
    "invalidation_level": "Exit Line",
    "invalidation": "Setup Failed Below",
    "first_target": "Target",
    "expected_return": "Paper Estimate",
    "expected_return_pct": "Paper Estimate",
    "confidence_bucket": "Confidence",
    "no_trade_reason": "Why No Pick?",
    "source_reliability": "Data Quality",
    "alpha_score": "Opportunity Score",
    "score": "Opportunity Score",
}

PUBLIC_SOURCE_LABELS = {
    "web_url": "Unverified free web data",
    "public_table_url": "Unverified free web data",
    "browser_table_url": "Unverified free web data",
    "url": "Unverified free web data",
    "manual": "Manual shadow data",
    "local_inbox": "Local inbox shadow data",
    "paid": "Paid/API data",
    "api": "Paid/API data",
    "paid/api": "Paid/API data",
}

RESEARCH_LABELS = {
    "WATCH": "Strong Watch",
    "BREAKOUT WATCH": "Strong Watch",
    "HIGH VOLATILITY WATCH": "Watch Only",
    "CAUTION": "Watch Only",
    "AVOID": "Avoid",
    "INVALIDATED": "Avoid",
    "OUTCOME NEEDED": "Outcome Needed",
}


def translate_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text in TECHNICAL_LABELS:
        return TECHNICAL_LABELS[text]
    lower = text.lower()
    if lower in TECHNICAL_LABELS:
        return TECHNICAL_LABELS[lower]
    return text.replace("_", " ").replace("-", " ").strip().capitalize()


def field_label(value: str) -> str:
    return FIELD_LABELS.get(value, value.replace("_", " ").title())


def translate_list(value: Any) -> str:
    if value in {None, ""}:
        return ""
    if isinstance(value, (list, tuple, set)):
        items = [translate_label(item) for item in value if str(item or "").strip()]
    else:
        raw = str(value).replace(";", ",")
        items = [translate_label(item.strip()) for item in raw.split(",") if item.strip()]
    return ", ".join(dict.fromkeys(items))


def no_trade_reason(value: Any) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "clean", "none", "n/a", "na"}:
        return "No hard risk flags, but confidence was not high enough."
    return translate_label(text)


def source_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown data source"
    return PUBLIC_SOURCE_LABELS.get(text.lower(), translate_label(text))


def research_label(value: Any, *, risk_flags: Any = None, score: Any = None) -> str:
    text = str(value or "").strip().upper()
    if text in RESEARCH_LABELS:
        return RESEARCH_LABELS[text]
    risks = translate_list(risk_flags).lower()
    if "halt" in risks or "offering" in risks or "reverse split" in risks:
        return "Avoid"
    try:
        numeric_score = float(score or 0)
    except (TypeError, ValueError):
        numeric_score = 0.0
    if numeric_score >= 75:
        return "Strong Watch"
    if numeric_score >= 45:
        return "Watch Only"
    if numeric_score > 0:
        return "Risky"
    return "Watch Only"


def evidence_status(audited_days: int) -> str:
    if audited_days < 20:
        return "Not enough real days yet"
    if audited_days < 60:
        return "Early evidence"
    return "Stronger evidence"
