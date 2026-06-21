"""AlphaOps v4 adaptive research scoring."""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.edge_calibrator import calibrate_edge, score_decile
from intraday_scanner.alpha.feature_factory import feature_for_model
from intraday_scanner.alpha.risk_governor import evaluate_risk

ALPHA_MODEL_VERSION = "dawnstrike-alphaops-v4"


class AlphaModel:
    """Rule-first adaptive model with honest insufficient-sample fallback."""

    def __init__(self, *, min_real_days: int = 20) -> None:
        self.min_real_days = min_real_days

    def score_candidates(
        self,
        candidates: list[dict[str, Any]],
        feature_vectors: list[dict[str, Any]],
        *,
        historical_outcomes: list[dict[str, Any]] | None = None,
        setup_memory: dict[str, dict[str, Any]] | None = None,
        real_shadow_days: int = 0,
    ) -> list[dict[str, Any]]:
        historical_outcomes = list(historical_outcomes or [])
        setup_memory = dict(setup_memory or {})
        feature_by_ticker = {
            str(row.get("ticker") or "").upper(): row for row in feature_vectors
        }
        scored: list[dict[str, Any]] = []
        for row in candidates:
            ticker = str(row.get("ticker") or "").upper()
            feature_record = feature_by_ticker.get(ticker, {})
            features = feature_for_model(feature_record) if feature_record else {}
            risk = evaluate_risk(row, features)
            setup_key = setup_key_for_candidate(row, features)
            bucket_rows = [
                outcome
                for outcome in historical_outcomes
                if str(outcome.get("setup_key") or "").lower() == setup_key.lower()
            ]
            calibration = calibrate_edge(
                bucket_rows=bucket_rows,
                global_rows=historical_outcomes,
                real_shadow_days=real_shadow_days,
            )
            base_score = _float(row.get("score") or row.get("total_score"), 0.0)
            explosive = _float(row.get("explosive_score"), base_score)
            catalyst = _float(row.get("catalyst_score"), 50.0)
            execution = _execution_score(row, features)
            expected_edge = _expected_edge_score(calibration, setup_memory.get(setup_key))
            alpha = (
                (base_score * 0.34)
                + (explosive * 0.20)
                + (catalyst * 0.12)
                + (execution * 0.18)
                + (expected_edge * 0.16)
            )
            risk_adjusted = max(0.0, alpha * (risk.risk_score / 100.0))
            no_trade_reason = "" if risk.can_alert else ";".join(risk.hard_avoid_reasons)
            output = {
                **row,
                "ticker": ticker,
                "model_version": ALPHA_MODEL_VERSION,
                "alpha_score": round(risk_adjusted, 2),
                "expected_edge_score": round(expected_edge, 2),
                "explosive_score": round(explosive, 2),
                "execution_score": round(execution, 2),
                "risk_adjusted_score": round(risk_adjusted, 2),
                "edge_bucket": _edge_bucket(risk_adjusted),
                "confidence_bucket": calibration["confidence_bucket"],
                "expectancy_status": (
                    "INSUFFICIENT_SAMPLE"
                    if real_shadow_days < self.min_real_days
                    else "EMPIRICAL_PRIOR"
                ),
                "expected_return_bucket": calibration["expected_return_bucket"],
                "drawdown_risk_bucket": calibration["drawdown_risk_bucket"],
                "hit_rate_bucket": calibration["hit_rate_bucket"],
                "outlier_dependency": calibration.get("outlier_dependency", 0.0),
                "sample_size": calibration.get("sample_size", 0),
                "score_decile": score_decile(risk_adjusted),
                "setup_key": setup_key,
                "risk_flags": ";".join(risk.risk_flags),
                "avoid_reasons": ";".join(risk.avoid_reasons),
                "hard_avoid_reasons": risk.hard_avoid_reasons,
                "soft_penalties": risk.soft_penalties,
                "risk_score": risk.risk_score,
                "can_alert": risk.can_alert,
                "no_trade_reason": no_trade_reason,
                "feature_config_hash": feature_record.get("config_hash", ""),
            }
            output.update(_execution_plan(output))
            scored.append(output)
        return sorted(scored, key=lambda item: float(item.get("alpha_score") or 0.0), reverse=True)


def setup_key_for_candidate(row: dict[str, Any], features: dict[str, Any] | None = None) -> str:
    features = dict(features or {})
    grade = str(row.get("setup_grade") or features.get("setup_grade") or "unknown").strip()
    gap_bucket = str(features.get("gap_bucket") or _gap_bucket(row.get("gap_pct")))
    volume_bucket = str(features.get("dollar_volume_bucket") or "unknown_volume")
    catalyst = str(row.get("catalyst_category") or features.get("catalyst_category") or "unclear")
    return f"grade:{grade}|gap:{gap_bucket}|volume:{volume_bucket}|catalyst:{catalyst}"


def _execution_score(row: dict[str, Any], features: dict[str, Any]) -> float:
    score = 50.0
    if _float(row.get("breakout_trigger") or features.get("breakout_trigger")):
        score += 15.0
    if _float(row.get("invalidation_level") or features.get("invalidation_level")):
        score += 15.0
    if _float(row.get("first_target") or features.get("first_target")):
        score += 10.0
    if str(row.get("best_exit_bias") or row.get("exit_bias") or ""):
        score += 5.0
    if _float(row.get("spread_pct") or features.get("spread_pct"), 0.0) > 4:
        score -= 10.0
    return max(0.0, min(100.0, score))


def _expected_edge_score(calibration: dict[str, Any], memory: dict[str, Any] | None) -> float:
    bucket = str(calibration.get("expected_return_bucket") or "")
    base = {
        "HIGH": 85.0,
        "MEDIUM": 65.0,
        "LOW": 50.0,
        "NEGATIVE": 25.0,
        "INSUFFICIENT_SAMPLE": 45.0,
    }.get(bucket, 40.0)
    if memory:
        win_rate = _float(memory.get("win_rate_pct"), 0.0)
        avg_return = _float(memory.get("avg_return_pct"), 0.0)
        bounded_return = min(25.0, max(-25.0, avg_return)) + 25
        base = (base * 0.5) + (min(100.0, win_rate) * 0.3) + bounded_return
    return max(0.0, min(100.0, base))


def _execution_plan(row: dict[str, Any]) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "UNKNOWN")
    trigger = _price(row.get("breakout_trigger") or row.get("target_1"))
    invalidation = _price(row.get("invalidation_level") or row.get("invalidation"))
    first_target = _price(row.get("first_target") or row.get("target_1"))
    return {
        "alert_plan": {
            "trigger": f"Watch {ticker} only if price confirms above {trigger}.",
            "confirmation": "Prefer sustained volume and clean holds above the trigger.",
            "invalidation": f"Research thesis is invalid below {invalidation}.",
            "do_not_chase": "Do not chase a vertical extension without a fresh pullback.",
            "monitor": "Re-check every 5 minutes until invalidated, extended, or target reached.",
            "outcome_fields": [
                "winner_1m",
                "winner_5m",
                "winner_15m",
                "winner_lunch",
                "winner_close",
                "high_after_entry_return",
                "low_after_entry_drawdown",
            ],
        },
        "entry_trigger": trigger,
        "invalidation": invalidation,
        "target_1": first_target,
    }


def _edge_bucket(score: float) -> str:
    if score >= 78:
        return "HIGH"
    if score >= 58:
        return "MEDIUM"
    if score >= 40:
        return "LOW"
    return "NO_EDGE"


def _gap_bucket(value: Any) -> str:
    gap = _float(value, 0.0)
    if gap < 50:
        return "clean_gap"
    if gap < 140:
        return "hot_gap"
    if gap < 300:
        return "extreme_gap"
    return "mega_gap"


def _float(value: Any, default: float = 0.0) -> float:
    if value in {None, ""}:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price(value: Any) -> str:
    number = _float(value, 0.0)
    return "n/a" if number <= 0 else f"${number:.4f}"
