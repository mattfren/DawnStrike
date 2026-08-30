from __future__ import annotations

import sqlite3

from intraday_scanner.performance.account_session_reporting import (
    build_account_session_report,
    public_account_session_report,
)
from intraday_scanner.performance.canonical_account_ledger import CanonicalAccountLedger
from intraday_scanner.storage.migrations import run_migrations


def _account(account_id: str = "paper-total") -> dict[str, object]:
    return {
        "account_id": account_id,
        "opening_equity_cents": 100_000,
        "strategy_id": "official-v5",
        "strategy_version": "v5.1",
        "execution_policy_version": "paper.v1",
        "cost_model_version": "cost.v1",
    }


def _session(day: str, status: str = "CLOSED") -> dict[str, object]:
    return {"session_id": f"session-{day}", "market_date": day, "status": status}


def _seed_expected(path, sessions: list[dict[str, object]]) -> None:
    with sqlite3.connect(path) as connection:
        run_migrations(connection)
        for session in sessions:
            connection.execute(
                """INSERT INTO expected_market_sessions
                   (session_id, market_date, exchange, session_open_utc,
                    session_close_utc, status, calendar_source,
                    calendar_source_hash_sha256, created_at, research_only,
                    broker_execution_enabled, payload_json)
                   VALUES (?, ?, 'NYSE', ?, ?, ?, 'test', 'calendar-hash',
                           '2026-08-30T00:00:00+00:00', 1, 0, '{}')""",
                (
                    session["session_id"],
                    session["market_date"],
                    f"{session['market_date']}T13:30:00+00:00",
                    f"{session['market_date']}T20:00:00+00:00",
                    session["status"],
                ),
            )


def test_no_rows_waits_for_canonical_ledger(tmp_path):
    path = tmp_path / "report.sqlite"
    _seed_expected(path, [_session("2026-08-28")])
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "WAITING_FOR_CANONICAL_ACCOUNT_LEDGER"
    assert report["compound_return_pct"] is None


def test_missing_session_row_is_incomplete_expected_sessions(tmp_path):
    path = tmp_path / "report.sqlite"
    sessions = [_session("2026-08-27"), _session("2026-08-28")]
    _seed_expected(path, sessions)
    service = CanonicalAccountLedger(path, account_id="paper-total")
    result = service.build_and_persist(
        account=_account(),
        expected_sessions=[sessions[0]],
        no_trade_receipts=[
            {
                "receipt_id": "nt-27",
                "account_id": "paper-total",
                "session_id": "session-2026-08-27",
                "market_date": "2026-08-27",
                "authoritative": True,
            }
        ],
    )
    assert result.rows[0]["status"] == "AUTHENTICATED_NO_TRADE"
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "INCOMPLETE_EXPECTED_SESSIONS"
    assert report["missing_count"] == 1


def test_authenticated_no_trade_is_zero_but_missing_is_not(tmp_path):
    path = tmp_path / "report.sqlite"
    session = _session("2026-08-28")
    _seed_expected(path, [session])
    service = CanonicalAccountLedger(path, account_id="paper-total")
    service.build_and_persist(
        account=_account(),
        expected_sessions=[session],
        no_trade_receipts=[
            {
                "receipt_id": "nt-28",
                "account_id": "paper-total",
                "session_id": "session-2026-08-28",
                "market_date": "2026-08-28",
                "authoritative": True,
            }
        ],
    )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "COMPLETE"
    assert report["no_trade_count"] == 1
    assert report["compound_return_pct"] == 0.0


def test_trade_row_reports_compound_only_for_complete_window(tmp_path):
    path = tmp_path / "report.sqlite"
    sessions = [_session("2026-08-27"), _session("2026-08-28")]
    _seed_expected(path, sessions)
    service = CanonicalAccountLedger(path, account_id="paper-total")
    service.build_and_persist(
        account=_account(),
        expected_sessions=sessions,
        trades=[
            {
                "trade_id": "trade-27",
                "market_date": "2026-08-27",
                "gross_pnl_cents": 1_300,
                "fees_cents": 100,
                "slippage_cents": 100,
                "net_pnl_cents": 1_100,
                "fill_truth_authenticated": True,
            }
        ],
        no_trade_receipts=[
            {
                "receipt_id": "nt-28",
                "account_id": "paper-total",
                "session_id": "session-2026-08-28",
                "market_date": "2026-08-28",
                "authoritative": True,
            }
        ],
    )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "COMPLETE"
    assert report["complete_count"] == 2
    assert report["compound_return_pct"] == 1.1
    assert report["geometric_mean_daily_return_pct"] is not None


def test_partial_fill_truth_blocks_compound_metric(tmp_path):
    path = tmp_path / "report.sqlite"
    session = _session("2026-08-28")
    _seed_expected(path, [session])
    service = CanonicalAccountLedger(path, account_id="paper-total")
    service.build_and_persist(
        account=_account(),
        expected_sessions=[session],
        trades=[{"trade_id": "untrusted", "market_date": "2026-08-28"}],
    )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    assert report["compound_return_pct"] is None


def test_v5_and_v6_are_not_combined(tmp_path):
    path = tmp_path / "report.sqlite"
    session = _session("2026-08-28")
    _seed_expected(path, [session])
    for account_id, cohort, strategy_id in (
        ("v5", "official_forward_paper", "official-v5"),
        ("v6", "shadow_challenger", "challenger-v6"),
    ):
        account = {**_account(account_id), "strategy_id": strategy_id, "cohort": cohort}
        CanonicalAccountLedger(path, account_id=account_id).build_and_persist(
            account=account,
            expected_sessions=[session],
            no_trade_receipts=[
                {
                    "receipt_id": f"nt-{account_id}",
                    "account_id": account_id,
                    "session_id": "session-2026-08-28",
                    "market_date": "2026-08-28",
                    "authoritative": True,
                }
            ],
        )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "INCOMPLETE_EXPECTED_SESSIONS"
    assert set(report["by_version"]) == {"v5", "v6"}
    assert report["compound_return_pct"] is None
    public = public_account_session_report(report)
    assert public and public["research_only"] is True
    assert "raw" not in str(public).lower()
