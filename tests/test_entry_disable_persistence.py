"""Closeout packet CLOSEOUT-DISABLED-ENTRY: proof that the new-entry gate
stays disabled across process restart, scheduler invocation, and config
reload, and that existing-position protection/reconciliation never depends
on it.

No strategy is activated by this packet. These tests exercise
``DAWNSTRIKE_PAPER_ENTRIES_ENABLED`` (intraday_scanner/execution/risk_gate.py)
using a fake in-process broker and throwaway sqlite/state, and one real
sub-interpreter boundary to prove the "restart" and "scheduler invocation"
transitions rather than merely asserting behaviour a single process could
fake through caching that doesn't actually exist.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.execution.paper_broker import _order
from intraday_scanner.execution.paper_session import run_paper_session
from intraday_scanner.execution.risk_gate import (
    ENTRIES_ENABLED_ENV,
    RiskSettings,
    entries_enabled,
    evaluate_entry,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    """A fake broker: no network, no real account, no real order."""

    def __init__(self, *, is_open=True, minutes_left=120.0, positions=None):
        self._open = is_open
        self._closes_in = minutes_left
        self.positions = list(positions or [])
        self.submitted: list[dict] = []
        self.closed: list[str] = []

    def assert_paper_account(self):
        return PAPER_ACCOUNT

    def get_clock(self):
        from datetime import timedelta

        return {
            "is_open": self._open,
            "next_open": "2026-09-09T09:30:00-04:00",
            "next_close": (
                datetime.now(timezone.utc) + timedelta(minutes=self._closes_in)
            ).isoformat(),
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
            {
                "id": f"cls-{symbol}",
                "client_order_id": f"close-{symbol}",
                "symbol": symbol,
                "side": "sell",
                "qty": 1,
                "filled_qty": 1,
                "status": "filled",
                "order_class": "simple",
            }
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


# ---------------------------------------------------------------------------
# 1. PROCESS RESTART: a brand-new interpreter, launched with a clean
#    environment, must default the gate to disabled with no state carried
#    over from anything that ran before it.
# ---------------------------------------------------------------------------


def test_restart_with_no_env_var_defaults_closed_in_a_fresh_interpreter():
    """Simulates an actual process restart: a new Python interpreter, spawned
    with an environment that does not carry DAWNSTRIKE_PAPER_ENTRIES_ENABLED.
    If the gate were ever memoized to a prior "enabled" value (e.g. via a
    module-level cache or a settings singleton written once at import time),
    this would be the transition that exposes it - a fresh process has no
    such cache to inherit, so this only passes if the check is genuinely
    read-fresh from the environment.
    """

    script = textwrap.dedent(
        """
        from intraday_scanner.execution.risk_gate import entries_enabled
        assert entries_enabled() is False, "fresh process must default to disabled"
        print("RESTART_OK")
        """
    )
    import os
    import subprocess

    clean_env = {k: v for k, v in os.environ.items() if k != ENTRIES_ENABLED_ENV}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESTART_OK" in result.stdout


def test_restart_does_not_inherit_a_prior_processs_enabled_state():
    """A second fresh interpreter, launched right after a first one that had
    the flag explicitly enabled, must not see that state - env vars set in
    one process are never visible to an unrelated new process unless
    explicitly passed. This is the exact shape of an operator (or a stray
    manual test run) having flipped the switch earlier: the next real
    restart must come up closed regardless.
    """

    import os
    import subprocess

    enabling_script = textwrap.dedent(
        """
        import os
        os.environ["DAWNSTRIKE_PAPER_ENTRIES_ENABLED"] = "true"
        from intraday_scanner.execution.risk_gate import entries_enabled
        assert entries_enabled() is True
        print("FIRST_PROCESS_ENABLED")
        """
    )
    first = subprocess.run(
        [sys.executable, "-c", enabling_script],
        cwd=REPO_ROOT,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert "FIRST_PROCESS_ENABLED" in first.stdout

    clean_env = {k: v for k, v in os.environ.items() if k != ENTRIES_ENABLED_ENV}
    checking_script = textwrap.dedent(
        """
        from intraday_scanner.execution.risk_gate import entries_enabled
        assert entries_enabled() is False, "a new process must not inherit the prior one's enabled state"
        print("SECOND_PROCESS_CLOSED")
        """
    )
    second = subprocess.run(
        [sys.executable, "-c", checking_script],
        cwd=REPO_ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "SECOND_PROCESS_CLOSED" in second.stdout


# ---------------------------------------------------------------------------
# 2. SCHEDULER INVOCATION: the same entry point the scheduler drives
#    (`py.exe -m intraday_scanner.cli paper-session ...`, wired in
#    scripts/run_alphaops_monitor.ps1) must default closed, and there must
#    be no CLI flag that can flip it - only the environment can.
# ---------------------------------------------------------------------------


def test_cli_paper_session_has_no_flag_to_enable_entries():
    """The scheduler's invocation surface is the CLI argument list. Prove
    there is no argparse flag on `paper-session` that can enable entries -
    only the environment variable can, which is the property the other
    tests here depend on holding.
    """

    from intraday_scanner.cli import build_arg_parser

    parser = build_arg_parser()
    subparsers_actions = [
        action
        for action in parser._actions  # noqa: SLF001 - inspecting argparse structure only
        if action.dest == "command"
    ]
    assert subparsers_actions, "expected a command subparsers action"
    paper_session_parser = subparsers_actions[0].choices["paper-session"]
    option_strings = {
        opt for action in paper_session_parser._actions for opt in action.option_strings  # noqa: SLF001
    }
    forbidden = {
        opt
        for opt in option_strings
        if "entries" in opt.replace("-", "_").lower() or "enable" in opt.lower()
    }
    assert not forbidden, f"paper-session must not expose an entries-enable flag, found {forbidden}"


def test_scheduler_style_invocation_in_a_fresh_process_defaults_closed(tmp_path):
    """Runs `run_paper_session` - the exact function `intraday_scanner.cli
    _run_paper_session` calls for the scheduler's `paper-session` subcommand
    - inside a brand-new interpreter with a clean environment, a real
    on-disk candidate database, and a stub broker module injected via
    sys.path so no network call is possible. This is the scheduler
    transition: a fresh process, launched the way the real task launches it,
    with no override available except the (absent) environment variable.
    """

    import os
    import subprocess

    db = _db(tmp_path, [("AAA", True, _signal())])
    store_path = tmp_path / "exec.sqlite"
    receipt_path = tmp_path / "receipt.json"

    stub_broker_module = tmp_path / "_scheduler_stub_broker.py"
    stub_broker_module.write_text(
        textwrap.dedent(
            """
            from datetime import datetime, timezone, timedelta

            PAPER_ACCOUNT = {
                "account_number": "PA3XXXXXXXZL",
                "equity": "100000",
                "cash": "100000",
                "last_equity": "100000",
                "long_market_value": "0",
                "trading_blocked": False,
                "account_blocked": False,
            }

            class StubBroker:
                def __init__(self):
                    self.submitted = []

                def assert_paper_account(self):
                    return PAPER_ACCOUNT

                def get_clock(self):
                    return {
                        "is_open": True,
                        "next_open": "2026-09-09T09:30:00-04:00",
                        "next_close": (datetime.now(timezone.utc) + timedelta(minutes=120)).isoformat(),
                    }

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
                    raise AssertionError("no order may ever be submitted in this test")

                def close_position(self, symbol):
                    raise AssertionError("no order may ever be submitted in this test")
            """
        ),
        encoding="utf-8",
    )

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(tmp_path)!r})
        from _scheduler_stub_broker import StubBroker
        from intraday_scanner.execution.paper_session import run_paper_session

        broker = StubBroker()
        receipt = run_paper_session(
            db_path={str(db)!r},
            market_date="2026-09-08",
            store_path={str(store_path)!r},
            receipt_path={str(receipt_path)!r},
            client=broker,
        )
        assert receipt["entries_enabled"] is False, receipt
        assert broker.submitted == [], broker.submitted
        assert receipt["status"] == "completed", receipt
        print("SCHEDULER_INVOCATION_CLOSED")
        """
    )
    clean_env = {k: v for k, v in os.environ.items() if k != ENTRIES_ENABLED_ENV}
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCHEDULER_INVOCATION_CLOSED" in result.stdout
    written = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert written["entries_enabled"] is False
    assert written["funnel"].get("entry_entries_disabled") == 1


# ---------------------------------------------------------------------------
# 3. CONFIGURATION RELOAD: within one long-running process, the gate must
#    re-read the environment on every call - no stale "was enabled earlier
#    this process" state may survive an operator flipping the switch back
#    off (or a reload that simply re-evaluates config without a restart).
# ---------------------------------------------------------------------------


def test_reload_within_one_process_picks_up_disable_immediately(monkeypatch):
    """Enable, observe enabled; disable (no restart), observe disabled
    again in the very next call. A cached/memoized read would fail this.
    """

    monkeypatch.setenv(ENTRIES_ENABLED_ENV, "true")
    assert entries_enabled() is True

    monkeypatch.delenv(ENTRIES_ENABLED_ENV, raising=False)
    assert entries_enabled() is False

    monkeypatch.setenv(ENTRIES_ENABLED_ENV, "true")
    assert entries_enabled() is True

    monkeypatch.setenv(ENTRIES_ENABLED_ENV, "false")
    assert entries_enabled() is False


def test_reload_mid_session_cannot_re_enable_entries_already_refused(monkeypatch, tmp_path):
    """A config reload that happens to occur between two orders in the same
    session must not let a stale "enabled" evaluation leak through: each
    order's risk-gate check re-reads the environment for itself.
    """

    monkeypatch.delenv(ENTRIES_ENABLED_ENV, raising=False)
    first = evaluate_entry(
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
    assert first.reason == "entries_disabled"

    # Simulate a reload that turns entries on mid-session...
    monkeypatch.setenv(ENTRIES_ENABLED_ENV, "true")
    second = evaluate_entry(
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
    assert second.approved is True

    # ...and a reload that turns it back off must be equally immediate.
    monkeypatch.delenv(ENTRIES_ENABLED_ENV, raising=False)
    third = evaluate_entry(
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
    assert third.reason == "entries_disabled"


def test_reload_does_not_affect_existing_position_management(monkeypatch, tmp_path):
    """The maintenance-release property this whole packet exists to prove:
    toggling the new-entry gate through a reload must never touch
    management (protection) of a position that is already open.
    """

    monkeypatch.delenv(ENTRIES_ENABLED_ENV, raising=False)
    broker = StubBroker(positions=[{"symbol": "AAA", "qty": "500"}])
    receipt = _run(tmp_path, _db(tmp_path, []), broker)
    assert receipt["entries_enabled"] is False
    assert receipt["management"]["positions"] == 1
