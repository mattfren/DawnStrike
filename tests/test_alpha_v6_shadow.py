from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from intraday_scanner.alpha.v6_shadow import (
    MIN_FORWARD_CLOSED_TRADES,
    build_v6_outcomes,
    build_v6_shadow_decisions,
    promotion_readiness,
    strict_walk_forward_evaluation,
)
from intraday_scanner.services.v6_learning_service import (
    build_v6_failure_attribution,
    v6_public_status,
)
from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, get_schema_version
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _signal(*, can_alert: bool = True) -> dict[str, object]:
    return {
        "scan_id": "scan-v6",
        "signal_id": "signal-v6",
        "ticker": "NOVA",
        "timestamp": "2026-08-03T12:10:00+00:00",
        "rank": 1,
        "can_alert": can_alert,
        "alert_gate_status": "PASS" if can_alert else "BLOCKED",
        "entry_watch_level": 10.0,
        "target_1": 12.0,
        "invalidation_level": 9.0,
        "source": "fixture",
    }


def _feature() -> dict[str, object]:
    return {
        "ticker": "NOVA",
        "timestamp": "2026-08-03T12:09:00+00:00",
        "config_hash": "f" * 64,
        "feature_json": {
            "playbook_setup": {"setup_key": "breakout"},
            "liquidity_execution": {"spread_pct": 0.1},
        },
    }


def _universe_membership() -> dict[str, dict[str, object]]:
    return {
        "NOVA": {
            "universe_id": "v6u-fixture",
            "status": "ACTIVE",
            "source_lineage_hash_sha256": "u" * 64,
        }
    }


def test_v6_records_point_in_time_shadow_decision_without_promoting() -> None:
    decisions = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary={"status": "success", "snapshot_sha256": "a" * 64},
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )

    decision = decisions[0]
    assert decision["action"] == "SHADOW_TRACK"
    assert decision["prediction"]["status"] == "UNCALIBRATED_INSUFFICIENT_OUTCOMES"
    assert decision["point_in_time"]["all_inputs_observed_at_or_before_decision"] is True
    assert decision["research_only"] is True
    assert decision["broker_execution_enabled"] is False


def test_v6_public_status_exposes_only_aggregate_failure_attribution(tmp_path) -> None:
    status = v6_public_status(SQLiteScanStore(tmp_path / "v6.sqlite"))

    attribution = status["failure_attribution"]

    assert attribution["status"] == "WAITING_FOR_OUTCOMES"
    assert attribution["categories"]["by_setup_regime"] == []
    assert attribution["missing_truth_is_zero"] is False
    assert status["account_comparison"] is None


def test_v6_safety_veto_cannot_be_learned_away() -> None:
    decisions = build_v6_shadow_decisions(
        signals=[_signal(can_alert=False)],
        feature_vectors=[_feature()],
        source_summary={"status": "success"},
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )

    assert decisions[0]["action"] == "SHADOW_REJECT_VETO"
    assert "legacy_alert_gate_not_passed" in decisions[0]["safety_vetoes"]


def test_v6_outcome_is_sourced_after_cost_and_missing_is_not_zero() -> None:
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary={"status": "success"},
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    outcomes = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[
            {
                "signal_id": decision["shadow_signal_id"],
                "outcome_status": "complete_sourced",
                "entry_opportunity": True,
                "gross_return_pct": 3.0,
                "benchmark_return_pct": 1.0,
                "benchmark_source_bar_hash_sha256": "p" * 64,
                "secondary_benchmark_return_pct": 0.5,
                "secondary_benchmark_source_bar_hash_sha256": "i" * 64,
                "source_bar_hash_sha256": "b" * 64,
                "independent_reconciliation_status": "PASSED",
                "benchmark_independent_reconciliation_status": "PASSED",
                "secondary_benchmark_independent_reconciliation_status": "PASSED",
                "validated_against_signal_timestamp": True,
                "no_lookahead": True,
                "captured_at": "2026-08-04T01:00:00+00:00",
            }
        ],
        capture_attempts=[],
    )

    assert outcomes[0]["learning_eligible"] is True
    assert outcomes[0]["net_return_pct"] < 3.0
    assert outcomes[0]["net_excess_return_pct"] is not None
    missing = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[],
        capture_attempts=[
            {
                "signal_id": decision["shadow_signal_id"],
                "status": "terminal_missing",
                "attempted_at": "2026-08-04T01:00:00+00:00",
            }
        ],
    )[0]
    assert missing["net_return_pct"] is None
    assert missing["learning_eligible"] is False


def test_v6_walk_forward_never_trains_on_its_test_date() -> None:
    start = date(2026, 1, 2)
    decisions = []
    outcomes = []
    for offset in range(35):
        market_date = (start + timedelta(days=offset)).isoformat()
        decision_id = f"d-{offset}"
        decisions.append(
            {
                "decision_id": decision_id,
                "market_date": market_date,
                "setup_key": "breakout",
                "regime_key": "SELECTIVE",
            }
        )
        outcomes.append(
            {
                "decision_id": decision_id,
                "market_date": market_date,
                "activation_status": "ACTIVATED",
                "net_excess_return_pct": 1.0,
                "learning_eligible": True,
            }
        )

    report = strict_walk_forward_evaluation(decisions=decisions, outcomes=outcomes)

    assert report["leakage_check"] is True
    assert report["evaluated_prediction_count"] >= 1


def test_v6_storage_is_additive_and_promotion_stays_manual(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary={"status": "success"},
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    assert store.persist_alpha_v6_decisions([decision]) == {"inserted": 1, "skipped": 0}
    assert store.persist_alpha_v6_decisions([decision]) == {"inserted": 0, "skipped": 1}
    assert len(store.load_alpha_v6_decisions()) == 1
    readiness = promotion_readiness([])
    assert readiness["automatic_promotion"] is False
    assert readiness["closed_paper_trade_count"] < MIN_FORWARD_CLOSED_TRADES
    with __import__("sqlite3").connect(tmp_path / "v6.sqlite") as connection:
        assert get_schema_version(connection) == CURRENT_SCHEMA_VERSION


def test_v6_failure_attribution_proposes_no_automatic_policy_change(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6-attribution.sqlite")
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary={"status": "success"},
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    outcome = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[
            {
                "signal_id": decision["shadow_signal_id"],
                "outcome_status": "complete_sourced",
                "entry_opportunity": True,
                "gross_return_pct": -4.0,
                "benchmark_return_pct": 0.0,
                "benchmark_source_bar_hash_sha256": "p" * 64,
                "secondary_benchmark_return_pct": 0.0,
                "secondary_benchmark_source_bar_hash_sha256": "i" * 64,
                "source_bar_hash_sha256": "b" * 64,
                "independent_reconciliation_status": "PASSED",
                "benchmark_independent_reconciliation_status": "PASSED",
                "secondary_benchmark_independent_reconciliation_status": "PASSED",
                "validated_against_signal_timestamp": True,
                "no_lookahead": True,
            }
        ],
        capture_attempts=[],
    )[0]
    store.persist_alpha_v6_decisions([decision])
    store.persist_alpha_v6_outcomes([outcome])

    report = build_v6_failure_attribution(store)

    assert report["status"] == "COMPLETE"
    assert report["breakdown"][0]["mean_net_excess_return_pct"] < 0
    assert report["causal_attribution"]["by_source_quality"]
    assert (
        report["causal_attribution"]["failure_modes"]["data_quality"]["missing_truth_is_zero"]
        is False
    )
    assert (
        report["causal_attribution"]["failure_modes"]["execution_cost"]["observed_slippage_status"]
        == "MISSING_NOT_IMPUTED"
    )
    assert report["automatic_policy_change"] is False
