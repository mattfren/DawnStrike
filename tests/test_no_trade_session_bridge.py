from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.alpha.commit_bridge import (
    NoTradeBridge,
    NoTradeIdentity,
    has_authenticated_no_trade_receipt,
)
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _receipt() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "dawnstrike.session.no_trade.v1",
        "receipt_id": "session:no-trade:1",
        "account_id": "paper-total",
        "strategy_id": "aggregate",
        "strategy_version": "aggregate.v1",
        "experiment_id": "exp-1",
        "arm_id": "control",
        "market_date": "2026-08-28",
        "session_id": "XNYS:2026-08-28",
        "run_id": "run-1",
        "status": "FINALIZED",
        "decision": "NO_TRADE",
        "no_entry": True,
        "source_artifact_hash_sha256": "a" * 64,
        "source_config_hash_sha256": "b" * 64,
        "calendar_source_hash_sha256": "c" * 64,
        "code_sha": "d" * 40,
        "created_at": "2026-08-28T21:00:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return payload


def _store(tmp_path: Path, receipt: dict[str, object]) -> SQLiteScanStore:
    store = SQLiteScanStore(tmp_path / "session.sqlite")
    assert store.persist_no_trade_session_receipt(receipt) is True
    return store


def test_no_trade_bridge_requires_exact_persisted_identity(tmp_path: Path) -> None:
    receipt = _receipt()
    store = _store(tmp_path, receipt)
    bridge = NoTradeBridge(store)
    identity = NoTradeIdentity(
        account_id="paper-total",
        market_date="2026-08-28",
        session_id="XNYS:2026-08-28",
        run_id="run-1",
        strategy_id="aggregate",
        strategy_version="aggregate.v1",
        experiment_id="exp-1",
        arm_id="control",
    )
    result = bridge.resolve(
        str(receipt["receipt_id"]),
        identity=identity,
        expected_code_sha="d" * 40,
        expected_source_artifact_hash="a" * 64,
        expected_source_config_hash="b" * 64,
        expected_calendar_source_hash="c" * 64,
    )
    assert result is not None
    assert has_authenticated_no_trade_receipt(result)
    assert not has_authenticated_no_trade_receipt(dict(receipt))
    assert (
        bridge.resolve(
            str(receipt["receipt_id"]),
            identity=NoTradeIdentity(session_id="wrong"),
        )
        is None
    )


def test_no_trade_bridge_rejects_payload_or_column_tamper(tmp_path: Path) -> None:
    receipt = _receipt()
    store = _store(tmp_path, receipt)
    with store._connect() as connection:
        connection.execute("DROP TRIGGER no_trade_session_receipts_no_update")
        connection.execute(
            "UPDATE no_trade_session_receipts SET session_id = ? WHERE receipt_id = ?",
            ("wrong", receipt["receipt_id"]),
        )
    assert NoTradeBridge(store).resolve(str(receipt["receipt_id"])) is None


def test_no_trade_storage_is_append_only_and_hash_bound(tmp_path: Path) -> None:
    receipt = _receipt()
    store = _store(tmp_path, receipt)
    assert store.persist_no_trade_session_receipt(receipt) is False
    bad = dict(receipt)
    bad["decision"] = "TRADE"
    with pytest.raises(StorageError, match="hash mismatch"):
        store.persist_no_trade_session_receipt(bad)
    with store._connect() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "DELETE FROM no_trade_session_receipts WHERE receipt_id = ?",
                (receipt["receipt_id"],),
            )
