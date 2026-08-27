from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest

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
from tests._alpha_path_truth import (
    canonical_path_receipt,
    canonical_return_outcome,
    canonical_v6_decision,
    causal_identity_from,
    replay_binding_from,
)


def _signal(*, can_alert: bool = True) -> dict[str, object]:
    return {
        "scan_id": "scan-v6",
        "signal_id": "signal-v6",
        "ticker": "NOVA",
        "timestamp": "2026-08-03T12:10:00+00:00",
        "rank": 1,
        "alpha_score": 82.5,
        "can_alert": can_alert,
        "alert_gate_status": "PASS" if can_alert else "BLOCKED",
        "source_confidence": 90.0,
        "entry_watch_level": 10.0,
        "target_1": 12.0,
        "invalidation_level": 9.0,
        "source": "fixture",
        "source_url": "fixture://v6/NOVA",
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


def _source_summary() -> dict[str, object]:
    return {
        "status": "success",
        "primary_source": "fixture-primary",
        "source_artifact_identity": "fixture:v6-source-summary",
        "source_artifact_hash_sha256": "a" * 64,
    }


def test_v6_records_point_in_time_shadow_decision_without_promoting() -> None:
    decisions = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
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
    signal = _signal(can_alert=False)
    signal["current_halt"] = True
    decisions = build_v6_shadow_decisions(
        signals=[signal],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )

    assert decisions[0]["action"] == "SHADOW_REJECT_VETO"
    assert "current_halt" in decisions[0]["safety_vetoes"]


def test_v6_outcome_is_sourced_after_cost_and_missing_is_not_zero() -> None:
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
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
                **_canonical_source_contract(decision),
                "captured_at": "2026-08-04T01:00:00+00:00",
            }
        ],
        capture_attempts=[],
    )

    assert outcomes[0]["learning_eligible"] is False
    assert outcomes[0]["fill_truth_status"] == "missing_committed_fill_truth"
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


def test_v6_projection_preserves_canonical_path_contract_and_rejects_missing_path() -> None:
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    source = {
        "signal_id": decision["shadow_signal_id"],
        "outcome_status": "complete_sourced",
        "entry_opportunity": True,
        "gross_return_pct": 3.0,
        **_canonical_source_contract(decision),
    }
    canonical = build_v6_outcomes(
        decisions=[decision], sourced_outcomes=[source], capture_attempts=[]
    )[0]
    without_path = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[
            {
                key: value
                for key, value in source.items()
                if key
                not in {
                    "path_replay_id",
                    "path_replay_schema_version",
                    "path_replay_policy_version",
                    "path_replay_policy_hash_sha256",
                    "path_replay_receipt",
                    "path_truth_status",
                    "exit_event",
                }
            }
        ],
        capture_attempts=[],
    )[0]

    for key, value in _canonical_source_contract(decision).items():
        if key == "learning_eligible":
            assert canonical[key] is False
        else:
            assert canonical[key] == value
    for key in canonical_path_receipt().keys():
        assert canonical[key] == source[key]
    assert canonical["path_replay_receipt"] == source["path_replay_receipt"]
    for key in (
        "after_cost_return_pct",
        "net_excess_return_pct",
        "cost_receipt_id",
        "cost_receipt_hash_sha256",
        "observed_cost_model_identity",
        "modeled_cost_model_identity",
        "cost_components",
        "benchmark_return_pct",
        "benchmark_source_bar_hash_sha256",
        "benchmark_independent_reconciliation_status",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_source_bar_hash_sha256",
        "secondary_benchmark_independent_reconciliation_status",
        "independent_reconciliation_status",
        "reconciliation_receipt_id",
        "reconciliation_receipt_hash_sha256",
        "causal_decision_identity",
        "retrospective_research_eligible",
        "prospective_promotion_eligible",
        "eligibility_policy_version",
    ):
        assert canonical[key] == source[key]
    assert canonical["eligibility_policy_version"] == (
        "dawnstrike.alphaops-v6-eligibility.v2"
    )
    assert canonical["learning_eligible"] is False
    assert without_path["learning_eligible"] is False


@pytest.mark.parametrize(
    ("path", "wrong"),
    (
        (("path_replay_receipt", "exit_price"), 999.0),
        (("path_replay_id",), "path-v2-" + "f" * 64),
        (("source_coverage_complete",), False),
        (("sequence_complete_through_exit",), False),
        (("return_truth_schema_version",), "attacker-return-v9"),
        (("return_truth_hash_sha256",), "f" * 64),
        (("cost_schema_version",), "attacker-cost-v9"),
        (("cost_receipt_hash_sha256",), "f" * 64),
        (("cost_receipt", "after_cost_return_pct"), 99.0),
        (("benchmark_symbol",), "QQQ"),
        (("benchmark_independent_reconciliation_status",), "FAILED"),
        (("secondary_benchmark_symbol",), "QQQ"),
        (("secondary_benchmark_independent_reconciliation_status",), "PENDING"),
        (("reconciliation_schema_version",), "attacker-recon-v9"),
        (("reconciliation_receipt_hash_sha256",), "f" * 64),
        (("reconciliation_receipt", "status"), "FAILED"),
        (("causal_decision_identity", "decision_id"), "attacker"),
        (("eligibility_policy_version",), "attacker-eligibility-v9"),
        (("retrospective_research_eligible",), False),
        (("prospective_promotion_eligible",), "true"),
    ),
)
def test_v6_projection_rejects_each_wrong_nonblank_current_dimension(
    path: tuple[str, ...],
    wrong: object,
) -> None:
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    source = {
        "signal_id": decision["shadow_signal_id"],
        "entry_opportunity": True,
        **_canonical_source_contract(decision),
    }
    mutated = copy.deepcopy(source)
    cursor = mutated
    for key in path[:-1]:
        child = cursor[key]
        assert isinstance(child, dict)
        cursor = child
    cursor[path[-1]] = wrong

    outcome = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[mutated],
        capture_attempts=[],
    )[0]

    assert outcome["learning_eligible"] is False


def test_v6_projection_never_recomputes_from_poisoned_legacy_aliases() -> None:
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    source = {
        "signal_id": decision["shadow_signal_id"],
        "entry_opportunity": True,
        **_canonical_source_contract(decision),
        "net_return_pct": 999.0,
        "estimated_round_trip_cost_bps": -1_000_000.0,
        "planned_first_touch_outcome": "invalidation",
        "high_after_entry": 9_999.0,
        "low_after_entry": 0.01,
    }

    outcome = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[source],
        capture_attempts=[],
    )[0]

    assert outcome["after_cost_return_pct"] == source["after_cost_return_pct"]
    assert outcome["net_excess_return_pct"] == source["net_excess_return_pct"]
    assert outcome["path_event"] == source["path_event"]
    assert outcome["mfe_price"] == source["mfe_price"]
    assert outcome["mae_price"] == source["mae_price"]


def test_v6_promotion_rejects_120_legacy_boolean_rows_without_current_truth() -> None:
    start = date(2026, 1, 2)
    legacy = [
        {
            "decision_id": f"legacy-{index}",
            "market_date": (start + timedelta(days=index % 60)).isoformat(),
            "activation_status": "ACTIVATED",
            "outcome_status": "COMPLETE_SOURCED",
            "source_bar_hash_sha256": "b" * 64,
            "benchmark_source_bar_hash_sha256": "p" * 64,
            "secondary_benchmark_source_bar_hash_sha256": "s" * 64,
            "benchmark_return_pct": 0.0,
            "secondary_benchmark_return_pct": 0.0,
            "net_excess_return_pct": 2.0,
            "estimated_round_trip_cost_bps": 10.0,
            "learning_eligible": True,
            "no_lookahead": True,
        }
        for index in range(120)
    ]

    readiness = promotion_readiness(legacy, manual_operator_approval=True)

    assert readiness["closed_paper_trade_count"] == 0
    assert readiness["forward_session_count"] == 0
    assert readiness["status"] == "NOT_ELIGIBLE_FOR_PROMOTION"
    assert readiness["criteria"]["minimum_closed_paper_trades"] is False


def test_v6_promotion_counts_one_authentic_current_contract() -> None:
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    outcome = {
        **canonical_return_outcome(
            causal_identity=causal_identity_from(
                decision,
                kind="alpha_v6_shadow_decision",
            ),
            replay_binding=replay_binding_from(
                decision,
                kind="alpha_v6_shadow_decision",
            ),
        ),
        "decision_id": decision["decision_id"],
        "market_date": decision["market_date"],
    }

    readiness = promotion_readiness([outcome], decisions=[decision])

    assert readiness["closed_paper_trade_count"] == 0
    assert readiness["forward_session_count"] == 0
    assert readiness["benchmark_coverage"]["primary_complete"] is False
    assert readiness["benchmark_coverage"]["secondary_complete"] is False


def test_v6_projection_rejects_120_pathless_forged_boolean_sources() -> None:
    base = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    start = date(2026, 1, 2)
    decisions = []
    sources = []
    for index in range(120):
        market_date = (start + timedelta(days=index % 60)).isoformat()
        decision_id = f"forged-decision-{index}"
        shadow_signal_id = f"forged-signal-{index}"
        decisions.append(
            {
                **base,
                "decision_id": decision_id,
                "shadow_signal_id": shadow_signal_id,
                "market_date": market_date,
            }
        )
        sources.append(
            {
                "signal_id": shadow_signal_id,
                "outcome_status": "complete_sourced",
                "entry_opportunity": True,
                "gross_return_pct": 3.0,
                "benchmark_return_pct": 0.5,
                "secondary_benchmark_return_pct": 0.25,
                "source_bar_hash_sha256": "a" * 64,
                "benchmark_source_bar_hash_sha256": "b" * 64,
                "secondary_benchmark_source_bar_hash_sha256": "c" * 64,
                "independent_reconciliation_status": "PASSED",
                "benchmark_independent_reconciliation_status": "PASSED",
                "secondary_benchmark_independent_reconciliation_status": "PASSED",
                "validated_against_signal_timestamp": True,
                "no_lookahead": True,
                "learning_eligible": True,
            }
        )

    outcomes = build_v6_outcomes(
        decisions=decisions,
        sourced_outcomes=sources,
        capture_attempts=[],
    )

    assert len(outcomes) == 120
    assert all(row["learning_eligible"] is False for row in outcomes)
    assert promotion_readiness(
        outcomes,
        decisions=decisions,
        manual_operator_approval=True,
    )["status"] == "NOT_ELIGIBLE_FOR_PROMOTION"


def test_v6_walk_forward_never_trains_on_its_test_date() -> None:
    start = date(2026, 1, 2)
    decisions = []
    outcomes = []
    for offset in range(35):
        market_date = (start + timedelta(days=offset)).isoformat()
        decision_id = f"d-{offset}"
        decision = {
            **canonical_v6_decision(decision_id, market_date=market_date),
            "setup_key": "breakout",
            "regime_key": "SELECTIVE",
        }
        decisions.append(decision)
        outcomes.append(
            {
                **canonical_return_outcome(
                    market_date=market_date,
                    causal_identity=causal_identity_from(
                        decision,
                        kind="alpha_v6_shadow_decision",
                    ),
                    replay_binding=replay_binding_from(
                        decision,
                        kind="alpha_v6_shadow_decision",
                    ),
                ),
                "decision_id": decision_id,
                "market_date": market_date,
            }
        )

    report = strict_walk_forward_evaluation(decisions=decisions, outcomes=outcomes)

    assert report["leakage_check"] is True
    assert report["evaluated_prediction_count"] == 0
    assert report["total_label_count"] == 0


def test_v6_walk_forward_rejects_120_pathless_boolean_rows() -> None:
    start = date(2026, 1, 2)
    decisions = []
    outcomes = []
    for index in range(120):
        market_date = (start + timedelta(days=index % 60)).isoformat()
        decision_id = f"legacy-{index}"
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
                "net_excess_return_pct": 2.0,
                "learning_eligible": True,
            }
        )

    report = strict_walk_forward_evaluation(decisions=decisions, outcomes=outcomes)

    assert report["total_label_count"] == 0
    assert report["evaluated_prediction_count"] == 0


def test_v6_projection_preserves_retro_only_truth_without_promotion() -> None:
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    source = {
        "signal_id": decision["shadow_signal_id"],
        "entry_opportunity": True,
        **canonical_return_outcome(
            prospective=False,
            causal_identity=causal_identity_from(
                decision,
                kind="alpha_v6_shadow_decision",
            ),
            replay_binding=replay_binding_from(
                decision,
                kind="alpha_v6_shadow_decision",
            ),
        ),
    }

    outcome = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[source],
        capture_attempts=[],
    )[0]

    assert outcome["retrospective_research_eligible"] is True
    assert outcome["prospective_promotion_eligible"] is False
    assert promotion_readiness(
        [outcome],
        decisions=[decision],
        manual_operator_approval=True,
    )["status"] == "NOT_ELIGIBLE_FOR_PROMOTION"


def _canonical_source_contract(decision: dict[str, object]) -> dict[str, object]:
    return canonical_return_outcome(
        causal_identity=causal_identity_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=replay_binding_from(
            decision,
            kind="alpha_v6_shadow_decision",
        ),
    )


def test_v6_storage_is_additive_and_promotion_stays_manual(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    decision = build_v6_shadow_decisions(
        signals=[_signal()],
        feature_vectors=[_feature()],
        source_summary=_source_summary(),
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
        source_summary=_source_summary(),
        regime={"regime": "SELECTIVE"},
        prior_outcomes=[],
        universe_membership_by_ticker=_universe_membership(),
    )[0]
    outcome = build_v6_outcomes(
        decisions=[decision],
        sourced_outcomes=[
            {
                **canonical_return_outcome(
                    causal_identity=causal_identity_from(
                        decision,
                        kind="alpha_v6_shadow_decision",
                    ),
                    replay_binding=replay_binding_from(
                        decision,
                        kind="alpha_v6_shadow_decision",
                    ),
                    case="ordered_stop",
                    net_excess_return_pct=-4.0,
                ),
                "signal_id": decision["shadow_signal_id"],
                "entry_opportunity": True,
            }
        ],
        capture_attempts=[],
    )[0]
    store.persist_alpha_v6_decisions([decision])
    store.persist_alpha_v6_outcomes([outcome])

    report = build_v6_failure_attribution(store)

    assert report["status"] == "COMPLETE"
    assert report["breakdown"][0]["mean_net_excess_return_pct"] is None
    assert report["breakdown"][0]["eligible_return_count"] == 0
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
