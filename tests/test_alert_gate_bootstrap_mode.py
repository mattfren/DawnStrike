"""Bootstrap paper mode must unblock the closed loop without waiving safety.

The live database was found in a state where 25 forward sessions produced zero
trades.  One cause was a closed loop: ``confidence_bucket`` stays
INSUFFICIENT_SAMPLE until 20 real outcome days exist, outcome days only accrue
from entries, and entries require passing this gate.

Bootstrap mode breaks that loop.  These tests pin the two properties that make
it safe to do so: it is OFF unless explicitly enabled, and it can never waive a
safety gate.
"""

from __future__ import annotations

from typing import Any

import pytest

from intraday_scanner.alpha import alert_gate
from intraday_scanner.alpha.alert_gate import (
    BOOTSTRAP_MODE_ENV,
    BOOTSTRAP_NEVER_WAIVED,
    BOOTSTRAP_WAIVABLE_EDGE_REASONS,
    BOOTSTRAP_WAIVABLE_MISSING,
    BOOTSTRAP_WAIVABLE_REASONS,
    BOOTSTRAP_WAIVABLE_WARNINGS,
    evaluate_alert_gate,
)

SAFETY_GATES = (
    "halt status not checked",
    "halt status is not verified clear",
    "SEC risk not checked",
    "SEC risk status is not verified clear",
    "extreme spread",
    "missing price",
    "missing volume",
    "data quality below alert threshold",
    "gap regime outside alert policy",
    "stop distance exceeds alert policy",
)


def _row(**overrides: Any) -> dict[str, Any]:
    """A candidate whose only problems are the closed-loop evidence gates."""

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
        "corporate_action_status": "UNKNOWN",
        "source_quality_status": "LIMITED",
        "setup_grade": "A",
        "edge_bucket": "LOW",
        "confidence_bucket": "INSUFFICIENT_SAMPLE",
        "data_quality_score": 90.0,
        "catalyst_type": "confirmed_catalyst",
        "catalyst_confidence": 0.8,
        "float_shares": None,
        "risk_flags": "unknown_float",
    }
    row.update(overrides)
    return row


def test_bootstrap_mode_is_off_unless_explicitly_enabled(monkeypatch) -> None:
    monkeypatch.delenv(BOOTSTRAP_MODE_ENV, raising=False)
    assert alert_gate._bootstrap_paper_mode_enabled() is False

    gate = evaluate_alert_gate(_row())
    assert gate["bootstrap_mode"] is False
    assert gate["bootstrap_waived_reasons"] == []


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_only_truthy_values_enable_bootstrap(monkeypatch, value: str) -> None:
    monkeypatch.setenv(BOOTSTRAP_MODE_ENV, value)
    assert alert_gate._bootstrap_paper_mode_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "y", "on"])
def test_truthy_values_enable_bootstrap(monkeypatch, value: str) -> None:
    monkeypatch.setenv(BOOTSTRAP_MODE_ENV, value)
    assert alert_gate._bootstrap_paper_mode_enabled() is True


def test_bootstrap_waives_the_closed_loop_gates_and_records_them(monkeypatch) -> None:
    monkeypatch.delenv(BOOTSTRAP_MODE_ENV, raising=False)
    before = evaluate_alert_gate(_row())

    monkeypatch.setenv(BOOTSTRAP_MODE_ENV, "true")
    after = evaluate_alert_gate(_row())

    assert after["bootstrap_mode"] is True
    # The waiver must strictly reduce the blocking set.
    assert len(after["alert_gate_reasons"]) < len(before["alert_gate_reasons"])
    # Nothing is discarded: every reason that disappeared is on the record.
    disappeared = set(before["alert_gate_reasons"]) - set(after["alert_gate_reasons"])
    assert disappeared
    assert disappeared.issubset(set(after["bootstrap_waived_reasons"]))
    # The specific closed-loop gate that made bootstrapping impossible.
    assert "not enough history yet" in after["bootstrap_waived_reasons"]


@pytest.mark.parametrize("safety_gate", SAFETY_GATES)
def test_bootstrap_never_waives_a_safety_gate(safety_gate: str) -> None:
    """No safety string may appear in any waivable set."""

    waivable = (
        BOOTSTRAP_WAIVABLE_REASONS
        | BOOTSTRAP_WAIVABLE_WARNINGS
        | BOOTSTRAP_WAIVABLE_MISSING
        | BOOTSTRAP_WAIVABLE_EDGE_REASONS
    )
    assert safety_gate not in waivable
    assert safety_gate in BOOTSTRAP_NEVER_WAIVED


def test_bootstrap_still_blocks_a_halted_symbol(monkeypatch) -> None:
    monkeypatch.setenv(BOOTSTRAP_MODE_ENV, "true")

    gate = evaluate_alert_gate(_row(halt_status="HALTED", risk_flags="current_halt"))

    assert gate["official_paper_gate_passed"] is False
    assert any("halt" in reason.lower() for reason in gate["alert_gate_reasons"])


def test_bootstrap_still_blocks_unverified_sec_risk(monkeypatch) -> None:
    monkeypatch.setenv(BOOTSTRAP_MODE_ENV, "true")

    gate = evaluate_alert_gate(
        _row(sec_risk_status="UNKNOWN", risk_flags="sec_risk_unverified")
    )

    assert gate["official_paper_gate_passed"] is False
    assert any("SEC" in reason for reason in gate["alert_gate_reasons"])


def test_bootstrap_does_not_waive_setup_grade_or_reward_risk() -> None:
    """Genuine setup judgements are never bootstrap-waivable."""

    waivable = (
        BOOTSTRAP_WAIVABLE_REASONS
        | BOOTSTRAP_WAIVABLE_WARNINGS
        | BOOTSTRAP_WAIVABLE_MISSING
        | BOOTSTRAP_WAIVABLE_EDGE_REASONS
    )
    assert "setup grade below alert threshold" not in waivable
    assert not any(reason.startswith("reward/risk below") for reason in waivable)
