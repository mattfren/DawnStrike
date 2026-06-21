"""Premarket setup intelligence and outcome evaluation."""

from __future__ import annotations

import json
from csv import DictWriter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.formula import FormulaResult
from intraday_scanner.models import SnapshotRow, utc_now_iso

ACTION_OPENING_BREAKOUT = "BREAKOUT WATCH"
ACTION_MOMENTUM_CONTINUATION = "HIGH VOLATILITY WATCH"
ACTION_WATCH_ONLY = "WATCH"
ACTION_NEEDS_CONFIRMATION = "CAUTION"
ACTION_AVOID = "AVOID"
ACTION_INVALIDATED = "INVALIDATED"
ACTION_THESIS_BROKEN = "THESIS BROKEN"
ACTION_OUTCOME_NEEDED = "OUTCOME NEEDED"

ALLOWED_SIGNAL_LABELS = {
    ACTION_WATCH_ONLY,
    ACTION_OPENING_BREAKOUT,
    ACTION_MOMENTUM_CONTINUATION,
    ACTION_NEEDS_CONFIRMATION,
    ACTION_AVOID,
    ACTION_INVALIDATED,
    ACTION_THESIS_BROKEN,
    ACTION_OUTCOME_NEEDED,
}

INTELLIGENCE_OUTCOME_COLUMNS = [
    "evaluated_at",
    "scan_id",
    "ticker",
    "premarket_price",
    "open",
    "high_of_day",
    "low_of_day",
    "close",
    "premarket_high",
    "premarket_low",
    "gap_percent",
    "dollar_volume",
    "float_rotation",
    "catalyst_tier",
    "premarket_structure",
    "risk_level",
    "classification",
    "predicted_action",
    "actual_outcome",
    "breakout_triggered",
    "max_gain_after_trigger_pct",
    "max_drawdown_after_trigger_pct",
    "stop_would_have_hit",
    "target_1_hit",
    "target_2_hit",
]


@dataclass(frozen=True)
class CatalystAssessment:
    catalyst_tier: str
    catalyst_category: str
    catalyst_summary: str
    catalyst_confidence: float
    catalyst_risk_flags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class StructureAssessment:
    premarket_structure: str
    structure_notes: str


@dataclass(frozen=True)
class FloatRotationAssessment:
    float_rotation: float | None
    float_rotation_label: str


@dataclass(frozen=True)
class OpeningPlan:
    action: str
    entry_trigger: str
    confirmation_needed: bool
    invalidation: str
    target_1: str
    target_2: str
    risk_level: str
    why_this_matters: str
    do_not_enter_if: str


@dataclass(frozen=True)
class IntelligenceResult:
    action: str
    classification: str
    predicted_action: str
    catalyst: CatalystAssessment
    structure: StructureAssessment
    float_rotation: FloatRotationAssessment
    opening_plan: OpeningPlan
    risk_level: str
    data_confidence_score: float
    data_warnings: list[str]
    field_sources: dict[str, str]
    historical_win_rate: str
    average_max_gain: str
    average_drawdown: str
    similar_setup_count: int
    probability_note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "classification": self.classification,
            "predicted_action": self.predicted_action,
            "catalyst_tier": self.catalyst.catalyst_tier,
            "catalyst_category": self.catalyst.catalyst_category,
            "catalyst_summary": self.catalyst.catalyst_summary,
            "catalyst_confidence": self.catalyst.catalyst_confidence,
            "catalyst_risk_flags": ";".join(self.catalyst.catalyst_risk_flags),
            "premarket_structure": self.structure.premarket_structure,
            "structure_notes": self.structure.structure_notes,
            "float_rotation": self.float_rotation.float_rotation
            if self.float_rotation.float_rotation is not None
            else "",
            "float_rotation_label": self.float_rotation.float_rotation_label,
            "entry_trigger": self.opening_plan.entry_trigger,
            "confirmation_needed": self.opening_plan.confirmation_needed,
            "invalidation": self.opening_plan.invalidation,
            "target_1": self.opening_plan.target_1,
            "target_2": self.opening_plan.target_2,
            "risk_level": self.risk_level,
            "why_this_matters": self.opening_plan.why_this_matters,
            "do_not_enter_if": self.opening_plan.do_not_enter_if,
            "data_confidence_score": self.data_confidence_score,
            "data_warnings": ";".join(self.data_warnings),
            "field_sources": json.dumps(self.field_sources, sort_keys=True),
            "historical_win_rate": self.historical_win_rate,
            "average_max_gain": self.average_max_gain,
            "average_drawdown": self.average_drawdown,
            "similar_setup_count": self.similar_setup_count,
            "probability_note": self.probability_note,
        }


def build_premarket_intelligence(
    row: SnapshotRow,
    formula: FormulaResult,
    config: ScannerConfig,
    *,
    breakout_trigger: float,
    invalidation_level: float,
    first_target: float,
    stretch_target: float,
    historical_outcomes: list[dict[str, Any]] | None = None,
) -> IntelligenceResult:
    catalyst = classify_catalyst(row.catalyst_headline, has_news=row.has_news)
    structure = classify_premarket_structure(row, formula)
    float_rotation = classify_float_rotation(row)
    liquidity_risks = liquidity_and_spread_risks(row, formula, config, catalyst)
    action = classify_trade_action(row, formula, catalyst, structure, liquidity_risks)
    risk_level = _risk_level(action, liquidity_risks, formula.avoid_reasons)
    warnings = data_quality_warnings(row, catalyst, float_rotation)
    confidence = data_confidence_score(row, warnings)
    probability = probability_summary(
        similar_historical_outcomes(
            historical_outcomes or [],
            action=action,
            catalyst_tier=catalyst.catalyst_tier,
            structure=structure.premarket_structure,
            risk_level=risk_level,
        )
    )
    plan = generate_opening_plan(
        row,
        action=action,
        catalyst=catalyst,
        structure=structure,
        risk_level=risk_level,
        breakout_trigger=breakout_trigger,
        invalidation_level=invalidation_level,
        first_target=first_target,
        stretch_target=stretch_target,
    )
    return IntelligenceResult(
        action=action,
        classification=action,
        predicted_action=action,
        catalyst=catalyst,
        structure=structure,
        float_rotation=float_rotation,
        opening_plan=plan,
        risk_level=risk_level,
        data_confidence_score=confidence,
        data_warnings=warnings,
        field_sources=field_sources(row),
        historical_win_rate=probability["historical_win_rate"],
        average_max_gain=probability["average_max_gain"],
        average_drawdown=probability["average_drawdown"],
        similar_setup_count=int(probability["similar_setup_count"]),
        probability_note=probability["probability_note"],
    )


def classify_catalyst(headline: str, *, has_news: bool = True) -> CatalystAssessment:
    text = str(headline or "").strip()
    lowered = text.lower()
    flags: list[str] = []
    if not text or not has_news:
        return CatalystAssessment(
            "C", "no_clear_catalyst", "No clear catalyst", 0.2, ["missing_catalyst"]
        )
    if any(term in lowered for term in ("paid promotion", "sponsored", "stock promotion")):
        flags.append("paid_promotion_style")
    if any(term in lowered for term in ("rumor", "social media", "viral", "reddit", "x post")):
        flags.append("social_media_hype")
    if any(term in lowered for term in ("reiterates", "reminds", "update on prior", "previously")):
        flags.append("recycled_news")
    if any(
        term in lowered
        for term in (
            "offering",
            "shelf",
            "atm",
            "warrant",
            "registered direct",
            "private placement",
        )
    ):
        flags.append("dilution_language")
        return CatalystAssessment("C", "dilution_risk", _summary(text), 0.25, flags)
    if any(
        term in lowered
        for term in (
            "sec investigation",
            "lawsuit",
            "subpoena",
            "delisting",
            "regulatory action",
            "clinical hold",
        )
    ):
        flags.append("legal_or_regulatory_language")
        return CatalystAssessment("C", "legal/regulatory_risk", _summary(text), 0.25, flags)

    tier_a = [
        "fda approval",
        "fda clears",
        "fda fast-track",
        "fast-track",
        "fast track",
        "breakthrough therapy",
        "positive clinical",
        "clinical trial",
        "phase 2",
        "phase ii",
        "phase 3",
        "phase iii",
        "buyout",
        "acquisition",
        "acquires",
        "earnings beat",
        "major earnings beat",
        "major contract",
        "strategic investment",
    ]
    tier_b = [
        "partnership",
        "contract",
        "supply agreement",
        "product launch",
        "guidance",
        "analyst upgrade",
        "expansion",
        "collaboration",
    ]
    theme_terms = (
        "ai",
        "artificial intelligence",
        "semiconductor",
        "semis",
        "nuclear",
        "crypto",
        "bitcoin",
        "defense",
        "quantum",
        "robotics",
        "biotech",
    )
    if flags:
        category = "sympathy_momentum" if "social_media_hype" in flags else "soft_catalyst"
        return CatalystAssessment("C", category, _summary(text), 0.35, flags)
    if any(term in lowered for term in tier_a):
        return CatalystAssessment("A", "confirmed_catalyst", _summary(text), 0.9, [])
    if any(term in lowered for term in tier_b):
        return CatalystAssessment("B", "soft_catalyst", _summary(text), 0.7, [])
    if any(term in lowered for term in theme_terms):
        return CatalystAssessment("B", "sympathy_momentum", _summary(text), 0.6, [])
    if any(term in lowered for term in ("corporate update", "shareholder update", "letter")):
        return CatalystAssessment(
            "C", "soft_catalyst", _summary(text), 0.45, ["vague_corporate_update"]
        )
    return CatalystAssessment(
        "C", "no_clear_catalyst", _summary(text), 0.5, ["unverified_catalyst_quality"]
    )


def classify_premarket_structure(row: SnapshotRow, formula: FormulaResult) -> StructureAssessment:
    position = formula.range_position_pct
    spread = row.spread_pct
    if row.premarket_volume <= 0:
        return StructureAssessment("weak", "Thin volume; no confirmation yet")
    if position >= 78 and spread < 5:
        return StructureAssessment("strong", "Holding near premarket highs")
    if position <= 35:
        return StructureAssessment("weak", "Fading from premarket high")
    if spread >= 5:
        return StructureAssessment("mixed", "Wide spread; needs cleaner confirmation")
    return StructureAssessment("mixed", "Mid-range consolidation")


def classify_float_rotation(row: SnapshotRow) -> FloatRotationAssessment:
    if row.float_shares is None or row.float_shares <= 0:
        return FloatRotationAssessment(None, "unknown")
    rotation = row.premarket_volume / row.float_shares
    if rotation < 0.25:
        label = "low pressure"
    elif rotation <= 1.0:
        label = "moderate pressure"
    elif rotation <= 3.0:
        label = "high pressure"
    else:
        label = "extreme pressure"
    return FloatRotationAssessment(round(rotation, 4), label)


def liquidity_and_spread_risks(
    row: SnapshotRow,
    formula: FormulaResult,
    config: ScannerConfig,
    catalyst: CatalystAssessment,
) -> list[str]:
    risks: list[str] = []
    if formula.dollar_volume < config.min_premarket_dollar_volume:
        risks.append("low_dollar_volume")
    if row.spread_pct >= config.wide_spread_pct:
        risks.append("wide_spread")
    if row.premarket_price < 1:
        risks.append("price_under_1")
    if row.premarket_volume < config.min_premarket_share_volume:
        risks.append("low_absolute_volume")
    if catalyst.catalyst_tier == "C":
        risks.append("weak_or_missing_catalyst")
    if row.float_shares is None or row.float_shares <= 0:
        risks.append("no_float_data")
    if row.stale_data_flag:
        risks.append("stale_source")
    if row.source_confidence and row.source_confidence < 50:
        risks.append("bad_source_quality_score")
    if row.recent_offering:
        risks.append("prior_offering_or_dilution")
    if row.current_halt:
        risks.append("halt_risk")
    for flag in formula.risk_flags:
        if flag not in risks:
            risks.append(flag)
    return risks


def classify_trade_action(
    row: SnapshotRow,
    formula: FormulaResult,
    catalyst: CatalystAssessment,
    structure: StructureAssessment,
    liquidity_risks: list[str],
) -> str:
    hard_risks = {
        "halt_risk",
        "current_halt",
        "prior_offering_or_dilution",
        "recent_offering",
        "low_dollar_volume",
        "low_absolute_volume",
        "price_under_1",
        "sub_min_price",
    }
    if any(risk in liquidity_risks for risk in hard_risks):
        return ACTION_AVOID
    if structure.premarket_structure == "weak" and catalyst.catalyst_tier == "C":
        return ACTION_AVOID
    if (
        catalyst.catalyst_tier == "A"
        and structure.premarket_structure == "strong"
        and formula.dollar_volume >= 1_500_000
        and row.spread_pct < 4
        and 15 <= formula.gap_pct <= 180
    ):
        return ACTION_OPENING_BREAKOUT
    if (
        catalyst.catalyst_tier in {"A", "B"}
        and structure.premarket_structure == "strong"
        and formula.dollar_volume >= 750_000
    ):
        return ACTION_MOMENTUM_CONTINUATION
    if structure.premarket_structure == "mixed" and catalyst.catalyst_tier in {"A", "B"}:
        return ACTION_NEEDS_CONFIRMATION
    if structure.premarket_structure == "weak":
        return ACTION_WATCH_ONLY
    if catalyst.catalyst_tier == "C" or "wide_spread" in liquidity_risks:
        return ACTION_WATCH_ONLY
    return ACTION_NEEDS_CONFIRMATION


def generate_opening_plan(
    row: SnapshotRow,
    *,
    action: str,
    catalyst: CatalystAssessment,
    structure: StructureAssessment,
    risk_level: str,
    breakout_trigger: float,
    invalidation_level: float,
    first_target: float,
    stretch_target: float,
) -> OpeningPlan:
    if action == ACTION_AVOID:
        entry = "Manual review only; setup is not eligible"
        avoid_if = "Catalyst stays weak, liquidity fades, or price rejects VWAP"
    elif action == ACTION_OPENING_BREAKOUT:
        entry = f"Watch confirmation over {_money(breakout_trigger)}"
        avoid_if = "Fails VWAP or rejects opening range"
    else:
        entry = f"Wait for opening range confirmation over {_money(breakout_trigger)}"
        avoid_if = f"Drops below {_money(max(invalidation_level, row.premarket_low))}"
    why = f"Tier {catalyst.catalyst_tier} catalyst; {structure.structure_notes.lower()}"
    return OpeningPlan(
        action=action,
        entry_trigger=entry,
        confirmation_needed=True,
        invalidation=_money(invalidation_level),
        target_1=_money(first_target),
        target_2=_money(stretch_target),
        risk_level=risk_level,
        why_this_matters=why,
        do_not_enter_if=avoid_if,
    )


def data_quality_warnings(
    row: SnapshotRow,
    catalyst: CatalystAssessment,
    float_rotation: FloatRotationAssessment,
) -> list[str]:
    warnings: list[str] = []
    if float_rotation.float_rotation is None:
        warnings.append("missing_float_data")
    if catalyst.catalyst_tier == "C":
        warnings.append("weak_or_missing_catalyst")
    if row.coverage_warning:
        warnings.extend(_split_flags(row.coverage_warning))
    if row.fixture_only or "fixture" in row.source.lower() or "sample" in row.source.lower():
        warnings.append("synthetic_or_test_data")
    if row.stale_data_flag:
        warnings.append("stale_data")
    if not row.as_of_timestamp:
        warnings.append("missing_timestamp")
    elif _is_stale_timestamp(row.as_of_timestamp):
        warnings.append("stale_data")
    if row.missing_enrichment_count > 0:
        warnings.append(f"missing_enrichment_count:{row.missing_enrichment_count}")
    return _dedupe(warnings)


def data_confidence_score(row: SnapshotRow, warnings: list[str]) -> float:
    score = 100.0
    if row.float_shares is None or row.float_shares <= 0:
        score -= 12
    if not row.catalyst_headline:
        score -= 15
    if row.coverage_warning:
        score -= 10
    if row.fixture_only:
        score -= 15
    score -= min(row.missing_enrichment_count * 4, 20)
    score -= min(len(warnings) * 3, 18)
    return round(max(0.0, min(100.0, score)), 1)


def field_sources(row: SnapshotRow) -> dict[str, str]:
    base = row.source or "unknown"
    return {
        "premarket_price": base,
        "premarket_high": base,
        "premarket_low": base,
        "premarket_volume": base,
        "gap_pct": base,
        "dollar_volume": base,
        "float_shares": base if row.float_shares else "missing",
        "catalyst_headline": base if row.catalyst_headline else "missing",
        "spread_pct": base,
        "halt_risk": base,
        "vwap": "unavailable",
        "relative_volume": "unavailable",
    }


def similar_historical_outcomes(
    outcomes: list[dict[str, Any]],
    *,
    action: str,
    catalyst_tier: str,
    structure: str,
    risk_level: str,
) -> list[dict[str, Any]]:
    exact = [
        row
        for row in outcomes
        if str(row.get("predicted_action") or row.get("classification") or "") == action
        and str(row.get("catalyst_tier") or "") == catalyst_tier
        and str(row.get("premarket_structure") or "") == structure
        and str(row.get("risk_level") or "") == risk_level
    ]
    if exact:
        return exact
    return [
        row
        for row in outcomes
        if str(row.get("predicted_action") or row.get("classification") or "") == action
        and str(row.get("catalyst_tier") or "") == catalyst_tier
    ]


def evaluate_intelligence_outcomes(
    *,
    store: Any,
    run_id: str | None = None,
    min_samples: int = 20,
    persist: bool = False,
) -> dict[str, Any]:
    scan = store.load_scan(run_id) if run_id else store.load_latest_scan()
    if not scan:
        return {
            "summary": {
                "created_at": utc_now_iso(),
                "status": "no_scan",
                "run_id": run_id or "",
                "evaluated_count": 0,
            },
            "rows": [],
        }
    resolved_run_id = str(scan.get("run_id") or run_id or "")
    candidates = _candidate_rows_for_evaluation(scan)
    outcomes = store.load_manual_outcomes(limit=5000)
    rows = []
    for candidate in candidates:
        outcome = _match_outcome(candidate, outcomes, resolved_run_id)
        if outcome is None or not _has_outcome_prices(outcome):
            continue
        rows.append(evaluate_outcome_for_candidate(candidate, outcome))
    probability = probability_summary(rows, min_samples=min_samples)
    summary = {
        "created_at": utc_now_iso(),
        "status": "evaluated" if rows else "no_price_outcomes",
        "run_id": resolved_run_id,
        "candidate_count": len(candidates),
        "manual_outcome_count": len(outcomes),
        "evaluated_count": len(rows),
        "breakout_triggered_count": sum(1 for row in rows if row.get("breakout_triggered")),
        "target_1_hit_count": sum(1 for row in rows if row.get("target_1_hit")),
        "target_2_hit_count": sum(1 for row in rows if row.get("target_2_hit")),
        "stop_hit_count": sum(1 for row in rows if row.get("stop_would_have_hit")),
        **probability,
    }
    if persist:
        store.persist_intelligence_outcomes(summary, rows, run_id=resolved_run_id)
    return {"summary": summary, "rows": rows}


def write_intelligence_outcome_outputs(
    result: dict[str, Any],
    out_dir: str | Path,
) -> dict[str, Path]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = list(result.get("rows") or [])
    summary = dict(result.get("summary") or {})
    rows_path = output_dir / "intelligence_outcomes.csv"
    summary_path = output_dir / "intelligence_outcome_summary.json"
    with rows_path.open("w", encoding="utf-8", newline="") as handle:
        writer = DictWriter(handle, fieldnames=INTELLIGENCE_OUTCOME_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return {"rows": rows_path, "summary": summary_path}


def evaluate_outcome_for_candidate(
    candidate: dict[str, Any],
    outcome: dict[str, Any],
) -> dict[str, Any]:
    trigger = _num(candidate.get("breakout_trigger") or candidate.get("entry_trigger_price"))
    stop = _num(candidate.get("invalidation_level") or candidate.get("invalidation"))
    target_1 = _num(candidate.get("first_target") or candidate.get("target_1"))
    target_2 = _num(candidate.get("stretch_target") or candidate.get("target_2"))
    high = _num(
        outcome.get("high_of_day")
        or outcome.get("high_after_entry")
        or outcome.get("high")
        or outcome.get("premarket_high")
    )
    low = _num(
        outcome.get("low_of_day")
        or outcome.get("low_after_entry")
        or outcome.get("low")
        or outcome.get("premarket_low")
    )
    close = _num(outcome.get("close") or outcome.get("close_price"))
    open_price = _num(
        outcome.get("open") or outcome.get("open_price") or outcome.get("entry_price")
    )
    triggered = bool(trigger > 0 and high >= trigger)
    max_gain = ((high - trigger) / trigger) * 100 if triggered and trigger > 0 else 0.0
    drawdown = ((low - trigger) / trigger) * 100 if triggered and trigger > 0 else 0.0
    stop_hit = bool(triggered and stop > 0 and low <= stop)
    target_1_hit = bool(triggered and target_1 > 0 and high >= target_1)
    target_2_hit = bool(triggered and target_2 > 0 and high >= target_2)
    actual = _actual_outcome(triggered, stop_hit, target_1_hit, target_2_hit, close, trigger)
    return {
        "evaluated_at": utc_now_iso(),
        "scan_id": candidate.get("scan_id") or outcome.get("scan_id") or "",
        "ticker": candidate.get("ticker") or outcome.get("ticker") or "",
        "premarket_price": candidate.get("premarket_price"),
        "open": open_price,
        "high_of_day": high,
        "low_of_day": low,
        "close": close,
        "premarket_high": candidate.get("premarket_high") or outcome.get("premarket_high"),
        "premarket_low": candidate.get("premarket_low") or outcome.get("premarket_low"),
        "gap_percent": candidate.get("gap_pct"),
        "dollar_volume": candidate.get("dollar_volume"),
        "float_rotation": candidate.get("float_rotation"),
        "catalyst_tier": candidate.get("catalyst_tier"),
        "premarket_structure": candidate.get("premarket_structure"),
        "risk_level": candidate.get("risk_level"),
        "classification": candidate.get("classification") or candidate.get("action"),
        "predicted_action": candidate.get("predicted_action") or candidate.get("action"),
        "actual_outcome": actual,
        "breakout_triggered": triggered,
        "max_gain_after_trigger_pct": round(max_gain, 2),
        "max_drawdown_after_trigger_pct": round(drawdown, 2),
        "stop_would_have_hit": stop_hit,
        "target_1_hit": target_1_hit,
        "target_2_hit": target_2_hit,
    }


def probability_summary(outcomes: list[dict[str, Any]], *, min_samples: int = 20) -> dict[str, Any]:
    count = len(outcomes)
    if count < min_samples:
        return {
            "historical_win_rate": "insufficient sample size",
            "average_max_gain": "insufficient sample size",
            "average_drawdown": "insufficient sample size",
            "similar_setup_count": count,
            "probability_note": "insufficient sample size",
        }
    wins = sum(1 for row in outcomes if row.get("target_1_hit") or row.get("target_2_hit"))
    avg_gain = sum(_num(row.get("max_gain_after_trigger_pct")) for row in outcomes) / count
    avg_drawdown = sum(_num(row.get("max_drawdown_after_trigger_pct")) for row in outcomes) / count
    return {
        "historical_win_rate": round((wins / count) * 100, 1),
        "average_max_gain": round(avg_gain, 2),
        "average_drawdown": round(avg_drawdown, 2),
        "similar_setup_count": count,
        "probability_note": "Historical sample only; no precision claim.",
    }


def _actual_outcome(
    triggered: bool,
    stop_hit: bool,
    target_1_hit: bool,
    target_2_hit: bool,
    close: float,
    trigger: float,
) -> str:
    if not triggered:
        return "no_trigger"
    if target_2_hit:
        return "target_2_hit"
    if target_1_hit:
        return "target_1_hit"
    if stop_hit:
        return "stop_hit"
    if close > trigger:
        return "closed_above_trigger"
    return "trigger_failed"


def _candidate_rows_for_evaluation(scan: dict[str, Any]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for row in list(scan.get("ranked_candidates") or []) + list(scan.get("avoid_list") or []):
        ticker = str(row.get("ticker") or "").upper()
        if ticker and ticker not in unique:
            candidate = dict(row)
            candidate.setdefault("scan_id", scan.get("run_id") or "")
            unique[ticker] = candidate
    return list(unique.values())


def _match_outcome(
    candidate: dict[str, Any],
    outcomes: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any] | None:
    ticker = str(candidate.get("ticker") or "").upper()
    same_scan = [
        row
        for row in outcomes
        if str(row.get("ticker") or "").upper() == ticker
        and str(row.get("scan_id") or "") == run_id
    ]
    if same_scan:
        return same_scan[0]
    same_ticker = [row for row in outcomes if str(row.get("ticker") or "").upper() == ticker]
    return same_ticker[0] if same_ticker else None


def _has_outcome_prices(outcome: dict[str, Any]) -> bool:
    return any(
        _num(outcome.get(key)) > 0
        for key in (
            "high_of_day",
            "high_after_entry",
            "high",
            "low_of_day",
            "low_after_entry",
            "low",
            "close",
            "close_price",
        )
    )


def _risk_level(action: str, risks: list[str], avoid_reasons: list[str]) -> str:
    if action == ACTION_AVOID or avoid_reasons:
        return "high"
    if any(risk in risks for risk in ("wide_spread", "no_float_data", "weak_or_missing_catalyst")):
        return "medium"
    return "low"


def _summary(value: str) -> str:
    cleaned = " ".join(str(value or "").split())
    if not cleaned:
        return "No clear catalyst"
    return cleaned[:57].rstrip() + "..." if len(cleaned) > 60 else cleaned


def _money(value: Any) -> str:
    number = _num(value)
    return "n/a" if number <= 0 else f"${number:.2f}"


def _is_stale_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(tz=timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()
    return age_seconds > 18 * 60 * 60


def _num(value: Any) -> float:
    if value in {None, ""}:
        return 0.0
    try:
        return float(str(value).replace("$", "").replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return 0.0


def _split_flags(value: str) -> list[str]:
    return [part.strip() for part in str(value).replace(",", ";").split(";") if part.strip()]


def _dedupe(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
