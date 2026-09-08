"""Deterministic tests for the signal-to-order driver.

These cover the decisions the driver makes on its own: which candidates may
reach the broker, how a zero-trade session explains itself, and the automatic
end-of-session flatten. The broker itself is a stand-in here; live integration
is proven separately against the real paper API.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.execution.paper_broker import _order
from intraday_scanner.execution.paper_session import (
    FLATTEN_MINUTES_BEFORE_CLOSE,
    load_candidates,
    minutes_to_close,
    run_paper_session,
)
from intraday_scanner.execution.risk_gate import RiskSettings

PAPER_ACCOUNT = {
    "account_number": "PA3XXXXXXXZL",
    "equity": "100000",
    "cash": "100000",
    "last_equity": "100000",
    "long_market_value": "0",
    "trading_blocked": False,
    "account_blocked": False,
}


def _signal(**over) -> dict:
    """A payload shaped like the real ones in alpha_signals."""

    payload = {
        "alert_gate_status": "PASS",
        "strategy_receipt_paper_entry_eligible": True,
        "entry_trigger": "$10.0500",
        "invalidation_level": 9.55,
        "target_1": "$11.6180",
        "strategy_version": "alphaops-v5",
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(over)
    return payload


def _db(tmp_path: Path, rows: list[tuple[str, bool, dict]], date: str = "2026-09-08") -> Path:
    path = tmp_path / "signals.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE alpha_signals (ticker TEXT, timestamp TEXT, alpha_score REAL, "
        "can_alert INTEGER, no_trade_reason TEXT, payload_json TEXT)"
    )
    for index, (ticker, can_alert, payload) in enumerate(rows):
        conn.execute(
            "INSERT INTO alpha_signals VALUES (?,?,?,?,?,?)",
            (
                ticker,
                f"{date}T13:35:00+00:00",
                100 - index,
                1 if can_alert else 0,
                "",
                json.dumps(payload),
            ),
        )
    conn.commit()
    conn.close()
    return path


class StubBroker:
    def __init__(self, *, is_open=True, minutes_left=120.0, positions=None):
        self._open = is_open
        self._closes = datetime.now(timezone.utc) + timedelta(minutes=minutes_left)
        self.positions = list(positions or [])
        self.submitted: list[dict] = []
        self.closed: list[str] = []

    def assert_paper_account(self):
        return PAPER_ACCOUNT

    def get_clock(self):
        return {
            "is_open": self._open,
            "next_open": "2026-09-09T09:30:00-04:00",
            "next_close": self._closes.isoformat(),
        }

    def get_positions(self):
        return list(self.positions)

    def get_orders_since(self, _after):
        return []

    def get_open_orders(self):
        return []

    def find_by_client_order_id(self, _coid):
        return None

    def submit_bracket_buy(self, **kw):
        self.submitted.append(kw)
        return _order(
            {
                "id": f"brk-{len(self.submitted)}",
                "client_order_id": kw["client_order_id"],
                "symbol": kw["symbol"],
                "side": "buy",
                "qty": kw["qty"],
                "filled_qty": 0,
                "status": "new",
                "order_class": "bracket",
            }
        )

    def close_position(self, symbol):
        self.closed.append(symbol)
        self.positions = [p for p in self.positions if p.get("symbol") != symbol]
        return _order(
            {"id": f"cls-{symbol}", "client_order_id": f"close-{symbol}", "symbol": symbol,
             "side": "sell", "qty": 1, "filled_qty": 1, "status": "filled",
             "order_class": "simple"}
        )


def _run(tmp_path, db, broker, **kw):
    return run_paper_session(
        db_path=db,
        market_date="2026-09-08",
        store_path=tmp_path / "exec.sqlite",
        receipt_path=tmp_path / "receipt.json",
        settings=RiskSettings(),
        client=broker,
        **kw,
    )


def _enable(monkeypatch):
    monkeypatch.setenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", "true")


# --------------------------------------------------------------------------
# screening: only what the gate approved may reach the broker
# --------------------------------------------------------------------------


def test_a_gate_approved_signal_reaches_the_broker(monkeypatch, tmp_path):
    _enable(monkeypatch)
    db = _db(tmp_path, [("AAA", True, _signal())])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker)

    assert receipt["status"] == "completed"
    assert len(broker.submitted) == 1
    sent = broker.submitted[0]
    assert sent["symbol"] == "AAA"
    assert sent["limit_price"] == 10.05
    assert sent["stop_loss"] == 9.55
    assert sent["take_profit"] == 11.618
    assert receipt["funnel"]["entry_submitted"] == 1


@pytest.mark.parametrize(
    "can_alert,over,expected",
    [
        (False, {}, "gate_can_alert_false"),
        (True, {"alert_gate_status": "BLOCKED"}, "gate_status_blocked"),
        (True, {"alert_gate_status": "WATCH_ONLY"}, "gate_status_watch_only"),
        (True, {"alert_gate_status": ""}, "gate_status_missing"),
        (True, {"strategy_receipt_paper_entry_eligible": None}, "receipt_not_paper_entry_eligible"),
        (
            True,
            {"strategy_receipt_paper_entry_eligible": False},
            "receipt_not_paper_entry_eligible",
        ),
        (True, {"target_1": None, "first_target": None}, "incomplete_plan_levels"),
    ],
)
def test_the_funnel_names_why_a_candidate_was_refused(
    monkeypatch, tmp_path, can_alert, over, expected
):
    _enable(monkeypatch)
    db = _db(tmp_path, [("AAA", can_alert, _signal(**over))])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker)

    assert broker.submitted == []
    assert receipt["funnel"][expected] == 1


def test_a_zero_trade_session_is_a_success_that_explains_itself(monkeypatch, tmp_path):
    """The whole point of the funnel: no trade is not the same as no answer."""

    _enable(monkeypatch)
    db = _db(
        tmp_path,
        [
            ("AAA", False, _signal()),
            ("BBB", True, _signal(alert_gate_status="BLOCKED")),
            ("CCC", True, _signal(strategy_receipt_paper_entry_eligible=None)),
        ],
    )
    receipt = _run(tmp_path, db, StubBroker())

    assert receipt["status"] == "completed"
    assert receipt["funnel"] == {
        "candidates_in_database": 3,
        "gate_can_alert_false": 1,
        "gate_status_blocked": 1,
        "receipt_not_paper_entry_eligible": 1,
    }


def test_a_stale_observation_is_refused_by_the_risk_gate(monkeypatch, tmp_path):
    _enable(monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    db = _db(tmp_path, [("AAA", True, _signal(observed_at=old))])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker)

    assert broker.submitted == []
    assert receipt["funnel"]["entry_stale_market_data"] == 1


def test_an_observation_with_no_timestamp_fails_closed(monkeypatch, tmp_path):
    """A missing stamp must not silently disable the staleness interlock."""

    _enable(monkeypatch)
    payload = _signal()
    del payload["observed_at"]
    db = _db(tmp_path, [("AAA", True, payload)])
    # No usable row timestamp either.
    path = tmp_path / "signals.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("UPDATE alpha_signals SET timestamp = '2026-09-08'")
    conn.commit()
    conn.close()

    broker = StubBroker()
    receipt = _run(tmp_path, db, broker)
    assert broker.submitted == []
    assert receipt["funnel"]["entry_stale_market_data"] == 1


# --------------------------------------------------------------------------
# the end-of-session flatten
# --------------------------------------------------------------------------


def test_minutes_to_close_is_none_when_the_market_is_shut():
    shut = {"market_open": False, "next_close": "x"}
    assert minutes_to_close(shut, datetime.now(timezone.utc)) is None


def test_positions_are_flattened_automatically_near_the_bell(monkeypatch, tmp_path):
    """Nothing is carried overnight, and the operator never has to remember."""

    _enable(monkeypatch)
    db = _db(tmp_path, [])
    broker = StubBroker(
        minutes_left=FLATTEN_MINUTES_BEFORE_CLOSE - 5,
        positions=[{"symbol": "AAA", "qty": "500"}],
    )
    receipt = _run(tmp_path, db, broker)

    assert receipt["flatten_triggered"] is True
    assert broker.closed == ["AAA"]
    assert receipt["management"]["actions"][0]["action"] == "time_exit_submitted"


def test_positions_are_held_mid_session(monkeypatch, tmp_path):
    """Mid-session the broker's own bracket manages the position."""

    _enable(monkeypatch)
    db = _db(tmp_path, [])
    broker = StubBroker(minutes_left=180.0, positions=[{"symbol": "AAA", "qty": "500"}])
    receipt = _run(tmp_path, db, broker)

    assert receipt["flatten_triggered"] is False
    assert broker.closed == []
    assert receipt["management"]["actions"][0]["action"] == "held_broker_bracket"


def test_no_new_entry_is_opened_into_the_flatten_window(monkeypatch, tmp_path):
    """Opening minutes before the bell would be closed again for nothing."""

    _enable(monkeypatch)
    db = _db(tmp_path, [("AAA", True, _signal())])
    broker = StubBroker(minutes_left=FLATTEN_MINUTES_BEFORE_CLOSE - 1)
    receipt = _run(tmp_path, db, broker)

    assert broker.submitted == []
    assert receipt["funnel"]["too_close_to_the_bell"] == 1


def test_nothing_is_submitted_while_the_market_is_closed(monkeypatch, tmp_path):
    _enable(monkeypatch)
    db = _db(tmp_path, [("AAA", True, _signal())])
    broker = StubBroker(is_open=False)
    receipt = _run(tmp_path, db, broker)

    assert broker.submitted == []
    assert receipt["funnel"]["market_closed"] == 1


def test_management_still_runs_while_the_market_is_closed(monkeypatch, tmp_path):
    """A closed market must not strand a position the driver can still see."""

    _enable(monkeypatch)
    broker = StubBroker(is_open=False, positions=[{"symbol": "AAA", "qty": "500"}])
    receipt = _run(tmp_path, _db(tmp_path, []), broker)
    assert receipt["management"]["positions"] == 1


# --------------------------------------------------------------------------
# receipt and provenance
# --------------------------------------------------------------------------


def test_the_receipt_states_the_paper_boundary(monkeypatch, tmp_path):
    _enable(monkeypatch)
    receipt = _run(tmp_path, _db(tmp_path, []), StubBroker())
    assert receipt["broker_endpoint"] == "https://paper-api.alpaca.markets"
    assert receipt["live_trading_enabled"] is False
    assert receipt["paper_execution_enabled"] is True
    written = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert written["live_trading_enabled"] is False


def test_the_receipt_records_that_entries_were_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", raising=False)
    broker = StubBroker()
    receipt = _run(tmp_path, _db(tmp_path, [("AAA", True, _signal())]), broker)
    assert receipt["entries_enabled"] is False
    assert broker.submitted == []
    assert receipt["funnel"]["entry_entries_disabled"] == 1


def test_preflight_failure_is_reported_not_raised(tmp_path):
    class Broken(StubBroker):
        def assert_paper_account(self):
            from intraday_scanner.execution.paper_broker import LiveTradingRefused

            raise LiveTradingRefused("account number 9X1... lacks the paper prefix")

    receipt = _run(tmp_path, _db(tmp_path, []), Broken())
    assert receipt["status"] == "preflight_failed"
    assert "paper prefix" in receipt["error"]


def test_the_strategy_database_is_opened_read_only(tmp_path):
    """The driver must never be able to write to the strategy database."""

    db = _db(tmp_path, [("AAA", True, _signal())])
    rows = load_candidates(db, "2026-09-08")
    assert len(rows) == 1
    conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO alpha_signals VALUES ('X','2026-09-08',1,1,'','{}')")
    conn.close()


def test_candidates_from_another_day_are_not_loaded(tmp_path):
    db = _db(tmp_path, [("AAA", True, _signal())], date="2026-09-04")
    assert load_candidates(db, "2026-09-08") == []
