"""Opening-day alert trust gates for AlphaOps candidates."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from typing import Any

from intraday_scanner.alpha.plan_constructor import COMPLETE, is_valid_alphaops_v5_plan
from intraday_scanner.alpha.v5_policy import (
    DEFAULT_V5_POLICY,
    modeled_alphaops_v5_plan_metrics,
)
from intraday_scanner.decisioning.contracts import (
    canonical_json,
    parse_strategy_decision_receipt,
)

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
# ``_setup_grade`` emits "A+" for scores >= 90, so omitting it here would reject
# the single best grade the scorer can produce as "setup grade below alert
# threshold".  A+ must rank at least as high as A.
ALERTABLE_SETUP_GRADES = frozenset({"A+", "A", "B"})
ALERTABLE_CONFIDENCE_BUCKETS = frozenset({"MEDIUM", "HIGH"})
RECEIPT_ALERTABLE_TIERS = frozenset(
    {"QUALIFIED_PICK", "PICK_WITH_DISCLOSED_GAPS", "CONDITIONAL_PICK"}
)

# --- Bootstrap paper mode -------------------------------------------------
#
# Several gates below are unsatisfiable until the system has already traded.
# ``confidence_bucket`` is INSUFFICIENT_SAMPLE until 20 real outcome days exist
# (``edge_calibrator.MIN_REAL_DAYS_FOR_EXPECTANCY``), but outcome days only
# accrue from entries, and entries require passing this gate.  That is a closed
# loop: without an explicit bootstrap the product can never take its first paper
# trade, which is exactly the state the live database was found in - 25 forward
# sessions, zero trades.
#
# Bootstrap mode waives ONLY that class of gate: evidence that is missing
# because it has not been collected yet, or calibration that cannot exist
# before the first trade.  It NEVER waives a safety gate.  Halt status, SEC
# risk, spread, price/level validity, volume, data quality, gap regime, stop
# distance and every edge judgement (setup grade, edge bucket, reward/risk,
# catalyst) remain fully enforced.
#
# Waived items are not discarded.  They are recorded on the row as
# ``bootstrap_waived_reasons`` and the row is stamped ``bootstrap_mode`` so any
# downstream evidence, performance or promotion consumer can exclude bootstrap
# entries from a calibrated-edge claim.
#
# Off unless DAWNSTRIKE_BOOTSTRAP_PAPER_MODE is truthy.
BOOTSTRAP_MODE_ENV = "DAWNSTRIKE_BOOTSTRAP_PAPER_MODE"

BOOTSTRAP_WAIVABLE_REASONS = frozenset(
    {
        # No producer sets corporate_action_status anywhere in the pipeline, so
        # it is permanently UNKNOWN and can never clear.
        "corporate action status is not verified clear",
        # Free public tables are inherently LIMITED; this can never be CLEAR
        # without a paid verified feed.
        "source quality status is not verified clear",
        "source confidence below alert threshold",
        "public table identity not verified",
    }
)

BOOTSTRAP_WAIVABLE_WARNINGS = frozenset(
    {
        "not enough history yet",
        "probability uncalibrated",
        "free web data - verify manually",
        "only one source confirmed it",
        "low source confidence",
        "secondary Yahoo range used - research only",
    }
)

BOOTSTRAP_WAIVABLE_MISSING = frozenset({"float unknown", "previous close missing"})

# Edge buckets are produced by the calibrator, which returns INSUFFICIENT_SAMPLE
# below MIN_REAL_DAYS_FOR_EXPECTANCY and drags the alpha score toward its
# insufficient-sample baseline.  They are therefore part of the same closed loop
# and must be waivable, or bootstrap mode still cannot place a first trade.
#
# Setup grade and reward/risk are deliberately EXCLUDED: those are genuine
# judgements about the setup itself, not artefacts of missing history.
BOOTSTRAP_WAIVABLE_EDGE_REASONS = frozenset(
    {
        "edge bucket below alert threshold",
        "confidence evidence below alert threshold",
    }
)

# Deliberately NOT waivable, at any setting.  These are the checks that prevent
# the worst single outcomes or that indicate the row is simply not evaluable.
BOOTSTRAP_NEVER_WAIVED = frozenset(
    {
        "halt status not checked",
        "halt status is not verified clear",
        "SEC risk not checked",
        "SEC risk status is not verified clear",
        "extreme spread",
        "missing price",
        "invalid price",
        "invalid entry trigger",
        "invalid target",
        "invalid invalidation",
        "invalid reward/risk",
        "missing volume",
        "data quality unavailable",
        "data quality below alert threshold",
        "gap regime outside alert policy",
        "stop distance exceeds alert policy",
    }
)


def _bootstrap_paper_mode_enabled() -> bool:
    """True when the operator has explicitly opted into bootstrap paper mode."""

    return str(os.environ.get(BOOTSTRAP_MODE_ENV, "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def apply_alert_gates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [apply_alert_gate(row) for row in rows]


def is_alertable_notification_candidate(row: dict[str, Any]) -> bool:
    """Return whether a ticker may appear in a research-alert notification.

    This is the shared final predicate for any notifier/watchlist path that
    renders a candidate ticker.  It re-evaluates the canonical gate instead of
    trusting a caller-supplied score or stale ``can_alert`` bit.  The predicate
    does not authorize an official paper entry; V5 execution policy remains a
    separate, stricter boundary.
    """

    gate = evaluate_alert_gate(row)
    reported_status = str(row.get("alert_gate_status") or "").upper()
    reported_version = str(row.get("alert_gate_version") or "")
    reported_manual = row.get("manual_confirmation_required")
    return bool(
        row.get("can_alert") is True
        and not str(row.get("no_trade_reason") or "").strip()
        and gate["alert_gate_status"] in {PASS, ALERT_OK}
        and gate["manual_confirmation_required"] is False
        and (not reported_status or reported_status == gate["alert_gate_status"])
        and (not reported_version or reported_version == ALERT_GATE_VERSION)
        and (reported_manual is None or reported_manual is False)
    )


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
    receipt_state = _strategy_receipt_gate_state(row)

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
    if (
        _has_any(
            risk_text,
            ("source_conflict", "gap_conflict", "price_conflict", "volume_conflict"),
        )
        or str(row.get("score_consensus") or "").lower() == "multi_source_conflict"
    ):
        reasons.append("source conflict unresolved")
    stale_status = str(row.get("stale_data_status") or "").lower()
    if _truthy(row.get("stale_data_flag")) or stale_status == "stale":
        reasons.append("stale source")
    if source_confidence < HARD_SOURCE_CONFIDENCE_FLOOR:
        reasons.append("source confidence below hard threshold")
    elif source_confidence < ALERT_SOURCE_CONFIDENCE_FLOOR:
        reasons.append("source confidence below alert threshold")
    price = _price(row)
    if price is None or price <= 0:
        reasons.append("missing price")
    if _invalid_positive_alias(row, "premarket_price", "price", "current_price"):
        reasons.append("invalid price")
    if _invalid_positive_alias(
        row,
        "entry_trigger",
        "breakout_trigger",
        "entry_watch_level",
        "premarket_price",
        "price",
    ):
        reasons.append("invalid entry trigger")
    if _invalid_positive_alias(row, "target_1", "first_target"):
        reasons.append("invalid target")
    if _invalid_positive_alias(row, "invalidation", "invalidation_level", "exit_line"):
        reasons.append("invalid invalidation")
    if _invalid_positive_alias(row, "reward_risk_ratio"):
        reasons.append("invalid reward/risk")
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
    if _truthy(row.get("enrichment_was_fallback")):
        warnings.append("secondary Yahoo range used - research only")
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
    # The scanner declares how large a gap it considers credible.  A hardcoded
    # ceiling here contradicted configurations whose ideal band already reached
    # 140%, rejecting exactly the explosive gappers the strategy exists to find.
    # A negative gap is always rejected; the upper bound follows the scan.
    configured_gap_ceiling = _optional_float(row.get("max_credible_gap_pct"))
    gap_ceiling = (
        configured_gap_ceiling
        if configured_gap_ceiling is not None and configured_gap_ceiling > 0
        else MAX_ALERT_GAP_PCT
    )
    if gap_pct is not None and (gap_pct < 0 or gap_pct > gap_ceiling):
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

    reasons.extend(receipt_state["blocking_reasons"])

    bootstrap_mode = _bootstrap_paper_mode_enabled()
    bootstrap_waived: list[str] = []
    if bootstrap_mode:
        # Partition, never discard.  A safety reason is never waivable even if a
        # future edit adds it to a waivable set by mistake.
        def _partition(
            items: list[str], waivable: frozenset[str]
        ) -> tuple[list[str], list[str]]:
            kept: list[str] = []
            waived: list[str] = []
            for item in items:
                if item in waivable and item not in BOOTSTRAP_NEVER_WAIVED:
                    waived.append(item)
                else:
                    kept.append(item)
            return kept, waived

        reasons, waived_reasons = _partition(reasons, BOOTSTRAP_WAIVABLE_REASONS)
        warnings, waived_warnings = _partition(warnings, BOOTSTRAP_WAIVABLE_WARNINGS)
        missing, waived_missing = _partition(missing, BOOTSTRAP_WAIVABLE_MISSING)
        edge_reasons, waived_edge = _partition(
            edge_reasons, BOOTSTRAP_WAIVABLE_EDGE_REASONS
        )
        bootstrap_waived = _unique(
            [*waived_reasons, *waived_edge, *waived_warnings, *waived_missing]
        )

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
        and ("SEC risk not checked" in warnings or "halt status not checked" in warnings)
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
        # Truth preservation: what bootstrap mode set aside stays on the record,
        # so a bootstrap entry can never be mistaken for a fully evidenced one.
        "bootstrap_mode": bootstrap_mode,
        "bootstrap_waived_reasons": bootstrap_waived,
        "public_data_reliability_grade": grade,
        "missing_critical_fields": _unique(missing),
        "manual_confirmation_required": manual_required,
        "official_paper_gate_passed": not manual_required,
        "official_paper_eligible": False,
        "public_data_warning": "; ".join(public_warnings),
        "data_quality_label": _data_quality_label(grade),
        "strategy_receipt_tier": receipt_state["tier"],
        "strategy_receipt_research_pick_eligible": receipt_state["research_eligible"],
        "strategy_receipt_paper_entry_eligible": receipt_state["paper_eligible"],
        "strategy_receipt_entry_confirmation_required": (
            receipt_state["entry_confirmation_required"]
        ),
        "strategy_receipt_disagreement": receipt_state["disagreements"],
        "strategy_receipt_gate_blocked": receipt_state["blocked"],
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
    return _optional_float(_first_nonblank(row, "premarket_price", "price", "current_price"))


def _previous_close(row: dict[str, Any]) -> float | None:
    value = _optional_float(row.get("previous_close"))
    return value if value and value > 0 else None


def _volume(row: dict[str, Any]) -> float | None:
    value = _optional_float(_first_nonblank(row, "premarket_volume", "volume", "dollar_volume"))
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
    explicit_value = _first_nonblank(row, "reward_risk_ratio")
    if explicit_value is not None:
        return _optional_float(explicit_value)
    trigger = _optional_float(
        _first_nonblank(
            row,
            "entry_trigger",
            "breakout_trigger",
            "entry_watch_level",
            "premarket_price",
            "price",
        )
    )
    target = _optional_float(_first_nonblank(row, "target_1", "first_target"))
    invalidation = _optional_float(
        _first_nonblank(row, "invalidation", "invalidation_level", "exit_line")
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
        _first_nonblank(
            row,
            "entry_trigger",
            "breakout_trigger",
            "entry_watch_level",
            "premarket_price",
            "price",
        )
    )
    invalidation = _optional_float(
        _first_nonblank(row, "invalidation", "invalidation_level", "exit_line")
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
        token.strip() for token in str(value or "").replace(",", ";").split(";") if token.strip()
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
        parsed = float(str(value).replace("$", "").replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _invalid_positive_alias(row: dict[str, Any], *names: str) -> bool:
    value = _first_nonblank(row, *names)
    parsed = _optional_float(value)
    return value is not None and (parsed is None or parsed <= 0)


def _first_nonblank(mapping: dict[str, Any], *names: str) -> Any:
    """Return the first non-null/non-blank alias, preserving numeric zero."""

    for name in names:
        if name in mapping and not _blank(mapping[name]):
            return mapping[name]
    return None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _valid_ticker(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,4}", value or ""))


def _strategy_receipt_gate_state(row: dict[str, Any]) -> dict[str, Any]:
    """Expose receipt policy to the alert gate without weakening legacy gates."""

    if not _truthy(row.get("strategy_receipt_enabled")):
        return {
            "blocked": False,
            "tier": "",
            "research_eligible": None,
            "paper_eligible": None,
            "entry_confirmation_required": False,
            "disagreements": [],
            "blocking_reasons": [],
        }

    tier = str(row.get("strategy_receipt_tier") or row.get("pick_tier") or "").upper()
    research_eligible = _bool_or_none(row.get("strategy_receipt_research_pick_eligible"))
    paper_eligible = _bool_or_none(row.get("strategy_receipt_paper_entry_eligible"))
    disagreements = _unique(
        [
            *_tokens(row.get("strategy_receipt_disagreement")),
        ]
    )
    blocking_reasons: list[str] = []
    receipt_id = str(row.get("receipt_id") or "").strip()
    receipt_valid = validate_strategy_receipt_envelope(row)
    construction_status = str(row.get("strategy_receipt_construction_status") or "").upper()
    if not receipt_id or construction_status != "COMPLETE" or not receipt_valid:
        _append_unique(disagreements, "strategy_receipt_construction_failed")
        blocking_reasons.append("strategy decision receipt unavailable or unauthenticated")
    elif research_eligible is not True:
        _append_unique(disagreements, "strategy_receipt_research_ineligible")
        blocking_reasons.append("strategy decision receipt is not research eligible")
    elif tier not in RECEIPT_ALERTABLE_TIERS:
        _append_unique(disagreements, "strategy_receipt_tier_not_alertable")
        blocking_reasons.append("strategy decision receipt tier is not alertable")

    legacy_can_alert = _bool_or_none(row.get("strategy_receipt_legacy_can_alert"))
    if (
        legacy_can_alert is not None
        and research_eligible is not None
        and legacy_can_alert != research_eligible
    ):
        _append_unique(disagreements, "legacy_vs_receipt_alert_disposition")

    return {
        "blocked": bool(blocking_reasons),
        "tier": tier,
        "research_eligible": research_eligible,
        "paper_eligible": paper_eligible,
        "entry_confirmation_required": bool(research_eligible and not paper_eligible),
        "disagreements": disagreements,
        "blocking_reasons": blocking_reasons,
    }


def validate_strategy_receipt_envelope(row: dict[str, Any]) -> bool:
    payload = row.get("strategy_decision_receipt")
    if not isinstance(payload, dict):
        return False
    try:
        typed_receipt = parse_strategy_decision_receipt(payload)
    except (TypeError, ValueError, KeyError):
        return False
    receipt_hash = typed_receipt.receipt_hash_sha256
    receipt_id = typed_receipt.receipt_id
    ticker = str(row.get("ticker") or row.get("symbol") or "").upper()
    schema_version = typed_receipt.schema_version
    strategy_id = typed_receipt.strategy_id
    if (
        schema_version
        not in {
            "dawnstrike.strategy_decision_receipt.v1",
            "dawnstrike.strategy_decision_receipt.v2",
        }
        or (
            strategy_id == "alphaops_v5"
            and schema_version != "dawnstrike.strategy_decision_receipt.v2"
        )
        or typed_receipt.symbol != ticker
        or strategy_id != str(row.get("strategy_id") or "")
        or typed_receipt.strategy_version != str(row.get("strategy_version") or "")
        or (
            str(row.get("market_date") or "").strip()
            and typed_receipt.market_date != str(row.get("market_date") or "").strip()
        )
        or receipt_hash != str(row.get("receipt_hash_sha256") or "").lower()
        or receipt_id != str(row.get("receipt_id") or "")
        or str(row.get("strategy_receipt_persistence_status") or "").upper()
        not in {"PERSISTED", "REUSED"}
        or str(payload.get("pick_tier") or "").upper()
        != str(row.get("strategy_receipt_tier") or row.get("pick_tier") or "").upper()
        or payload.get("research_pick_eligible")
        is not _bool_or_none(row.get("strategy_receipt_research_pick_eligible"))
        or payload.get("paper_entry_eligible")
        is not _bool_or_none(row.get("strategy_receipt_paper_entry_eligible"))
        or payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        return False
    if schema_version == "dawnstrike.strategy_decision_receipt.v2":
        input_payload_text = typed_receipt.input_payload_json
        if not isinstance(input_payload_text, str) or not input_payload_text:
            return False
        try:
            input_payload = json.loads(input_payload_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if (
            not isinstance(input_payload, dict)
            or canonical_json(input_payload) != input_payload_text
            or hashlib.sha256(input_payload_text.encode("utf-8")).hexdigest()
            != str(payload.get("input_hash_sha256") or "")
        ):
            return False
        if strategy_id == "alphaops_v5" and not _valid_v5_receipt_plan_binding(
            row, payload, input_payload
        ):
            return False
        input_source_signal_id = next(
            (
                str(input_payload.get(key) or "").strip()
                for key in (
                    "source_signal_id",
                    "prior_session_signal_id",
                    "signal_id",
                    "signal_key",
                )
                if str(input_payload.get(key) or "").strip()
            ),
            "",
        )
        row_source_signal_id = next(
            (
                str(row.get(key) or "").strip()
                for key in (
                    "source_signal_id",
                    "prior_session_signal_id",
                    "signal_id",
                    "signal_key",
                )
                if str(row.get(key) or "").strip()
            ),
            "",
        )
        if row_source_signal_id and input_source_signal_id != row_source_signal_id:
            return False
        input_direction = str(input_payload.get("direction") or "").strip().lower()
        row_direction = str(row.get("direction") or "").strip().lower()
        if row_direction and (
            row_direction not in {"long", "short"} or input_direction != row_direction
        ):
            return False
    for key, expected in (
        ("alpha_score", typed_receipt.final_score),
        ("score", typed_receipt.final_score),
        ("final_score", typed_receipt.final_score),
        ("base_strategy_score", typed_receipt.base_strategy_score),
        ("score_adjustment", typed_receipt.score_adjustment),
    ):
        if key not in row or row.get(key) in {None, ""}:
            continue
        parsed = _optional_float(row.get(key))
        if parsed is None or parsed != expected:
            return False
    for payload_key, row_keys in (
        ("entry_reference", ("entry_reference", "entry_watch_level", "entry_trigger")),
        ("stop", ("stop", "invalidation_level", "invalidation")),
        ("target", ("target", "target_1", "first_target")),
        ("reward_risk_ratio", ("reward_risk_ratio",)),
    ):
        row_value = next((row.get(key) for key in row_keys if row.get(key) is not None), None)
        if _optional_float(payload.get(payload_key)) != _optional_float(row_value):
            return False
    return True


def _valid_v5_receipt_plan_binding(
    row: dict[str, Any], receipt: dict[str, Any], input_payload: dict[str, Any]
) -> bool:
    row_plan = row.get("alphaops_market_structure_plan")
    input_plan = input_payload.get("alphaops_market_structure_plan")
    if not isinstance(row_plan, dict) or not isinstance(input_plan, dict):
        return False
    if not is_valid_alphaops_v5_plan(row_plan) or canonical_json(row_plan) != canonical_json(
        input_plan
    ):
        return False
    plan_hash = str(row_plan.get("plan_hash_sha256") or "")
    if (
        not re.fullmatch(r"[0-9a-f]{64}", plan_hash)
        or str(row.get("plan_hash_sha256") or "") != plan_hash
        or str(receipt.get("plan_hash_sha256") or "") != plan_hash
        or str(input_payload.get("plan_hash_sha256") or "") != plan_hash
    ):
        return False
    input_observations = input_payload.get("market_structure_observations")
    row_observations = row.get("market_structure_observations")
    if canonical_json(input_observations) != canonical_json(row_observations):
        return False
    if str(row_plan.get("status") or "") == COMPLETE and (
        not isinstance(input_observations, dict) or not input_observations
    ):
        return False
    metrics = modeled_alphaops_v5_plan_metrics(row_plan)
    for receipt_key, metric_key in (
        ("gross_reward_risk_ratio", "gross_reward_risk"),
        ("after_cost_reward_risk_ratio", "actual_after_cost_reward_risk"),
        ("stop_distance_pct", "stop_distance_pct"),
    ):
        if _optional_float(receipt.get(receipt_key)) != _optional_float(metrics.get(metric_key)):
            return False
    after_cost = _optional_float(metrics.get("actual_after_cost_reward_risk"))
    stop_distance = _optional_float(metrics.get("stop_distance_pct"))
    expected_blockers: list[str] = []
    if after_cost is None or after_cost + 1e-12 < DEFAULT_V5_POLICY.minimum_after_cost_reward_risk:
        expected_blockers.append("after_cost_reward_risk_below_policy")
    if stop_distance is None or stop_distance > DEFAULT_V5_POLICY.maximum_stop_distance_pct:
        expected_blockers.append("stop_distance_exceeds_policy")
    if list(receipt.get("paper_entry_blockers") or []) != expected_blockers:
        return False
    return not (receipt.get("paper_entry_eligible") is True and expected_blockers)


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    return None


def _unique(items: list[str]) -> list[str]:
    output: list[str] = []
    for item in items:
        clean = item.strip()
        if clean and clean not in output:
            output.append(clean)
    return output
