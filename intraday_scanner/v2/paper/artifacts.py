"""Artifact writers for paper lifecycle outputs."""

from __future__ import annotations

import json
from pathlib import Path

from intraday_scanner.v2.paper.lifecycle import PaperAuditEvent, PaperLifecycleResult
from intraday_scanner.v2.reports.writers import AlphaLabPaths, write_csv_rows, write_json


def write_paper_artifacts(paths: AlphaLabPaths, result: PaperLifecycleResult) -> dict[str, Path]:
    paths.paper.mkdir(parents=True, exist_ok=True)
    picks_path = paths.paper / "paper_picks.json"
    entries_path = paths.paper / "paper_entries.csv"
    checks_path = paths.paper / "paper_checks.csv"
    exits_path = paths.paper / "paper_exits.csv"
    strategy_pnl_csv_path = paths.paper / "strategy_pnl.csv"
    strategy_pnl_json_path = paths.paper / "strategy_pnl.json"
    calendar_csv_path = paths.paper / "calendar_returns.csv"
    calendar_json_path = paths.paper / "calendar_returns.json"
    audit_log_path = paths.paper / "paper_audit_log.jsonl"
    summary_path = paths.paper / "paper_lifecycle_summary.json"

    write_json(picks_path, [pick.to_dict() for pick in result.picks])
    write_csv_rows(
        entries_path,
        [entry.to_dict() for entry in result.entries],
        (
            "entry_id",
            "pick_id",
            "strategy_id",
            "symbol",
            "direction",
            "entry_time",
            "entry_price",
            "quantity",
            "notional",
            "risk_amount",
            "entry_fee",
            "entry_slippage",
            "status",
        ),
    )
    write_csv_rows(
        checks_path,
        [check.to_dict() for check in result.checks],
        (
            "check_id",
            "entry_id",
            "check_time",
            "symbol",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "stop_hit",
            "target_hit",
            "decision",
            "warnings",
        ),
    )
    write_csv_rows(
        exits_path,
        [exit_record.to_dict() for exit_record in result.exits],
        (
            "exit_id",
            "entry_id",
            "pick_id",
            "strategy_id",
            "symbol",
            "direction",
            "exit_time",
            "exit_price",
            "exit_reason",
            "gross_pnl",
            "net_pnl",
            "return_pct",
            "r_multiple",
            "entry_fee",
            "exit_fee",
            "total_fees",
            "entry_slippage",
            "exit_slippage",
            "total_slippage",
        ),
    )
    write_csv_rows(
        strategy_pnl_csv_path,
        [row.to_dict() for row in result.strategy_pnl],
        (
            "strategy_id",
            "trade_count",
            "wins",
            "losses",
            "gross_pnl",
            "net_pnl",
            "return_on_equity",
            "fees_paid",
            "slippage_paid",
            "average_r",
            "best_trade",
            "worst_trade",
        ),
    )
    write_json(strategy_pnl_json_path, [row.to_dict() for row in result.strategy_pnl])
    write_csv_rows(
        calendar_csv_path,
        [row.to_dict() for row in result.calendar_returns],
        (
            "market_date",
            "entry_count",
            "exit_count",
            "wins",
            "losses",
            "gross_pnl",
            "net_pnl",
            "return_on_equity",
        ),
    )
    write_json(calendar_json_path, [row.to_dict() for row in result.calendar_returns])
    write_audit_log(audit_log_path, result.audit_events)
    write_json(summary_path, result.summary())

    return {
        "paper_picks": picks_path,
        "paper_entries": entries_path,
        "paper_checks": checks_path,
        "paper_exits": exits_path,
        "strategy_pnl_csv": strategy_pnl_csv_path,
        "strategy_pnl_json": strategy_pnl_json_path,
        "calendar_returns_csv": calendar_csv_path,
        "calendar_returns_json": calendar_json_path,
        "paper_audit_log": audit_log_path,
        "paper_lifecycle_summary": summary_path,
    }


def write_audit_log(path: Path, events: tuple[PaperAuditEvent, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            json.dump(event.to_dict(), handle, sort_keys=True)
            handle.write("\n")
