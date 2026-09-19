"""DS-02a regression: execution short-circuit safety must be unchanged.

DS-02a only touches configuration resolution (intraday_scanner/config.py,
intraday_scanner/config_schema.py) - it does not touch alert_gate.py or
paper_session.py. These tests pin known-good and known-blocked outcomes for
the alert gate and the paper-session entry screen so any accidental change
to execution behavior (rather than diagnostics/config observability) is
caught.
"""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.alert_gate import PASS, evaluate_alert_gate
from intraday_scanner.execution.paper_session import _screen


def _qualified_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": "TEST",
        "premarket_price": 5.0,
        "price": 5.0,
        "premarket_high": 5.2,
        "premarket_low": 4.6,
        "premarket_volume": 900_000,
        "previous_close": 4.0,
        "entry_watch_level": 5.2,
        "breakout_trigger": 5.2,
        "target_1": 6.4,
        "invalidation_level": 4.6,
        "reward_risk_ratio": 2.0,
        "spread_pct": 0.4,
        "gap_pct": 25.0,
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "source_quality_status": "CLEAR",
        "setup_grade": "A",
        "edge_bucket": "HIGH",
        "confidence_bucket": "HIGH",
        "data_quality_score": 90.0,
        "catalyst_type": "confirmed_catalyst",
        "catalyst_summary": "confirmed FDA approval",
        "catalyst_category": "regulatory",
        "catalyst_confidence": 0.8,
        "float_shares": 5_000_000,
        "source_confidence": 90.0,
        "source_count": 2,
        "risk_flags": "",
    }
    row.update(overrides)
    return row


def test_alert_gate_passes_an_otherwise_qualified_row():
    gate = evaluate_alert_gate(_qualified_row())
    assert gate["alert_gate_status"] == PASS
    assert gate["manual_confirmation_required"] is False


def test_alert_gate_still_blocks_on_stale_data():
    gate = evaluate_alert_gate(_qualified_row(stale_data_flag=True))
    assert gate["alert_gate_status"] != PASS
    assert any("stale" in reason for reason in gate["alert_gate_reasons"])


def test_alert_gate_still_blocks_on_missing_price():
    gate = evaluate_alert_gate(_qualified_row(premarket_price=None, price=None))
    assert gate["alert_gate_status"] != PASS
    assert "missing price" in gate["alert_gate_reasons"]


def test_paper_session_screen_refuses_when_gate_cannot_alert():
    candidate = {"can_alert": False, "payload": {}}
    assert _screen(candidate) == "gate_can_alert_false"


def test_paper_session_screen_refuses_when_gate_status_not_entry_eligible():
    candidate = {
        "can_alert": True,
        "payload": {"alert_gate_status": "BLOCKED"},
    }
    refusal = _screen(candidate)
    assert refusal is not None
    assert refusal.startswith("gate_status_")


def test_paper_session_screen_refuses_without_receipt_paper_eligibility():
    candidate = {
        "can_alert": True,
        "payload": {
            "alert_gate_status": "PASS",
            "strategy_receipt_paper_entry_eligible": False,
        },
    }
    assert _screen(candidate) == "receipt_not_paper_entry_eligible"
