"""Orders must use prices the broker accepts.

Regression for 2026-09-24: the risk gate approved a paper-pilot entry in MSS,
and Alpaca rejected it with HTTP 422 "invalid limit_price 1.9999. sub-penny
increment does not fulfill minimum pricing criteria".
"""

from __future__ import annotations

import random
from decimal import Decimal

import pytest

from intraday_scanner.execution.paper_broker import _order
from intraday_scanner.execution.paper_engine import (
    EntryPlan,
    PaperExecutionEngine,
    PaperExecutionStore,
    tick_rounded_levels,
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


class Broker:
    def __init__(self):
        self.submitted: list[dict] = []

    def assert_paper_account(self):
        return PAPER_ACCOUNT

    def get_positions(self):
        return []

    def find_by_client_order_id(self, _coid):
        return None

    def submit_bracket_buy(self, **kw):
        self.submitted.append(kw)
        return _order({"id": "brk-1", "client_order_id": kw["client_order_id"], "symbol": kw["symbol"],
                       "side": "buy", "qty": kw["qty"], "filled_qty": 0, "status": "new",
                       "order_class": "bracket"})


@pytest.fixture
def entries_on(monkeypatch):
    monkeypatch.setenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", "true")
    monkeypatch.delenv("DAWNSTRIKE_PAPER_KILL_SWITCH_ENGAGED", raising=False)


def _engine(tmp_path, broker):
    return PaperExecutionEngine(client=broker, store=PaperExecutionStore(tmp_path / "p.sqlite"),
                                settings=RiskSettings())


def _plan(entry, stop, target, symbol="MSS"):
    return EntryPlan(symbol=symbol, market_date="2026-09-24", entry=entry, stop=stop, target=target,
                     strategy_version="paper-pilot-v1", signal_id=symbol, data_age_seconds=2.0,
                     data_age_source="timestamp")


def _is_valid_increment(price: float) -> bool:
    d = Decimal(str(price))
    tick = Decimal("0.01") if d >= 1 else Decimal("0.0001")
    return d == d.quantize(tick)


def test_the_rejected_mss_bracket_is_rounded_to_whole_cents():
    assert tick_rounded_levels(1.9999, 1.8599, 2.443) == (1.99, 1.86, 2.44)


def test_rounding_never_adds_risk():
    # entry down, stop up, target down
    assert tick_rounded_levels(10.057, 9.551, 11.618) == (10.05, 9.56, 11.61)


def test_sub_dollar_prices_keep_four_decimals():
    assert tick_rounded_levels(0.45678, 0.40001, 0.60009) == (0.4567, 0.4001, 0.6)


def test_bracket_that_collapses_after_rounding_is_refused():
    assert tick_rounded_levels(1.005, 1.0, 1.012) is None      # entry == stop after rounding
    assert tick_rounded_levels(5.009, 4.0, 5.0099) is None     # target == entry after rounding
    assert tick_rounded_levels(5.009, 4.0, 5.013) == (5.0, 4.0, 5.01)  # exactly one cent: allowed
    assert tick_rounded_levels(0.4567, 0.4566, 0.60) is None   # sub-cent stop gap


def test_random_brackets_are_valid_increments_and_never_riskier():
    rng = random.Random(20260925)
    for _ in range(5000):
        entry = round(rng.uniform(0.2, 80), 4)
        stop = round(entry * rng.uniform(0.80, 0.995), 4)
        target = round(entry * rng.uniform(1.005, 1.4), 4)
        out = tick_rounded_levels(entry, stop, target)
        if out is None:
            continue
        e, s, t = out
        assert all(_is_valid_increment(x) for x in out), out
        assert e <= entry and s >= stop and t <= target
        assert (e - s) <= (entry - stop) + 1e-9            # per-share risk never grows
        assert e - s >= 0.01 - 1e-9 and t - e >= 0.01 - 1e-9


def test_engine_submits_broker_valid_prices_for_the_mss_case(entries_on, tmp_path):
    broker = Broker()
    result = _engine(tmp_path, broker).submit_entry(_plan(1.9999, 1.8599, 2.443))
    assert result["submitted"] is True
    sent = broker.submitted[0]
    assert (sent["limit_price"], sent["stop_loss"], sent["take_profit"]) == (1.99, 1.86, 2.44)
    assert all(_is_valid_increment(sent[k]) for k in ("limit_price", "stop_loss", "take_profit"))


def test_engine_sizes_on_the_rounded_prices(entries_on, tmp_path):
    broker = Broker()
    engine = _engine(tmp_path, broker)
    result = engine.submit_entry(_plan(1.9999, 1.8599, 2.443))
    decision = result["decision"]
    # per-share risk the gate sized on must be the submitted one: 1.99 - 1.86
    assert decision.detail["per_share_risk"] == pytest.approx(0.13)


def test_engine_refuses_a_bracket_that_collapses_after_rounding(entries_on, tmp_path):
    broker = Broker()
    result = _engine(tmp_path, broker).submit_entry(_plan(1.005, 1.0, 1.012))
    assert result == {"submitted": False, "reason": "plan_invalid_after_tick_rounding"}
    assert broker.submitted == []
