from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from intraday_scanner.alpha.commit_bridge import (
    CommitBridge,
    FillTruthIdentity,
    NoTradeBridge,
    NoTradeIdentity,
)
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.performance.account_session_reporting import (
    build_account_session_report,
    public_account_session_report,
)
from intraday_scanner.performance.canonical_account_ledger import CanonicalAccountLedger
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


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
    return {"session_id": f"XNYS:{day}:regular", "market_date": day, "status": status}


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


def _no_trade_receipt(
    path: Path,
    receipt_id: str,
    day: str,
    account_id: str = "paper-total",
    strategy_id: str = "official-v5",
    strategy_version: str = "v5.1",
):
    payload: dict[str, object] = {
        "schema_version": "dawnstrike.session.no_trade.v1",
        "receipt_id": receipt_id,
        "account_id": account_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "market_date": day,
        "session_id": f"XNYS:{day}:regular",
        "run_id": f"run-{day}",
        "status": "FINALIZED",
        "decision": "NO_TRADE",
        "no_entry": True,
        "source_artifact_hash_sha256": "a" * 64,
        "source_config_hash_sha256": "b" * 64,
        "calendar_source_hash_sha256": "c" * 64,
        "code_sha": "d" * 40,
        "created_at": f"{day}T21:00:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    store = SQLiteScanStore(path)
    assert store.persist_no_trade_session_receipt(payload) is True
    result = NoTradeBridge(store).resolve(
        receipt_id,
        identity=NoTradeIdentity(
            account_id=account_id,
            market_date=day,
            session_id=f"XNYS:{day}:regular",
            run_id=f"run-{day}",
            strategy_id=strategy_id,
            strategy_version=strategy_version,
        ),
    )
    assert result is not None
    return result


def _fill_truth(path: Path, day: str, account_id: str = "paper-total"):
    payload: dict[str, object] = {
        "schema_version": "dawnstrike.filltruth.commit.v1",
        "receipt_id": f"filltruth:{day}",
        "account_id": account_id,
        "strategy_id": "official-v5",
        "strategy_version": "v5.1",
        "symbol": "NOVA",
        "market_date": day,
        "session_id": f"XNYS:{day}:regular",
        "run_id": f"run-{day}",
        "fill_id": f"fill-{day}",
        "order_id": f"order-{day}",
        "position_id": f"position-{day}",
        "execution_status": "CLOSED",
        "fill_truth_status": "COMMITTED",
        "committed": True,
        "side": "buy",
        "quantity": 10.0,
        "entry_price": 100.0,
        "exit_price": 101.106,
        "spread_cost_cents": 2,
        "slippage_cost_cents": 3,
        "fees_cents": 1,
        "regulatory_cost_cents": 0,
        "borrow_cost_cents": 0,
        "source_artifact_hash_sha256": "e" * 64,
        "code_sha": "f" * 40,
        "frozen_window": f"{day}T14:30:00+00:00/{day}T20:00:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
        "entry_at": f"{day}T14:31:00+00:00",
        "exit_at": f"{day}T15:31:00+00:00",
        "created_at": f"{day}T21:00:00+00:00",
    }
    payload["receipt_hash_sha256"] = hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()
    store = SQLiteScanStore(path)
    assert store.persist_committed_fill_truth_receipt(payload) is True
    result = CommitBridge(store).resolve(
        str(payload["receipt_id"]),
        identity=FillTruthIdentity(
            account_id=account_id,
            market_date=day,
            strategy_id="official-v5",
            strategy_version="v5.1",
            symbol="NOVA",
            run_id=f"run-{day}",
            session_id=f"XNYS:{day}:regular",
            fill_id=f"fill-{day}",
        ),
    )
    assert result is not None
    return result


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
        no_trade_receipts=[_no_trade_receipt(path, "nt-27", "2026-08-27")],
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
        no_trade_receipts=[_no_trade_receipt(path, "nt-28", "2026-08-28")],
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
                    "fill_truth": _fill_truth(path, "2026-08-27"),
                }
        ],
        no_trade_receipts=[_no_trade_receipt(path, "nt-28", "2026-08-28")],
    )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "COMPLETE"
    assert report["complete_count"] == 2
    assert report["compound_return_pct"] == 1.1
    assert report["geometric_mean_daily_return_pct"] is not None


def test_release_lineage_binds_current_session_without_rewriting_history(tmp_path):
    path = tmp_path / "report.sqlite"
    account_id = "paper-total"
    historical = _session("2026-08-27")
    current = _session("2026-08-28")
    _seed_expected(path, [historical, current])
    account = _account(account_id)
    service = CanonicalAccountLedger(path, account_id=account_id)

    def lineage(day: str, release_sha: str) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "account_id": account_id,
                    "session_id": f"XNYS:{day}:regular",
                    "market_date": day,
                    "calendar_source_hash_sha256": "calendar-hash",
                    "release_sha": release_sha,
                    "research_only": True,
                    "broker_execution_enabled": False,
                }
            ).encode("utf-8")
        ).hexdigest()

    service.build_and_persist(
        account=account,
        expected_sessions=[historical],
        no_trade_receipts=[_no_trade_receipt(path, "nt-27", "2026-08-27")],
        lineage_sha256=lineage("2026-08-27", "a" * 40),
    )
    service.build_and_persist(
        account=account,
        expected_sessions=[current],
        no_trade_receipts=[_no_trade_receipt(path, "nt-28", "2026-08-28")],
        lineage_sha256=lineage("2026-08-28", "b" * 40),
    )

    report = build_account_session_report(
        path,
        market_date="2026-08-28",
        account_id=account_id,
        code_sha="b" * 40,
    )
    assert report["status"] == "COMPLETE"
    assert report["complete_count"] == 2
    assert report["current_session_lineage_match"] is True
    assert report["current_session_lineage_sha256"] == (
        report["expected_current_session_lineage_sha256"]
    )


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
                _no_trade_receipt(
                    path,
                    f"nt-{account_id}",
                    "2026-08-28",
                    account_id,
                    strategy_id,
                    "v5.1",
                )
            ],
        )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "INCOMPLETE_EXPECTED_SESSIONS"
    assert set(report["by_version"]) == {"v5", "v6"}
    assert report["compound_return_pct"] is None
    public = public_account_session_report(report)
    assert public and public["research_only"] is True
    assert "raw" not in str(public).lower()
    selected = build_account_session_report(
        path, market_date="2026-08-28", account_id="v5"
    )
    assert selected["status"] == "COMPLETE"
    assert selected["account_id"] == "v5"
    assert selected["version_bucket"] == "v5"
    assert selected["cohort"] == "official_forward_paper"
    assert selected["strategy_id"] == "official-v5"
    assert len(selected["series"]) == 1


def test_missing_return_cannot_leave_report_marked_complete(tmp_path: Path) -> None:
    path = tmp_path / "missing-return.sqlite"
    session = _session("2026-08-28")
    _seed_expected(path, [session])
    CanonicalAccountLedger(path, account_id="paper-total").build_and_persist(
        account=_account(),
        expected_sessions=[session],
        no_trade_receipts=[_no_trade_receipt(path, "nt-missing-return", "2026-08-28")],
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE paper_account_daily_ledger SET net_return_pct = NULL "
            "WHERE account_id = ? AND market_date = ?",
            ("paper-total", "2026-08-28"),
        )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    assert report["compound_return_pct"] is None


def test_unsafe_persisted_account_contract_blocks_report(tmp_path: Path) -> None:
    path = tmp_path / "unsafe-account.sqlite"
    session = _session("2026-08-28")
    _seed_expected(path, [session])
    CanonicalAccountLedger(path, account_id="paper-total").build_and_persist(
        account=_account(),
        expected_sessions=[session],
        no_trade_receipts=[_no_trade_receipt(path, "nt-unsafe", "2026-08-28")],
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE paper_accounts SET research_only = 0 WHERE account_id = ?",
            ("paper-total",),
        )
    report = build_account_session_report(path, market_date="2026-08-28")
    assert report["status"] == "WAITING_FOR_AUTHENTICATED_FILL_TRUTH"
    assert report["unsafe_ledger_count"] == 1
    assert report["compound_return_pct"] is None


def test_multi_account_report_does_not_truncate_rows_globally(tmp_path: Path) -> None:
    path = tmp_path / "multi-account.sqlite"
    sessions = [_session("2026-08-27"), _session("2026-08-28")]
    _seed_expected(path, sessions)
    for account_id, strategy_id in (("paper-a", "official-v5-a"), ("paper-b", "official-v5-b")):
        account = {**_account(account_id), "strategy_id": strategy_id}
        CanonicalAccountLedger(path, account_id=account_id).build_and_persist(
            account=account,
            expected_sessions=sessions,
            no_trade_receipts=[
                _no_trade_receipt(
                    path,
                    f"nt-{account_id}-27",
                    "2026-08-27",
                    account_id,
                    strategy_id,
                    "v5.1",
                ),
                _no_trade_receipt(
                    path,
                    f"nt-{account_id}-28",
                    "2026-08-28",
                    account_id,
                    strategy_id,
                    "v5.1",
                ),
            ],
        )
    report = build_account_session_report(path, market_date="2026-08-28", window_days=2)
    assert report["status"] == "INCOMPLETE_EXPECTED_SESSIONS"
    assert len(report["series"]) == 2
    assert sorted(item["ledger_row_count"] for item in report["series"]) == [2, 2]
