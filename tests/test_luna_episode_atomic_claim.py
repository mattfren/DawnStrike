"""Adversarial durable episode-claim coverage for the paper watcher."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from intraday_scanner.storage.sqlite_store import SQLiteScanStore, StorageError


def _lifecycle_rows(*, suffix: str, episode_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    intent_id = f"intent-{suffix}"
    position_id = f"position-{suffix}"
    fill_id = f"fill-{suffix}"
    intent = {
        "intent_id": intent_id,
        "signal_id": f"signal-{suffix}",
        "market_date": "2026-08-27",
        "ticker": "NOVA",
        "episode_id": episode_id,
        "strategy_id": "alphaops_v5",
        "account_id": "alphaops_paper_v5",
        "mode": "paper_execute",
        "lifecycle_state": "ENTRY_TRIGGERED",
        "action": "ENTER_LONG",
        "decision_time": "2026-08-27T14:35:00+00:00",
        "decision_price": 10.0,
        "reason": "test",
        "created_at": "2026-08-27T14:35:00+00:00",
    }
    position = {
        "position_id": position_id,
        "signal_id": intent["signal_id"],
        "market_date": intent["market_date"],
        "ticker": intent["ticker"],
        "status": "OPEN",
        "quantity": 10,
        "entry_intent_id": intent_id,
        "updated_at": intent["decision_time"],
    }
    fill = {
        "fill_id": fill_id,
        "position_id": position_id,
        "intent_id": intent_id,
        "signal_id": intent["signal_id"],
        "market_date": intent["market_date"],
        "ticker": intent["ticker"],
        "side": "BUY",
        "fill_time": intent["decision_time"],
        "fill_price": 10.0,
        "quantity": 10,
        "gross_notional": 100.0,
        "slippage_bps": 0.0,
    }
    return [intent], [position], [fill]


def _persist(db_path: Path, suffix: str) -> dict:
    intents, positions, fills = _lifecycle_rows(suffix=suffix, episode_id="episode:atomic")
    intent_id = intents[0]["intent_id"]
    return SQLiteScanStore(db_path).persist_trade_watcher_lifecycle(
        intents=intents,
        paper_positions=positions,
        paper_fills=fills,
        signal_events=[
            {
                "event_id": f"event-{suffix}",
                "signal_id": intents[0]["signal_id"],
                "event_type": "ENTRY_SIGNAL",
                "event_timestamp": intents[0]["decision_time"],
                "source": "test",
                "payload_json": {"intent_id": intent_id},
            }
        ],
    )


def test_episode_claim_blocks_losing_position_and_fill_side_effects(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    first = _persist(db_path, "first")
    second = _persist(db_path, "second")
    store = SQLiteScanStore(db_path)

    assert first["intents"]["inserted"] == 1
    assert first["paper_fills"]["inserted"] == 1
    assert second["intents"] == {"inserted": 0, "skipped": 1, "row_count": 1}
    assert second["paper_positions"]["inserted"] == 0
    assert second["paper_fills"] == {"inserted": 0, "skipped": 0, "row_count": 1}
    assert len(store.load_trade_intents(market_date="2026-08-27")) == 1
    assert len(store.load_paper_positions(market_date="2026-08-27")) == 1
    assert len(store.load_paper_trade_fills(market_date="2026-08-27")) == 1
    assert len(store.load_signal_events(signal_id="signal-first")) == 1
    assert not store.load_signal_events(signal_id="signal-second")


def test_normalized_entry_action_cannot_evade_episode_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    first = _persist(db_path, "canonical")
    intents, positions, fills = _lifecycle_rows(
        suffix="padded", episode_id="episode:atomic"
    )
    intents[0]["action"] = "  enter_long "
    result = SQLiteScanStore(db_path).persist_trade_watcher_lifecycle(
        intents=intents,
        paper_positions=positions,
        paper_fills=fills,
        signal_events=[
            {
                "event_id": "event-padded",
                "signal_id": "signal-padded",
                "event_type": "ENTRY_SIGNAL",
                "event_timestamp": intents[0]["decision_time"],
                "source": "test",
                "payload_json": {"intent_id": "intent-padded"},
            }
        ],
    )

    store = SQLiteScanStore(db_path)
    assert first["intents"]["inserted"] == 1
    assert result["intents"] == {"inserted": 0, "skipped": 1, "row_count": 1}
    assert result["paper_positions"]["inserted"] == 0
    assert result["paper_fills"] == {"inserted": 0, "skipped": 0, "row_count": 1}
    assert store.load_trade_intents(market_date="2026-08-27")[0]["action"] == "ENTER_LONG"
    assert not store.load_signal_events(signal_id="signal-padded")


def test_same_intent_payload_drift_fails_closed_without_side_effects(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(suffix="drift", episode_id="episode:drift")
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=intents, paper_positions=positions, paper_fills=fills, signal_events=[]
    )
    drifted = [dict(intents[0], reason="tampered", payload_json={"reason": "tampered"})]
    drifted_position = [dict(positions[0], quantity=999)]
    drifted_fill = [dict(fills[0], fill_price=999)]

    with pytest.raises(StorageError, match="identity conflict"):
        store.persist_trade_watcher_lifecycle(
            intents=drifted,
            paper_positions=drifted_position,
            paper_fills=drifted_fill,
            signal_events=[],
        )

    assert len(store.load_trade_intents(market_date="2026-08-27")) == 1
    assert store.load_paper_positions(market_date="2026-08-27")[0]["quantity"] == 10
    assert store.load_paper_trade_fills(market_date="2026-08-27")[0]["fill_price"] == 10


def test_exact_retry_with_position_drift_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="position-drift", episode_id="episode:position-drift"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=intents, paper_positions=positions, paper_fills=fills, signal_events=[]
    )
    drifted_position = [dict(positions[0], quantity=999)]

    with pytest.raises(StorageError, match="paper position identity conflict"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=drifted_position,
            paper_fills=[],
            signal_events=[],
        )

    assert store.load_paper_positions(market_date="2026-08-27")[0]["quantity"] == 10


def test_exact_retry_with_fill_drift_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="fill-drift", episode_id="episode:fill-drift"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=intents, paper_positions=positions, paper_fills=fills, signal_events=[]
    )
    drifted_fill = [dict(fills[0], fill_price=999)]

    with pytest.raises(StorageError, match="paper fill identity conflict"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=drifted_fill,
            signal_events=[],
        )

    assert store.load_paper_trade_fills(market_date="2026-08-27")[0]["fill_price"] == 10


def test_overlapping_watcher_claims_are_serialized_by_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    SQLiteScanStore(db_path).initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda suffix: _persist(db_path, suffix), ("left", "right")))

    store = SQLiteScanStore(db_path)
    assert sorted(result["intents"]["inserted"] for result in results) == [0, 1]
    assert sum(result["paper_positions"]["inserted"] for result in results) == 1
    assert sum(result["paper_fills"]["inserted"] for result in results) == 1
    assert len(store.load_trade_intents(market_date="2026-08-27")) == 1
    assert len(store.load_paper_trade_fills(market_date="2026-08-27")) == 1


def test_same_intent_retry_remains_idempotently_reusable(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(suffix="retry", episode_id="episode:retry")
    store = SQLiteScanStore(db_path)
    first = store.persist_trade_watcher_lifecycle(
        intents=intents, paper_positions=positions, paper_fills=fills, signal_events=[]
    )
    retry = store.persist_trade_watcher_lifecycle(
        intents=intents, paper_positions=positions, paper_fills=fills, signal_events=[]
    )

    assert first["intents"]["inserted"] == 1
    assert retry["intents"]["skipped"] == 1
    assert retry["paper_positions"]["inserted"] == 1
    assert retry["paper_fills"]["inserted"] == 0
    assert len(store.load_paper_positions(market_date="2026-08-27")) == 1
    assert len(store.load_paper_trade_fills(market_date="2026-08-27")) == 1


def test_replace_cannot_evict_a_different_episode_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    first, _, _ = _lifecycle_rows(suffix="winner", episode_id="episode:replace")
    second, _, _ = _lifecycle_rows(suffix="loser", episode_id="episode:replace")
    store = SQLiteScanStore(db_path)

    assert store.persist_trade_intents(first)["inserted"] == 1
    assert store.persist_trade_intents(second, replace=True)["inserted"] == 0
    rows = store.load_trade_intents(market_date="2026-08-27")
    assert [row["intent_id"] for row in rows] == ["intent-winner"]


def test_existing_trade_intents_are_backfilled_during_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE trade_intents (
                intent_id TEXT PRIMARY KEY, signal_id TEXT, market_date TEXT NOT NULL,
                ticker TEXT NOT NULL, mode TEXT NOT NULL, lifecycle_state TEXT NOT NULL,
                action TEXT NOT NULL, decision_time TEXT NOT NULL, decision_price REAL,
                trigger_price REAL, stop_price REAL, target_price REAL, quantity REAL,
                notional REAL, risk_amount REAL, reason TEXT NOT NULL,
                blocked_reason TEXT, source_observation_id TEXT,
                notification_event_key TEXT, created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        for intent_id, signal_id, decision_time, created_at in (
            (
                "legacy-late",
                "legacy-late-signal",
                "2026-08-27T14:40:00+00:00",
                "2026-08-27T14:40:00+00:00",
            ),
            (
                "legacy-early",
                "legacy-early-signal",
                "2026-08-27T14:35:00+00:00",
                "2026-08-27T14:35:00+00:00",
            ),
        ):
            connection.execute(
                """
                INSERT INTO trade_intents
                (intent_id, signal_id, market_date, ticker, mode, lifecycle_state, action,
                 decision_time, reason, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    signal_id,
                    "2026-08-27",
                    "NOVA",
                    "paper_execute",
                    "ENTRY_TRIGGERED",
                    "  enter_long ",
                    decision_time,
                    "legacy",
                    created_at,
                    json.dumps(
                        {
                            "episode_id": "episode:legacy",
                            "strategy_id": "alphaops_v5",
                            "account_id": "alphaops_paper_v5",
                        }
                    ),
                ),
            )
        connection.commit()

    store = SQLiteScanStore(db_path)
    store.initialize()
    records = store.load_trade_intent_records(market_date="2026-08-27")
    assert len(records) == 2
    by_id = {record["columns"]["intent_id"]: record for record in records}
    assert by_id["legacy-early"]["columns"]["episode_id"] == "episode:legacy"
    assert by_id["legacy-late"]["columns"]["episode_id"] == ""
    for record in records:
        assert record["columns"]["strategy_id"] == "alphaops_v5"
        assert record["columns"]["account_id"] == "alphaops_paper_v5"
        assert record["payload_json"]["episode_id"] == "episode:legacy"

    later_intents, later_positions, later_fills = _lifecycle_rows(
        suffix="migration-later", episode_id="episode:legacy"
    )
    result = store.persist_trade_watcher_lifecycle(
        intents=later_intents,
        paper_positions=later_positions,
        paper_fills=later_fills,
        signal_events=[],
    )
    assert result["intents"]["inserted"] == 0
    assert len(store.load_trade_intents(market_date="2026-08-27")) == 2
