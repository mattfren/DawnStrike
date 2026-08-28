"""Adversarial durable episode-claim coverage for the paper watcher."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from intraday_scanner.storage.sqlite_store import SQLiteScanStore, StorageError


def _hash_mapping(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _watcher_proof(suffix: str) -> dict:
    common = {
        "signal_id": f"signal-{suffix}",
        "ticker": "NOVA",
        "plan_hash_sha256": "a" * 64,
        "selection_id": f"selection-{suffix}",
        "cohort": "TIER3",
        "source_scan_id": f"scan-{suffix}",
        "frozen_slate_id": f"slate-{suffix}",
        "frozen_slate_content_hash_sha256": "b" * 64,
        "frozen_research_selection_id": f"research-{suffix}",
    }
    checked_at = "2026-08-27T14:35:00+00:00"
    quote = {**common, "status": "CURRENT", "checked_at": checked_at}
    portfolio = {
        **common,
        "status": "ADMITTED",
        "simulated_account_id": "alphaops_v5_simulated",
        "checked_at": checked_at,
    }
    proof = {
        **common,
        "schema_version": "alphaops.watcher_current.v1",
        "status": "CURRENT",
        "checked_at": checked_at,
        "quote_receipt": quote,
        "quote_hash_sha256": _hash_mapping(quote),
        "portfolio_receipt": portfolio,
        "portfolio_hash_sha256": _hash_mapping(portfolio),
        "evaluate_v5_official_paper": {"decision_fingerprint": "fingerprint-v1"},
    }
    proof["proof_hash_sha256"] = _hash_mapping(proof)
    return proof


def _lifecycle_rows(*, suffix: str, episode_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    intent_id = f"intent-{suffix}"
    position_id = f"position-{suffix}"
    fill_id = f"fill-{suffix}"
    proof = _watcher_proof(suffix)
    intent = {
        "intent_id": intent_id,
        "signal_id": f"signal-{suffix}",
        "market_date": "2026-08-27",
        "ticker": "NOVA",
        "episode_id": episode_id,
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5",
        "account_id": "alphaops_v5_simulated",
        "selection_id": f"selection-{suffix}",
        "cohort": "TIER3",
        "mode": "paper_execute",
        "lifecycle_state": "ENTRY_TRIGGERED",
        "action": "ENTER_LONG",
        "decision_time": "2026-08-27T14:35:00+00:00",
        "decision_price": 10.0,
        "reason": "test",
        "created_at": "2026-08-27T14:35:00+00:00",
        "decision_fingerprint": "fingerprint-v1",
        "official_paper_eligible": True,
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
        "decision_trace": {
            "account_id": "alphaops_v5_simulated",
            "plan_hash_sha256": "a" * 64,
            "decision_fingerprint": "fingerprint-v1",
        },
        "watcher_current_proof": proof,
        "monitor_proof_lineage": {
            key: proof[key]
            for key in (
                "selection_id",
                "cohort",
                "source_scan_id",
                "frozen_slate_id",
                "frozen_slate_content_hash_sha256",
                "frozen_research_selection_id",
            )
        },
    }
    position = {
        "position_id": position_id,
        "signal_id": intent["signal_id"],
        "market_date": intent["market_date"],
        "ticker": intent["ticker"],
        "direction": "long",
        "episode_id": episode_id,
        "strategy_id": intent["strategy_id"],
        "strategy_version": intent["strategy_version"],
        "account_id": intent["account_id"],
        "selection_id": intent["selection_id"],
        "cohort": intent["cohort"],
        "official_paper_eligible": True,
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
        "status": "OPEN",
        "quantity": 10,
        "entry_intent_id": intent_id,
        "entry_price": 10.0,
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
        "episode_id": episode_id,
        "strategy_id": intent["strategy_id"],
        "strategy_version": intent["strategy_version"],
        "account_id": intent["account_id"],
        "selection_id": intent["selection_id"],
        "cohort": intent["cohort"],
        "decision_fingerprint": intent["decision_fingerprint"],
        "official_paper_eligible": True,
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
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
    store = SQLiteScanStore(db_path)
    # The production watcher receives only selections whose source signal is
    # already durable.  Seed that governed parent here so this fixture tests
    # atomic episode admission rather than relying on an orphan event.
    store.persist_historical_signals(
        [
            {
                "signal_id": intents[0]["signal_id"],
                "generated_at": "2026-08-27T14:30:00+00:00",
                "market_date": intents[0]["market_date"],
                "ticker": intents[0]["ticker"],
                "signal_label": "WATCH",
                "risk_flags_json": [],
                "avoid_reasons_json": [],
                "raw_payload_json": {"fixture": "governed_episode_parent"},
            }
        ]
    )
    return store.persist_trade_watcher_lifecycle(
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


def _monitor_receipt(intent_id: str, suffix: str) -> dict:
    proof = _watcher_proof(suffix)
    proof_hash = proof["proof_hash_sha256"]
    receipt = {
        "schema_version": "dawnstrike.alphaops.monitor_publication_receipt.v1",
        "receipt_id": "monitor-publication-"
        + hashlib.sha256(
            f"signal-{suffix}:{intent_id}:{'a' * 64}:{proof_hash}".encode()
        ).hexdigest()[:24],
        "intent_id": intent_id,
        "simulated_account_id": "alphaops_v5_simulated",
        "market_date": "2026-08-27",
        "ticker": "NOVA",
        "signal_id": f"signal-{suffix}",
        "plan_hash_sha256": "a" * 64,
        "publication_count": 1,
        "publication_tier": "ALERTABLE_PAPER_ENTRY",
        "research_only": True,
        "broker_execution": "disabled",
        "checked_at": "2026-08-27T14:35:00+00:00",
        "decision_trace_fingerprint": "fingerprint-v1",
        "selection_id": f"selection-{suffix}",
        "cohort": "TIER3",
        "source_scan_id": f"scan-{suffix}",
        "frozen_slate_id": f"slate-{suffix}",
        "frozen_slate_content_hash_sha256": "b" * 64,
        "frozen_research_selection_id": f"research-{suffix}",
        "watcher_proof_hash_sha256": proof_hash,
        "quote_receipt_hash_sha256": proof["quote_hash_sha256"],
        "portfolio_receipt_hash_sha256": proof["portfolio_hash_sha256"],
    }
    receipt["content_hash_sha256"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()
    return receipt


def _exit_rows(
    *, position: dict, suffix: str, realized_pnl: float = 16.0
) -> tuple[list[dict], list[dict], list[dict]]:
    intent_id = f"intent-exit-{suffix}"
    decision_time = "2026-08-27T15:00:00+00:00"
    intent = {
        "intent_id": intent_id,
        "signal_id": position["signal_id"],
        "market_date": position["market_date"],
        "ticker": position["ticker"],
        "episode_id": position["episode_id"],
        "strategy_id": "alphaops_v5",
        "strategy_version": position["strategy_version"],
        "account_id": "alphaops_v5_simulated",
        "selection_id": position["selection_id"],
        "cohort": position["cohort"],
        "mode": "paper_execute",
        "lifecycle_state": "EXIT_TRIGGERED",
        "action": "EXIT_LONG",
        "decision_time": decision_time,
        "decision_price": 11.6,
        "reason": "target reached",
        "created_at": decision_time,
        "decision_fingerprint": "fingerprint-exit-v1",
        "official_paper_eligible": True,
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
    }
    closed = {
        **position,
        "status": "CLOSED",
        "exit_intent_id": intent_id,
        "closed_at": decision_time,
        "exit_price": 11.6,
        "realized_pnl": realized_pnl,
        "realized_return_pct": 16.0,
        "updated_at": decision_time,
    }
    fill = {
        "fill_id": f"fill-exit-{suffix}",
        "position_id": position["position_id"],
        "intent_id": intent_id,
        "signal_id": position["signal_id"],
        "market_date": position["market_date"],
        "ticker": position["ticker"],
        "side": "SELL",
        "episode_id": intent["episode_id"],
        "strategy_id": intent["strategy_id"],
        "strategy_version": intent["strategy_version"],
        "account_id": intent["account_id"],
        "selection_id": intent["selection_id"],
        "cohort": intent["cohort"],
        "decision_fingerprint": intent["decision_fingerprint"],
        "official_paper_eligible": True,
        "research_only": True,
        "broker_execution": "disabled",
        "broker_execution_enabled": False,
        "fill_time": decision_time,
        "fill_price": 11.6,
        "quantity": position["quantity"],
        "gross_notional": 116.0,
        "slippage_bps": 0.0,
    }
    return [intent], [closed], [fill]


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
    assert second["monitor_publication_receipts"]["count"] == 0
    assert len(store.load_trade_intents(market_date="2026-08-27")) == 1
    assert len(store.load_paper_positions(market_date="2026-08-27")) == 1
    assert len(store.load_paper_trade_fills(market_date="2026-08-27")) == 1
    assert len(store.load_signal_events(signal_id="signal-first")) == 1
    assert not store.load_signal_events(signal_id="signal-second")
    assert not store.load_monitor_publication_receipts(market_date="2026-08-27")


def test_normalized_entry_action_cannot_evade_episode_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    first = _persist(db_path, "canonical")
    intents, positions, fills = _lifecycle_rows(suffix="padded", episode_id="episode:atomic")
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

    with pytest.raises(StorageError, match="paper fill (identity conflict|is not bound)"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=drifted_fill,
            signal_events=[],
        )

    assert store.load_paper_trade_fills(market_date="2026-08-27")[0]["fill_price"] == 10


def test_legitimate_open_to_closed_transition_is_admitted(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    entry_intents, positions, entry_fills = _lifecycle_rows(
        suffix="close-transition", episode_id="episode:closed-retry"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=entry_intents,
        paper_positions=positions,
        paper_fills=entry_fills,
        signal_events=[],
    )
    exit_intents, closed_positions, exit_fills = _exit_rows(
        position=positions[0], suffix="close-transition"
    )

    result = store.persist_trade_watcher_lifecycle(
        intents=exit_intents,
        paper_positions=closed_positions,
        paper_fills=exit_fills,
        signal_events=[],
    )

    persisted = store.load_paper_positions(market_date="2026-08-27")[0]
    assert result["intents"]["inserted"] == 1
    assert result["paper_positions"]["inserted"] == 1
    assert result["paper_fills"]["inserted"] == 1
    assert persisted["status"] == "CLOSED"
    assert persisted["exit_intent_id"] == "intent-exit-close-transition"
    assert persisted["realized_pnl"] == 16.0


def test_exact_closed_position_retry_cannot_rewrite_return_truth(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    entry_intents, positions, entry_fills = _lifecycle_rows(
        suffix="closed-retry", episode_id="episode:closed-retry"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=entry_intents,
        paper_positions=positions,
        paper_fills=entry_fills,
        signal_events=[],
    )
    exit_intents, closed_positions, exit_fills = _exit_rows(
        position=positions[0], suffix="closed-retry"
    )
    store.persist_trade_watcher_lifecycle(
        intents=exit_intents,
        paper_positions=closed_positions,
        paper_fills=exit_fills,
        signal_events=[],
    )
    fabricated = [
        dict(
            closed_positions[0],
            realized_pnl=9999.0,
            realized_return_pct=999.0,
        )
    ]
    fabricated[0]["payload_json"] = dict(fabricated[0])

    with pytest.raises(StorageError, match="paper position identity conflict"):
        store.persist_trade_watcher_lifecycle(
            intents=exit_intents,
            paper_positions=fabricated,
            paper_fills=[],
            signal_events=[],
        )

    persisted = store.load_paper_positions(market_date="2026-08-27")[0]
    assert persisted["realized_pnl"] == 16.0
    assert persisted["realized_return_pct"] == 16.0


def test_exact_open_intent_cannot_acquire_second_distinct_fill(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="open-second-fill", episode_id="episode:open-second-fill"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=intents, paper_positions=positions, paper_fills=fills, signal_events=[]
    )
    retry_fill = dict(fills[0], fill_id="fill-open-second-distinct")

    with pytest.raises(StorageError, match="paper fill claim conflict"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=[retry_fill],
            signal_events=[],
        )

    assert len(store.load_paper_trade_fills(market_date="2026-08-27")) == 1


def test_exact_closed_intent_cannot_acquire_second_distinct_fill(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="closed-second-fill", episode_id="episode:closed-second-fill"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=intents, paper_positions=positions, paper_fills=fills, signal_events=[]
    )
    exit_intents, closed_positions, exit_fills = _exit_rows(
        position=positions[0], suffix="closed-second-fill"
    )
    store.persist_trade_watcher_lifecycle(
        intents=exit_intents,
        paper_positions=closed_positions,
        paper_fills=exit_fills,
        signal_events=[],
    )
    retry_fill = dict(exit_fills[0], fill_id="fill-exit-second-distinct")

    with pytest.raises(StorageError, match="paper fill claim conflict"):
        store.persist_trade_watcher_lifecycle(
            intents=exit_intents,
            paper_positions=closed_positions,
            paper_fills=[retry_fill],
            signal_events=[],
        )

    assert len(store.load_paper_trade_fills(market_date="2026-08-27")) == 2


def test_close_transition_rejects_nonpersistable_bound_fill(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    entry_intents, positions, entry_fills = _lifecycle_rows(
        suffix="missing-exit-fill-id", episode_id="episode:missing-exit-fill-id"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=entry_intents,
        paper_positions=positions,
        paper_fills=entry_fills,
        signal_events=[],
    )
    exit_intents, closed_positions, exit_fills = _exit_rows(
        position=positions[0], suffix="missing-exit-fill-id"
    )
    exit_fills[0]["fill_id"] = ""

    with pytest.raises(StorageError, match="transition/fill invariants"):
        store.persist_trade_watcher_lifecycle(
            intents=exit_intents,
            paper_positions=closed_positions,
            paper_fills=exit_fills,
            signal_events=[],
        )

    assert store.load_paper_positions(market_date="2026-08-27")[0]["status"] == "OPEN"


def test_close_transition_binds_side_to_stored_direction(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    entry_intents, positions, entry_fills = _lifecycle_rows(
        suffix="wrong-exit-side", episode_id="episode:wrong-exit-side"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=entry_intents,
        paper_positions=positions,
        paper_fills=entry_fills,
        signal_events=[],
    )
    exit_intents, closed_positions, exit_fills = _exit_rows(
        position=positions[0], suffix="wrong-exit-side"
    )
    exit_intents[0]["action"] = "EXIT_SHORT"
    exit_fills[0]["side"] = "BUY_TO_COVER"

    with pytest.raises(StorageError, match="transition/fill invariants"):
        store.persist_trade_watcher_lifecycle(
            intents=exit_intents,
            paper_positions=closed_positions,
            paper_fills=exit_fills,
            signal_events=[],
        )

    assert store.load_paper_positions(market_date="2026-08-27")[0]["status"] == "OPEN"


def test_close_transition_binds_fill_price_to_exit_intent(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    entry_intents, positions, entry_fills = _lifecycle_rows(
        suffix="wrong-exit-price", episode_id="episode:wrong-exit-price"
    )
    store = SQLiteScanStore(db_path)
    store.persist_trade_watcher_lifecycle(
        intents=entry_intents,
        paper_positions=positions,
        paper_fills=entry_fills,
        signal_events=[],
    )
    exit_intents, closed_positions, exit_fills = _exit_rows(
        position=positions[0], suffix="wrong-exit-price"
    )
    exit_fills[0]["fill_price"] = 100.0
    closed_positions[0].update(
        exit_price=100.0,
        realized_pnl=900.0,
        realized_return_pct=900.0,
    )

    with pytest.raises(StorageError, match="transition/fill invariants"):
        store.persist_trade_watcher_lifecycle(
            intents=exit_intents,
            paper_positions=closed_positions,
            paper_fills=exit_fills,
            signal_events=[],
        )

    assert store.load_paper_positions(market_date="2026-08-27")[0]["status"] == "OPEN"


def test_brand_new_closed_position_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    _, positions, _ = _lifecycle_rows(suffix="new-closed", episode_id="episode:new-closed")
    exit_intents, closed_positions, exit_fills = _exit_rows(
        position=positions[0], suffix="new-closed"
    )
    store = SQLiteScanStore(db_path)

    with pytest.raises(StorageError, match="existing open position"):
        store.persist_trade_watcher_lifecycle(
            intents=exit_intents,
            paper_positions=closed_positions,
            paper_fills=exit_fills,
            signal_events=[],
        )

    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_paper_positions(market_date="2026-08-27")


def test_brand_new_open_position_requires_bound_entry_fill(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, _ = _lifecycle_rows(
        suffix="missing-entry-fill", episode_id="episode:missing-entry-fill"
    )
    store = SQLiteScanStore(db_path)

    with pytest.raises(StorageError, match="valid bound fill"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=[],
            signal_events=[],
        )

    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_paper_positions(market_date="2026-08-27")


def test_entry_fill_and_receipt_without_position_roll_back(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, _, fills = _lifecycle_rows(
        suffix="orphan-receipt", episode_id="episode:orphan-receipt"
    )
    store = SQLiteScanStore(db_path)
    with pytest.raises(StorageError, match="durable bound position"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=[],
            paper_fills=fills,
            signal_events=[],
            monitor_publication_receipts=[
                _monitor_receipt("intent-orphan-receipt", "orphan-receipt")
            ],
        )
    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_paper_trade_fills(market_date="2026-08-27")
    assert not store.load_monitor_publication_receipts(market_date="2026-08-27")


def test_stand_down_cannot_persist_arbitrary_fill(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="stand-down-fill", episode_id="episode:stand-down-fill"
    )
    intents[0]["action"] = "STAND_DOWN"
    positions[0]["entry_intent_id"] = intents[0]["intent_id"]
    store = SQLiteScanStore(db_path)
    with pytest.raises(StorageError, match="not bound to an admitted intent"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
        )
    assert not store.load_trade_intents(market_date="2026-08-27")


def test_scoped_portfolio_date_must_match_entry_date(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="wrong-cap-date", episode_id="episode:wrong-cap-date"
    )
    store = SQLiteScanStore(db_path)
    with pytest.raises(StorageError, match="account/date conflicts"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
            portfolio_account_id="alphaops_v5_simulated",
            portfolio_market_date="2026-08-26",
            max_daily_entries=1,
        )
    assert not store.load_trade_intents(market_date="2026-08-27")


def test_entry_fill_price_is_bound_to_entry_intent(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="wrong-entry-price", episode_id="episode:wrong-entry-price"
    )
    positions[0]["entry_price"] = 100.0
    fills[0]["fill_price"] = 100.0
    fills[0]["gross_notional"] = 1000.0
    store = SQLiteScanStore(db_path)

    with pytest.raises(StorageError, match="valid bound fill"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
        )

    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_paper_positions(market_date="2026-08-27")


@pytest.mark.parametrize(("field", "value"), (("episode_id", ""), ("account_id", "other")))
def test_fill_requires_exact_nonempty_research_identity(
    tmp_path: Path, field: str, value: str
) -> None:
    intents, positions, fills = _lifecycle_rows(
        suffix=f"fill-{field}", episode_id=f"episode:fill-{field}"
    )
    fills[0][field] = value
    store = SQLiteScanStore(tmp_path / f"{field}.sqlite")
    with pytest.raises(StorageError, match="valid bound fill"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
        )
    assert not store.load_trade_intents(market_date="2026-08-27")


def test_position_requires_exact_research_identity(tmp_path: Path) -> None:
    intents, positions, fills = _lifecycle_rows(
        suffix="position-identity", episode_id="episode:position-identity"
    )
    positions[0]["selection_id"] = "selection-other"
    store = SQLiteScanStore(tmp_path / "position.sqlite")
    with pytest.raises(StorageError, match="valid bound fill"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
        )
    assert not store.load_trade_intents(market_date="2026-08-27")


def test_unknown_position_status_is_rejected(tmp_path: Path) -> None:
    intents, positions, fills = _lifecycle_rows(
        suffix="unknown-status", episode_id="episode:unknown-status"
    )
    positions[0]["status"] = "UNKNOWN"
    store = SQLiteScanStore(tmp_path / "unknown.sqlite")
    with pytest.raises(StorageError, match="unsupported lifecycle status"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
        )
    assert not store.load_trade_intents(market_date="2026-08-27")


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
    assert not store.load_monitor_publication_receipts(market_date="2026-08-27")


def _reticker(rows: tuple[list[dict], list[dict], list[dict]], ticker: str) -> None:
    intents, positions, fills = rows
    for row in (*intents, *positions, *fills):
        row["ticker"] = ticker


def _persist_with_caps(
    db_path: Path,
    *,
    suffix: str,
    episode_id: str,
    ticker: str,
    max_open_positions: int,
    max_daily_entries: int = 10,
) -> dict:
    rows = _lifecycle_rows(suffix=suffix, episode_id=episode_id)
    _reticker(rows, ticker)
    intents, positions, fills = rows
    return SQLiteScanStore(db_path).persist_trade_watcher_lifecycle(
        intents=intents,
        paper_positions=positions,
        paper_fills=fills,
        signal_events=[],
        portfolio_account_id="alphaops_v5_simulated",
        portfolio_market_date="2026-08-27",
        max_open_positions=max_open_positions,
        max_daily_entries=max_daily_entries,
    )


def test_concurrent_distinct_entries_cannot_exceed_open_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    _persist_with_caps(
        db_path,
        suffix="preseed-cap",
        episode_id="episode:preseed-cap",
        ticker="NOVA",
        max_open_positions=2,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _persist_with_caps,
                db_path,
                suffix=suffix,
                episode_id=f"episode:{suffix}",
                ticker=ticker,
                max_open_positions=2,
            )
            for suffix, ticker in (("cap-left", "LEFT"), ("cap-right", "RIGHT"))
        ]
        results = [future.result() for future in futures]

    assert sum(result["intents"]["inserted"] for result in results) == 1
    assert sum(len(result["rejected_intents"]) for result in results) == 1
    assert len(SQLiteScanStore(db_path).load_paper_positions(market_date="2026-08-27")) == 2


def test_concurrent_same_symbol_distinct_episodes_admit_only_one(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    SQLiteScanStore(db_path).initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _persist_with_caps,
                db_path,
                suffix=suffix,
                episode_id=f"episode:{suffix}",
                ticker="NOVA",
                max_open_positions=3,
            )
            for suffix in ("symbol-left", "symbol-right")
        ]
        results = [future.result() for future in futures]

    assert sum(result["intents"]["inserted"] for result in results) == 1
    assert sum(len(result["rejected_intents"]) for result in results) == 1
    assert len(SQLiteScanStore(db_path).load_paper_positions(market_date="2026-08-27")) == 1


def test_concurrent_distinct_entries_cannot_exceed_daily_cap(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    SQLiteScanStore(db_path).initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _persist_with_caps,
                db_path,
                suffix=suffix,
                episode_id=f"episode:{suffix}",
                ticker=ticker,
                max_open_positions=3,
                max_daily_entries=1,
            )
            for suffix, ticker in (("daily-left", "LEFT"), ("daily-right", "RIGHT"))
        ]
        results = [future.result() for future in futures]

    assert sum(result["intents"]["inserted"] for result in results) == 1
    assert sum(len(result["rejected_intents"]) for result in results) == 1
    assert len(SQLiteScanStore(db_path).load_paper_trade_fills(market_date="2026-08-27")) == 1


def test_same_intent_retry_remains_idempotently_reusable(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(suffix="retry", episode_id="episode:retry")
    store = SQLiteScanStore(db_path)
    first = store.persist_trade_watcher_lifecycle(
        intents=intents,
        paper_positions=positions,
        paper_fills=fills,
        signal_events=[],
    )
    retry = store.persist_trade_watcher_lifecycle(
        intents=intents,
        paper_positions=positions,
        paper_fills=fills,
        signal_events=[],
    )

    assert first["intents"]["inserted"] == 1
    assert retry["intents"]["skipped"] == 1
    assert retry["paper_positions"]["inserted"] == 1
    assert retry["paper_fills"]["inserted"] == 0
    assert first["monitor_publication_receipts"]["count"] == 0
    assert retry["monitor_publication_receipts"]["count"] == 0
    assert len(store.load_paper_positions(market_date="2026-08-27")) == 1
    assert len(store.load_paper_trade_fills(market_date="2026-08-27")) == 1


def test_monitor_receipt_failure_rolls_back_intent_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="receipt-rollback", episode_id="episode:receipt-rollback"
    )
    receipt = _monitor_receipt("intent-receipt-rollback", "receipt-rollback")
    receipt["unencodable"] = object()
    store = SQLiteScanStore(db_path)

    with pytest.raises(TypeError, match="JSON serializable"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
            monitor_publication_receipts=[receipt],
        )

    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_monitor_publication_receipts(market_date="2026-08-27")


def test_minimal_self_hashed_watcher_proof_cannot_publish_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="self-hashed-proof", episode_id="episode:self-hashed-proof"
    )
    store = SQLiteScanStore(db_path)
    with pytest.raises(StorageError, match="strict watcher validation envelope"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
            monitor_publication_receipts=[
                _monitor_receipt("intent-self-hashed-proof", "self-hashed-proof")
            ],
        )
    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_monitor_publication_receipts(market_date="2026-08-27")


def test_monitor_receipt_must_match_admitted_intent_dimensions(tmp_path: Path) -> None:
    db_path = tmp_path / "watcher.sqlite"
    intents, positions, fills = _lifecycle_rows(
        suffix="receipt-binding", episode_id="episode:receipt-binding"
    )
    receipt = _monitor_receipt("intent-receipt-binding", "receipt-binding")
    receipt["signal_id"] = "signal-other"
    store = SQLiteScanStore(db_path)

    with pytest.raises(StorageError, match="monitor publication receipt"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
            monitor_publication_receipts=[receipt],
        )

    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_monitor_publication_receipts(market_date="2026-08-27")


@pytest.mark.parametrize(
    ("field", "value", "recompute_hash"),
    (
        ("content_hash_sha256", "0" * 64, False),
        ("plan_hash_sha256", "b" * 64, True),
        ("simulated_account_id", "other-account", True),
    ),
)
def test_monitor_receipt_rejects_forged_hash_plan_or_account(
    tmp_path: Path, field: str, value: str, recompute_hash: bool
) -> None:
    db_path = tmp_path / f"watcher-{field}.sqlite"
    intents, positions, fills = _lifecycle_rows(suffix=field, episode_id=f"episode:{field}")
    receipt = _monitor_receipt(f"intent-{field}", field)
    receipt[field] = value
    if recompute_hash:
        unsigned = {key: item for key, item in receipt.items() if key != "content_hash_sha256"}
        receipt["content_hash_sha256"] = hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
                "utf-8"
            )
        ).hexdigest()
    store = SQLiteScanStore(db_path)

    with pytest.raises(StorageError, match="monitor publication receipt"):
        store.persist_trade_watcher_lifecycle(
            intents=intents,
            paper_positions=positions,
            paper_fills=fills,
            signal_events=[],
            monitor_publication_receipts=[receipt],
        )

    assert not store.load_trade_intents(market_date="2026-08-27")
    assert not store.load_monitor_publication_receipts(market_date="2026-08-27")


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
                            "account_id": "alphaops_v5_simulated",
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
        assert record["columns"]["account_id"] == "alphaops_v5_simulated"
        assert record["payload_json"]["episode_id"] == "episode:legacy"

    effective_rows = store.load_trade_intents(market_date="2026-08-27", action="ENTER_LONG")
    effective_by_id = {row["intent_id"]: row for row in effective_rows}
    assert len(effective_rows) == 2
    assert effective_by_id["legacy-early"]["episode_id"] == "episode:legacy"
    assert effective_by_id["legacy-late"]["episode_id"] == ""
    assert {row["action"] for row in effective_rows} == {"ENTER_LONG"}

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
