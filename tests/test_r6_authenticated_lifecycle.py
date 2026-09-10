from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

from intraday_scanner.decisioning.contracts import StrategyDecisionReceipt, canonical_json
from intraday_scanner.execution.r6_lifecycle import (
    AuthenticatedEntryIntent,
    CanonicalLedger,
    FakeBroker,
    R6Lifecycle,
    ReceiptAuthenticationError,
    ReceiptContext,
    authenticate_entry_intent,
)

SOURCE = "alpha-feed:fixture-session-1"
CONFIG = "a" * 64
CODE = "1" * 40
PLAN = "b" * 64
DAY = "2026-09-09"


def _row(*, decision_at: str = "2026-09-09T16:00:00+00:00") -> tuple[dict, ReceiptContext]:
    input_payload = {
        "account_id": "acct-r6",
        "host_id": "host-r6",
        "market_date": DAY,
        "plan_hash_sha256": PLAN,
        "source_config_hash_sha256": CONFIG,
        "source_identity": SOURCE,
        "source_signal_id": "signal-r6-1",
    }
    input_text = canonical_json(input_payload)
    receipt = StrategyDecisionReceipt(
        schema_version="dawnstrike.strategy_decision_receipt.v2",
        receipt_id="",
        strategy_id="r6_fixture_strategy",
        strategy_version="r6-v1",
        symbol="TEST",
        market_date=DAY,
        decision_at=decision_at,
        code_sha=CODE,
        policy_version="r6-fixture-policy-v1",
        condition_results=(),
        first_blocking_failure=None,
        all_blocking_failures=(),
        disclosed_gaps=(),
        research_pick_eligible=True,
        paper_entry_eligible=True,
        pick_tier="QUALIFIED_PICK",
        base_strategy_score=95.0,
        score_adjustment=0.0,
        final_score=95.0,
        entry_reference=10.0,
        stop=9.0,
        target=13.0,
        reward_risk_ratio=3.0,
        source_identity=SOURCE,
        input_hash_sha256=hashlib.sha256(input_text.encode()).hexdigest(),
        input_payload_json=input_text,
        plan_hash_sha256=PLAN,
    )
    row = {
        "ticker": "TEST",
        "strategy_id": receipt.strategy_id,
        "strategy_version": receipt.strategy_version,
        "market_date": DAY,
        "source_identity": SOURCE,
        "source_config_hash_sha256": CONFIG,
        "account_id": "acct-r6",
        "host_id": "host-r6",
        "plan_hash_sha256": PLAN,
        "source_signal_id": "signal-r6-1",
        "strategy_decision_receipt": receipt.to_dict(),
        "receipt_id": receipt.receipt_id,
        "receipt_hash_sha256": receipt.receipt_hash_sha256,
        "strategy_receipt_enabled": True,
        "strategy_receipt_construction_status": "COMPLETE",
        "strategy_receipt_persistence_status": "PERSISTED",
        "strategy_receipt_tier": "QUALIFIED_PICK",
        "strategy_receipt_research_pick_eligible": True,
        "strategy_receipt_paper_entry_eligible": True,
        "strategy_receipt_legacy_can_alert": True,
        "source_confidence": 95,
        "price": 10.0,
        "premarket_price": 10.0,
        "entry_trigger": 10.0,
        "target_1": 13.0,
        "invalidation_level": 9.0,
        "reward_risk_ratio": 3.0,
        "premarket_high": 10.2,
        "premarket_low": 9.8,
        "catalyst_summary": "verified product event",
        "volume": 1000000,
        "spread_pct": 1.0,
        "previous_close": 9.5,
        "float_shares": 10000000,
        "source_count": 2,
        "catalyst_confidence": 0.9,
        "confidence_bucket": "HIGH",
        "edge_bucket": "HIGH",
        "setup_grade": "A",
        "data_quality_score": 95,
        "gap_pct": 5.0,
        "max_credible_gap_pct": 50.0,
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "source_quality_status": "CLEAR",
        "r6_risk_inputs": {
            "equity": "100000.00",
            "cash": "100000.00",
            "open_positions": 0,
            "entries_today": 0,
            "day_loss_pct": 0,
            "ticker_notional": 0,
            "correlated_positions": 0,
            "halt_status": "CLEAR",
            "corporate_action_status": "CLEAR",
            "sec_risk_status": "CLEAR",
            "source_quality_status": "CLEAR",
            "spread_bps": 50,
            "session_status": "CLEAR",
        },
    }
    return row, ReceiptContext(SOURCE, CONFIG, DAY, "acct-r6", "host-r6", CODE, PLAN)


def test_authenticated_entry_reaches_fake_broker_and_reconciles_partial_full_exit(
    tmp_path: Path,
) -> None:
    row, context = _row()
    intent = authenticate_entry_intent(row, context)
    assert isinstance(intent, AuthenticatedEntryIntent)
    assert intent.quantity == 250  # strict V5 0.25% cap binds the 0.5% runtime default
    ledger = CanonicalLedger(tmp_path / "ledger.sqlite", account_id="acct-r6")
    broker = FakeBroker()
    lifecycle = R6Lifecycle(broker, ledger)
    partial = lifecycle.enter(intent, fills=((100, "10.00"),))
    assert partial["status"] == "ENTRY_PARTIAL"
    assert ledger.position("TEST")["quantity"] == 100
    broker.fill_remaining(intent.intent_id, price="10.05", market_date=DAY)
    assert lifecycle.enter(intent)["status"] == "DEDUPLICATED"
    assert ledger.position("TEST")["quantity"] == 250
    exited = lifecycle.exit(
        symbol="TEST", market_date=DAY, quantity=250, price="11.00", client_order_id="exit-1"
    )
    assert exited["status"] == "EXIT_FILLED"
    assert ledger.position("TEST") is None
    assert ledger.cash() == pytest.approx(Decimal("100237.24"))


def test_restart_recovery_and_duplicate_fill_are_idempotent(tmp_path: Path) -> None:
    row, context = _row()
    intent = authenticate_entry_intent(row, context)
    ledger = CanonicalLedger(tmp_path / "ledger.sqlite", account_id="acct-r6")
    broker = FakeBroker()
    first = R6Lifecycle(broker, ledger).enter(intent, fills=((250, "10.00"),))
    assert first["status"] == "ENTRY_FILLED"
    ledger.close()
    restarted = CanonicalLedger(tmp_path / "ledger.sqlite", account_id="acct-r6")
    assert R6Lifecycle(broker, restarted).enter(intent)["status"] == "DEDUPLICATED"
    assert restarted.position("TEST")["quantity"] == 250


def test_ambiguous_ack_reconciles_by_client_id_without_retry(tmp_path: Path) -> None:
    row, context = _row()
    intent = authenticate_entry_intent(row, context)
    ledger = CanonicalLedger(tmp_path / "ledger.sqlite", account_id="acct-r6")
    broker = FakeBroker()
    broker.ambiguous_client_ids.add(intent.intent_id)
    result = R6Lifecycle(broker, ledger).enter(intent, fills=((250, "10.00"),))
    assert result["status"] == "RECONCILED_AFTER_AMBIGUOUS_ACK"
    assert len(broker.orders) == 1
    assert ledger.position("TEST")["quantity"] == 250


@pytest.mark.parametrize(
    "field",
    [
        "source_identity",
        "source_config_hash_sha256",
        "market_date",
        "account_id",
        "host_id",
        "plan_hash_sha256",
    ],
)
def test_wrong_bound_identity_rejects(field: str) -> None:
    row, context = _row()
    values = (
        context.__dict__
        if hasattr(context, "__dict__")
        else {name: getattr(context, name) for name in context.__dataclass_fields__}
    )
    values[field] = "wrong" if field != "market_date" else "2026-09-08"
    with pytest.raises(ReceiptAuthenticationError, match="identity mismatch"):
        authenticate_entry_intent(row, ReceiptContext(**values))


def test_forged_receipt_rejects_without_hash_rewrite() -> None:
    row, context = _row()
    row["strategy_decision_receipt"]["target"] = 99.0
    with pytest.raises(ReceiptAuthenticationError):
        authenticate_entry_intent(row, context)


def test_entry_kill_switch_does_not_block_existing_exit(tmp_path: Path) -> None:
    row, context = _row()
    intent = authenticate_entry_intent(row, context)
    ledger = CanonicalLedger(tmp_path / "ledger.sqlite", account_id="acct-r6")
    broker = FakeBroker()
    lifecycle = R6Lifecycle(broker, ledger)
    lifecycle.enter(intent, fills=((250, "10.00"),))
    broker.entry_enabled = False
    assert lifecycle.enter(intent)["status"] == "DEDUPLICATED"
    assert (
        lifecycle.exit(
            symbol="TEST", market_date=DAY, quantity=250, price="10.50", client_order_id="exit-kill"
        )["status"]
        == "EXIT_FILLED"
    )


def test_close_rejection_preserves_unresolved_position(tmp_path: Path) -> None:
    row, context = _row()
    intent = authenticate_entry_intent(row, context)
    ledger = CanonicalLedger(tmp_path / "ledger.sqlite", account_id="acct-r6")
    broker = FakeBroker()
    lifecycle = R6Lifecycle(broker, ledger)
    lifecycle.enter(intent, fills=((250, "10.00"),))
    broker.reject_exits = True
    result = lifecycle.manage_before_close(
        symbol="TEST",
        market_date=DAY,
        close_at="2026-09-09T20:00:00+00:00",
        now="2026-09-09T19:51:00+00:00",
        price="10.50",
    )
    assert result["status"] == "EXIT_UNRESOLVED"
    assert result["position"]["quantity"] == 250


def test_cutoff_is_exclusive_and_deadline_rejects() -> None:
    row, context = _row(decision_at="2026-09-09T19:30:00+00:00")
    with pytest.raises(ReceiptAuthenticationError, match="cutoff"):
        authenticate_entry_intent(row, context)


def test_close_management_uses_ten_minute_deadline(tmp_path: Path) -> None:
    row, context = _row()
    intent = authenticate_entry_intent(row, context)
    ledger = CanonicalLedger(tmp_path / "ledger.sqlite", account_id="acct-r6")
    broker = FakeBroker()
    lifecycle = R6Lifecycle(broker, ledger)
    lifecycle.enter(intent, fills=((250, "10.00"),))
    held = lifecycle.manage_before_close(
        symbol="TEST",
        market_date=DAY,
        close_at="2026-09-09T20:00:00+00:00",
        now="2026-09-09T19:40:00+00:00",
        price="10.50",
    )
    assert held["status"] == "HELD_UNTIL_EXIT_RULE"
