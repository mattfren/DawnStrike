"""Read-only PaperOps ledger rebuild and state comparison."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
    _recover_pending_transaction,
)
from intraday_scanner.v2.paper_ops.models import PaperRunMode
from intraday_scanner.v2.paper_ops.storage import (
    read_json,
    read_jsonl,
    write_csv,
    write_json,
)

CALENDAR_FIELDNAMES = (
    "date",
    "mode",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "execution_policy_version",
    "strategy_semantics_fingerprint",
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
    _recover_pending_transaction(paths)
    source_events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    events: list[dict[str, object]] = []
    registry = _registry(paths)
    starting_equity = _starting_equity(paths)
    pending: dict[str, dict[str, object]] = {}
    open_positions: dict[str, dict[str, object]] = {}
    closed_positions: dict[str, dict[str, object]] = {}
    realized_by_key: dict[tuple[str, str, str, str, str], float] = {}
    warnings: list[str] = []
    if not source_events:
        warnings.append("paper ledger contains no events")

    for event in sorted(source_events, key=_event_sort_key):
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
            continue
        events.append(event)
        mode = (
            event_mode if event_mode in {item.value for item in PaperRunMode} else payload_mode
        )
        strategy_id = str(event.get("strategy_id") or payload.get("strategy_id") or "")
        strategy_version = _strategy_version(payload, registry, strategy_id)
        execution_policy_version = _execution_policy_version(payload, registry, strategy_id)
        strategy_semantics_fingerprint = _strategy_semantics_fingerprint(payload)
        key = (
            mode,
            strategy_id,
            strategy_version,
            execution_policy_version,
            strategy_semantics_fingerprint,
        )
        if event_type == "paper_order_created":
            order_id = str(payload.get("order_id", ""))
            if order_id:
                pending[order_id] = dict(payload)
        elif event_type == "paper_order_blocked":
            pending.pop(str(payload.get("order_id", "")), None)
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

    account_rows = _account_rows(
        registry,
        starting_equity,
        realized_by_key,
        open_positions,
        events,
    )
    calendar_rows = _calendar_rows_from_events(events, registry, starting_equity, open_positions)
    if write_rebuilt:
        _upsert_rebuilt_calendar(paths, calendar_rows)
    calendar_mismatches = _compare_calendar(paths, calendar_rows)
    account_mismatches = _compare_accounts(paths, account_rows)
    status = (
        "passed"
        if not calendar_mismatches and not account_mismatches and not warnings
        else "mismatch"
    )
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
    realized_by_key: dict[tuple[str, str, str, str, str], float],
    open_positions: dict[str, dict[str, object]],
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    modes = tuple(item.value for item in PaperRunMode)
    rows: list[dict[str, object]] = []
    for mode in modes:
        for strategy in _strategy_series_for_mode(events, registry, mode):
            strategy_id = str(strategy["strategy_id"])
            strategy_version = str(strategy["strategy_version"])
            execution_policy_version = str(
                strategy.get("execution_policy_version")
                or "legacy_unspecified"
            )
            strategy_semantics_fingerprint = str(
                strategy.get("strategy_semantics_fingerprint") or "unknown"
            )
            unrealized = sum(
                _to_float(position.get("unrealized_pnl"))
                for position in open_positions.values()
                if _row_mode(position) == mode
                and _row_matches_series(
                    position,
                    strategy_id,
                    strategy_version,
                    execution_policy_version,
                    strategy_semantics_fingerprint,
                )
            )
            realized = realized_by_key.get(
                (
                    mode,
                    strategy_id,
                    strategy_version,
                    execution_policy_version,
                    strategy_semantics_fingerprint,
                ),
                0.0,
            )
            rows.append(
                {
                    "mode": mode,
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "execution_policy_version": execution_policy_version,
                    "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
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
    cumulative: dict[tuple[str, str, str, str, str], float] = {}
    peak_equity: dict[tuple[str, str, str, str, str], float] = {}
    previous_equity: dict[tuple[str, str, str, str, str], float] = {}
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
            elif event_type == "paper_order_blocked":
                pending.pop(str(payload.get("order_id", "")), None)
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
                key = (
                    mode,
                    strategy_id,
                    _strategy_version(payload, registry, strategy_id),
                    _execution_policy_version(payload, registry, strategy_id),
                    _strategy_semantics_fingerprint(payload),
                )
                cumulative[key] = cumulative.get(key, 0.0) + _to_float(payload.get("net_pnl"))
        modes = sorted({str(event.get("mode")) for event in day_events_all if event.get("mode")})
        for mode in modes:
            for strategy in _strategy_series_for_mode(events, registry, mode):
                strategy_id = str(strategy["strategy_id"])
                strategy_version = str(strategy["strategy_version"])
                execution_policy_version = str(
                    strategy.get("execution_policy_version")
                    or "legacy_unspecified"
                )
                strategy_semantics_fingerprint = str(
                    strategy.get("strategy_semantics_fingerprint") or "unknown"
                )
                key = (
                    mode,
                    strategy_id,
                    strategy_version,
                    execution_policy_version,
                    strategy_semantics_fingerprint,
                )
                day_events = [
                    event
                    for event in events
                    if event.get("trade_date") == row_date
                    and event.get("mode") == mode
                    and event.get("strategy_id") == strategy_id
                    and _event_matches_series(
                        event,
                        strategy_version,
                        execution_policy_version,
                        strategy_semantics_fingerprint,
                    )
                ]
                has_live_series_state = any(
                    _row_mode(row) == mode
                    and _row_matches_series(
                        row,
                        strategy_id,
                        strategy_version,
                        execution_policy_version,
                        strategy_semantics_fingerprint,
                    )
                    for row in [*pending.values(), *open_positions.values()]
                )
                if not day_events and not has_live_series_state:
                    continue
                closes = _payloads(day_events, "paper_position_closed")
                fills = _payloads(day_events, "paper_fill")
                r_values = [_to_float(payload.get("r_multiple")) for payload in closes]
                realized = sum(_to_float(payload.get("net_pnl")) for payload in closes)
                unrealized = sum(
                    _to_float(position.get("unrealized_pnl"))
                    for position in open_positions.values()
                    if _row_mode(position) == mode
                    and _row_matches_series(
                        position,
                        strategy_id,
                        strategy_version,
                        execution_policy_version,
                        strategy_semantics_fingerprint,
                    )
                )
                fees_paid = sum(_to_float(payload.get("fee")) for payload in closes + fills)
                slippage = sum(_to_float(payload.get("slippage")) for payload in closes + fills)
                wins = sum(1 for payload in closes if _to_float(payload.get("net_pnl")) > 0)
                losses = sum(1 for payload in closes if _to_float(payload.get("net_pnl")) < 0)
                flats = sum(1 for payload in closes if _to_float(payload.get("net_pnl")) == 0)
                open_count = sum(
                    1
                    for position in open_positions.values()
                    if _row_mode(position) == mode
                    and _row_matches_series(
                        position,
                        strategy_id,
                        strategy_version,
                        execution_policy_version,
                        strategy_semantics_fingerprint,
                    )
                )
                pending_count = sum(
                    1
                    for order in pending.values()
                    if _row_mode(order) == mode
                    and _row_matches_series(
                        order,
                        strategy_id,
                        strategy_version,
                        execution_policy_version,
                        strategy_semantics_fingerprint,
                    )
                )
                cumulative_pnl = cumulative.get(key, 0.0)
                equity = starting_equity + cumulative_pnl + unrealized
                prior_equity = previous_equity.get(key, starting_equity)
                daily_pnl = equity - prior_equity
                peak_equity[key] = max(peak_equity.get(key, starting_equity), equity)
                drawdown = (
                    (equity - peak_equity[key]) / peak_equity[key]
                    if peak_equity[key]
                    else 0.0
                )
                gross_exposure = sum(
                    _position_gross_exposure(position)
                    for position in open_positions.values()
                    if _row_mode(position) == mode
                    and _row_matches_series(
                        position,
                        strategy_id,
                        strategy_version,
                        execution_policy_version,
                        strategy_semantics_fingerprint,
                    )
                )
                rows.append(
                    {
                        "data_snapshot_id": "ledger_rebuild",
                        "date": row_date,
                        "ending_equity": equity,
                        "average_r": sum(r_values) / len(r_values) if r_values else 0.0,
                        "expectancy_r": sum(r_values) / len(r_values) if r_values else 0.0,
                        "exposure_pct": gross_exposure / equity if equity > 0 else 0.0,
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
                        "strategy_version": strategy_version,
                        "execution_policy_version": execution_policy_version,
                        "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                        "realized_pnl": realized,
                        "unrealized_pnl": unrealized,
                        "total_pnl": daily_pnl,
                        "daily_return_pct": (
                            daily_pnl / prior_equity if prior_equity else 0.0
                        ),
                        "cumulative_return_pct": (equity - starting_equity) / starting_equity,
                        "drawdown_pct": min(0.0, drawdown),
                        "trades_opened": len(fills),
                        "trades_closed": len(closes),
                        "warnings": "",
                        "wins": wins,
                    }
                )
                previous_equity[key] = equity
    return rows


def _upsert_rebuilt_calendar(paths: PaperOpsPaths, rows: list[dict[str, object]]) -> None:
    stored_rows = _read_calendar(paths.calendar / "strategy_daily_returns.csv")
    reference_rows: list[dict[str, object]] = [
        dict(row)
        for row in stored_rows
        if str(row.get("strategy_status") or "") in {"baseline", "benchmark"}
    ]
    write_csv(
        paths.calendar / "strategy_daily_returns.csv",
        [*reference_rows, *rows],
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
        (
            row["date"],
            row["mode"],
            row["strategy_id"],
            row.get("strategy_version", "unknown"),
            row.get(
                "execution_policy_version",
                "legacy_unspecified",
            ),
            row.get("strategy_semantics_fingerprint", "unknown"),
        ): row
        for row in stored
        if {"date", "mode", "strategy_id"}.issubset(row)
    }
    mismatches: list[str] = []
    rebuilt_keys: set[tuple[str, str, str, str, str, str]] = set()
    for row in rebuilt_rows:
        key = (
            str(row["date"]),
            str(row["mode"]),
            str(row["strategy_id"]),
            str(row.get("strategy_version") or "unknown"),
            str(
                row.get("execution_policy_version")
                or "legacy_unspecified"
            ),
            str(row.get("strategy_semantics_fingerprint") or "unknown"),
        )
        rebuilt_keys.add(key)
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
        for field in (
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
        ):
            if abs(_to_float(stored_row.get(field)) - _to_float(row.get(field))) > 0.0001:
                mismatches.append(f"calendar mismatch {key} {field}")
    for stored_candidate in stored:
        if str(stored_candidate.get("strategy_status") or "") in {
            "baseline",
            "benchmark",
        }:
            continue
        key = (
            str(stored_candidate.get("date") or ""),
            str(stored_candidate.get("mode") or ""),
            str(stored_candidate.get("strategy_id") or ""),
            str(stored_candidate.get("strategy_version") or "unknown"),
            str(
                stored_candidate.get("execution_policy_version")
                or "legacy_unspecified"
            ),
            str(stored_candidate.get("strategy_semantics_fingerprint") or "unknown"),
        )
        if key not in rebuilt_keys:
            mismatches.append(f"stored calendar row has no ledger reconstruction {key}")
    return sorted(set(mismatches))


def _compare_accounts(paths: PaperOpsPaths, rebuilt_rows: list[dict[str, object]]) -> list[str]:
    mismatches: list[str] = []
    for mode in PaperRunMode:
        account_path = (
            paths.state / "paper_accounts.json"
            if mode is PaperRunMode.FORWARD
            else paths.state / f"{mode.value}_paper_accounts.json"
        )
        if not account_path.exists():
            continue
        stored = read_json(account_path, {})
        if not isinstance(stored, dict):
            mismatches.append(f"{account_path.name} is not an object")
            continue
        stored_accounts = {
            (
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_version") or "unknown"),
                str(
                    row.get("execution_policy_version")
                    or "legacy_unspecified"
                ),
                str(row.get("strategy_semantics_fingerprint") or "unknown"),
            ): row
            for row in stored.get("accounts", [])
            if isinstance(row, dict)
        }
        mode_rows = [row for row in rebuilt_rows if row["mode"] == mode.value]
        rebuilt_keys: set[tuple[str, str, str, str]] = set()
        for row in mode_rows:
            key = (
                str(row["strategy_id"]),
                str(row.get("strategy_version") or "unknown"),
                str(
                    row.get("execution_policy_version")
                    or "legacy_unspecified"
                ),
                str(row.get("strategy_semantics_fingerprint") or "unknown"),
            )
            rebuilt_keys.add(key)
            stored_row = stored_accounts.get(key)
            if not stored_row:
                mismatches.append(f"missing {mode.value} account {key}")
                continue
            for field in (
                "starting_equity",
                "current_equity",
                "realized_pnl",
                "unrealized_pnl",
            ):
                if (
                    abs(
                        _to_float(stored_row.get(field))
                        - _to_float(row.get(field))
                    )
                    > 0.01
                ):
                    mismatches.append(
                        f"account {field} mismatch {mode.value} {key}"
                    )
        for key in stored_accounts:
            if key not in rebuilt_keys:
                mismatches.append(f"stored {mode.value} account has no ledger series {key}")
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


def _position_gross_exposure(row: dict[str, object]) -> float:
    mark = _to_float(row.get("last_mark_price") or row.get("entry_price"))
    quantity = _to_float(row.get("quantity"))
    return abs(mark * quantity)


def _strategy_version(
    payload: dict[str, object],
    _registry: list[dict[str, object]],
    _strategy_id: str,
) -> str:
    explicit = str(payload.get("strategy_version") or "")
    if explicit:
        return explicit
    return "unknown"


def _execution_policy_version(
    payload: dict[str, object],
    _registry: list[dict[str, object]],
    _strategy_id: str,
) -> str:
    explicit = str(payload.get("execution_policy_version") or "")
    if explicit:
        return explicit
    return "legacy_unspecified"


def _strategy_semantics_fingerprint(payload: dict[str, object]) -> str:
    return str(payload.get("strategy_semantics_fingerprint") or "unknown")


def _strategy_series_for_mode(
    events: list[dict[str, object]],
    registry: list[dict[str, object]],
    mode: str,
) -> list[dict[str, object]]:
    """Return current and observed archived series without blending their evidence."""

    by_key: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for row in registry:
        strategy_id = str(row.get("strategy_id") or "")
        strategy_version = str(row.get("strategy_version") or "unknown")
        execution_policy_version = str(
            row.get("execution_policy_version")
            or "legacy_unspecified"
        )
        strategy_semantics_fingerprint = str(
            row.get("strategy_semantics_fingerprint") or "unknown"
        )
        if strategy_id:
            by_key[
                (
                    strategy_id,
                    strategy_version,
                    execution_policy_version,
                    strategy_semantics_fingerprint,
                )
            ] = dict(row)
    for event in events:
        if str(event.get("mode") or "") != mode:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        strategy_id = str(event.get("strategy_id") or payload.get("strategy_id") or "")
        if not strategy_id:
            continue
        strategy_version = _strategy_version(payload, registry, strategy_id)
        execution_policy_version = _execution_policy_version(
            payload,
            registry,
            strategy_id,
        )
        strategy_semantics_fingerprint = _strategy_semantics_fingerprint(payload)
        key = (
            strategy_id,
            strategy_version,
            execution_policy_version,
            strategy_semantics_fingerprint,
        )
        if key not in by_key:
            by_key[key] = {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "strategy_status": str(payload.get("strategy_status") or "archived"),
            }
    return [by_key[key] for key in sorted(by_key)]


def _event_matches_series(
    event: dict[str, object],
    strategy_version: str,
    execution_policy_version: str,
    strategy_semantics_fingerprint: str,
) -> bool:
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        str(payload.get("strategy_version") or "unknown") == strategy_version
        and str(payload.get("execution_policy_version") or "legacy_unspecified")
        == execution_policy_version
        and _strategy_semantics_fingerprint(payload) == strategy_semantics_fingerprint
    )


def _row_matches_series(
    row: dict[str, object],
    strategy_id: str,
    strategy_version: str,
    execution_policy_version: str,
    strategy_semantics_fingerprint: str,
) -> bool:
    return (
        str(row.get("strategy_id") or "") == strategy_id
        and str(row.get("strategy_version") or "unknown") == strategy_version
        and str(row.get("execution_policy_version") or "legacy_unspecified")
        == execution_policy_version
        and _strategy_semantics_fingerprint(row) == strategy_semantics_fingerprint
    )


def _event_sort_key(event: dict[str, object]) -> tuple[str, str, int, str, str]:
    """Return deterministic lifecycle order independent of JSONL append history."""

    event_order = {
        "paper_order_created": 10,
        "paper_order_pending_no_fill_data": 20,
        "paper_fill": 30,
        "paper_position_opened": 40,
        "paper_position_checked_no_action": 50,
        "paper_position_marked_to_market": 60,
        "paper_position_closed": 70,
    }
    return (
        str(event.get("trade_date") or ""),
        str(event.get("mode") or ""),
        event_order.get(str(event.get("event_type") or ""), 100),
        str(event.get("strategy_id") or ""),
        str(event.get("event_id") or ""),
    )
