from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from intraday_scanner.alpha.v5_policy import ALPHAOPS_V5_ACCOUNT_ID
from intraday_scanner.services.luna_research_slate_service import _watcher_current


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _row(*, portfolio_account_id: str, row_account_id: str = "") -> dict[str, object]:
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    signal_id = "signal-account-binding"
    ticker = "AAA"
    plan_hash = "a" * 64
    quote = {
        "schema_version": "dawnstrike.alphaops.quote_receipt.v1",
        "status": "USABLE",
        "signal_id": signal_id,
        "ticker": ticker,
        "plan_hash_sha256": plan_hash,
        "source": "authenticated-test-feed",
        "observed_at": checked_at,
        "bid": 10.09,
        "ask": 10.11,
        "last": 10.10,
        "entry_reference": 10.0,
        "entry_window_status": "OPEN",
        "trigger_status": "CONFIRMED",
        "research_only": True,
        "broker_execution": "disabled",
    }
    portfolio = {
        "schema_version": "dawnstrike.alphaops.portfolio_admission.v1",
        "status": "ADMITTED",
        "admitted": True,
        "blocking_reasons": [],
        "account_mode": "PAPER",
        "simulated_account_id": portfolio_account_id,
        "admission_id": "admission-account-binding",
        "admission_key": f"paper-admission:{signal_id}:{plan_hash[:16]}",
        "checked_at": checked_at,
        "signal_id": signal_id,
        "ticker": ticker,
        "plan_hash_sha256": plan_hash,
        "research_only": True,
        "broker_execution": "disabled",
    }
    proof: dict[str, object] = {
        "schema_version": "alphaops.watcher_current.v1",
        "status": "CURRENT",
        "signal_id": signal_id,
        "ticker": ticker,
        "plan_hash_sha256": plan_hash,
        "checked_at": checked_at,
        "quote_receipt": quote,
        "quote_hash_sha256": _hash(quote),
        "portfolio_receipt": portfolio,
        "portfolio_hash_sha256": _hash(portfolio),
        "research_only": True,
        "broker_execution": "disabled",
    }
    proof["proof_hash_sha256"] = _hash(proof)
    return {
        "signal_id": signal_id,
        "ticker": ticker,
        "market_date": checked_at[:10],
        "current_price": 10.10,
        "account_id": row_account_id,
        "alphaops_market_structure_plan": {
            "direction": "long",
            "entry": 10.0,
            "stop": 9.0,
            "target": 12.5,
            "plan_hash_sha256": plan_hash,
        },
        "watcher_current_proof": proof,
    }


def test_watcher_rejects_hash_valid_admission_for_wrong_simulated_account() -> None:
    assert not _watcher_current(_row(portfolio_account_id="WRONG_ACCOUNT"))


def test_watcher_requires_row_and_trace_account_to_match_v5_account() -> None:
    assert _watcher_current(
        _row(
            portfolio_account_id=ALPHAOPS_V5_ACCOUNT_ID,
            row_account_id=ALPHAOPS_V5_ACCOUNT_ID,
        )
    )
    assert not _watcher_current(
        _row(
            portfolio_account_id=ALPHAOPS_V5_ACCOUNT_ID,
            row_account_id="WRONG_ACCOUNT",
        )
    )
    trace_mismatch = _row(portfolio_account_id=ALPHAOPS_V5_ACCOUNT_ID)
    trace_mismatch["decision_trace"] = {"account_id": "WRONG_ACCOUNT"}
    assert not _watcher_current(trace_mismatch)
