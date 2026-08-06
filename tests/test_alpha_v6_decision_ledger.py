from __future__ import annotations

from intraday_scanner.alpha.v6.decision_ledger import (
    build_candidate_decisions,
    validate_decision_batch,
)


def test_v6_ledgers_tracked_and_policy_rejected_candidates() -> None:
    decisions = build_candidate_decisions(
        signals=[
            {
                "scan_id": "scan-1",
                "signal_id": "signal-a",
                "ticker": "ALFA",
                "timestamp": "2026-08-03T12:00:00+00:00",
                "can_alert": True,
                "alert_gate_status": "PASS",
                "source_confidence": 90.0,
            }
        ],
        candidates=[{"ticker": "ALFA"}, {"ticker": "BETA", "source": "fixture"}],
        feature_vectors=[
            _feature("ALFA"),
            _feature("BETA"),
        ],
        source_summary={"status": "success"},
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        decision_at="2026-08-03T12:00:00+00:00",
        scan_id="scan-1",
        universe_membership_by_ticker={
            ticker: {
                "universe_id": "v6u-fixture",
                "status": "ACTIVE",
                "source_lineage_hash_sha256": "u" * 64,
            }
            for ticker in ("ALFA", "BETA")
        },
    )

    tracked, rejected = decisions
    assert tracked["action"] == "SHADOW_TRACK"
    assert rejected["action"] == "SHADOW_REJECTED_POLICY"
    assert rejected["rejected_sampling"]["inclusion_probability"] == 0.2
    assert validate_decision_batch(decisions)["valid"] is True


def _feature(ticker: str) -> dict[str, object]:
    return {
        "ticker": ticker,
        "timestamp": "2026-08-03T11:59:00+00:00",
        "config_hash": "c" * 64,
        "feature_json": {"liquidity_execution": {"spread_pct": 0.1}},
    }


def test_v6_records_explicit_no_trade_when_nothing_is_admitted() -> None:
    decisions = build_candidate_decisions(
        signals=[],
        candidates=[],
        feature_vectors=[],
        source_summary={"status": "complete", "source": "fixture"},
        regime={"regime": "UNKNOWN"},
        prior_outcomes=[],
        decision_at="2026-08-04T12:00:00+00:00",
        scan_id="scan-no-trade",
        universe_membership_by_ticker={},
    )

    assert len(decisions) == 1
    assert decisions[0]["action"] == "SHADOW_NO_TRADE"
    assert decisions[0]["decision_state"] == "NO_TRADE"
    assert validate_decision_batch(decisions)["valid"] is True
