from __future__ import annotations

import hashlib
import json
from collections import Counter

import pytest

from intraday_scanner.alpha.episode_identity import (
    EpisodeIdentityError,
    build_episode_identity,
    deduplicate_episode_candidates,
)
from intraday_scanner.performance.strategy_miss_attribution import (
    AttributionState,
    Eligibility,
    attribute_strategy_misses,
)


def test_mixed_aggregate_expands_closed_and_open_once() -> None:
    row = {
        "record_id": "daily:2026-08-21",
        "market_date": "2026-08-21",
        "cohort": "shadow_challenger",
        "strategy_id": "s1",
        "strategy_version": "v1",
        "record_status": "realized",
        "trade_count": 1,
        "open_position_count": 1,
        "payload_json": {
            "trade_lifecycles": [
                {"trade_id": "closed-1", "status": "closed", "return_pct": -2.0},
                {"position_id": "open-1", "status": "open", "return_pct": 4.0},
            ],
            "source_hash_sha256": "a" * 64,
        },
    }
    report = attribute_strategy_misses([row], date_cutoff="2026-08-21")
    assert len(report.rows) == 2
    assert sum(item.state is AttributionState.CLOSED for item in report.rows) == 1
    assert sum(item.state is AttributionState.OPEN_MTM for item in report.rows) == 1
    assert {item.lifecycle_id for item in report.rows} == {"closed-1", "open-1"}
    assert report.summaries[0].closed_loss_count == 1


def test_replayed_aggregate_does_not_repeat_lifecycle() -> None:
    base = {
        "market_date": "2026-08-21",
        "cohort": "shadow_challenger",
        "strategy_id": "s1",
        "payload_json": {
            "trade_lifecycles": [
                {"trade_id": "same-trade", "status": "closed", "return_pct": 1.0}
            ]
        },
    }
    report = attribute_strategy_misses(
        [{**base, "record_id": "retry-1"}, {**base, "record_id": "retry-2"}]
    )
    assert len(report.rows) == 1
    assert report.rows[0].lifecycle_id == "same-trade"


def test_mixed_aggregate_without_children_stays_missing() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "aggregate",
                "market_date": "2026-08-21",
                "cohort": "shadow_challenger",
                "strategy_id": "s1",
                "record_status": "realized",
                "return_pct": -3.0,
                "trade_count": 1,
                "open_position_count": 1,
            }
        ]
    )
    assert report.rows[0].state is AttributionState.MISSING_OUTCOME
    assert report.rows[0].return_pct is None


def test_late_close_after_cutoff_remains_open_unknown() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "daily",
                "market_date": "2026-08-21",
                "cohort": "shadow_challenger",
                "strategy_id": "s1",
                "payload_json": {
                    "trade_lifecycles": [
                        {
                            "trade_id": "late",
                            "status": "closed",
                            "closed_at": "2026-08-22T10:00:00Z",
                            "return_pct": 7.0,
                        }
                    ]
                },
            }
        ],
        date_cutoff="2026-08-21",
    )
    assert report.rows[0].state is AttributionState.MISSING_OUTCOME
    assert report.rows[0].return_pct is None


def test_fill_without_committed_fill_truth_is_unresolved() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "daily",
                "market_date": "2026-08-21",
                "cohort": "shadow_challenger",
                "strategy_id": "s1",
                "payload_json": {
                    "trade_lifecycles": [
                        {"fill_id": "fill-1", "status": "closed", "return_pct": 9.0}
                    ]
                },
            }
        ]
    )
    assert report.rows[0].state is AttributionState.MISSING_OUTCOME
    assert report.rows[0].return_pct is None


def test_source_bar_hash_is_not_fill_truth() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "daily",
                "market_date": "2026-08-21",
                "cohort": "shadow_challenger",
                "strategy_id": "s1",
                "payload_json": {
                    "trade_lifecycles": [
                        {
                            "fill_id": "fill-1",
                            "status": "closed",
                            "source_bar_hash_sha256": "a" * 64,
                            "return_pct": 9.0,
                        }
                    ]
                },
            }
        ]
    )
    assert report.rows[0].state is AttributionState.MISSING_OUTCOME
    assert report.rows[0].return_pct is None


def test_forged_fill_truth_receipt_stays_provisional_and_ineligible() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "forged-fill-truth",
                "market_date": "2026-08-21",
                "cohort": "official_forward_paper",
                "strategy_id": "s1",
                "strategy_version": "v1",
                "record_status": "realized",
                "return_pct": 2.0,
                "fill_id": "fill-1",
                "close_time": "2026-08-21T15:00:00Z",
                "fill_truth_status": "committed",
                "fill_truth_hash_sha256": "a" * 64,
                "fill_truth_contract_verified": True,
                "fill_truth_receipt": {
                    "schema_version": "dawnstrike.filltruth.commit.v1",
                    "status": "committed",
                    "committed": True,
                    "fill_id": "fill-1",
                    "fill_truth_hash_sha256": "a" * 64,
                },
            }
        ],
        date_cutoff="2026-08-21T16:00:00+00:00",
    )
    row = report.rows[0]
    assert row.state is AttributionState.CLOSED
    assert row.classification == "closed_provisional"
    assert row.eligibility is Eligibility.INELIGIBLE


def test_exact_timestamp_cutoff_quarantines_later_same_day_close() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "same-day-lifecycles",
                "market_date": "2026-08-21",
                "cohort": "shadow_challenger",
                "strategy_id": "s1",
                "strategy_version": "v1",
                "payload_json": {
                    "trade_lifecycles": [
                        {
                            "trade_id": "before-cutoff",
                            "status": "closed",
                            "close_time": "2026-08-21T14:00:00+00:00",
                            "return_pct": -1.0,
                        },
                        {
                            "trade_id": "after-cutoff",
                            "status": "closed",
                            "close_time": "2026-08-21T15:00:00+00:00",
                            "return_pct": 3.0,
                        },
                    ]
                },
            }
        ],
        date_cutoff="2026-08-21T14:30:00+00:00",
    )
    rows = {row.lifecycle_id: row for row in report.rows}
    assert rows["before-cutoff"].state is AttributionState.CLOSED
    assert rows["after-cutoff"].state is AttributionState.MISSING_OUTCOME
    assert rows["after-cutoff"].return_pct is None


def test_readonly_blotter_lifecycle_supersedes_mixed_aggregate_and_stays_provisional() -> None:
    aggregate = {
        "record_id": "aggregate",
        "market_date": "2026-08-21",
        "cohort": "official_forward_paper",
        "strategy_id": "s1",
        "strategy_version": "v1",
        "record_type": "portfolio_observation",
        "record_status": "realized",
        "trade_count": 1,
        "open_position_count": 1,
        "return_pct": 3.0,
    }
    blotter = [
        {
            "mode": "forward",
            "signal_date": "2026-08-21",
            "strategy_id": "s1",
            "strategy_version": "v1",
            "series_role": "champion",
            "symbol": "ABCD",
            "lifecycle_status": "closed",
            "close_id": "close-1",
            "close_time": "2026-08-21T15:00:00Z",
            "trade_return_pct": -1.25,
            "ledger_source_hash_sha256": "b" * 64,
        },
        {
            "mode": "forward",
            "signal_date": "2026-08-21",
            "strategy_id": "s1",
            "strategy_version": "v1",
            "series_role": "champion",
            "symbol": "EFGH",
            "lifecycle_status": "open",
            "position_id": "position-1",
            "ledger_source_hash_sha256": "b" * 64,
        },
    ]
    report = attribute_strategy_misses([aggregate], paper_ops_rows=blotter)
    assert {row.lifecycle_id for row in report.rows} == {"close-1", "position-1"}
    closed = next(row for row in report.rows if row.lifecycle_id == "close-1")
    assert closed.state is AttributionState.CLOSED
    assert closed.eligibility.value == "ineligible"
    assert closed.fill_truth_status == "missing_committed_fill_truth"
    assert closed.return_pct == -1.25
    assert (
        next(row for row in report.rows if row.lifecycle_id == "position-1").state
        is AttributionState.OPEN_MTM
    )


def test_forward_blotter_cutoff_is_exact_once_and_excludes_late_closes() -> None:
    def blotter_row(index: int, role: str, close_date: str) -> dict[str, object]:
        strategy = "champion_strategy" if role == "champion" else "challenger_strategy"
        return {
            "mode": "forward",
            "signal_date": "2026-08-18",
            "strategy_id": strategy,
            "strategy_version": "v1.0",
            "series_role": role,
            "symbol": f"SYM{index:02d}",
            "lifecycle_status": "closed",
            "close_id": f"close-{index}",
            "close_time": f"{close_date}T15:00:00Z",
            "trade_return_pct": -1.0,
            "ledger_source_hash_sha256": "c" * 64,
        }

    exact = [
        *[blotter_row(index, "champion", "2026-08-20") for index in range(36)],
        *[blotter_row(100 + index, "challenger", "2026-08-21") for index in range(2)],
        *[blotter_row(200 + index, "champion", "2026-08-25") for index in range(3)],
    ]
    # A materializer retry can repeat a row; lifecycle identity still wins.
    exact.append(dict(exact[0]))
    aggregates = [
        {
            "record_id": f"aggregate-{index}",
            "market_date": "2026-08-20",
            "cohort": "official_forward_paper",
            "strategy_id": "champion_strategy",
            "strategy_version": "v1.0",
            "record_type": "portfolio_observation",
            "record_status": "realized",
            "trade_count": 1,
            "open_position_count": 0,
            "return_pct": 9.0,
        }
        for index in range(7)
    ]
    report = attribute_strategy_misses(
        aggregates,
        date_cutoff="2026-08-21",
        paper_ops_rows=exact,
    )
    closed = [
        row
        for row in report.rows
        if row.record_type == "paper_ops_blotter_lifecycle"
        and row.state is AttributionState.CLOSED
    ]
    assert len(closed) == 38
    assert Counter(row.series_role for row in closed) == {"champion": 36, "challenger": 2}
    assert all(row.market_date <= "2026-08-21" for row in closed)
    assert all(row.fill_truth_status == "missing_committed_fill_truth" for row in closed)
    assert all(row.eligibility.value == "ineligible" for row in closed)
    assert all(row.record_type == "paper_ops_blotter_lifecycle" for row in closed)
    assert all(
        not (
            row.record_type != "paper_ops_blotter_lifecycle"
            and row.state is AttributionState.CLOSED
            and row.eligibility.value == "eligible"
        )
        for row in report.rows
    )
    assert report.point_in_time_limitations


def test_blotter_integrity_warning_quarantines_lifecycle() -> None:
    report = attribute_strategy_misses(
        [],
        paper_ops_rows=[
            {
                "mode": "forward",
                "signal_date": "2026-08-21",
                "strategy_id": "s1",
                "strategy_version": "v1",
                "series_role": "champion",
                "symbol": "ABCD",
                "lifecycle_status": "closed",
                "close_id": "close-corrupt",
                "close_time": "2026-08-21T15:00:00Z",
                "trade_return_pct": 2.0,
                "blotter_warnings": ["duplicate event id event-1"],
            }
        ],
        date_cutoff="2026-08-21",
    )
    row = report.rows[0]
    assert row.state is AttributionState.MISSING_OUTCOME
    assert row.eligibility.value == "ineligible"
    assert "data_unavailable" in row.categories


def _candidate(strategy: str, direction: str = "long") -> dict[str, object]:
    receipt = {"status": "committed"}
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    levels = (
        {"entry": 100.0, "stop": 101.0, "target": 98.0}
        if direction == "short"
        else {"entry": 100.0, "stop": 99.0, "target": 102.0}
    )
    plan = {
        "schema_version": "dawnstrike.episode_plan.v1",
        "strategy": strategy,
        "direction": direction,
        "levels": levels,
        "provenance": {"source_hash_sha256": "d" * 64},
        "freeze_receipt": receipt,
    }
    plan_hash = hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return {
        "market_date": "2026-08-21",
        "session_id": "morning",
        "ticker": " abcd ",
        "direction": direction,
        "entry_window": "09:30-09:35",
        "frozen_plan": plan,
        "frozen_plan_hash": plan_hash,
        "plan_freeze_status": "frozen",
        "strategy_id": strategy,
        "selection_id": strategy + "-selection",
    }


def test_episode_retry_and_multi_strategy_votes_collapse_with_counts() -> None:
    result = deduplicate_episode_candidates([_candidate("s2"), _candidate("s1"), _candidate("s1")])
    assert len(result["selected"]) == 1
    assert result["selected"][0]["matched_strategy_ids"] == ["s1", "s2"]
    assert result["counts"]["raw_pair_count"] == 3
    assert result["counts"]["unique_symbol_count"] == 1
    assert result["counts"]["unique_episode_count"] == 2
    assert result["counts"]["unique_reservation_count"] == 1
    assert result["counts"]["duplicate_collapse_count"] == 1
    assert result["counts"]["overlapping_reservation_collapse_count"] == 1
    assert result["counts"]["conflicting_direction_episode_count"] == 0
    assert result["counts"]["blocked_count"] == 0
    assert (
        result["selected"][0]["episode_id"]
        == build_episode_identity(_candidate("s1")).episode_id
    )


def test_conflicting_directions_are_distinct_but_blocked() -> None:
    result = deduplicate_episode_candidates([_candidate("s1", "long"), _candidate("s2", "short")])
    assert result["selected"] == []
    assert all(
        item["blocked_reason"] == "conflicting_direction_candidates"
        for item in result["blocked"]
    )
    assert result["counts"]["conflicting_direction_episode_count"] == 1
    assert result["counts"]["unique_episode_count"] == 2


def test_conflicting_directions_block_when_plan_hashes_differ() -> None:
    long = _candidate("s1", "long")
    short = _candidate("s2", "short")
    short["frozen_plan"] = {
        "schema_version": "dawnstrike.episode_plan.v1",
        "strategy": "s2-short",
        "direction": "short",
        "levels": {"entry": 100.0, "stop": 101.0, "target": 98.0},
        "provenance": {"source_hash_sha256": "d" * 64},
        "freeze_receipt": {"status": "committed"},
    }
    short_receipt = short["frozen_plan"]["freeze_receipt"]
    short_receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            short_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    short["frozen_plan_hash"] = hashlib.sha256(
        json.dumps(
            short["frozen_plan"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    result = deduplicate_episode_candidates([long, short])
    assert result["selected"] == []
    assert result["counts"]["unique_episode_count"] == 2


def test_conflicting_directions_require_overlapping_entry_windows() -> None:
    long = _candidate("s1", "long")
    short = _candidate("s2", "short")
    short["entry_window"] = "10:00-10:05"
    result = deduplicate_episode_candidates([long, short])
    assert len(result["selected"]) == 2
    assert result["counts"]["conflicting_direction_episode_count"] == 0


def test_plan_hash_and_freeze_provenance_are_strict() -> None:
    candidate = _candidate("s1")
    candidate["frozen_plan_hash"] = "a" * 16
    try:
        build_episode_identity(candidate)
    except EpisodeIdentityError:
        pass
    else:
        raise AssertionError("short plan hash must fail closed")
    candidate["frozen_plan_hash"] = hashlib.sha256(
        json.dumps(
            candidate["frozen_plan"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    candidate.pop("plan_freeze_status")
    try:
        build_episode_identity(candidate)
    except EpisodeIdentityError:
        pass
    else:
        raise AssertionError("unfrozen plan provenance must fail closed")

    hash_only = _candidate("s1")
    hash_only.pop("frozen_plan")
    try:
        build_episode_identity(hash_only)
    except EpisodeIdentityError:
        pass
    else:
        raise AssertionError("hash-only episode identity must fail closed")

    retry_identity = _candidate("s1")
    retry_identity.pop("session_id")
    retry_identity["run_id"] = "retry-variant"
    retry_identity["scan_id"] = "scan-variant"
    try:
        build_episode_identity(retry_identity)
    except EpisodeIdentityError:
        pass
    else:
        raise AssertionError("retry IDs must not replace stable market session")


def test_same_symbol_overlapping_plan_hashes_select_one_reservation() -> None:
    first = _candidate("s1")
    second = _candidate("s2")
    second["frozen_plan"] = {
        "schema_version": "dawnstrike.episode_plan.v1",
        "strategy": "different-plan",
        "direction": "long",
        "levels": {"entry": 100.0, "stop": 99.0, "target": 102.0},
        "provenance": {"source_hash_sha256": "d" * 64},
        "freeze_receipt": {"status": "committed"},
    }
    second_receipt = second["frozen_plan"]["freeze_receipt"]
    second_receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            second_receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    second["frozen_plan_hash"] = hashlib.sha256(
        json.dumps(
            second["frozen_plan"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
    result = deduplicate_episode_candidates([first, second])
    assert len(result["selected"]) == 1
    selected = result["selected"][0]
    assert selected["matched_strategy_ids"] == ["s1", "s2"]
    assert selected["alternative_strategy_ids"] == ["s2"]
    assert result["counts"]["unique_episode_count"] == 2
    assert result["counts"]["unique_reservation_count"] == 1
    assert result["counts"]["overlapping_reservation_collapse_count"] == 1


def test_malformed_window_fails_closed_before_conflict_check() -> None:
    candidate = _candidate("s1")
    candidate["entry_window"] = "not-a-window"
    result = deduplicate_episode_candidates([candidate])
    assert result["selected"] == []
    assert result["counts"]["blocked_count"] == 1


def test_valid_alphaops_serialized_plan_uses_contract_hash_and_freeze_marker() -> None:
    plan_constructor = pytest.importorskip(
        "intraday_scanner.alpha.plan_constructor",
        reason="AlphaOps strict plan contract is supplied by the Alpha lane",
    )
    signal = {
        "ticker": "NOVA",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
        "entry_watch_level": 10.0,
        "invalidation_level": 9.0,
        "target_1": 12.75,
        "target_basis_kind": "sourced_resistance",
        "market_structure_observations": {
            "entry": {
                "value": 10.0,
                "observed_at": "2026-08-26T13:00:00+00:00",
                "completed_at": "2026-08-26T13:00:00+00:00",
                "source": "completed-market-feed",
                "source_url": "https://example.test/market",
                "source_hash": "a" * 64,
                "observation_kind": "sourced_entry",
                "raw_value": 10.0,
                "derivation_policy": "identity",
                "is_complete": True,
            },
            "stop": {
                "value": 9.0,
                "observed_at": "2026-08-26T13:00:00+00:00",
                "completed_at": "2026-08-26T13:00:00+00:00",
                "source": "completed-market-feed",
                "source_url": "https://example.test/market",
                "source_hash": "b" * 64,
                "observation_kind": "sourced_stop",
                "raw_value": 9.0,
                "derivation_policy": "identity",
                "is_complete": True,
            },
            "target": {
                "value": 12.75,
                "observed_at": "2026-08-26T13:00:00+00:00",
                "completed_at": "2026-08-26T13:00:00+00:00",
                "source": "completed-market-feed",
                "source_url": "https://example.test/market",
                "source_hash": "c" * 64,
                "observation_kind": "prior_day_resistance",
                "raw_value": 12.75,
                "target_basis_kind": "sourced_resistance",
                "derivation_policy": "identity",
                "is_complete": True,
            },
        },
    }
    plan = plan_constructor.construct_alphaops_v5_plan(
        signal, decision_at="2026-08-26T13:30:00+00:00"
    )
    if plan.status != plan_constructor.COMPLETE:
        pytest.skip("fixture did not produce a complete AlphaOps plan")
    candidate = {
        "market_date": "2026-08-26",
        "session_id": "morning",
        "ticker": "NOVA",
        "direction": plan.direction,
        "entry_window": "09:30-09:35",
        "alphaops_market_structure_plan": plan.to_dict(),
        "plan_hash_sha256": plan.plan_hash_sha256,
        # This is the actual _signal_payload vocabulary; no generic
        # plan_freeze_status is required for a validator-approved Alpha plan.
        "plan_levels_frozen": True,
        "plan_construction_status": plan_constructor.COMPLETE,
    }
    identity = build_episode_identity(candidate)
    assert identity.frozen_plan_hash == plan.plan_hash_sha256

    tampered = dict(candidate)
    tampered_plan = dict(plan.to_dict())
    tampered_plan["target"] = 99.0
    tampered["alphaops_market_structure_plan"] = tampered_plan
    with pytest.raises(EpisodeIdentityError):
        build_episode_identity(tampered)

    mismatched = dict(candidate)
    mismatched["plan_hash_sha256"] = "a" * 64
    with pytest.raises(EpisodeIdentityError):
        build_episode_identity(mismatched)


def test_missing_episode_identity_is_blocked_without_guessing() -> None:
    result = deduplicate_episode_candidates([{"ticker": "ABCD", "direction": "long"}])
    assert result["selected"] == []
    assert result["counts"]["blocked_count"] == 1
    try:
        build_episode_identity({"ticker": "ABCD", "direction": "long"})
    except EpisodeIdentityError:
        pass
    else:
        raise AssertionError("missing episode identity must fail closed")
