"""Hostile, private learning-queue contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from intraday_scanner.services.managed_learning_queue_service import (
    LearningQueueValidationError,
    build_managed_learning_queue,
    write_managed_learning_queue,
)


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _artifact(day: str, *, version: str = "v1", sample: int | None = 30, sessions: int | None = 10):
    receipt = {
        "schema_version": "dawnstrike.strategy_learning_daily.v1",
        "market_date": day,
        "cutoff": f"{day}T15:00:00+00:00",
        "run_id": f"run-{day}-{version}",
        "input_hash_sha256": "a" * 64,
        "source_hash_sha256": "b" * 64,
        "code_sha": "c" * 64,
        "config_hash_sha256": "d" * 64,
        "window_hash_sha256": "e" * 64,
        "catalog": [],
        "research_only": True,
        "broker_execution_enabled": False,
        "automatic_policy_change": False,
        "automatic_promotion": False,
    }
    proposal = {
        "schema_version": "dawnstrike.strategy_remediation_proposals.v1",
        "market_date": day,
        "cutoff": receipt["cutoff"],
        "run_id": receipt["run_id"],
        "input_hash_sha256": receipt["input_hash_sha256"],
        "proposals": [
            {
                "strategy_id": "alpha",
                "strategy_version": version,
                "status": "PROPOSED_NOT_APPLIED",
                "applied": False,
                "research_only": True,
                "broker_execution_enabled": False,
                "automatic_policy_change": False,
                "automatic_promotion": False,
                "root_cause_category": "LIQUIDITY",
                "controlled_change": {"scope": "research", "component": "lookback"},
                "sample_count": sample,
                "session_count": sessions,
                "claimed_return_pct": 999,
            }
        ],
        "research_only": True,
        "broker_execution_enabled": False,
        "automatic_policy_change": False,
        "automatic_promotion": False,
    }
    receipt["receipt_sha256"] = _hash(receipt)
    proposal["artifact_sha256"] = _hash(proposal)
    return {"receipt": receipt, "proposals": proposal}


def _rehash(artifact: dict) -> dict:
    receipt = artifact["receipt"]
    proposal = artifact["proposals"]
    receipt["receipt_sha256"] = _hash(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    proposal["artifact_sha256"] = _hash(
        {key: value for key, value in proposal.items() if key != "artifact_sha256"}
    )
    return artifact


def _calendar(*dates: str) -> dict:
    result = {"calendar_identity": "xnys-xnas-test-v1", "market_dates": list(dates)}
    result["calendar_hash_sha256"] = _hash(result)
    return result


def test_recurrence_coalesces_and_keeps_occurrence_receipts() -> None:
    queue = build_managed_learning_queue(
        [_artifact("2026-08-25"), _artifact("2026-08-26"), _artifact("2026-08-26")],
        as_of_market_date="2026-08-30",
    )
    item = queue["items"][0]
    assert item["occurrence_count"] == 2
    assert item["first_seen_market_date"] == "2026-08-25"
    assert len(item["receipt_hashes_sha256"]) == 2
    assert item["sample_count"] is None
    assert item["session_count"] is None
    assert "disjointness contract" in item["evidence_maturity_reason"]


def test_cross_day_lineage_changes_do_not_change_typed_group_identity() -> None:
    first = _artifact("2026-08-25")
    second = _artifact("2026-08-26")
    second["receipt"]["source_hash_sha256"] = "f" * 64
    _rehash(second)
    first_queue = build_managed_learning_queue([first], as_of_market_date="2026-08-30")
    second_queue = build_managed_learning_queue([second], as_of_market_date="2026-08-30")
    combined = build_managed_learning_queue(
        [first, second], as_of_market_date="2026-08-30"
    )
    assert (
        first_queue["items"][0]["queue_item_id"]
        == second_queue["items"][0]["queue_item_id"]
    )
    assert combined["items"][0]["occurrence_count"] == 2
    assert combined["items"][0]["occurrences"][1]["lineage"]["source_hash_sha256"] == "f" * 64


def test_incompatible_versions_are_separate() -> None:
    queue = build_managed_learning_queue(
        [_artifact("2026-08-25", version="v1"), _artifact("2026-08-26", version="v2")],
        as_of_market_date="2026-08-30",
    )
    assert len(queue["items"]) == 2


def test_low_n_is_collect_evidence_and_missing_is_null() -> None:
    queue = build_managed_learning_queue(
        [_artifact("2026-08-25", sample=2, sessions=None)], as_of_market_date="2026-08-30"
    )
    item = queue["items"][0]
    assert item["evidence_maturity"] == "COLLECT_EVIDENCE"
    assert item["sample_count"] == 2
    assert item["session_count"] is None
    assert item["evi_score_bps"] == 0


def test_disjoint_evidence_counts_sum_once_and_repeated_cohort_is_deduped() -> None:
    first = _artifact("2026-08-25")
    second = _artifact("2026-08-26")
    third = _artifact("2026-08-27")
    for artifact, evidence in ((first, "a" * 64), (second, "b" * 64), (third, "a" * 64)):
        row = artifact["proposals"]["proposals"][0]
        row["evidence_hash_sha256"] = evidence
        row["evidence_disjointness_proven"] = True
        _rehash(artifact)
    item = build_managed_learning_queue(
        [first, second, third], as_of_market_date="2026-08-30"
    )["items"][0]
    assert item["sample_count"] == 60
    assert item["session_count"] == 20


def test_missing_calendar_never_invents_weekday_validation_date() -> None:
    item = build_managed_learning_queue(
        [_artifact("2026-08-28")], as_of_market_date="2026-08-30"
    )["items"][0]
    assert item["next_eligible_validation_date"] is None
    assert item["status"] == "NOT_EVALUABLE_CALENDAR_REQUIRED"


def test_calendar_hash_order_and_holiday_are_rejected() -> None:
    with pytest.raises(LearningQueueValidationError, match="hash"):
        build_managed_learning_queue(
            [_artifact("2026-08-28")],
            as_of_market_date="2026-08-30",
            calendar={
                "market_dates": ["2026-08-28", "2026-09-01"],
                "calendar_hash_sha256": "0" * 64,
            },
        )
    with pytest.raises(LearningQueueValidationError, match="strictly increasing"):
        build_managed_learning_queue(
            [_artifact("2026-08-28")],
            calendar=_calendar("2026-09-02", "2026-09-01"),
        )
    with pytest.raises(LearningQueueValidationError, match="holiday"):
        build_managed_learning_queue(
            [_artifact("2026-08-28")],
            calendar=_calendar("2026-09-01", "2026-09-07"),
        )


def test_permutation_stability_and_no_return_claim() -> None:
    first = build_managed_learning_queue(
        [_artifact("2026-08-25"), _artifact("2026-08-26", version="v2")],
        as_of_market_date="2026-08-30",
    )
    second = build_managed_learning_queue(
        [_artifact("2026-08-26", version="v2"), _artifact("2026-08-25")],
        as_of_market_date="2026-08-30",
    )
    assert first == second
    assert "claimed_return_pct" not in json.dumps(first)


def test_forged_hash_and_public_flag_are_rejected() -> None:
    artifact = _artifact("2026-08-25")
    artifact["receipt"]["public"] = True
    artifact["receipt"]["receipt_sha256"] = _hash(
        {key: value for key, value in artifact["receipt"].items() if key != "receipt_sha256"}
    )
    with pytest.raises(LearningQueueValidationError):
        build_managed_learning_queue([artifact], as_of_market_date="2026-08-30")


def test_immutable_retry_and_conflict(tmp_path: Path) -> None:
    queue = build_managed_learning_queue([_artifact("2026-08-25")], as_of_market_date="2026-08-30")
    path = tmp_path / "private-queue.json"
    assert write_managed_learning_queue(path, queue) is False
    assert write_managed_learning_queue(path, queue) is True
    conflicting = dict(queue)
    conflicting["items"] = []
    conflicting["queue_sha256"] = _hash(
        {key: value for key, value in conflicting.items() if key != "queue_sha256"}
    )
    with pytest.raises(LearningQueueValidationError, match="conflict"):
        write_managed_learning_queue(path, conflicting)
