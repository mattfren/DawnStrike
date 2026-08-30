from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import intraday_scanner.alpha.empirical_execution_cost_challenger as empirical_cost
import intraday_scanner.alpha.shadow_utility_reranker as shadow_utility
import intraday_scanner.services.strategy_challenger_evaluation_service as evaluation_service
from intraday_scanner.alpha.empirical_execution_cost_challenger import (
    build_empirical_cost_challenger,
)
from intraday_scanner.alpha.shadow_utility_reranker import (
    build_shadow_utility_receipt,
    persist_immutable_receipt,
    rerank_strategy_scores,
)
from intraday_scanner.alpha.shadow_utility_reranker import canonical_hash as utility_hash
from intraday_scanner.services.strategy_challenger_evaluation_service import (
    build_challenger_registry,
    build_prospective_shadow_evaluation_receipt,
    build_weekly_purged_evaluation_receipt,
    build_weekly_purged_splits,
    persist_evaluation_receipt,
)
from intraday_scanner.services.strategy_challenger_evaluation_service import (
    canonical_hash as evaluation_hash,
)


def _pit_row(index: int) -> dict[str, object]:
    entry = {
        "fill_price": 10.01,
        "quote_mid_price": 10.00,
        "fill_at": f"2026-08-{index + 1:02d}T14:00:01Z",
        "quote_at": f"2026-08-{index + 1:02d}T14:00:00Z",
        "commission": 0.0,
        "fill_hash_sha256": "f" * 64,
        "quote_hash_sha256": "b" * 64,
        "source_lineage_hash_sha256": "a" * 64,
    }
    exit_ = {
        "fill_price": 10.02,
        "quote_mid_price": 10.00,
        "fill_at": f"2026-08-{index + 1:02d}T14:01:01Z",
        "quote_at": f"2026-08-{index + 1:02d}T14:01:00Z",
        "commission": 0.0,
        "fill_hash_sha256": "f" * 64,
        "quote_hash_sha256": "b" * 64,
        "source_lineage_hash_sha256": "a" * 64,
    }
    return {
        "observation_id": f"obs-{index:03d}",
        "decision_at": f"2026-08-{index + 1:02d}T14:00:00Z",
        "market_date": f"2026-08-{index + 1:02d}",
        "direction": "long",
        "research_only": True,
        "broker_execution_enabled": False,
        "input_hash_sha256": "1" * 64,
        "source_lineage_hash_sha256": "a" * 64,
        "point_in_time": {"all_inputs_observed_at_or_before_decision": True},
        "entry": entry,
        "exit": exit_,
        "round_trip_notional": 20.0,
    }


def _calibration(
    score: float,
    strategy_id: str,
    strategy_version: str,
    *,
    input_hash: str = "1" * 64,
    source_hash: str = "a" * 64,
    decision_id: str = "decision",
    code_sha: str = "c" * 40,
    window_hash: str = "b" * 64,
) -> dict[str, object]:
    output = {
        "expected_return_lcb_pct": score,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "model_run_id": "run-1",
        "window_id": "window-1",
        "decision_id": decision_id,
        "configuration_hash_sha256": utility_hash({"calibration": "oos_lcb_v1"}),
        "model_hash_sha256": utility_hash({"model_run_id": "run-1"}),
        "source_hash_sha256": source_hash,
        "code_sha": code_sha,
        "window_hash_sha256": window_hash,
    }
    receipt: dict[str, object] = {
        "status": "AUTHENTICATED_OOS_CALIBRATION",
        "input_hash_sha256": input_hash,
        "output_hash_sha256": utility_hash(output),
        "source_hash_sha256": source_hash,
        "configuration_hash_sha256": utility_hash({"calibration": "oos_lcb_v1"}),
        "model_hash_sha256": utility_hash({"model_run_id": "run-1"}),
        "window_hash_sha256": window_hash,
        "decision_id": decision_id,
        "code_sha": code_sha,
        "model_run_id": "run-1",
        "window_id": "window-1",
        "output": output,
    }
    receipt["receipt_hash_sha256"] = utility_hash(receipt)
    return receipt


def _cost_evidence(monkeypatch) -> dict[str, object]:
    def trusted(row) -> bool:
        return True
    monkeypatch.setattr(empirical_cost, "has_authenticated_committed_fill_truth", trusted)
    monkeypatch.setattr(shadow_utility, "has_authenticated_committed_fill_truth", trusted)
    return build_empirical_cost_challenger(
        [_pit_row(index) for index in range(20)],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )


def test_reranker_is_shadow_only_and_does_not_mutate_champion_rows(monkeypatch) -> None:
    cost_receipt = _cost_evidence(monkeypatch)
    cost_bps = cost_receipt["p75_cost_bps"]
    source = [
        {
            "decision_id": "d-b",
            "research_only": True,
            "broker_execution_enabled": False,
            "strategy_id": "b",
            "strategy_version": "v1",
            "expected_return_lcb_pct": 0.8,
            "expected_cost_bps": cost_bps,
            "cost_quantile": "p75",
            "cost_model_version": cost_receipt["cost_model_version"],
            "input_hash_sha256": "1" * 64,
            "cost_input_observations_hash_sha256": cost_receipt["input_observations_hash_sha256"],
            "source_lineage_hash_sha256": "a" * 64,
            "source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
            "window_hash_sha256": cost_receipt["window_hash_sha256"],
            "code_sha": cost_receipt["code_sha"],
            "cost_source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
            "cost_window_hash_sha256": cost_receipt["window_hash_sha256"],
            "cost_code_sha": cost_receipt["code_sha"],
            "cost_receipt_hash_sha256": cost_receipt["receipt_hash_sha256"],
            "model_run_id": "run-1",
            "window_id": "window-1",
            "calibration_configuration_hash_sha256": utility_hash({"calibration": "oos_lcb_v1"}),
            "calibration_model_hash_sha256": utility_hash({"model_run_id": "run-1"}),
            "oos_calibration": _calibration(
                0.8,
                "b",
                "v1",
                input_hash="1" * 64,
                source_hash=cost_receipt["source_manifest_hash_sha256"],
                decision_id="d-b",
                code_sha=cost_receipt["code_sha"],
                window_hash=cost_receipt["window_hash_sha256"],
            ),
            "cost_receipt": cost_receipt,
        },
        {
            "decision_id": "d-a",
            "research_only": True,
            "broker_execution_enabled": False,
            "strategy_id": "a",
            "strategy_version": "v1",
            "expected_return_lcb_pct": 0.7,
            "expected_cost_bps": cost_bps,
            "cost_quantile": "p75",
            "cost_model_version": cost_receipt["cost_model_version"],
            "input_hash_sha256": "1" * 64,
            "cost_input_observations_hash_sha256": cost_receipt["input_observations_hash_sha256"],
            "source_lineage_hash_sha256": "a" * 64,
            "source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
            "window_hash_sha256": cost_receipt["window_hash_sha256"],
            "code_sha": cost_receipt["code_sha"],
            "cost_source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
            "cost_window_hash_sha256": cost_receipt["window_hash_sha256"],
            "cost_code_sha": cost_receipt["code_sha"],
            "cost_receipt_hash_sha256": cost_receipt["receipt_hash_sha256"],
            "model_run_id": "run-1",
            "window_id": "window-1",
            "calibration_configuration_hash_sha256": utility_hash({"calibration": "oos_lcb_v1"}),
            "calibration_model_hash_sha256": utility_hash({"model_run_id": "run-1"}),
            "oos_calibration": _calibration(
                0.7,
                "a",
                "v1",
                input_hash="1" * 64,
                source_hash=cost_receipt["source_manifest_hash_sha256"],
                decision_id="d-a",
                code_sha=cost_receipt["code_sha"],
                window_hash=cost_receipt["window_hash_sha256"],
            ),
            "cost_receipt": cost_receipt,
        },
    ]
    original = [dict(row) for row in source]
    ranked = rerank_strategy_scores(source)

    assert source == original
    assert ranked[0]["champion_slate_unchanged"] is True
    assert ranked[0]["research_only"] is True
    assert ranked[0]["after_cost_utility_pct"] is not None
    assert {row["after_cost_rank"] for row in ranked} == {1, 2}
    monkeypatch.setattr(shadow_utility, "has_authenticated_committed_fill_truth", lambda row: False)
    blocked = rerank_strategy_scores(source)
    assert all(row["after_cost_utility_pct"] is None for row in blocked)


def test_reranker_explicit_missing_cost_stays_blocked_and_null() -> None:
    row = rerank_strategy_scores(
        [
            {
                "decision_id": "d-x",
                "strategy_id": "x",
                "expected_return_lcb_pct": 0.8,
                "expected_cost_bps": None,
                "cost_model_version": "cost-v1",
                "input_hash_sha256": "1" * 64,
                "source_lineage_hash_sha256": "a" * 64,
                "model_run_id": "run-1",
                "window_id": "window-1",
                "oos_calibration": {
                    **_calibration(0.8, "x", "v1"),
                },
            }
        ]
    )[0]
    assert row["after_cost_utility_pct"] is None
    assert row["after_cost_utility_status"] == "BLOCKED_MISSING_EXECUTION_COST"


def test_reranker_quarantines_every_duplicate_decision_member() -> None:
    ranked = rerank_strategy_scores(
        [
            {"decision_id": "duplicate", "strategy_id": "a", "expected_return_lcb_pct": 1.0},
            {"decision_id": "duplicate", "strategy_id": "b", "expected_return_lcb_pct": 2.0},
        ]
    )
    assert len(ranked) == 2
    assert all(row["after_cost_utility_pct"] is None for row in ranked)
    assert all(row["after_cost_rank"] is None for row in ranked)


def test_reranker_rejects_forged_calibration_output_hash() -> None:
    row = {
        "decision_id": "d-forged",
        "strategy_id": "x",
        "strategy_version": "v1",
        "expected_return_lcb_pct": 0.8,
        "expected_cost_bps": 20,
        "cost_model_version": "cost-v1",
        "input_hash_sha256": "1" * 64,
        "source_lineage_hash_sha256": "a" * 64,
        "model_run_id": "run-1",
        "window_id": "window-1",
        "oos_calibration": {**_calibration(0.8, "x", "v1"), "output_hash_sha256": "2" * 64},
    }
    assert rerank_strategy_scores([row])[0]["after_cost_utility_pct"] is None


def test_empirical_cost_default_boundary_rejects_forged_json_authentication() -> None:
    receipt = build_empirical_cost_challenger(
        [_pit_row(index) for index in range(25)],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    assert receipt["status"] == "BLOCKED_INSUFFICIENT_AUTHENTICATED_PIT_EVIDENCE"
    assert receipt["p75_cost_bps"] is None
    assert receipt["p90_cost_bps"] is None


def test_empirical_cost_legitimate_boundary_is_versioned_and_conservative(monkeypatch) -> None:
    monkeypatch.setattr(
        "intraday_scanner.alpha.empirical_execution_cost_challenger.has_authenticated_committed_fill_truth",
        lambda row: bool(row["point_in_time"]),
    )
    receipt = build_empirical_cost_challenger(
        [_pit_row(index) for index in range(20)],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    assert receipt["status"] == "EMPIRICAL_COST_EVALUABLE"
    assert receipt["p75_cost_bps"] == receipt["p90_cost_bps"]
    assert receipt["provisional_champion_cost_model_unchanged"] is True
    assert receipt["promotion_eligible"] is False


def test_empirical_cost_requires_commission_and_orders_quote_before_fill() -> None:
    missing_commission = _pit_row(0)
    del missing_commission["entry"]["commission"]  # type: ignore[index]
    receipt = build_empirical_cost_challenger(
        [missing_commission],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    assert receipt["authenticated_observation_count"] == 0
    assert receipt["p75_cost_bps"] is None
    reversed_quote = _pit_row(1)
    reversed_quote["entry"]["quote_at"] = "2026-08-01T14:00:02Z"  # type: ignore[index]
    blocked = build_empirical_cost_challenger(
        [reversed_quote],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    assert blocked["authenticated_observation_count"] == 0


def test_empirical_cost_dedupes_identities_and_is_permutation_stable(monkeypatch) -> None:
    def trusted(row) -> bool:
        return True
    monkeypatch.setattr(empirical_cost, "has_authenticated_committed_fill_truth", trusted)
    observations = [_pit_row(index) for index in range(20)]
    first = build_empirical_cost_challenger(
        observations,
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    reversed_receipt = build_empirical_cost_challenger(
        list(reversed(observations)),
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    assert first["receipt_hash_sha256"] == reversed_receipt["receipt_hash_sha256"]
    duplicate_rows = [_pit_row(index) for index in range(20)]
    for row in duplicate_rows:
        row["observation_id"] = "same-observation"
    duplicate_receipt = build_empirical_cost_challenger(
        duplicate_rows,
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    assert duplicate_receipt["authenticated_observation_count"] == 0
    assert duplicate_receipt["authenticated_session_count"] == 0
    assert duplicate_receipt["p75_cost_bps"] is None


def test_empirical_sessions_are_canonical_dates_not_caller_session_ids(monkeypatch) -> None:
    monkeypatch.setattr(empirical_cost, "has_authenticated_committed_fill_truth", lambda row: True)
    observations = [_pit_row(index) for index in range(20)]
    for row in observations:
        row["market_date"] = "2026-08-01"
        row["decision_at"] = "2026-08-01T14:00:00Z"
        row["session_id"] = f"forged-session-{row['observation_id']}"
        row["entry"]["fill_at"] = "2026-08-01T14:00:01Z"  # type: ignore[index]
        row["entry"]["quote_at"] = "2026-08-01T14:00:00Z"  # type: ignore[index]
        row["exit"]["fill_at"] = "2026-08-01T14:01:01Z"  # type: ignore[index]
        row["exit"]["quote_at"] = "2026-08-01T14:01:00Z"  # type: ignore[index]
    receipt = build_empirical_cost_challenger(
        observations,
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"date": "2026-08-01"},
    )
    assert receipt["authenticated_observation_count"] == 20
    assert receipt["authenticated_session_count"] == 1
    assert receipt["p75_cost_bps"] is None


def test_reranker_recomputes_each_authenticated_cost_observation(monkeypatch) -> None:
    receipt = _cost_evidence(monkeypatch)
    row = {
        "expected_cost_bps": receipt["p75_cost_bps"],
        "cost_quantile": "p75",
        "cost_model_version": receipt["model_version"],
        "cost_input_observations_hash_sha256": receipt["input_observations_hash_sha256"],
        "cost_source_manifest_hash_sha256": receipt["source_manifest_hash_sha256"],
        "cost_window_hash_sha256": receipt["window_hash_sha256"],
        "cost_code_sha": receipt["code_sha"],
        "cost_receipt_hash_sha256": receipt["receipt_hash_sha256"],
        "cost_receipt": receipt,
    }
    assert shadow_utility._cost_is_bound(row) is True
    tampered = copy.deepcopy(receipt)
    tampered["evidence_rows"][0]["observed_cost_bps"] += 1.0  # type: ignore[index]
    tampered["receipt_hash_sha256"] = utility_hash(
        {key: value for key, value in tampered.items() if key != "receipt_hash_sha256"}
    )
    row["cost_receipt"] = tampered
    row["cost_receipt_hash_sha256"] = tampered["receipt_hash_sha256"]
    assert shadow_utility._cost_is_bound(row) is False


def test_nine_challengers_have_weekly_purged_receipts_and_missing_outcomes_null() -> None:
    registry = build_challenger_registry()
    assert len(registry) == 9
    assert all(item["one_variable"] for item in registry)
    rows = [
        {
            "strategy_id": registry[0]["challenger_id"],
            "market_date": f"2026-01-{day:02d}",
            "decision_at": f"2026-01-{day:02d}T14:00:00-06:00",
            "research_only": True,
            "broker_execution_enabled": False,
        }
        for day in range(1, 50)
    ]
    folds = build_weekly_purged_splits(rows, minimum_training_weeks=2, embargo_weeks=1)
    assert folds and all(fold["no_lookahead"] and fold["purged"] for fold in folds)
    receipt = build_weekly_purged_evaluation_receipt(
        rows,
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-01-01", "end": "2026-02-19"},
        minimum_training_weeks=2,
    )
    first = next(
        item
        for item in receipt["challengers"]
        if item["challenger_id"] == registry[0]["challenger_id"]
    )
    # Fold boundaries are driven only by authenticated closed-paper truth;
    # decision-only rows cannot manufacture training/test weeks.
    assert first["folds"] == []
    assert all(fold["after_cost_expectancy_pct"] is None for fold in first["folds"])
    assert receipt["missing_outcomes_are_zero"] is False


def test_prospective_receipt_rejects_outcomes_and_requires_exact_lineage() -> None:
    entry = build_challenger_registry()[0]
    source_hash = evaluation_hash({"snapshot": "fixture"})
    window_hash = evaluation_hash({"date": "2026-08-29"})
    decision = {
        "decision_id": "decision-1",
        "challenger_id": entry["challenger_id"],
        "strategy_version": entry["challenger_version"],
        "market_date": "2026-08-29",
        "decision_at": "2026-08-29T14:00:00Z",
        "configuration_hash_sha256": entry["configuration_hash_sha256"],
        "source_lineage_hash_sha256": source_hash,
        "source_manifest_hash_sha256": source_hash,
        "code_sha": "c" * 40,
        "window_hash_sha256": window_hash,
        "research_only": True,
        "broker_execution_enabled": False,
        "after_cost_return_pct": 1.0,
    }
    receipt = build_prospective_shadow_evaluation_receipt(
        [decision],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"start": "2026-08-01", "end": "2026-08-29"},
    )
    assert receipt["observation_count"] == 0
    assert receipt["rejected_observation_counts"]["outcome_fields_in_prospective_decision"] == 1


def test_prospective_receipt_requires_aware_time_and_rejects_nested_alias() -> None:
    entry = build_challenger_registry()[0]
    source_hash = evaluation_hash({"snapshot": "fixture"})
    window_hash = evaluation_hash({"date": "2026-08-29"})
    base = {
        "decision_id": "decision-aware",
        "challenger_id": entry["challenger_id"],
        "strategy_version": entry["challenger_version"],
        "market_date": "2026-08-29",
        "decision_at": "2026-08-29T14:00:00",  # naive timestamps are not evidence
        "configuration_hash_sha256": entry["configuration_hash_sha256"],
        "source_lineage_hash_sha256": source_hash,
        "source_manifest_hash_sha256": source_hash,
        "code_sha": "c" * 40,
        "window_hash_sha256": window_hash,
        "research_only": True,
        "broker_execution_enabled": False,
        "metadata": {"netReturnPct": 1.0},
    }
    receipt = build_prospective_shadow_evaluation_receipt(
        [base],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"date": "2026-08-29"},
    )
    assert receipt["observation_count"] == 0
    assert receipt["rejected_observation_counts"]["missing_decision_time"] == 1

    base["decision_at"] = "2026-08-29T14:00:00Z"
    receipt = build_prospective_shadow_evaluation_receipt(
        [base],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"date": "2026-08-29"},
    )
    assert receipt["observation_count"] == 0
    assert receipt["rejected_observation_counts"]["outcome_fields_in_prospective_decision"] == 1


def test_weekly_account_day_aggregation_is_cash_retaining_and_deterministic(monkeypatch) -> None:
    entry = build_challenger_registry()[0]
    source_manifest = {"snapshot": "fixture"}
    window = {"start": "2026-08-28", "end": "2026-08-29"}
    source_hash = evaluation_hash(source_manifest)
    window_hash = evaluation_hash(window)

    def row(
        record_id: str,
        market_date: str,
        account: str,
        role: str,
        value: float,
        weight: float,
        account_weight: float = 1.0,
    ):
        payload = {
            "record_id": record_id,
            # The paired decision identity is shared by champion and
            # challenger; record IDs remain role-specific receipts.
            "decision_id": record_id.removeprefix("extra-").split("-", 1)[1],
            "pair_id": record_id.removeprefix("extra-").split("-", 1)[1],
            "market_date": market_date,
            "account_id": account,
            "series_role": role,
            "ticker": "SPY",
            "direction": "long",
            "champion_strategy_id": entry["champion_strategy_id"],
            "challenger_strategy_id": entry["challenger_id"],
            "champion_strategy_version": entry["champion_strategy_version"],
            "challenger_strategy_version": entry["challenger_version"],
            "challenger_configuration_hash_sha256": entry["configuration_hash_sha256"],
            "source_manifest_hash_sha256": source_hash,
            "code_sha": "c" * 40,
            "window_hash_sha256": window_hash,
            "after_cost_return_pct": value,
            "allocation_weight": weight,
            "account_weight": account_weight,
            "fill_truth_hash_sha256": "f" * 64,
            "source_lineage_hash_sha256": "a" * 64,
        }
        return {
            "record_id": record_id,
            "decision_id": record_id.removeprefix("extra-").split("-", 1)[1],
            "pair_id": record_id.removeprefix("extra-").split("-", 1)[1],
            "account_id": account,
            "market_date": market_date,
            "decision_at": f"{market_date}T14:00:00-05:00",
            "series_role": role,
            "ticker": "SPY",
            "direction": "long",
            "record_type": "closed_paper_position",
            "after_cost_return_pct": value,
            "paper_truth": {
                "status": "closed",
                "fill_truth_hash_sha256": "f" * 64,
                "source_lineage_hash_sha256": "a" * 64,
                "return_payload": payload,
                "return_payload_hash_sha256": evaluation_hash(payload),
            },
            "champion_strategy_id": entry["champion_strategy_id"],
            "champion_strategy_version": entry["champion_strategy_version"],
            "challenger_id": entry["challenger_id"],
            "challenger_strategy_id": entry["challenger_id"],
            "challenger_strategy_version": entry["challenger_version"],
            "challenger_configuration_hash_sha256": entry["configuration_hash_sha256"],
            "source_manifest_hash_sha256": source_hash,
            "code_sha": "c" * 40,
            "window_hash_sha256": window_hash,
            "allocation_weight": weight,
            "account_weight": account_weight,
        }

    monkeypatch.setattr(
        evaluation_service, "has_authenticated_committed_fill_truth", lambda row: True
    )
    rows = []
    for day in ("2026-08-28", "2026-08-29"):
        for role, value in (("champion", 1.0), ("challenger", 2.0)):
            rows.extend(
                [
                    row(f"{role}-{day}-b", day, "acct", role, value, 0.4, 0.5),
                    row(f"{role}-{day}-a", day, "acct", role, value, 0.6, 0.5),
                ]
            )
    metrics = evaluation_service._fold_metrics(
        list(reversed(rows)),
        challenger_id=entry["challenger_id"],
        challenger=entry,
        source_hash=source_hash,
        code_sha="c" * 40,
        window_hash=window_hash,
        window=window,
    )
    assert metrics["sample_size"] == 2
    assert metrics["session_count"] == 2
    assert metrics["minimum_sample_size_met"] is False
    assert metrics["minimum_session_count_met"] is False
    assert metrics["champion_after_cost_expectancy_pct"] is None
    assert metrics["challenger_after_cost_expectancy_pct"] is None

    for candidate in rows:
        if candidate["pair_id"].endswith("-b"):
            candidate["allocation_weight"] = 0.2
            candidate["paper_truth"]["return_payload"]["allocation_weight"] = 0.2  # type: ignore[index]
            candidate["paper_truth"]["return_payload_hash_sha256"] = evaluation_hash(  # type: ignore[index]
                candidate["paper_truth"]["return_payload"]  # type: ignore[index]
            )
    blocked = evaluation_service._fold_metrics(
        rows,
        challenger_id=entry["challenger_id"],
        challenger=entry,
        source_hash=source_hash,
        code_sha="c" * 40,
        window_hash=window_hash,
        window=window,
    )
    # Partial invested allocation is valid and retains the remaining cash.
    assert blocked["sample_size"] == 2

    for candidate in rows:
        if candidate["pair_id"].endswith("-b"):
            candidate["allocation_weight"] = 0.4
            candidate["paper_truth"]["return_payload"]["allocation_weight"] = 0.4  # type: ignore[index]
            candidate["paper_truth"]["return_payload_hash_sha256"] = evaluation_hash(  # type: ignore[index]
                candidate["paper_truth"]["return_payload"]  # type: ignore[index]
            )
    overallocated = list(rows)
    for day in ("2026-08-28", "2026-08-29"):
        for role, value in (("champion", 1.0), ("challenger", 2.0)):
            overallocated.extend(
                [
                    row(f"extra-{role}-{day}-b", day, "acct2", role, value, 0.4, 0.6),
                    row(f"extra-{role}-{day}-a", day, "acct2", role, value, 0.6, 0.6),
                ]
            )
    overallocated_metrics = evaluation_service._fold_metrics(
        overallocated,
        challenger_id=entry["challenger_id"],
        challenger=entry,
        source_hash=source_hash,
        code_sha="c" * 40,
        window_hash=window_hash,
        window=window,
    )
    assert overallocated_metrics["sample_size"] == 0
    assert overallocated_metrics["session_count"] == 0


def test_receipts_are_immutable(monkeypatch, tmp_path: Path) -> None:
    cost_receipt = _cost_evidence(monkeypatch)
    utility = build_shadow_utility_receipt(
        [{"decision_id": "d-x", "strategy_id": "x", "strategy_version": "v1",
              "research_only": True, "broker_execution_enabled": False,
              "expected_return_lcb_pct": 0.7,
              "expected_cost_bps": cost_receipt["p75_cost_bps"], "cost_quantile": "p75",
              "cost_model_version": cost_receipt["cost_model_version"],
              "input_hash_sha256": "1" * 64,
              "cost_input_observations_hash_sha256": cost_receipt["input_observations_hash_sha256"],
              "source_lineage_hash_sha256": "a" * 64,
              "source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
              "window_hash_sha256": cost_receipt["window_hash_sha256"],
              "code_sha": cost_receipt["code_sha"],
              "cost_source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
              "cost_window_hash_sha256": cost_receipt["window_hash_sha256"],
              "cost_code_sha": cost_receipt["code_sha"],
              "cost_receipt_hash_sha256": cost_receipt["receipt_hash_sha256"],
              "model_run_id": "run-1", "window_id": "window-1",
              "oos_calibration": _calibration(
                  0.7,
                  "x",
                  "v1",
                  input_hash="1" * 64,
              ),
              "cost_receipt": cost_receipt}],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"date": "2026-08-29"},
    )
    target = tmp_path / "utility.json"
    assert persist_immutable_receipt(target, utility) is False
    assert persist_immutable_receipt(target, utility) is True
    changed = {**utility, "code_sha": "d" * 40}
    with pytest.raises(ValueError, match="self-hash"):
        persist_immutable_receipt(target, changed)
    changed["receipt_hash_sha256"] = utility_hash(
        {key: value for key, value in changed.items() if key != "receipt_hash_sha256"}
    )
    with pytest.raises(ValueError, match="immutable"):
        persist_immutable_receipt(target, changed)

    evaluation_target = tmp_path / "evaluation.json"
    assert persist_evaluation_receipt(evaluation_target, utility) is False
    with pytest.raises(ValueError, match="immutable"):
        persist_evaluation_receipt(evaluation_target, changed)


def test_evaluation_persistence_is_race_safe_and_self_hashed(monkeypatch, tmp_path: Path) -> None:
    cost_receipt = _cost_evidence(monkeypatch)
    receipt = build_shadow_utility_receipt(
        [{"decision_id": "d-race", "strategy_id": "race", "strategy_version": "v1",
          "research_only": True, "broker_execution_enabled": False,
          "expected_return_lcb_pct": 0.7,
          "expected_cost_bps": cost_receipt["p75_cost_bps"], "cost_quantile": "p75",
          "cost_model_version": cost_receipt["cost_model_version"],
          "input_hash_sha256": "1" * 64,
          "cost_input_observations_hash_sha256": cost_receipt["input_observations_hash_sha256"],
          "source_lineage_hash_sha256": "a" * 64,
          "source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
          "window_hash_sha256": cost_receipt["window_hash_sha256"],
          "code_sha": cost_receipt["code_sha"],
          "cost_source_manifest_hash_sha256": cost_receipt["source_manifest_hash_sha256"],
          "cost_window_hash_sha256": cost_receipt["window_hash_sha256"],
          "cost_code_sha": cost_receipt["code_sha"],
          "cost_receipt_hash_sha256": cost_receipt["receipt_hash_sha256"],
          "model_run_id": "run-1", "window_id": "window-1",
          "oos_calibration": _calibration(
              0.7,
              "race",
              "v1",
              input_hash="1" * 64,
          ),
          "cost_receipt": cost_receipt}],
        source_manifest={"snapshot": "fixture"},
        code_sha="c" * 40,
        window={"date": "2026-08-29"},
    )
    target = tmp_path / "race.json"
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(lambda _: persist_evaluation_receipt(target, receipt), range(6))
        )
    assert results.count(False) == 1
    assert results.count(True) == 5
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    assert receipt["receipt_hash_sha256"] == utility_hash(unsigned)
