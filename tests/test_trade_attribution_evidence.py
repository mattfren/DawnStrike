from __future__ import annotations

from intraday_scanner.services.alpha_attribution_service import (
    SINGLE_TRADE_ATTRIBUTION_STATUS,
    build_trade_attribution_cases,
)
from intraday_scanner.storage.attribution_evidence_store import AttributionEvidenceStore


def test_trade_attribution_is_case_level_and_fail_closed_for_single_trade() -> None:
    result = build_trade_attribution_cases([_trade()], generated_at="2026-08-09T20:00:00+00:00")

    assert result["case_count"] == 1
    case = result["cases"][0]
    assert case["attribution_status"] == SINGLE_TRADE_ATTRIBUTION_STATUS
    assert case["unsupported_unique_causal_claim"] is False
    assert case["automatic_policy_mutation"] is False
    assert case["coverage_status"] == "partial"
    assert result["aggregate"]["status"] == "NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL"
    assert result["missing_truth_is_zero"] is False

    factors = {factor["factor_key"]: factor for factor in case["factors"]}
    assert factors["setup_risk_catalyst"]["factor_status"] == "suspected"
    assert factors["execution_quality"]["factor_status"] == "supported_contributor"
    assert factors["execution_quality"]["coverage_status"] == "complete"
    assert "realized_atr" not in factors["setup_risk_catalyst"]["evidence"]
    assert all(
        factor["factor_status"]
        in {
            "observed_defect",
            "supported_contributor",
            "suspected",
            "unknown",
            "not_applicable",
        }
        for factor in case["factors"]
    )


def test_trade_attribution_store_is_append_only_and_idempotent(tmp_path) -> None:
    result = build_trade_attribution_cases([_trade()], generated_at="2026-08-09T20:00:00+00:00")
    store = AttributionEvidenceStore(tmp_path / "attribution.sqlite")

    assert store.persist_cases(result["cases"]) == {
        "case_inserted": 1,
        "case_skipped": 0,
        "factor_inserted": 3,
        "factor_skipped": 0,
    }
    assert store.persist_cases(result["cases"]) == {
        "case_inserted": 0,
        "case_skipped": 1,
        "factor_inserted": 0,
        "factor_skipped": 3,
    }
    cases = store.load_cases()
    assert len(cases) == 1
    assert len(store.load_factors(cases[0]["case_id"])) == 3


def _trade() -> dict[str, object]:
    return {
        "trade_id": "trade-1",
        "market_date": "2026-08-08",
        "ticker": "TEST",
        "strategy_id": "alphaops_v5",
        "reconciliation_status": "RECONCILED",
        "exit_reason": "invalidation",
        "net_return_pct": -2.0,
        "realized_atr": 999.0,
        "setup_attribution_status": "suspected",
        "execution_attribution_status": "supported_contributor",
        "frozen_decision_inputs": {
            "setup_key": "breakout",
            "regime_key": "SELECTIVE",
            "catalyst_class": "earnings",
            "risk_at_decision": 1.0,
            "atr_at_decision": 0.4,
        },
        "intent_timestamp": "2026-08-08T14:30:00+00:00",
        "order_timestamp": "2026-08-08T14:30:01+00:00",
        "quote_feed_identity": "alpaca-iex-quotes",
        "trade_feed_identity": "alpaca-iex-trades",
        "intent_size": 1000.0,
        "order_size": 1000.0,
        "halt_luld_state": "not_halted",
        "latency_ms": 250.0,
        "modeled_cost_bps": 100.0,
        "observed_cost_bps": 145.0,
        "source_lineage_hash_sha256": "a" * 64,
    }
