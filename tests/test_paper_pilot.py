"""Paper pilot (paper-pilot-v1): gate-rejected candidates may reach the risk
gate under one fixed rule, judged against a live quote. Every existing
execution-safety check must still apply. See intraday_scanner/execution/paper_pilot.py.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.execution import paper_pilot as pilot
from intraday_scanner.execution.paper_broker import _order
from intraday_scanner.execution.paper_engine import client_order_id
from intraday_scanner.execution.paper_session import run_paper_session
from intraday_scanner.execution.risk_gate import RiskSettings

REPO = Path(__file__).resolve().parents[1]
DATE = "2026-09-08"
PAPER_ACCOUNT = {
    "account_number": "PA3XXXXXXXZL",
    "equity": "100000",
    "cash": "100000",
    "last_equity": "100000",
    "long_market_value": "0",
    "trading_blocked": False,
    "account_blocked": False,
}


def _rejected(**over) -> dict:
    """A gate-REJECTED payload shaped like the real ones (can_alert false)."""

    payload = {
        "alert_gate_status": "BLOCK",
        "strategy_receipt_paper_entry_eligible": False,
        "entry_trigger": "$10.0000",
        "invalidation_level": 9.00,
        "target_1": "$12.0000",
        "strategy_version": "alphaops-v5",
        "observed_at": (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(),
        "halt_status": "CLEAR",
    }
    payload.update(over)
    return payload


def _approved(**over) -> dict:
    payload = _rejected(
        alert_gate_status="PASS",
        strategy_receipt_paper_entry_eligible=True,
        observed_at=datetime.now(timezone.utc).isoformat(),
    )
    payload.update(over)
    return payload


def _db(tmp_path: Path, rows: list[tuple[str, bool, dict]]) -> Path:
    path = tmp_path / "signals.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE alpha_signals (ticker TEXT, timestamp TEXT, alpha_score REAL, "
        "can_alert INTEGER, no_trade_reason TEXT, payload_json TEXT)"
    )
    for index, (ticker, can_alert, payload) in enumerate(rows):
        conn.execute(
            "INSERT INTO alpha_signals VALUES (?,?,?,?,?,?)",
            (ticker, f"{DATE}T13:00:00+00:00", 100 - index, 1 if can_alert else 0,
             "setup grade below alert threshold", json.dumps(payload)),
        )
    conn.commit()
    conn.close()
    return path


class StubBroker:
    def __init__(self, *, is_open=True, minutes_left=120.0):
        self._open = is_open
        self._closes = datetime.now(timezone.utc) + timedelta(minutes=minutes_left)
        self.submitted: list[dict] = []

    def assert_paper_account(self):
        return PAPER_ACCOUNT

    def get_clock(self):
        return {"is_open": self._open, "next_open": None, "next_close": self._closes.isoformat()}

    def get_positions(self):
        return []

    def get_orders_since(self, _after):
        return []

    def get_open_orders(self):
        return []

    def find_by_client_order_id(self, _coid):
        return None

    def submit_bracket_buy(self, **kw):
        self.submitted.append(kw)
        return _order({"id": f"brk-{len(self.submitted)}", "client_order_id": kw["client_order_id"],
                       "symbol": kw["symbol"], "side": "buy", "qty": kw["qty"], "filled_qty": 0,
                       "status": "new", "order_class": "bracket"})

    def close_position(self, symbol):
        return _order({"id": f"cls-{symbol}", "client_order_id": f"close-{symbol}", "symbol": symbol,
                       "side": "sell", "qty": 1, "filled_qty": 1, "status": "filled",
                       "order_class": "simple"})


def _quote(symbol="PLT", price=10.5, bid=10.49, ask=10.51, age_s=2.0):
    return pilot.LiveQuote(
        symbol=symbol, price=price, bid=bid, ask=ask,
        as_of=datetime.now(timezone.utc) - timedelta(seconds=age_s),
    )


class Fetcher:
    def __init__(self, quotes):
        self.quotes = quotes
        self.calls: list[list[str]] = []

    def __call__(self, symbols):
        self.calls.append(list(symbols))
        return {q.symbol: q for q in self.quotes}


def _run(tmp_path, db, broker, fetcher, settings=None):
    return run_paper_session(
        db_path=db, market_date=DATE, store_path=tmp_path / "exec.sqlite",
        receipt_path=tmp_path / "receipt.json", settings=settings or RiskSettings(),
        client=broker, quote_fetcher=fetcher,
    )


@pytest.fixture
def entries_on(monkeypatch):
    monkeypatch.setenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", "true")
    monkeypatch.delenv("DAWNSTRIKE_PAPER_KILL_SWITCH_ENGAGED", raising=False)
    monkeypatch.delenv(pilot.PILOT_ENABLED_ENV, raising=False)


@pytest.fixture
def pilot_on(entries_on, monkeypatch):
    monkeypatch.setenv(pilot.PILOT_ENABLED_ENV, "true")


# ------------------------------------------------------------------ switch


def test_pilot_switch_is_opt_in(monkeypatch):
    monkeypatch.delenv(pilot.PILOT_ENABLED_ENV, raising=False)
    assert pilot.pilot_enabled() is False
    for off in ("", "false", "0", "no", "off", "enabled?"):
        monkeypatch.setenv(pilot.PILOT_ENABLED_ENV, off)
        assert pilot.pilot_enabled() is False
    for on in ("1", "true", "YES", " on "):
        monkeypatch.setenv(pilot.PILOT_ENABLED_ENV, on)
        assert pilot.pilot_enabled() is True


def test_pilot_switch_is_on_the_runtime_env_allowlist():
    """runtime.env keys not on this allowlist are silently dropped, which would
    leave the pilot switch unreachable in production."""

    text = (REPO / "scripts" / "import_dawnstrike_environment.ps1").read_text(encoding="utf-8")
    assert '"DAWNSTRIKE_PAPER_PILOT_ENABLED"' in text


# ------------------------------------------------------------ OFF unchanged


def test_pilot_off_rejected_candidate_is_not_submitted(entries_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker, fetcher = StubBroker(), Fetcher([_quote()])
    receipt = _run(tmp_path, db, broker, fetcher)
    assert broker.submitted == []
    assert fetcher.calls == []
    assert receipt["pilot_enabled"] is False
    assert not any(k.startswith("pilot_") for k in receipt["funnel"])
    assert receipt["funnel"]["gate_can_alert_false"] == 1


# ---------------------------------------------------------------- happy path


def test_pilot_on_submits_a_labelled_paper_entry(pilot_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker, fetcher = StubBroker(), Fetcher([_quote()])
    receipt = _run(tmp_path, db, broker, fetcher)
    assert len(broker.submitted) == 1
    sent = broker.submitted[0]
    assert sent["symbol"] == "PLT"
    assert sent["limit_price"] == 10.0 and sent["stop_loss"] == 9.0 and sent["take_profit"] == 12.0
    assert sent["client_order_id"] == client_order_id(
        symbol="PLT", market_date=DATE, strategy_version=pilot.PILOT_STRATEGY_VERSION
    )
    action = receipt["actions"][0]
    assert action["eligibility"] == "PILOT" and action["pilot_rule"] == "paper-pilot-v1"
    assert action["submitted"] is True and action["live_price"] == 10.5
    assert receipt["funnel"]["pilot_eligible"] == 1
    assert receipt["funnel"]["pilot_entry_submitted"] == 1
    assert receipt["pilot_rule"] == "paper-pilot-v1"
    assert fetcher.calls == [["PLT"]]


def test_gate_approved_candidates_are_unchanged_when_pilot_is_on(pilot_on, tmp_path):
    db = _db(tmp_path, [("GOOD", True, _approved())])
    broker, fetcher = StubBroker(), Fetcher([])
    receipt = _run(tmp_path, db, broker, fetcher)
    assert len(broker.submitted) == 1
    assert broker.submitted[0]["client_order_id"] == client_order_id(
        symbol="GOOD", market_date=DATE, strategy_version="alphaops-v5"
    )
    assert "eligibility" not in receipt["actions"][0]
    assert fetcher.calls == []


# ------------------------------------------------------------ pilot rule


@pytest.mark.parametrize(
    "over, reason",
    [
        ({"target_1": "$10.0000"}, "pilot_plan_invalid"),
        ({"invalidation_level": 10.5}, "pilot_plan_invalid"),
        ({"spread_pct": 12.0}, "pilot_extreme_spread"),
        ({"halt_status": "BLOCKED"}, "pilot_halted"),
        ({"current_halt": True}, "pilot_halted"),
        ({"recent_offering": True}, "pilot_active_offering"),
        ({"risk_flags": ["recent_offering"]}, "pilot_active_offering"),
    ],
)
def test_pilot_rule_refusals(pilot_on, tmp_path, over, reason):
    db = _db(tmp_path, [("PLT", False, _rejected(**over))])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker, Fetcher([_quote()]))
    assert broker.submitted == []
    assert receipt["funnel"][reason] == 1


def test_spread_just_below_the_extreme_line_is_eligible(pilot_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected(spread_pct=11.99))])
    broker = StubBroker()
    _run(tmp_path, db, broker, Fetcher([_quote()]))
    assert len(broker.submitted) == 1


@pytest.mark.parametrize(
    "quote, reason",
    [
        (None, "pilot_no_live_quote"),
        (_quote(bid=None, ask=None), "pilot_no_live_quote"),
        (_quote(price=12.0), "pilot_price_outside_plan"),
        (_quote(price=9.0), "pilot_price_outside_plan"),
        (_quote(price=10.5, bid=10.0, ask=11.5), "pilot_extreme_spread_live"),
    ],
)
def test_live_quote_refusals(pilot_on, tmp_path, quote, reason):
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker, Fetcher([quote] if quote else []))
    assert broker.submitted == []
    assert receipt["funnel"][reason] == 1


def test_sub_min_price_is_refused(pilot_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected(
        entry_trigger="0.45", invalidation_level=0.40, target_1="0.60"))])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker, Fetcher([_quote(price=0.44, bid=0.439, ask=0.441)]))
    assert broker.submitted == []
    assert receipt["funnel"]["pilot_sub_min_price"] == 1


# ------------------------------------------- existing safety still applies


def test_entries_disabled_still_blocks_pilot(pilot_on, monkeypatch, tmp_path):
    monkeypatch.setenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", "false")
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker, Fetcher([_quote()]))
    assert broker.submitted == []
    assert receipt["funnel"]["pilot_entry_entries_disabled"] == 1


def test_kill_switch_still_blocks_pilot(pilot_on, monkeypatch, tmp_path):
    monkeypatch.setenv("DAWNSTRIKE_PAPER_KILL_SWITCH_ENGAGED", "true")
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker, Fetcher([_quote()]))
    assert broker.submitted == []
    assert receipt["funnel"]["pilot_entry_kill_switch_engaged"] == 1


def test_stale_live_quote_is_rejected_by_the_risk_gate(pilot_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker, Fetcher([_quote(age_s=2000)]))
    assert broker.submitted == []
    assert receipt["funnel"]["pilot_entry_stale_market_data"] == 1


def test_future_dated_live_quote_is_rejected_by_the_risk_gate(pilot_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker = StubBroker()
    receipt = _run(tmp_path, db, broker, Fetcher([_quote(age_s=-60)]))
    assert broker.submitted == []
    assert receipt["funnel"]["pilot_entry_future_market_data"] == 1


def test_max_entries_per_day_caps_pilot_entries(pilot_on, tmp_path):
    db = _db(tmp_path, [("AAA", False, _rejected()), ("BBB", False, _rejected())])
    broker = StubBroker()
    fetcher = Fetcher([_quote(symbol="AAA"), _quote(symbol="BBB")])
    receipt = _run(tmp_path, db, broker, fetcher, settings=RiskSettings(max_entries_per_day=1))
    assert len(broker.submitted) == 1
    assert receipt["funnel"]["pilot_entry_submitted"] == 1


def test_market_closed_makes_no_quote_call_and_no_entry(pilot_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker, fetcher = StubBroker(is_open=False), Fetcher([_quote()])
    receipt = _run(tmp_path, db, broker, fetcher)
    assert broker.submitted == [] and fetcher.calls == []
    assert receipt["funnel"]["pilot_market_closed"] == 1


def test_too_close_to_the_bell_makes_no_entry(pilot_on, tmp_path):
    db = _db(tmp_path, [("PLT", False, _rejected())])
    broker, fetcher = StubBroker(minutes_left=5.0), Fetcher([_quote()])
    receipt = _run(tmp_path, db, broker, fetcher)
    assert broker.submitted == [] and fetcher.calls == []
    assert receipt["funnel"]["pilot_too_close_to_the_bell"] == 1


# ------------------------------------------------------------ quote parsing


def test_quotes_from_snapshots_parses_alpaca_shape():
    payload = {"PLT": {"latestTrade": {"p": 10.5, "t": "2026-09-08T14:00:01.5Z"},
                       "latestQuote": {"bp": 10.49, "ap": 10.51}}}
    q = pilot.quotes_from_snapshots(payload)["PLT"]
    assert q.price == 10.5 and q.bid == 10.49 and q.ask == 10.51
    assert q.as_of == datetime(2026, 9, 8, 14, 0, 1, 500000, tzinfo=timezone.utc)
    assert round(q.spread_pct, 4) == round(0.02 / 10.5 * 100, 4)


def test_fetch_live_quotes_fails_closed(monkeypatch):
    import intraday_scanner.providers.alpaca_provider as ap

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ap.AlpacaProvider, "_request_json", boom)
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    assert pilot.fetch_live_quotes(["PLT"]) == {}
