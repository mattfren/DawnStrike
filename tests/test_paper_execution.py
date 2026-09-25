"""Deterministic proof of the paper execution path. No network.

Covers the lifecycle the operator asked to see demonstrated: a qualifying setup
reaching the adapter, a non-qualifying one refused for the right reason,
duplicate submissions not creating duplicate orders, ambiguous timeouts
reconciling rather than resubmitting, partial fills surfacing, restart recovery,
and exits still running while entries are disabled.

These are software tests. They prove the plumbing, not that the strategy makes
money, and they never touch the forward performance ledger.
"""

from __future__ import annotations

import pytest

from intraday_scanner.execution.paper_broker import (
    LiveTradingRefused,
    PaperBrokerClient,
    PaperBrokerError,
    _order,
    _reject_live,
)
from intraday_scanner.execution.paper_engine import (
    EntryPlan,
    PaperExecutionEngine,
    PaperExecutionStore,
    client_order_id,
)
from intraday_scanner.execution.risk_gate import (
    AGE_SOURCE_OBSERVED_AT,
    AGE_SOURCE_ROW_TIMESTAMP,
    AGE_SOURCE_TIMESTAMP,
    AGE_SOURCE_UNKNOWN,
    FUTURE_OBSERVATION_TOLERANCE_SECONDS,
    RiskSettings,
    evaluate_entry,
)

PAPER_ACCOUNT = {
    "account_number": "PA3XXXXXXXZL",
    "equity": "100000",
    "cash": "100000",
    "last_equity": "100000",
    "long_market_value": "0",
    "trading_blocked": False,
    "account_blocked": False,
}


def _plan(**kw):
    base = dict(
        symbol="TEST",
        market_date="2026-09-08",
        entry=10.0,
        stop=9.0,
        target=13.0,
        strategy_version="alphaops-v5",
        data_age_seconds=10.0,
    )
    base.update(kw)
    return EntryPlan(**base)


def _enable(monkeypatch):
    monkeypatch.setenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", "true")


# --------------------------------------------------------------------------
# paper-only interlocks
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://api.alpaca.markets/v2/orders",
        "https://api.alpaca.markets/v2/account",
    ],
)
def test_live_trading_host_is_refused(url):
    """The live endpoint must be unreachable by construction, not by policy."""

    with pytest.raises(LiveTradingRefused):
        _reject_live(url)


def test_only_the_paper_host_is_accepted():
    _reject_live("https://paper-api.alpaca.markets/v2/orders")  # must not raise
    for other in ("https://evil.example.com/v2/orders", "https://data.alpaca.markets/v2/orders"):
        with pytest.raises(LiveTradingRefused):
            _reject_live(other)


def test_client_base_url_is_pinned_to_paper(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    assert PaperBrokerClient().base_url == "https://paper-api.alpaca.markets"


def test_non_paper_account_number_is_refused(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    client = PaperBrokerClient()
    monkeypatch.setattr(client, "get_account", lambda: {"account_number": "9X12345"})
    with pytest.raises(LiveTradingRefused, match="paper prefix"):
        client.assert_paper_account()


# --------------------------------------------------------------------------
# risk gate
# --------------------------------------------------------------------------


def _evaluate(monkeypatch, **over):
    args = dict(
        entry=10.0,
        stop=9.0,
        target=13.0,
        account=PAPER_ACCOUNT,
        open_positions=0,
        entries_today=0,
        day_pnl_pct=0.0,
        data_age_seconds=10.0,
        settings=RiskSettings(),
    )
    args.update(over)
    return evaluate_entry(**args)


def test_qualifying_setup_is_approved_and_sized(monkeypatch):
    _enable(monkeypatch)
    d = _evaluate(monkeypatch)
    assert d.approved and d.reason == "approved"
    # 0.5% of 100k = $500 risk; $1/share risk -> 500 shares, but the 10%
    # position cap ($10,000 / $10) binds first at 1000 -> 500 stands.
    assert d.qty == 500
    assert d.notional == pytest.approx(5000.0)
    assert d.detail["planned_r_multiple"] == pytest.approx(3.0)


def test_entries_are_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", raising=False)
    assert _evaluate(monkeypatch).reason == "entries_disabled"


def test_kill_switch_beats_everything(monkeypatch, tmp_path):
    _enable(monkeypatch)
    sw = tmp_path / "STOP"
    sw.write_text("halt", encoding="utf-8")
    d = _evaluate(monkeypatch, settings=RiskSettings(kill_switch_path=sw))
    assert d.reason == "kill_switch_engaged"


@pytest.mark.parametrize(
    "over,expected",
    [
        ({"data_age_seconds": 5000.0}, "stale_market_data"),
        ({"data_age_seconds": -3600.0}, "future_market_data"),
        ({"day_pnl_pct": -2.5}, "daily_loss_limit_reached"),
        ({"open_positions": 3}, "max_concurrent_positions"),
        ({"entries_today": 5}, "max_entries_per_day"),
        ({"target": 9.5}, "incoherent_plan"),
        ({"stop": 10.0}, "incoherent_plan"),
    ],
)
def test_nonqualifying_setups_are_refused_for_the_right_reason(monkeypatch, over, expected):
    _enable(monkeypatch)
    d = _evaluate(monkeypatch, **over)
    assert not d.approved
    assert d.reason == expected


def test_future_dated_observation_is_rejected_not_treated_as_fresh(monkeypatch):
    """A negative age must not slide through `age < max_staleness_seconds`."""

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=-90.0, data_age_source=AGE_SOURCE_TIMESTAMP)
    assert not d.approved
    assert d.reason == "future_market_data"
    assert d.reason != "stale_market_data"


def test_benign_clock_skew_within_tolerance_is_still_accepted(monkeypatch):
    """A tiny negative age on a capture-instant field (ordinary clock drift)
    must not be over-rejected."""

    _enable(monkeypatch)
    d = _evaluate(
        monkeypatch,
        data_age_seconds=-(FUTURE_OBSERVATION_TOLERANCE_SECONDS - 1.0),
        data_age_source=AGE_SOURCE_TIMESTAMP,
    )
    assert d.approved
    assert d.reason == "approved"


# --------------------------------------------------------------------------
# future-dating grace is conditional on provenance (bar-label vs capture-instant)
# --------------------------------------------------------------------------


def test_future_dated_observed_at_within_tolerance_is_still_rejected(monkeypatch):
    """observed_at is a bar label (premarket_enrichment_service), not a
    capture instant - it gets zero grace even for a small future value that
    a capture-instant field would absorb as benign clock skew."""

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=-3.0, data_age_source=AGE_SOURCE_OBSERVED_AT)
    assert not d.approved
    assert d.reason == "future_market_data"
    assert d.detail["source"] == AGE_SOURCE_OBSERVED_AT
    assert d.detail["capture_instant"] is False
    assert d.detail["tolerance_s"] == 0.0


def test_future_dated_timestamp_within_tolerance_is_admitted(monkeypatch):
    """timestamp (alpha_cycle_service.cycle_decision_timestamp) is a genuine
    capture instant - a small future value is benign clock skew and is
    admitted."""

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=-3.0, data_age_source=AGE_SOURCE_TIMESTAMP)
    assert d.approved
    assert d.reason == "approved"


def test_future_dated_timestamp_beyond_tolerance_still_rejects(monkeypatch):
    _enable(monkeypatch)
    d = _evaluate(
        monkeypatch,
        data_age_seconds=-(FUTURE_OBSERVATION_TOLERANCE_SECONDS + 1.0),
        data_age_source=AGE_SOURCE_TIMESTAMP,
    )
    assert not d.approved
    assert d.reason == "future_market_data"
    assert d.detail["capture_instant"] is True


@pytest.mark.parametrize(
    "age_seconds,expect_approved",
    [
        (-(FUTURE_OBSERVATION_TOLERANCE_SECONDS - 0.1), True),  # -4.9: admit
        (-FUTURE_OBSERVATION_TOLERANCE_SECONDS, True),  # -5.0: inclusive edge, admit
        (-(FUTURE_OBSERVATION_TOLERANCE_SECONDS + 0.000001), False),  # -5.000001: reject
    ],
)
def test_capture_instant_future_dating_boundary_is_unchanged(monkeypatch, age_seconds, expect_approved):
    """The +/-5s boundary on a capture-instant source is the pre-existing
    behaviour and must not shift."""

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=age_seconds, data_age_source=AGE_SOURCE_TIMESTAMP)
    assert d.approved is expect_approved
    if not expect_approved:
        assert d.reason == "future_market_data"


def test_row_timestamp_fallback_is_also_a_capture_instant_source(monkeypatch):
    """The DB-row timestamp fallback is the same alpha_signals cycle
    timestamp as `timestamp` - also a capture instant, also gets grace."""

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=-3.0, data_age_source=AGE_SOURCE_ROW_TIMESTAMP)
    assert d.approved
    assert d.reason == "approved"


def test_unknown_age_source_gets_no_future_dating_grace(monkeypatch):
    """An unrecognized or absent source is not assumed to be a capture
    instant - fail closed, same as an unparseable timestamp."""

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=-1.0, data_age_source=AGE_SOURCE_UNKNOWN)
    assert not d.approved
    assert d.reason == "future_market_data"


# --------------------------------------------------------------------------
# stale-boundary and malformed-timestamp behaviour, unchanged by the above
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "age_seconds,expect_approved",
    [
        (899.0, True),
        (900.0, True),
        (900.0000001, False),
        (901.0, False),
        (float("inf"), False),
    ],
)
def test_stale_boundary_is_untouched(monkeypatch, age_seconds, expect_approved):
    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=age_seconds, data_age_source=AGE_SOURCE_TIMESTAMP)
    assert d.approved is expect_approved
    if not expect_approved:
        assert d.reason == "stale_market_data"


@pytest.mark.parametrize("raw", [None, "", "not-a-date", "banana", 12345, object(), {"x": 1}])
def test_malformed_stamps_land_on_infinite_age_and_reject_without_raising(monkeypatch, raw):
    from intraday_scanner.execution.paper_session import _age_seconds, UNKNOWN_AGE_SECONDS
    from datetime import datetime, timezone

    age, source = _age_seconds({"timestamp": raw}, None, datetime.now(timezone.utc))
    assert age == UNKNOWN_AGE_SECONDS
    assert source == AGE_SOURCE_UNKNOWN

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, data_age_seconds=age, data_age_source=source)
    assert not d.approved
    assert d.reason == "stale_market_data"


@pytest.mark.parametrize("suffix", ["Z", "+00:00", "-05:00", "+05:30"])
def test_timezone_equivalence_still_holds_for_capture_instant_parsing(suffix):
    from intraday_scanner.execution.paper_session import _age_seconds, _parse_stamp
    from datetime import datetime, timezone

    now = datetime(2026, 9, 8, 14, 30, 0, tzinfo=timezone.utc)
    stamp = {
        "Z": "2026-09-08T14:29:50Z",
        "+00:00": "2026-09-08T14:29:50+00:00",
        "-05:00": "2026-09-08T09:29:50-05:00",
        "+05:30": "2026-09-08T19:59:50+05:30",
    }[suffix]
    parsed = _parse_stamp(stamp)
    assert parsed is not None
    age, source = _age_seconds({"timestamp": stamp}, None, now)
    assert age == pytest.approx(10.0, abs=1e-6)
    assert source == AGE_SOURCE_TIMESTAMP


def test_future_market_data_rejection_leaves_manage_positions_working(monkeypatch, tmp_path):
    """An entry refused for future_market_data must not disturb the
    independent exit path for a position already open."""

    _enable(monkeypatch)
    broker = FakeBroker()
    broker.positions = [{"symbol": "TEST", "qty": "500"}]
    engine, _ = _engine(tmp_path, broker)

    plan = _plan(data_age_seconds=-3600.0, data_age_source=AGE_SOURCE_OBSERVED_AT)
    result = engine.submit_entry(plan)
    assert result["submitted"] is False
    assert result["reason"] == "future_market_data"

    out = engine.manage_positions(market_date="2026-09-08", force_exit=True)
    assert broker.closed == ["TEST"]
    assert out["actions"][0]["action"] == "time_exit_submitted"


def test_sizing_never_uses_margin(monkeypatch):
    """Broker grants 4x. We must size against settled cash, never buying power."""

    _enable(monkeypatch)
    thin = dict(PAPER_ACCOUNT, cash="1000", buying_power="400000")
    d = _evaluate(monkeypatch, account=thin)
    assert d.approved
    assert d.notional <= 1000.0


def test_shorting_is_not_reachable(monkeypatch):
    """An inverted plan is refused rather than silently flipped to a short."""

    _enable(monkeypatch)
    d = _evaluate(monkeypatch, entry=10.0, stop=11.0, target=8.0)
    assert not d.approved and d.reason == "incoherent_plan"


# --------------------------------------------------------------------------
# order lifecycle
# --------------------------------------------------------------------------


class FakeBroker:
    """Records calls so duplicate-submission behaviour is observable."""

    def __init__(self, *, existing=None, raise_on_submit=None):
        self.submitted: list[dict] = []
        self._existing = existing
        self._raise = raise_on_submit
        self.positions: list[dict] = []
        self.closed: list[str] = []

    def assert_paper_account(self):
        return PAPER_ACCOUNT

    def get_positions(self):
        return self.positions

    def get_orders_since(self, _after):
        return list(self._existing or [])

    def get_open_orders(self):
        return []

    def find_by_client_order_id(self, coid):
        for o in self._existing or []:
            if o.client_order_id == coid:
                return o
        return None

    def submit_bracket_buy(self, **kw):
        if self._raise:
            raise self._raise
        self.submitted.append(kw)
        return _order(
            {
                "id": "brk-1",
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
        return _order(
            {"id": f"cls-{symbol}", "client_order_id": f"close-{symbol}", "symbol": symbol,
             "side": "sell", "qty": 1, "filled_qty": 1, "status": "filled", "order_class": "simple"}
        )


def _engine(tmp_path, broker):
    store = PaperExecutionStore(tmp_path / "paper.sqlite")
    return PaperExecutionEngine(client=broker, store=store, settings=RiskSettings()), store


def test_qualifying_signal_reaches_the_order_adapter(monkeypatch, tmp_path):
    _enable(monkeypatch)
    broker = FakeBroker()
    engine, _ = _engine(tmp_path, broker)
    result = engine.submit_entry(_plan())
    assert result["submitted"] is True
    assert len(broker.submitted) == 1
    sent = broker.submitted[0]
    assert sent["symbol"] == "TEST" and sent["qty"] == 500
    assert sent["stop_loss"] == 9.0 and sent["take_profit"] == 13.0


def test_duplicate_submission_does_not_create_a_second_order(monkeypatch, tmp_path):
    _enable(monkeypatch)
    coid = client_order_id(symbol="TEST", market_date="2026-09-08", strategy_version="alphaops-v5")
    prior = _order(
        {"id": "brk-1", "client_order_id": coid, "symbol": "TEST", "side": "buy",
         "qty": 500, "filled_qty": 0, "status": "new", "order_class": "bracket"}
    )
    broker = FakeBroker(existing=[prior])
    engine, _ = _engine(tmp_path, broker)
    result = engine.submit_entry(_plan())
    assert result["submitted"] is False
    assert result["reason"] == "already_submitted"
    assert broker.submitted == []


def test_client_order_id_is_deterministic():
    a = client_order_id(symbol="ABC", market_date="2026-09-08", strategy_version="v5")
    b = client_order_id(symbol="ABC", market_date="2026-09-08", strategy_version="v5")
    c = client_order_id(symbol="ABC", market_date="2026-09-09", strategy_version="v5")
    assert a == b and a != c
    assert len(a) <= 128


def test_ambiguous_timeout_is_not_retried_blindly(monkeypatch, tmp_path):
    """A submit that may have landed must never be resubmitted automatically."""

    _enable(monkeypatch)
    broker = FakeBroker(raise_on_submit=PaperBrokerError("outcome is ambiguous; reconcile"))
    engine, store = _engine(tmp_path, broker)
    result = engine.submit_entry(_plan())
    assert result["submitted"] is False
    assert result["reason"] == "submit_failed"
    assert broker.submitted == []


def test_partial_fill_is_surfaced_by_reconcile(monkeypatch, tmp_path):
    _enable(monkeypatch)
    coid = client_order_id(symbol="TEST", market_date="2026-09-08", strategy_version="alphaops-v5")
    partial = _order(
        {"id": "brk-1", "client_order_id": coid, "symbol": "TEST", "side": "buy",
         "qty": 500, "filled_qty": 200, "filled_avg_price": "10.02",
         "status": "partially_filled", "order_class": "bracket"}
    )
    assert partial.is_partial
    broker = FakeBroker(existing=[partial])
    engine, _ = _engine(tmp_path, broker)
    out = engine.reconcile("2026-09-08")
    assert out["partial_fills"] == [coid]


def test_restart_recovery_rebuilds_state_from_the_broker(monkeypatch, tmp_path):
    """A fresh store must recover real orders rather than assume none exist."""

    _enable(monkeypatch)
    coid = client_order_id(symbol="TEST", market_date="2026-09-08", strategy_version="alphaops-v5")
    live = _order(
        {"id": "brk-1", "client_order_id": coid, "symbol": "TEST", "side": "buy",
         "qty": 500, "filled_qty": 500, "filled_avg_price": "10.01",
         "status": "filled", "order_class": "bracket"}
    )
    broker = FakeBroker(existing=[live])
    engine, store = _engine(tmp_path, broker)  # store starts empty, as after a restart
    engine.reconcile("2026-09-08")
    assert store.entries_today("2026-09-08") == 1
    # And a re-submission after recovery is deduplicated, not doubled.
    assert engine.submit_entry(_plan())["reason"] == "already_submitted"


def test_reconcile_ignores_orders_from_other_market_dates(monkeypatch, tmp_path):
    """The broker's `after` filter is a timestamp, so yesterday's order comes
    back too. Counting it against today's entry limit would be wrong."""

    _enable(monkeypatch)
    yesterday = client_order_id(
        symbol="TEST", market_date="2026-09-04", strategy_version="alphaops-v5"
    )
    stale = _order(
        {"id": "brk-old", "client_order_id": yesterday, "symbol": "TEST", "side": "buy",
         "qty": 500, "filled_qty": 500, "status": "filled", "order_class": "bracket"}
    )
    broker = FakeBroker(existing=[stale])
    engine, store = _engine(tmp_path, broker)
    out = engine.reconcile("2026-09-08")
    assert out["orders"] == 0
    assert out["orders_seen"] == 1
    assert store.entries_today("2026-09-08") == 0
    # And today's entry is therefore still allowed.
    assert engine.submit_entry(_plan())["submitted"] is True


def test_positions_are_still_managed_while_entries_are_disabled(monkeypatch, tmp_path):
    """Stale data or a disabled switch must not strand an open position."""

    monkeypatch.delenv("DAWNSTRIKE_PAPER_ENTRIES_ENABLED", raising=False)
    broker = FakeBroker()
    broker.positions = [{"symbol": "TEST", "qty": "500"}]
    engine, _ = _engine(tmp_path, broker)

    assert engine.submit_entry(_plan())["reason"] == "entries_disabled"
    out = engine.manage_positions(market_date="2026-09-08", force_exit=True)
    assert broker.closed == ["TEST"]
    assert out["actions"][0]["action"] == "time_exit_submitted"


class BracketBroker(FakeBroker):
    """A broker whose close fails while the bracket's legs are still resting."""

    def __init__(self, *, legs_cancel=True):
        super().__init__()
        self.legs_cancel = legs_cancel
        self.cancelled: list[str] = []
        self.positions = [{"symbol": "TEST", "qty": "500"}]
        self._legs_live = True

    def get_open_orders(self):
        if not self._legs_live:
            return []
        return [
            _order(
                {
                    "id": "brk-1", "client_order_id": "ds-x", "symbol": "TEST",
                    "side": "buy", "qty": 500, "filled_qty": 500, "status": "filled",
                    "order_class": "bracket",
                    "legs": [
                        {"id": "leg-tp", "symbol": "TEST", "status": "new"},
                        {"id": "leg-sl", "symbol": "TEST", "status": "new"},
                    ],
                }
            )
        ]

    def cancel_order(self, order_id):
        if not self.legs_cancel:
            raise PaperBrokerError(f"DELETE /v2/orders/{order_id} -> 422: already terminal")
        self.cancelled.append(order_id)
        self._legs_live = False

    def close_position(self, symbol):
        if self._legs_live:
            raise PaperBrokerError("403: insufficient qty available for order (requested: 500)")
        return super().close_position(symbol)


def test_flatten_cancels_resting_bracket_legs_before_closing(monkeypatch, tmp_path):
    """Otherwise the close is rejected and the position is stranded overnight."""

    broker = BracketBroker()
    engine, _ = _engine(tmp_path, broker)
    out = engine.manage_positions(market_date="2026-09-08", force_exit=True)

    assert "leg-tp" in broker.cancelled and "leg-sl" in broker.cancelled
    assert broker.closed == ["TEST"]
    assert out["actions"][0]["action"] == "time_exit_submitted"


def test_a_leg_that_cannot_be_cancelled_is_reported_not_swallowed(monkeypatch, tmp_path):
    """A racing fill makes cancel fail; the close attempt must still be recorded."""

    broker = BracketBroker(legs_cancel=False)
    engine, _ = _engine(tmp_path, broker)
    out = engine.manage_positions(market_date="2026-09-08", force_exit=True)

    assert broker.closed == []
    assert out["actions"][0]["action"] == "time_exit_failed"
    assert "insufficient qty" in out["actions"][0]["error"]


def test_bracket_rejects_incoherent_prices(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    client = PaperBrokerClient()
    with pytest.raises(PaperBrokerError, match="incoherent bracket"):
        client.submit_bracket_buy(
            symbol="X", qty=1, limit_price=10.0, take_profit=9.0, stop_loss=11.0,
            client_order_id="x",
        )


def test_bracket_rejects_non_positive_quantity(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    client = PaperBrokerClient()
    with pytest.raises(PaperBrokerError, match="non-positive quantity"):
        client.submit_bracket_buy(
            symbol="X", qty=0, limit_price=10.0, take_profit=13.0, stop_loss=9.0,
            client_order_id="x",
        )
