from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.alpha.commit_bridge import (
    AuthenticatedFillTruth,
    CommitBridge,
    FillTruthIdentity,
)
from intraday_scanner.alpha.fill_truth import has_authenticated_committed_fill_truth
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _receipt() -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": "dawnstrike.filltruth.commit.v1",
        "receipt_id": "filltruth:run-1:fill-1",
        "account_id": "paper-account",
        "strategy_id": "alphaops_v6",
        "strategy_version": "v6.1.0",
        "symbol": "SPY",
        "market_date": "2026-08-28",
        "run_id": "run-1",
        "session_id": "session-1",
        "fill_id": "fill-1",
        "order_id": "order-1",
        "position_id": "position-1",
        "execution_status": "CLOSED",
        "fill_truth_status": "COMMITTED",
        "committed": True,
        "entry_at": "2026-08-28T14:31:00+00:00",
        "exit_at": "2026-08-28T15:31:00+00:00",
        "quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 101.0,
        "spread_cost_cents": 2,
        "slippage_cost_cents": 3,
        "fees_cents": 1,
        "regulatory_cost_cents": 0,
        "borrow_cost_cents": 0,
        "source_artifact_hash_sha256": "a" * 64,
        "code_sha": "b" * 40,
        "frozen_window": "2026-08-28T14:30:00+00:00/2026-08-28T20:00:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
        "created_at": "2026-08-28T21:00:00+00:00",
    }
    receipt["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(receipt).encode("utf-8")
    ).hexdigest()
    return receipt


def _store(tmp_path: Path, receipt: dict[str, object]) -> SQLiteScanStore:
    store = SQLiteScanStore(tmp_path / "filltruth.sqlite")
    assert store.persist_committed_fill_truth_receipt(receipt) is True
    return store


def test_exact_persisted_filltruth_resolves_as_immutable_authenticated_object(
    tmp_path: Path,
) -> None:
    receipt = _receipt()
    store = _store(tmp_path, receipt)
    result = CommitBridge(store).resolve(
        str(receipt["receipt_id"]),
        identity=FillTruthIdentity(
            account_id="paper-account",
            market_date="2026-08-28",
            strategy_id="alphaops_v6",
            strategy_version="v6.1.0",
            symbol="SPY",
            run_id="run-1",
            session_id="session-1",
            fill_id="fill-1",
        ),
        expected_code_sha="b" * 40,
        expected_source_artifact_hash="a" * 64,
    )
    assert result is not None
    assert has_authenticated_committed_fill_truth(result)
    assert not has_authenticated_committed_fill_truth(AuthenticatedFillTruth())
    forged_instance = object.__new__(AuthenticatedFillTruth)
    assert not has_authenticated_committed_fill_truth(forged_instance)
    assert result["exit_price"] == 101.0
    with pytest.raises(TypeError):
        result["exit_price"] = 99.0  # type: ignore[index]


@pytest.mark.parametrize(
    ("mutation",),
    [
        ("dict",),
        ("missing",),
        ("identity",),
        ("run",),
        ("sha",),
        ("status",),
    ],
)
def test_commit_bridge_fail_closed_for_untrusted_or_wrong_identity(
    tmp_path: Path, mutation: str
) -> None:
    receipt = _receipt()
    store = _store(tmp_path, receipt)
    bridge = CommitBridge(store)
    identity = FillTruthIdentity(
        account_id="paper-account",
        market_date="2026-08-28",
        strategy_id="alphaops_v6",
        strategy_version="v6.1.0",
        symbol="SPY",
        run_id="run-1",
        fill_id="fill-1",
    )
    if mutation == "dict":
        forged = {**receipt, "fill_truth_contract_verified": True}
        assert not has_authenticated_committed_fill_truth(forged)
        return
    if mutation == "missing":
        assert bridge.resolve("filltruth:does-not-exist", identity=identity) is None
        return
    if mutation == "identity":
        identity = FillTruthIdentity(**{**identity.to_dict(), "symbol": "IWM"})
        assert bridge.resolve(str(receipt["receipt_id"]), identity=identity) is None
        return
    if mutation == "run":
        assert bridge.resolve(
            str(receipt["receipt_id"]), identity=identity, expected_run_id="run-2"
        ) is None
        return
    if mutation == "sha":
        assert bridge.resolve(
            str(receipt["receipt_id"]), identity=identity, expected_code_sha="c" * 40
        ) is None
        return
    assert mutation == "status"
    with store._connect() as connection:
        connection.execute("DROP TRIGGER committed_fill_truth_receipts_no_update")
        connection.execute(
            "UPDATE committed_fill_truth_receipts SET execution_status = ? WHERE receipt_id = ?",
            ("PENDING", receipt["receipt_id"]),
        )
    assert bridge.resolve(str(receipt["receipt_id"]), identity=identity) is None


def test_persist_rejects_hash_tamper_and_provisional_status(tmp_path: Path) -> None:
    receipt = _receipt()
    receipt["receipt_hash_sha256"] = "0" * 64
    with pytest.raises(StorageError, match="hash mismatch"):
        SQLiteScanStore(tmp_path / "hash.sqlite").persist_committed_fill_truth_receipt(receipt)

    provisional = _receipt()
    provisional["execution_status"] = "FILLED"
    provisional["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(
            {key: value for key, value in provisional.items() if key != "receipt_hash_sha256"}
        ).encode("utf-8")
    ).hexdigest()
    store = SQLiteScanStore(tmp_path / "provisional.sqlite")
    assert store.persist_committed_fill_truth_receipt(provisional) is True
    assert CommitBridge(store).resolve(str(provisional["receipt_id"])) is None


def test_database_payload_tamper_is_detected_even_when_json_remains_valid(tmp_path: Path) -> None:
    receipt = _receipt()
    store = _store(tmp_path, receipt)
    with store._connect() as connection:
        payload = dict(receipt)
        payload["exit_price"] = 999.0
        connection.execute("DROP TRIGGER committed_fill_truth_receipts_no_update")
        connection.execute(
            "UPDATE committed_fill_truth_receipts SET payload_json = ? WHERE receipt_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), receipt["receipt_id"]),
        )
    assert CommitBridge(store).resolve(str(receipt["receipt_id"])) is None


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("experiment_id", "other-experiment"),
        ("quantity", 999.0),
        ("spread_cost_cents", 999),
        ("created_at", "2026-08-29T21:00:00+00:00"),
    ],
)
def test_every_persisted_identity_and_cost_column_is_bound_to_payload(
    tmp_path: Path, column: str, replacement: object
) -> None:
    receipt = _receipt()
    receipt["experiment_id"] = "experiment-1"
    receipt["arm_id"] = "control"
    receipt["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(
            {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
        ).encode("utf-8")
    ).hexdigest()
    store = _store(tmp_path, receipt)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute("DROP TRIGGER committed_fill_truth_receipts_no_update")
        connection.execute(
            f"UPDATE committed_fill_truth_receipts SET {column} = ? WHERE receipt_id = ?",  # nosec B608
            (replacement, receipt["receipt_id"]),
        )
    assert CommitBridge(store).resolve(str(receipt["receipt_id"])) is None
