from __future__ import annotations

from datetime import datetime, timezone

from intraday_scanner.v2.strategies.alphaops_intraday import (
    IntradayDecisionPoint,
    build_alphaops_intraday_strategy,
    build_point_in_time_observation,
    evaluate_alphaops_intraday,
)

UTC = timezone.utc
DECISION_AT = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _signal() -> dict[str, object]:
    return {
        "ticker": "NOVA",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
        "decision": "clean_edge",
        "alert_gate_status": "PASS",
        "manual_confirmation_required": False,
        "classification": "TRADE SETUP",
        "source_confidence": 92.0,
        "source_count": 3,
        "source_quality_status": "verified",
        "stale_data_flag": False,
        "previous_close": 8.0,
        "premarket_price": 10.0,
        "premarket_high": 10.1,
        "premarket_low": 9.6,
        "dollar_volume": 5_000_000,
        "gap_pct": 25.0,
        "spread_pct": 0.5,
        "liquidity_tier": "high_liquidity",
        "float_shares": 8_000_000,
        "float_status": "verified",
        "float_source": "fixture",
        "catalyst_summary": "FDA clearance announced before market open",
        "catalyst_url": "https://example.test/catalyst",
        "catalyst_status": "verified",
        "catalyst_tier": "A",
        "halt_status": "clear",
        "sec_risk_status": "clear",
        "corporate_action_status": "clear",
        "entry_watch_level": 10.0,
        "invalidation_level": 9.0,
        "target_1": 12.75,
        "target_basis_kind": "sourced_resistance",
        "target_derived_from_risk": False,
    }


def test_v5_intraday_adapter_preserves_policy_and_lineage() -> None:
    point = IntradayDecisionPoint(
        symbol="NOVA",
        decision_at=DECISION_AT,
        signal=_signal(),
        observation={
            "price": 10.05,
            "observed_at": DECISION_AT.isoformat(),
            "requested_at": DECISION_AT.isoformat(),
            "freshness_seconds": 0,
            "is_usable": True,
        },
        artifact_identity="fixture:bars:NOVA:2026-08-03",
        artifact_hash_sha256="bars-hash",
        exchange_session_id="XNYS:2026-08-03:regular",
    )

    evaluation = evaluate_alphaops_intraday(point)

    assert evaluation.decision.eligible_for_official_paper is True
    assert evaluation.decision.policy_version.startswith("alphaops-v5")
    assert evaluation.decision.broker_execution_enabled is False
    assert evaluation.to_dict()["point_in_time"] is True
    assert evaluation.to_dict()["artifact_hash_sha256"] == "bars-hash"


def test_point_in_time_observation_excludes_future_bars() -> None:
    bars = [
        {"timestamp": "2026-08-03T13:59:00+00:00", "close": 10.0},
        {"timestamp": "2026-08-03T14:01:00+00:00", "close": 99.0},
    ]

    observation = build_point_in_time_observation(bars, as_of=DECISION_AT)

    assert observation["price"] == 10.0
    assert observation["future_bars_excluded"] == 1


def test_strategy_factory_is_research_only_and_delegates_v5() -> None:
    strategy = build_alphaops_intraday_strategy(
        {
            "NOVA": {
                **_signal(),
                "artifact_identity": "fixture:bars:NOVA:2026-08-03",
                "artifact_hash_sha256": "bars-hash",
                "exchange_session_id": "XNYS:2026-08-03:regular",
            }
        }
    )

    assert strategy.strategy_id == "alphaops_v5"
    assert strategy.status == "research_only_causal_intraday"
    assert strategy.parameters["broker_execution_enabled"] is False
