"""Adversarial durable episode-claim coverage for the paper watcher."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from intraday_scanner.storage.sqlite_store import SQLiteScanStore


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
    return SQLiteScanStore(db_path).persist_trade_watcher_lifecycle(
        intents=intents,
        paper_positions=positions,
        paper_fills=fills,
        signal_events=[],
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
        connection.execute(
            """
            INSERT INTO trade_intents
            (intent_id, signal_id, market_date, ticker, mode, lifecycle_state, action,
             decision_time, reason, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-intent",
                "legacy-signal",
                "2026-08-27",
                "NOVA",
                "paper_execute",
                "ENTRY_TRIGGERED",
                "ENTER_LONG",
                "2026-08-27T14:35:00+00:00",
                "legacy",
                "2026-08-27T14:35:00+00:00",
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
    row = store.load_trade_intents(market_date="2026-08-27")[0]
    assert row["episode_id"] == "episode:legacy"
    assert row["strategy_id"] == "alphaops_v5"
    assert row["account_id"] == "alphaops_paper_v5"
