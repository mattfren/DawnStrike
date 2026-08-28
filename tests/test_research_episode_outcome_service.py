import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from intraday_scanner.errors import SnapshotValidationError, StorageError
from intraday_scanner.notifiers import NotificationEvent
from intraday_scanner.services import research_episode_outcome_service as bridge
from intraday_scanner.services.alpha_cycle_service import _persist_research_radar_selections
from intraday_scanner.services.daily_strategy_learning_service import (
    _aggregate_decision_receipts,
    _apply_research_episode_outcomes,
)
from intraday_scanner.services.luna_research_slate_service import build_ranked_research_slate
from intraday_scanner.services.premarket_enrichment_service import (
    _canonical_observation_payload,
    observation_from_alpaca_bars,
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
                # Production _persist_research_radar_selections stores this
                # exact nested contributor list under payload_json.signal.
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
        "source_coverage_complete": True,
        "coverage_maximum_gap_seconds": 60,
        "coverage_allowed_gap_seconds": 60,
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


def test_nested_json_envelopes_preserve_primary_and_secondary_receipts(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    payload = dict(selection["payload_json"])
    signal = dict(payload["signal"])
    signal["strategy_contributors"] = json.dumps(
        signal["strategy_contributors"], sort_keys=True
    )
    payload["signal"] = json.dumps(signal, sort_keys=True)
    selection["payload_json"] = json.dumps(payload, sort_keys=True)
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert [
        (row["strategy_id"], row["receipt_id"], row["receipt_hash_sha256"])
        for row in rows
    ] == [
        ("primary", "sdr-primary", "d" * 64),
        ("secondary", "sdr-secondary", "e" * 64),
    ]


def test_production_persist_shape_binds_nested_contributors_once(
    tmp_path: Path,
) -> None:
    selected_at = "2026-08-28T13:00:00+00:00"
    cycle_at = datetime.fromisoformat(selected_at)
    observation = observation_from_alpaca_bars(
        "NOVA",
        [
            {
                "ticker": "NOVA",
                "timestamp": (cycle_at - timedelta(minutes=2)).isoformat(),
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000,
            }
        ],
        previous_close=9.5,
        requested_at=cycle_at,
        max_age_seconds=600,
        feed="iex",
    )
    observation_hash, observation_payload = _canonical_observation_payload(observation)
    signal = {
        "signal_id": "signal:production-nova",
        "ticker": "NOVA",
        "episode_id": "episode:" + "f" * 32,
        "market_date": "2026-08-28",
        "universe_lane": "mover",
        "evidence_lane": "mover",
        "source_count": 1,
        "source_quality_status": "VERIFIED",
        "freshness_status": "FRESH",
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "input_status": "VERIFIED",
        "evidence_status": "VERIFIED",
        "enrichment_observation_sha256": observation_hash,
        "enrichment_observation_payload_json": observation_payload,
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
    }
    slate = build_ranked_research_slate(
        [signal],
        generated_at=selected_at,
        market_date="2026-08-28",
        scan_id="scan-production",
        require_safety=True,
    )
    store = SQLiteScanStore(tmp_path / "production-shape.sqlite")
    event = NotificationEvent(
        event_key="alphaops:scan-production:alpha_morning_watch",
        title="Dawnstrike Alpha Watch",
        body="Research radar: NOVA",
        channel_hint="alpha_morning_watch",
        payload={"run_id": "scan-production", "signals": []},
    )
    _persist_research_radar_selections(
        store,
        scan_id="scan-production",
        radar=list(slate["rows"]),
        slate=slate,
        selected_at=selected_at,
        event=event,
    )
    persisted = store.load_signal_selections(cohort="research_radar")
    assert len(persisted) == 1
    nested = persisted[0]["payload_json"]["signal"]["strategy_contributors"]
    assert [item["receipt_id"] for item in nested] == ["sdr-primary", "sdr-secondary"]
    outcome = {
        **_outcome(),
        "selection_id": persisted[0]["selection_id"],
        "signal_id": persisted[0]["signal_id"],
    }
    rows = bridge.build_research_episode_outcome_bridges(
        persisted,
        [outcome],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert [row["receipt_id"] for row in rows] == ["sdr-primary", "sdr-secondary"]
    assert all(row["learning_eligible"] is True for row in rows)


def test_stripped_nested_contributors_are_ineligible_and_do_not_inherit(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    del selection["payload_json"]["signal"]["strategy_contributors"]
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert len(rows) == 1
    assert rows[0]["receipt_id"] == ""
    assert rows[0]["outcome_status"] == "INELIGIBLE"
    assert rows[0]["learning_eligible"] is False


def test_mutated_nested_receipt_hash_cannot_join_learning(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    selection["payload_json"]["signal"]["strategy_contributors"][0][
        "receipt_hash_sha256"
    ] = "9" * 64
    rows = bridge.build_research_episode_outcome_bridges(
        [selection],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
    )
    assert rows[0]["receipt_id"] == "sdr-primary"
    assert rows[0]["receipt_hash_sha256"] == "9" * 64
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
    )
    overlaid = _apply_research_episode_outcomes(receipts, rows)
    assert overlaid[0]["outcome_state"] == "MISSING"


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
    conflicting_lineage = {**_outcome(), "selection_id": "selection:evil"}
    unauthenticated = {
        **_outcome(),
        "source_authenticated": False,
        "automatic_sourced_data": True,
    }
    for outcome in (cross_date, conflicting_lineage, unauthenticated):
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
        {"source_coverage_complete": None},
        {"coverage_maximum_gap_seconds": None},
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
    replay = bridge.build_research_episode_outcome_bridges(
        [_selection()],
        [_outcome()],
        market_date="2026-08-28",
        cutoff="2026-08-28T21:00:00+00:00",
        created_at="2026-08-28T23:59:00+00:00",
    )
    assert [row["bridge_id"] for row in replay] == [row["bridge_id"] for row in rows]
    assert bridge.persist_research_episode_outcome_bridges(store, replay)["reused"] == 2
    for mutation in (
        {"outcome_status": "LOSS"},
        {"source_bar_hash_sha256": "3" * 64},
        {"source_observation_hash_sha256": "4" * 64},
        {"source_lineage": [{"source": "different-provider", "request": "changed"}]},
    ):
        conflict = bridge.build_research_episode_outcome_bridges(
            [_selection()],
            [{**_outcome(), **mutation}],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )
        with pytest.raises(StorageError, match="hash mismatch|identity/payload mismatch"):
            bridge.persist_research_episode_outcome_bridges(store, [conflict[0]])


def test_logical_key_is_delimiter_collision_safe() -> None:
    left = bridge._logical_key(
        market_date="2026-08-28",
        selection_id="selection|primary",
        strategy_id="v1",
        strategy_version="receipt",
        receipt_id="r",
    )
    right = bridge._logical_key(
        market_date="2026-08-28",
        selection_id="selection",
        strategy_id="primary|v1",
        strategy_version="receipt",
        receipt_id="r",
    )
    assert left != right


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


def test_frozen_timestamp_and_lineage_overrides_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(bridge, "validate_ranked_research_slate", lambda slate, **_: slate)
    selection = _selection()
    payload = dict(selection["payload_json"])
    payload["selected_at"] = "2026-08-28T14:01:00+00:00"
    selection["payload_json"] = payload
    with pytest.raises(SnapshotValidationError, match="timestamp"):
        bridge.build_research_episode_outcome_bridges(
            [selection],
            [_outcome()],
            market_date="2026-08-28",
            cutoff="2026-08-28T21:00:00+00:00",
        )
    selection = _selection()
    selection["episode_id"] = "episode:" + "e" * 32
    with pytest.raises(SnapshotValidationError, match="episode identity"):
        bridge.build_research_episode_outcome_bridges(
            [selection],
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
        {
            "receipt_id": "sdr-secondary",
            "receipt_hash_sha256": "e" * 64,
            "strategy_id": "secondary",
            "strategy_version": "v2",
            "symbol": "NOVA",
            "market_date": "2026-08-28",
            "outcome_state": "MISSING",
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
        {
            "bridge_id": "rep-three",
            "receipt_id": "sdr-secondary",
            "receipt_hash_sha256": "e" * 64,
            "ticker": "NOVA",
            "market_date": "2026-08-28",
            "outcome_status": "WIN",
            "learning_eligible": True,
        },
    ]
    overlaid = _apply_research_episode_outcomes(receipts, joined)
    summary = _aggregate_decision_receipts(overlaid)
    assert summary["outcome_state_counts"]["WIN"] == 2
    assert summary["outcome_state_counts"]["MISSING_OUTCOME"] == 1
    reversed_overlay = _apply_research_episode_outcomes(receipts, list(reversed(joined)))
    assert reversed_overlay == overlaid
