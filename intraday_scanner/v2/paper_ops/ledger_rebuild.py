"""Read-only PaperOps ledger rebuild and state comparison."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.models import PaperRunMode
from intraday_scanner.v2.paper_ops.storage import (
    read_json,
    read_jsonl,
    upsert_rows,
    write_csv,
    write_json,
)

CALENDAR_FIELDNAMES = (
    "date",
    "mode",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "data_snapshot_id",
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "trades_opened",
    "trades_closed",
    "pending_orders",
    "open_positions",
    "wins",
    "losses",
    "flats",
    "average_r",
    "expectancy_r",
    "exposure_pct",
    "fees_paid",
    "slippage_estimate",
    "warnings",
    "run_id",
)


@dataclass(frozen=True)
class LedgerRebuildResult:
    status: str
    pending_orders: tuple[dict[str, object], ...]
    open_positions: tuple[dict[str, object], ...]
    closed_positions: tuple[dict[str, object], ...]
    account_rows: tuple[dict[str, object], ...]
    calendar_rows: tuple[dict[str, object], ...]
    calendar_mismatches: tuple[str, ...]
    account_mismatches: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: str = "v2.paper_ops_ledger_rebuild.v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "account_mismatches": list(self.account_mismatches),
            "account_rows": list(self.account_rows),
            "calendar_mismatches": list(self.calendar_mismatches),
            "calendar_rows": list(self.calendar_rows),
            "closed_positions": list(self.closed_positions),
            "open_positions": list(self.open_positions),
            "pending_orders": list(self.pending_orders),
            "schema_version": self.schema_version,
            "status": self.status,
            "warnings": list(self.warnings),
        }


def rebuild_ledger(
    *,
    output_root: Path = Path("data/v2_paper_ops"),
    write_rebuilt: bool = False,
) -> LedgerRebuildResult:
    paths = PaperOpsPaths.create(output_root)
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    registry = _registry(paths)
    starting_equity = _starting_equity(paths)
    pending: dict[str, dict[str, object]] = {}
    open_positions: dict[str, dict[str, object]] = {}
    closed_positions: dict[str, dict[str, object]] = {}
    realized_by_key: dict[tuple[str, str], float] = {}
    warnings: list[str] = []

    for event in events:
        event_type = str(event.get("event_type", ""))
        event_mode = str(event.get("mode", ""))
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        payload_mode = _payload_mode(payload) or event_mode
        if payload_mode != event_mode:
            warnings.append(
                f"{event.get('event_id')}: event mode {event_mode} differs "
                f"from payload mode {payload_mode}"
            )
        mode = (
            event_mode if event_mode in {item.value for item in PaperRunMode} else payload_mode
        )
        strategy_id = str(event.get("strategy_id") or payload.get("strategy_id") or "")
        key = (mode, strategy_id)
        if event_type == "paper_order_created":
            order_id = str(payload.get("order_id", ""))
            if order_id:
                pending[order_id] = dict(payload)
        elif event_type == "paper_fill":
            order_id = str(payload.get("order_id", ""))
            pending.pop(order_id, None)
        elif event_type == "paper_position_opened":
            position_id = str(payload.get("position_id", ""))
            if position_id:
                open_positions[position_id] = dict(payload)
        elif event_type in {"paper_position_checked_no_action", "paper_position_marked_to_market"}:
            position_id = str(payload.get("position_id", ""))
            if position_id and position_id in open_positions:
                open_positions[position_id] = dict(payload)
        elif event_type == "paper_position_closed":
            position_id = str(payload.get("position_id", ""))
            if position_id:
                open_positions.pop(position_id, None)
                closed_positions[position_id] = dict(payload)
                realized_by_key[key] = realized_by_key.get(key, 0.0) + _to_float(
                    payload.get("net_pnl")
                )

    account_rows = _account_rows(registry, starting_equity, realized_by_key, open_positions)
    calendar_rows = _calendar_rows_from_events(events, registry, starting_equity, open_positions)
    if write_rebuilt:
        _upsert_rebuilt_calendar(paths, calendar_rows)
    calendar_mismatches = _compare_calendar(paths, calendar_rows)
    account_mismatches = _compare_accounts(paths, account_rows)
    status = "passed" if not calendar_mismatches and not account_mismatches else "mismatch"
    result = LedgerRebuildResult(
        status=status,
        pending_orders=tuple(pending.values()),
        open_positions=tuple(open_positions.values()),
        closed_positions=tuple(closed_positions.values()),
        account_rows=tuple(account_rows),
        calendar_rows=tuple(calendar_rows),
        calendar_mismatches=tuple(calendar_mismatches),
        account_mismatches=tuple(account_mismatches),
        warnings=tuple(dict.fromkeys(warnings)),
    )
    _write_reports(paths, result)
    if write_rebuilt:
        write_json(paths.reconciliation / "rebuilt_state.json", result.to_dict())
    return result


def _registry(paths: PaperOpsPaths) -> list[dict[str, object]]:
    payload = read_json(paths.state / "strategy_registry.json", [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _starting_equity(paths: PaperOpsPaths) -> float:
    payload = read_json(paths.state / "paper_ops_config.json", {})
    if isinstance(payload, dict):
        return float(payload.get("starting_equity", 100_000.0))
    return 100_000.0


def _account_rows(
    registry: list[dict[str, object]],
    starting_equity: float,
    realized_by_key: dict[tuple[str, str], float],
    open_positions: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    modes = tuple(item.value for item in PaperRunMode)
    rows: list[dict[str, object]] = []
    for mode in modes:
        for strategy in registry:
            strategy_id = str(strategy["strategy_id"])
            unrealized = sum(
                _to_float(position.get("unrealized_pnl"))
                for position in open_positions.values()
                if _row_mode(position) == mode and position.get("strategy_id") == strategy_id
            )
            realized = realized_by_key.get((mode, strategy_id), 0.0)
            rows.append(
                {
                    "mode": mode,
                    "strategy_id": strategy_id,
                    "strategy_version": strategy["strategy_version"],
                    "starting_equity": starting_equity,
                    "realized_pnl": realized,
                    "unrealized_pnl": unrealized,
                    "current_equity": starting_equity + realized + unrealized,
                }
            )
    return rows


def _calendar_rows_from_events(
    events: list[dict[str, object]],
    registry: list[dict[str, object]],
    starting_equity: float,
    _final_open_positions: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    dates = sorted({str(event.get("trade_date")) for event in events if event.get("trade_date")})
    rows: list[dict[str, object]] = []
    cumulative: dict[tuple[str, str], float] = {}
    peak_equity: dict[tuple[str, str], float] = {}
    pending: dict[str, dict[str, object]] = {}
    open_positions: dict[str, dict[str, object]] = {}
    for row_date in dates:
        day_events_all = [event for event in events if event.get("trade_date") == row_date]
        for event in day_events_all:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            event_type = str(event.get("event_type", ""))
            if event_type == "paper_order_created":
                order_id = str(payload.get("order_id", ""))
                if order_id:
                    pending[order_id] = dict(payload)
            elif event_type == "paper_fill":
                pending.pop(str(payload.get("order_id", "")), None)
            elif event_type == "paper_position_opened":
                position_id = str(payload.get("position_id", ""))
                if position_id:
                    open_positions[position_id] = dict(payload)
            elif event_type in {
                "paper_position_checked_no_action",
                "paper_position_marked_to_market",
            }:
                position_id = str(payload.get("position_id", ""))
                if position_id and position_id in open_positions:
                    open_positions[position_id] = dict(payload)
            elif event_type == "paper_position_closed":
                position_id = str(payload.get("position_id", ""))
                if position_id:
                    open_positions.pop(position_id, None)
                mode = str(event.get("mode", ""))
                strategy_id = str(event.get("strategy_id") or payload.get("strategy_id") or "")
                key = (mode, strategy_id)
                cumulative[key] = cumulative.get(key, 0.0) + _to_float(payload.get("net_pnl"))
        modes = sorted({str(event.get("mode")) for event in day_events_all if event.get("mode")})
        for mode in modes:
            for strategy in registry:
                strategy_id = str(strategy["strategy_id"])
                key = (mode, strategy_id)
                day_events = [
                    event
                    for event in events
                    if event.get("trade_date") == row_date
                    and event.get("mode") == mode
                    and event.get("strategy_id") == strategy_id
                ]
                closes = _payloads(day_events, "paper_position_closed")
                fills = _payloads(day_events, "paper_fill")
                realized = sum(_to_float(payload.get("net_pnl")) for payload in closes)
                unrealized = sum(
                    _to_float(position.get("unrealized_pnl"))
                    for position in open_positions.values()
                    if _row_mode(position) == mode and position.get("strategy_id") == strategy_id
                )
                fees_paid = sum(_to_float(payload.get("fee")) for payload in closes + fills)
                slippage = sum(_to_float(payload.get("slippage")) for payload in closes + fills)
                wins = sum(1 for payload in closes if _to_float(payload.get("net_pnl")) > 0)
                losses = sum(1 for payload in closes if _to_float(payload.get("net_pnl")) < 0)
                flats = sum(1 for payload in closes if _to_float(payload.get("net_pnl")) == 0)
                open_count = sum(
                    1
                    for position in open_positions.values()
                    if _row_mode(position) == mode and position.get("strategy_id") == strategy_id
                )
                pending_count = sum(
                    1
                    for order in pending.values()
                    if _row_mode(order) == mode and order.get("strategy_id") == strategy_id
                )
                cumulative_pnl = cumulative.get(key, 0.0)
                equity = starting_equity + cumulative_pnl + unrealized
                peak_equity[key] = max(peak_equity.get(key, starting_equity), equity)
                drawdown = (
                    (equity - peak_equity[key]) / peak_equity[key]
                    if peak_equity[key]
                    else 0.0
                )
                rows.append(
                    {
                        "data_snapshot_id": "ledger_rebuild",
                        "date": row_date,
                        "ending_equity": equity,
                        "average_r": 0.0,
                        "expectancy_r": 0.0,
                        "exposure_pct": 0.0,
                        "fees_paid": fees_paid,
                        "flats": flats,
                        "losses": losses,
                        "mode": mode,
                        "open_positions": open_count,
                        "pending_orders": pending_count,
                        "run_id": f"paper_ops:ledger_rebuild:{row_date}:{mode}:{strategy_id}",
                        "slippage_estimate": slippage,
                        "starting_equity": starting_equity,
                        "strategy_id": strategy_id,
                        "strategy_status": strategy.get("strategy_status", "unknown"),
                        "strategy_version": strategy.get("strategy_version", "unknown"),
                        "realized_pnl": realized,
                        "unrealized_pnl": unrealized,
                        "total_pnl": realized + unrealized,
                        "daily_return_pct": (realized + unrealized) / starting_equity,
                        "cumulative_return_pct": (equity - starting_equity) / starting_equity,
                        "drawdown_pct": min(0.0, drawdown),
                        "trades_opened": len(fills),
                        "trades_closed": len(closes),
                        "warnings": "",
                        "wins": wins,
                    }
                )
    return rows


def _upsert_rebuilt_calendar(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    upsert_rows(
        paths.calendar / "strategy_daily_returns.csv",
        rows,
        ("date", "mode", "strategy_id"),
        CALENDAR_FIELDNAMES,
    )
    stored = _read_calendar(paths.calendar / "strategy_daily_returns.csv")
    write_json(paths.calendar / "strategy_daily_returns.json", stored if stored else rows)


def _payloads(events: list[dict[str, object]], event_type: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for event in events:
        if event.get("event_type") != event_type:
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def _compare_calendar(paths: PaperOpsPaths, rebuilt_rows: list[dict[str, object]]) -> list[str]:
    stored = _read_calendar(paths.calendar / "strategy_daily_returns.csv")
    by_key = {
        (row["date"], row["mode"], row["strategy_id"]): row
        for row in stored
        if {"date", "mode", "strategy_id"}.issubset(row)
    }
    mismatches: list[str] = []
    for row in rebuilt_rows:
        key = (str(row["date"]), str(row["mode"]), str(row["strategy_id"]))
        stored_row = by_key.get(key)
        if stored_row is None:
            has_activity = (
                _to_float(row.get("daily_return_pct")) != 0
                or int(_to_float(row.get("trades_opened")))
                or int(_to_float(row.get("trades_closed")))
            )
            if has_activity:
                mismatches.append(f"missing stored calendar row {key}")
            continue
        for field in ("realized_pnl", "daily_return_pct", "trades_opened", "trades_closed"):
            if abs(_to_float(stored_row.get(field)) - _to_float(row.get(field))) > 0.0001:
                mismatches.append(f"calendar mismatch {key} {field}")
    return sorted(set(mismatches))


def _compare_accounts(paths: PaperOpsPaths, rebuilt_rows: list[dict[str, object]]) -> list[str]:
    stored = read_json(paths.state / "paper_accounts.json", {})
    if not isinstance(stored, dict):
        return ["paper_accounts.json is not an object"]
    stored_accounts = {
        str(row.get("strategy_id")): row
        for row in stored.get("accounts", [])
        if isinstance(row, dict)
    }
    forward_rows = [row for row in rebuilt_rows if row["mode"] == PaperRunMode.FORWARD.value]
    mismatches: list[str] = []
    for row in forward_rows:
        stored_row = stored_accounts.get(str(row["strategy_id"]))
        if not stored_row:
            mismatches.append(f"missing account {row['strategy_id']}")
            continue
        if (
            abs(_to_float(stored_row.get("current_equity")) - _to_float(row.get("current_equity")))
            > 0.01
        ):
            mismatches.append(f"account equity mismatch {row['strategy_id']}")
    return mismatches


def _read_calendar(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_reports(paths: PaperOpsPaths, result: LedgerRebuildResult) -> None:
    write_json(paths.reconciliation / "ledger_rebuild_latest.json", result.to_dict())
    write_csv(
        paths.reconciliation / "calendar_rebuild_diff.csv",
        [{"mismatch": item} for item in result.calendar_mismatches],
        ("mismatch",),
    )
    write_csv(
        paths.reconciliation / "account_rebuild_diff.csv",
        [{"mismatch": item} for item in result.account_mismatches],
        ("mismatch",),
    )
    lines = [
        "# PaperOps Ledger Rebuild",
        "",
        f"- Status: `{result.status}`",
        f"- Pending orders rebuilt: `{len(result.pending_orders)}`",
        f"- Open positions rebuilt: `{len(result.open_positions)}`",
        f"- Closed positions rebuilt: `{len(result.closed_positions)}`",
        f"- Calendar mismatches: `{len(result.calendar_mismatches)}`",
        f"- Account mismatches: `{len(result.account_mismatches)}`",
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in result.warnings or ("None.",))
    (paths.reconciliation / "ledger_rebuild_latest.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _row_mode(row: dict[str, object]) -> str:
    mode = row.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    joined = " ".join(str(row.get(key, "")) for key in ("order_id", "position_id", "run_id"))
    for item in PaperRunMode:
        if f":{item.value}:" in joined:
            return item.value
    return PaperRunMode.FORWARD.value


def _payload_mode(row: dict[str, object]) -> str | None:
    mode = row.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    joined = " ".join(str(row.get(key, "")) for key in ("order_id", "position_id", "run_id"))
    for item in PaperRunMode:
        if f":{item.value}:" in joined or joined.startswith(f"{item.value}:"):
            return item.value
    return None


def _to_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0
