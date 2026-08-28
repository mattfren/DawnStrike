from pathlib import Path

import pytest

from intraday_scanner.errors import SnapshotValidationError, StorageError
from intraday_scanner.services import research_episode_outcome_service as bridge
from intraday_scanner.services.daily_strategy_learning_service import (
    _aggregate_decision_receipts,
    _apply_research_episode_outcomes,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _selection() -> dict[str, object]:
    return {
        "selection_id": "selection:nova",
        "signal_id": "signal:nova",
        "ticker": "NOVA",
        "market_date": "2026-08-28",
        "cohort": "research_radar",
        "strategy_id": "research_radar",
        "strategy_version": "v1",
        "selected_at": "2026-08-28T14:00:00+00:00",
        "episode_id": "episode:" + "a" * 32,
        "payload_json": {
            "frozen_ranked_research_slate": {
                "slate_id": "luna-slate-" + "b" * 24,
                "content_hash_sha256": "c" * 64,
                "selection_ids": ["research-selection:nova"],
            },
            "signal": {
                "signal_id": "signal:nova",
                "ticker": "NOVA",
                "research_selection_id": "research-selection:nova",
                "episode_id": "episode:" + "a" * 32,
            },
            "strategy_contributors": [
                {
                    "strategy_id": "primary",
                    "strategy_version": "v1",
                    "receipt_id": "sdr-primary",
                    "receipt_hash_sha256": "d" * 64,
                },
                {
                    "strategy_id": "secondary",
                    "strategy_version": "v2",
                    "receipt_id": "sdr-secondary",
                    "receipt_hash_sha256": "e" * 64,
                },
            ],
        },
    }


def _outcome() -> dict[str, object]:
    return {
        "selection_id": "selection:nova",
        "signal_id": "signal:nova",
        "market_date": "2026-08-28",
        "outcome_status": "WIN",
        "source_authenticated": True,
        "automatic_sourced_data": True,
        "requested_at": "2026-08-28T20:00:00+00:00",
        "source_observation_hash_sha256": "f" * 64,
        "replay_receipt_hash_sha256": "1" * 64,
        "source_bar_hash_sha256": "2" * 64,
        "path_replay_id": "path-v2-nova",
        "source_last_bar_at": "2026-08-28T19:59:00+00:00",
        "learning_eligible": True,
        "entry_price": 999.0,
        "gross_return_pct": 99.0,
    }


def test_bridge_binds_every_contributor_and_scrubs_trade_fields(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert [row["receipt_id"] for row in rows] == ["sdr-primary", "sdr-secondary"]
    assert all(row["outcome_status"] == "WIN" for row in rows)
    assert all("entry_price" not in row and "gross_return_pct" not in row for row in rows)
    assert all(row["research_only"] is True for row in rows)
    assert all(row["broker_execution_enabled"] is False for row in rows)


def test_missing_outcome_does_not_inherit_neighbor_or_become_zero(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert len(rows) == 2
    assert {row["outcome_status"] for row in rows} == {"MISSING"}
    assert all(row["learning_eligible"] is False for row in rows)


def test_cross_date_and_unauthenticated_outcomes_are_ineligible(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    cross_date = {**_outcome(), "market_date": "2026-08-27"}
    unauthenticated = {**_outcome(), "source_authenticated": False, "automatic_sourced_data": False}
    for outcome in (cross_date, unauthenticated):
        rows = bridge.build_research_episode_outcome_bridges(
            [_selection()],
            [outcome],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )
        assert {row["outcome_status"] for row in rows} == {"INELIGIBLE"}
        assert all(row["learning_eligible"] is False for row in rows)


@pytest.mark.parametrize(
    "mutation",
    [
        {"outcome_status": "STALE_OBSERVATION"},
        {"source_coverage_complete": False},
        {"coverage_maximum_gap_seconds": 61, "coverage_allowed_gap_seconds": 60},
    ],
)
def test_stale_or_gapped_source_is_ineligible(monkeypatch, mutation) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [{**_outcome(), **mutation}],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert {row["outcome_status"] for row in rows} == {"INELIGIBLE"}
    assert all(row["learning_eligible"] is False for row in rows)


def test_bridge_retry_is_idempotent_and_collision_rejected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    rows = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
        created_at="2026-08-28T21:01:00+00:00",
    )
    store = SQLiteScanStore(tmp_path / "bridge.sqlite")
    assert bridge.persist_research_episode_outcome_bridges(store, rows)["inserted"] == 2
    assert bridge.persist_research_episode_outcome_bridges(store, rows)["reused"] == 2
    conflict = {**rows[0], "outcome_status": "LOSS"}
    with pytest.raises(StorageError, match="hash mismatch|identity/payload mismatch"):
        bridge.persist_research_episode_outcome_bridges(store, [conflict])


def test_invalid_frozen_selection_fails_closed(monkeypatch) -> None:
    def reject(*_args, **_kwargs):
        raise ValueError("bad slate")

    monkeypatch.setattr(bridge, "validate_ranked_research_slate", reject)
    with pytest.raises(SnapshotValidationError, match="frozen slate"):
        bridge.build_research_episode_outcome_bridges(
            [_selection()],
            [_outcome()],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )


def test_learning_overlay_matches_receipt_identity_once_without_inheriting() -> None:
    receipts = (
        {
            "receipt_id": "sdr-primary",
            "receipt_hash_sha256": "d" * 64,
            "strategy_id": "primary",
            "strategy_version": "v1",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
            "outcome_state": "MISSING",
        },
        {
            "receipt_id": "sdr-other",
            "receipt_hash_sha256": "9" * 64,
            "strategy_id": "other",
            "strategy_version": "v1",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
        },
    )
    joined = [
        {
            "bridge_id": "rep-one",
            "receipt_id": "sdr-primary",
            "receipt_hash_sha256": "d" * 64,
            "ticker": "NOVA",
            "market_date": "2026-08-28",
            "outcome_status": "WIN",
            "learning_eligible": True,
        },
        {
            "bridge_id": "rep-two",
            "receipt_id": "sdr-primary",
            "receipt_hash_sha256": "d" * 64,
            "ticker": "NOVA",
            "market_date": "2026-08-28",
            "outcome_status": "LOSS",
            "learning_eligible": True,
        },
    ]
    overlaid = _apply_research_episode_outcomes(receipts, joined)
    summary = _aggregate_decision_receipts(overlaid)
    assert summary["outcome_state_counts"]["WIN"] == 1
    assert summary["outcome_state_counts"]["MISSING_OUTCOME"] == 1
