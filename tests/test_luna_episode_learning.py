from __future__ import annotations

from intraday_scanner.alpha.episode_identity import (
    EpisodeIdentityError,
    build_episode_identity,
    deduplicate_episode_candidates,
)
from intraday_scanner.performance.strategy_miss_attribution import (
    AttributionState,
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
    assert report.rows[0].state is AttributionState.OPEN_MTM
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


def _candidate(strategy: str, direction: str = "long") -> dict[str, object]:
    return {
        "market_date": "2026-08-21",
        "session_id": "morning",
        "ticker": " abcd ",
        "direction": direction,
        "entry_window": "09:30-09:35",
        "frozen_plan_hash": "p" * 64,
        "strategy_id": strategy,
        "selection_id": strategy + "-selection",
    }


def test_episode_retry_and_multi_strategy_votes_collapse_with_counts() -> None:
    result = deduplicate_episode_candidates([_candidate("s2"), _candidate("s1"), _candidate("s1")])
    assert len(result["selected"]) == 1
    assert result["selected"][0]["matched_strategy_ids"] == ["s1", "s2"]
    assert result["counts"] == {
        "raw_pair_count": 3,
        "unique_symbol_count": 1,
        "unique_episode_count": 1,
        "duplicate_collapse_count": 2,
        "conflicting_direction_episode_count": 0,
        "blocked_count": 0,
    }
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
    assert result["counts"]["unique_episode_count"] == 1


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
