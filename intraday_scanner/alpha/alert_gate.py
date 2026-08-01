"""Opening-day alert trust gates for AlphaOps candidates."""

from __future__ import annotations

import re
from typing import Any

ALERT_OK = "ALERT_OK"
PASS = "PASS"
WATCH_ONLY = "WATCH_ONLY"
NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
NO_EDGE = "NO_EDGE"
BLOCKED = "BLOCKED"

GOOD = "GOOD"
LIMITED = "LIMITED"
WEAK = "WEAK"

HARD_SOURCE_CONFIDENCE_FLOOR = 18.0
LOW_SOURCE_CONFIDENCE_FLOOR = 35.0
ALERT_SOURCE_CONFIDENCE_FLOOR = 80.0
EXTREME_SPREAD_PCT = 12.0
MIN_REWARD_RISK_RATIO = 1.5
MIN_HISTORICAL_FIRST_TOUCH_SAMPLE = 20
MIN_HISTORICAL_FIRST_TOUCH_WIN_RATE = 52.0
MAX_ALERT_GAP_PCT = 50.0
MAX_ALERT_STOP_DISTANCE_PCT = 15.0
MIN_CATALYST_CONFIDENCE = 0.60
ALERT_GATE_VERSION = "dawnstrike-alert-gate-v2.0.0"
PASSING_EVIDENCE_STATUSES = frozenset({"CLEAR", "VERIFIED", "OK", "PASS"})
ALERTABLE_EDGE_BUCKETS = frozenset({"MEDIUM", "HIGH"})
ALERTABLE_SETUP_GRADES = frozenset({"A", "B"})
ALERTABLE_CONFIDENCE_BUCKETS = frozenset({"MEDIUM", "HIGH"})


def apply_alert_gates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [apply_alert_gate(row) for row in rows]


def apply_alert_gate(row: dict[str, Any]) -> dict[str, Any]:
    gate = evaluate_alert_gate(row)
    output = dict(row)
    output.update(gate)
    gate_passed = (
        gate["alert_gate_status"] in {PASS, ALERT_OK}
        and gate["manual_confirmation_required"] is False
    )
    # Alert qualification is necessary but never sufficient for an official
    # AlphaOps v5 paper entry.  The execution policy owns the final predicate.
    output["official_paper_gate_passed"] = gate_passed
    output["official_paper_eligible"] = False
    output["official_paper_eligibility_status"] = (
        "PENDING_V5_EXECUTION_POLICY" if gate_passed else "RESEARCH_ONLY"
    )
    if not gate_passed:
        output["can_alert"] = False
        output["no_trade_reason"] = ";".join(
            _unique(
                [
                    *_tokens(output.get("no_trade_reason")),
                    *gate["alert_gate_reasons"],
                ]
            )
        )
    if gate["alert_gate_status"] in {WATCH_ONLY, NEEDS_CONFIRMATION}:
        output["classification"] = "WATCH ONLY"
        output["review_label"] = "NEEDS CONFIRMATION"
    return output


def evaluate_alert_gate(row: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    edge_reasons: list[str] = []
    missing: list[str] = []
    warnings: list[str] = []

    ticker = str(row.get("ticker") or "").upper().strip()
    risk_text = _combined_text(
        row.get("risk_flags"),
        row.get("avoid_reasons"),
        row.get("data_warnings"),
        row.get("coverage_warning"),
        row.get("conflict_flags"),
        row.get("catalyst_risk_flags"),
    )
    source_confidence = _float(row.get("source_confidence"), 0.0)

    if _truthy(row.get("fixture_only")) or "synthetic_or_test_data" in risk_text:
        reasons.append("fixture/test data ineligible for alerts")
    if not _valid_ticker(ticker):
        reasons.append("invalid ticker")
    if _truthy(row.get("current_halt")) or "current_halt" in risk_text:
        reasons.append("current halt")
    if _has_any(risk_text, ("recent_offering", "active_offering", "dilution")):
        reasons.append("offering/dilution risk")
    if _truthy(row.get("reverse_split_90d")) or _has_any(
        risk_text,
        ("reverse_split_90d", "reverse_split_risk", "reverse split", "reverse_stock_split"),
    ):
        reasons.append("recent reverse split risk")
    if _has_any(
        risk_text,
        ("source_conflict", "gap_conflict", "price_conflict", "volume_conflict"),
    ) or str(row.get("score_consensus") or "").lower() == "multi_source_conflict":
        reasons.append("source conflict unresolved")
    stale_status = str(row.get("stale_data_status") or "").lower()
    if _truthy(row.get("stale_data_flag")) or stale_status == "stale":
        reasons.append("stale source")
    if source_confidence < HARD_SOURCE_CONFIDENCE_FLOOR:
        reasons.append("source confidence below hard threshold")
    elif source_confidence < ALERT_SOURCE_CONFIDENCE_FLOOR:
        reasons.append("source confidence below alert threshold")
    if _price(row) is None:
        reasons.append("missing price")
    if _volume(row) is None:
        reasons.append("missing volume")
    spread = _float(row.get("spread_pct"), 0.0)
    if spread >= EXTREME_SPREAD_PCT or "wide_spread" in risk_text:
        reasons.append("extreme spread")

    if _previous_close(row) is None:
        missing.append("previous close missing")
    if _float(row.get("float_shares"), 0.0) <= 0 or "unknown_float" in risk_text:
        missing.append("float unknown")
    if _source_count(row) <= 1:
        warnings.append("only one source confirmed it")
    if _has_any(risk_text, ("sec_risk_unverified", "sec_unchecked")):
        reasons.append("SEC risk not checked")
    if _has_any(risk_text, ("halt_status_unverified", "halt_unchecked")):
        reasons.append("halt status not checked")
    if "url_table_unverified" in risk_text:
        reasons.append("public table identity not verified")
    if _premarket_range_missing(row):
        missing.append("premarket high/low missing")
    if _is_public_url(row):
        warnings.append("free web data - verify manually")
    if _no_catalyst(row):
        edge_reasons.append("no clear catalyst")
    catalyst_confidence = _optional_float(row.get("catalyst_confidence"))
    if catalyst_confidence is None:
        edge_reasons.append("catalyst confidence unavailable")
    elif catalyst_confidence < MIN_CATALYST_CONFIDENCE:
        edge_reasons.append("catalyst confidence below alert threshold")
    if source_confidence < LOW_SOURCE_CONFIDENCE_FLOOR:
        warnings.append("low source confidence")
    confidence_bucket = str(row.get("confidence_bucket") or "").upper()
    if confidence_bucket == "INSUFFICIENT_SAMPLE":
        warnings.append("not enough history yet")
    elif confidence_bucket not in ALERTABLE_CONFIDENCE_BUCKETS:
        edge_reasons.append("confidence evidence below alert threshold")

    edge_bucket = str(row.get("edge_bucket") or "").upper()
    if edge_bucket not in ALERTABLE_EDGE_BUCKETS:
        edge_reasons.append("edge bucket below alert threshold")
    setup_grade = str(row.get("setup_grade") or "").upper()
    if setup_grade not in ALERTABLE_SETUP_GRADES:
        edge_reasons.append("setup grade below alert threshold")
    data_quality = _optional_float(row.get("data_quality_score"))
    if data_quality is None:
        reasons.append("data quality unavailable")
    elif data_quality < 75.0:
        reasons.append("data quality below alert threshold")

    gap_pct = _optional_float(row.get("gap_pct"))
    if gap_pct is not None and (gap_pct < 0 or gap_pct > MAX_ALERT_GAP_PCT):
        reasons.append("gap regime outside alert policy")
    stop_distance = _stop_distance_pct(row)
    if stop_distance is not None and stop_distance > MAX_ALERT_STOP_DISTANCE_PCT:
        reasons.append("stop distance exceeds alert policy")
    if _truthy(row.get("target_derived_from_risk")):
        edge_reasons.append("target is manufactured from risk multiple")

    for field, label in (
        ("halt_status", "halt status"),
        ("sec_risk_status", "SEC risk status"),
        ("corporate_action_status", "corporate action status"),
        ("source_quality_status", "source quality status"),
    ):
        status_value = str(row.get(field) or "").upper()
        if status_value not in PASSING_EVIDENCE_STATUSES:
            reasons.append(f"{label} is not verified clear")

    expected_value = _optional_float(row.get("expected_value_score"))
    expected_drawdown = _optional_float(row.get("expected_max_drawdown"))
    prediction_status = str(row.get("prediction_status") or "").upper()
    probability_status = str(row.get("probability_status") or "").lower()
    if expected_value is not None and expected_value < 0:
        edge_reasons.append("negative expected value")
    if expected_drawdown is not None and abs(expected_drawdown) >= 12:
        warnings.append("expected drawdown too high")
    if prediction_status == "INSUFFICIENT_SAMPLE" or probability_status == "uncalibrated":
        warnings.append("probability uncalibrated")

    plan_status = str(row.get("trade_plan_quality_status") or "").upper()
    if plan_status == "MISSING_VERIFIED_PLAN_INPUTS":
        reasons.append(
            str(row.get("trade_plan_quality_reason") or "verified plan inputs unavailable")
        )
    else:
        reward_risk = _reward_risk_ratio(row)
        if reward_risk is None:
            warnings.append("reward/risk unavailable")
        elif reward_risk < MIN_REWARD_RISK_RATIO:
            edge_reasons.append(f"reward/risk below {MIN_REWARD_RISK_RATIO:.2f}R")
    if plan_status == "LOW_REWARD_RISK":
        edge_reasons.append(f"reward/risk below {MIN_REWARD_RISK_RATIO:.2f}R")
    elif plan_status == "NEGATIVE_FIRST_TOUCH_HISTORY":
        edge_reasons.append("historical first-touch edge is negative or weak")
    historical_sample = _optional_float(row.get("historical_first_touch_sample_size"))
    historical_return = _optional_float(row.get("historical_first_touch_return_pct"))
    historical_win_rate = _optional_float(row.get("historical_first_touch_win_rate_pct"))
    if historical_sample is not None and historical_sample >= MIN_HISTORICAL_FIRST_TOUCH_SAMPLE:
        if historical_return is not None and historical_return <= 0:
            edge_reasons.append("historical first-touch return is not positive")
        if (
            historical_win_rate is not None
            and historical_win_rate < MIN_HISTORICAL_FIRST_TOUCH_WIN_RATE
        ):
            edge_reasons.append("historical first-touch win rate is too low")

    public_warnings = _unique([*missing, *warnings])
    if reasons:
        status = BLOCKED
        grade = BLOCKED
    elif edge_reasons:
        status = NO_EDGE
        grade = WEAK
    elif len(public_warnings) >= 5 or (
        "previous close missing" in missing
        and "float unknown" in missing
        and (
            "SEC risk not checked" in warnings
            or "halt status not checked" in warnings
        )
    ):
        status = NEEDS_CONFIRMATION
        grade = WEAK
    elif public_warnings:
        status = WATCH_ONLY
        grade = LIMITED
    else:
        status = ALERT_OK if row.get("prediction_run_id") else PASS
        grade = GOOD

    manual_required = status not in {PASS, ALERT_OK}
    return {
        "alert_gate_version": ALERT_GATE_VERSION,
        "alert_gate_status": status,
        "alert_gate_reasons": _unique(reasons + edge_reasons + public_warnings),
        "public_data_reliability_grade": grade,
        "missing_critical_fields": _unique(missing),
        "manual_confirmation_required": manual_required,
        "official_paper_gate_passed": not manual_required,
        "official_paper_eligible": False,
        "public_data_warning": "; ".join(public_warnings),
        "data_quality_label": _data_quality_label(grade),
    }


def _data_quality_label(grade: str) -> str:
    if grade == GOOD:
        return "Good"
    if grade == LIMITED:
        return "Limited"
    if grade == WEAK:
        return "Weak"
    return "Blocked"


def _price(row: dict[str, Any]) -> float | None:
    return _optional_float(
        row.get("premarket_price") or row.get("price") or row.get("current_price")
    )


def _previous_close(row: dict[str, Any]) -> float | None:
    value = _optional_float(row.get("previous_close"))
    return value if value and value > 0 else None


def _volume(row: dict[str, Any]) -> float | None:
    value = _optional_float(
        row.get("premarket_volume") or row.get("volume") or row.get("dollar_volume")
    )
    return value if value and value > 0 else None


def _source_count(row: dict[str, Any]) -> int:
    try:
        return int(float(str(row.get("source_count") or 0)))
    except ValueError:
        return 0


def _premarket_range_missing(row: dict[str, Any]) -> bool:
    high = _optional_float(row.get("premarket_high"))
    low = _optional_float(row.get("premarket_low"))
    if high is None or low is None:
        return True
    return high <= 0 or low <= 0 or high == low


def _reward_risk_ratio(row: dict[str, Any]) -> float | None:
    explicit = _optional_float(row.get("reward_risk_ratio"))
    if explicit is not None and explicit > 0:
        return explicit
    trigger = _optional_float(
        row.get("entry_trigger")
        or row.get("breakout_trigger")
        or row.get("entry_watch_level")
        or row.get("premarket_price")
        or row.get("price")
    )
    target = _optional_float(row.get("target_1") or row.get("first_target"))
    invalidation = _optional_float(
        row.get("invalidation")
        or row.get("invalidation_level")
        or row.get("exit_line")
    )
    if trigger is None or target is None or invalidation is None:
        return None
    reward = target - trigger
    risk = trigger - invalidation
    if trigger <= 0 or reward <= 0 or risk <= 0:
        return None
    return round(reward / risk, 4)


def _stop_distance_pct(row: dict[str, Any]) -> float | None:
    trigger = _optional_float(
        row.get("entry_trigger")
        or row.get("breakout_trigger")
        or row.get("entry_watch_level")
        or row.get("premarket_price")
        or row.get("price")
    )
    invalidation = _optional_float(
        row.get("invalidation")
        or row.get("invalidation_level")
        or row.get("exit_line")
    )
    if trigger is None or invalidation is None or trigger <= 0 or invalidation >= trigger:
        return None
    return round((trigger - invalidation) / trigger * 100.0, 4)


def _is_public_url(row: dict[str, Any]) -> bool:
    text = _combined_text(
        row.get("data_source_kind"),
        row.get("source"),
        row.get("preferred_source"),
        row.get("extraction_mode"),
    )
    return any(part in text for part in ("web_url", "public_table", "stockanalysis", "tradingview"))


def _no_catalyst(row: dict[str, Any]) -> bool:
    text = str(row.get("catalyst_summary") or row.get("catalyst_headline") or "").strip().lower()
    category = str(row.get("catalyst_category") or "").strip().lower()
    return not text or text in {"no clear catalyst", "none"} or category == "no_clear_catalyst"


def _combined_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        else:
            parts.append(str(value or ""))
    return ";".join(parts).lower()


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [
        token.strip()
        for token in str(value or "").replace(",", ";").split(";")
        if token.strip()
    ]


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _float(value: Any, default: float) -> float:
    number = _optional_float(value)
    return default if number is None else number


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("%", ""))
    except ValueError:
        return None


def _valid_ticker(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,4}", value or ""))


def _unique(items: list[str]) -> list[str]:
    output: list[str] = []
    for item in items:
        clean = item.strip()
        if clean and clean not in output:
            output.append(clean)
    return output
