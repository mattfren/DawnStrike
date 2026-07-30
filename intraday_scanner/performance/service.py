"""Canonical performance reconciliation over Dawnstrike's existing raw tables."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.performance.contracts import (
    Cohort,
    PerformanceRow,
    RecordStatus,
    as_decimal,
    money_to_cents,
    normalize_cohort,
    percentage_from_prices,
    safe_float,
    stable_hash,
)
from intraday_scanner.storage.migrations import run_migrations


class CanonicalPerformanceService:
    """Build one deterministic, cohort-separated performance read model.

    The service consumes existing signal, outcome, paper-position, fill, and
    benchmark tables. It never turns an absent price into zero and never
    blends official paper rows with research or backtest rows.
    """

    DEFAULT_STRATEGY_ID = "alphaops_v4"
    DEFAULT_STRATEGY_VERSION = "dawnstrike-alphaops-v4"

    def __init__(
        self,
        db_path: str | Path,
        *,
        strategy_id: str = DEFAULT_STRATEGY_ID,
        strategy_version: str = DEFAULT_STRATEGY_VERSION,
    ) -> None:
        self.db_path = Path(db_path)
        self.strategy_id = strategy_id
        self.strategy_version = strategy_version

    def reconcile(
        self,
        *,
        market_date: str | None = None,
        persist: bool = True,
        now: str | None = None,
    ) -> dict[str, Any]:
        calculated_at = now or _utc_now()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            run_migrations(connection)
            inputs = self._read_inputs(connection, market_date=market_date)
            benchmark = inputs["benchmark"]
            rows = [
                *self._position_rows(
                    inputs["positions"], inputs["fills"], benchmark, calculated_at
                ),
                *self._research_rows(
                    inputs["signals"], inputs["outcomes"], benchmark, calculated_at
                ),
                *self._strategy_trade_rows(inputs["strategy_trades"], benchmark, calculated_at),
            ]
            rows.sort(
                key=lambda item: (
                    item.market_date,
                    item.cohort.value,
                    item.rank or 999999,
                    item.record_id,
                )
            )
            input_hash = stable_hash(inputs["hash_inputs"])
            rows = [_replace_input_hash(row, input_hash) for row in rows]
            daily = _aggregate_daily(rows, calculated_at, input_hash)
            issues = _issues(rows, calculated_at)
            if persist:
                self._persist(connection, rows, daily, issues, market_date=market_date)
            return {
                "status": _overall_status(rows, daily),
                "market_date": market_date,
                "calculated_at": calculated_at,
                "input_hash_sha256": input_hash,
                "row_count": len(rows),
                "daily_count": len(daily),
                "issue_count": len(issues),
                "rows": [row.to_dict() for row in rows],
                "daily": daily,
                "issues": issues,
            }

    def load_public_data(self, *, days: int = 30, row_limit: int = 250) -> dict[str, Any]:
        """Return a bounded, secret-free payload for the static public site."""

        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            run_migrations(connection)
            daily_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT * FROM portfolio_daily_performance
                    ORDER BY market_date DESC, cohort ASC, strategy_id ASC
                    LIMIT ?
                    """,
                    (max(1, days) * 8,),
                )
            ]
            performance_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT record_id, market_date, ticker, cohort, strategy_id,
                           strategy_version, record_status, notional_cents,
                           gross_pnl_cents, gross_return_pct, fees_cents, slippage_cents,
                           net_pnl_cents, return_pct, benchmark_return_pct,
                           excess_return_pct, source_refs_json, source_hash_sha256,
                           input_hash_sha256, observed_at, reconciled_at,
                           quarantine_reason
                    FROM portfolio_performance_rows
                    ORDER BY market_date DESC, cohort ASC, rank ASC, record_id ASC
                    LIMIT ?
                    """,
                    (max(1, row_limit),),
                )
            ]
        return {
            "schema_version": "dawnstrike.public_performance.v1",
            "generated_at": _utc_now(),
            "research_only": True,
            "live_trading_enabled": False,
            "daily": [_public_daily(row) for row in daily_rows],
            "rows": [_public_row(row) for row in performance_rows],
            "limits": {"days": days, "row_limit": row_limit},
        }

    def _read_inputs(
        self, connection: sqlite3.Connection, *, market_date: str | None
    ) -> dict[str, Any]:
        signals = _select_rows(connection, "historical_signals", market_date)
        outcomes = _select_rows(connection, "signal_outcomes", market_date)
        positions = _select_rows(connection, "paper_positions", market_date)
        fills = _select_rows(connection, "paper_trade_fills", market_date)
        strategy_trades = _select_rows(connection, "strategy_paper_trades", market_date)
        benchmark_rows = _select_rows(connection, "benchmark_performance", market_date)
        benchmark = _benchmark_lookup(benchmark_rows)
        return {
            "signals": signals,
            "outcomes": outcomes,
            "positions": positions,
            "fills": fills,
            "strategy_trades": strategy_trades,
            "benchmark": benchmark,
            "hash_inputs": {
                "signals": signals,
                "outcomes": outcomes,
                "positions": positions,
                "fills": fills,
                "strategy_trades": strategy_trades,
                "benchmark": benchmark_rows,
            },
        }

    def _position_rows(
        self,
        positions: list[dict[str, Any]],
        fills: list[dict[str, Any]],
        benchmark: dict[str, float],
        calculated_at: str,
    ) -> list[PerformanceRow]:
        fills_by_position: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fill in fills:
            fills_by_position[str(fill.get("position_id") or "")].append(fill)
        rows: list[PerformanceRow] = []
        for raw in positions:
            payload = _payload(raw)
            position_id = str(raw.get("position_id") or "")
            market_date = str(raw.get("market_date") or "")
            ticker = str(raw.get("ticker") or "").upper()
            status = str(raw.get("status") or "").upper()
            strategy_id = str(payload.get("strategy_id") or self.strategy_id)
            strategy_version = str(payload.get("strategy_version") or self.strategy_version)
            cohort = normalize_cohort(payload.get("cohort"), default=Cohort.OFFICIAL_FORWARD_PAPER)
            entry = safe_float(raw.get("entry_price"))
            exit_price = safe_float(raw.get("exit_price"))
            quantity = safe_float(raw.get("quantity"))
            notional_cents = money_to_cents(raw.get("notional"))
            if notional_cents is None and entry is not None and quantity is not None:
                notional_cents = money_to_cents(entry * quantity)
            gross_pnl_cents = money_to_cents(
                (exit_price - entry) * quantity
                if exit_price is not None and entry is not None and quantity is not None
                else None
            )
            gross_return_pct = percentage_from_prices(entry, exit_price)
            source_net_cents = money_to_cents(raw.get("realized_pnl"))
            fill_costs = _fill_costs(fills_by_position.get(position_id, []))
            fees_cents = _optional_money(payload, "fees_cents", "fees")
            slippage_cents = _optional_money(payload, "slippage_cost_cents", "slippage_cost")
            if slippage_cents is None:
                slippage_cents = fill_costs["slippage_cents"]
            if status in {"CLOSED", "REALIZED", "COMPLETE"}:
                record_status = (
                    RecordStatus.REALIZED
                    if source_net_cents is not None or gross_pnl_cents is not None
                    else RecordStatus.MISSING_OUTCOME
                )
            elif status in {"OPEN", "HELD", "UNREALIZED"}:
                record_status = RecordStatus.UNREALIZED
            else:
                record_status = RecordStatus.MISSING_OUTCOME
            quarantine_reason = _position_quarantine(
                raw, market_date, ticker, quantity, notional_cents
            )
            if quarantine_reason:
                record_status = RecordStatus.QUARANTINED
            net_pnl_cents = source_net_cents if fees_cents is not None else None
            if (
                net_pnl_cents is None
                and record_status == RecordStatus.REALIZED
                and gross_pnl_cents is not None
            ):
                if fees_cents is not None and slippage_cents is not None:
                    net_pnl_cents = gross_pnl_cents - fees_cents - slippage_cents
            return_pct = None
            if return_pct is None and net_pnl_cents is not None and notional_cents:
                return_pct = round(net_pnl_cents / notional_cents * 100.0, 4)
            observed_at = (
                str(payload.get("source_last_bar_at") or raw.get("updated_at") or "") or None
            )
            benchmark_return = benchmark.get(market_date)
            source_hash = str(payload.get("source_bar_hash_sha256") or stable_hash(raw))
            source_refs = _source_refs(
                position_id,
                payload.get("selection_id"),
                payload.get("source_url"),
                payload.get("source_bar_hash_sha256"),
            )
            row = PerformanceRow(
                record_id=f"paper_position:{position_id}",
                market_date=market_date,
                ticker=ticker,
                cohort=cohort,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                signal_id=str(raw.get("signal_id") or "") or None,
                rank=_int_or_none(payload.get("rank")),
                record_status=record_status,
                entry_price=entry,
                exit_price=exit_price,
                quantity=quantity,
                notional_cents=notional_cents,
                gross_pnl_cents=gross_pnl_cents,
                gross_return_pct=gross_return_pct,
                fees_cents=fees_cents,
                slippage_cents=slippage_cents,
                net_pnl_cents=net_pnl_cents,
                return_pct=return_pct,
                benchmark_return_pct=benchmark_return,
                excess_return_pct=_excess(return_pct, benchmark_return),
                source_refs=source_refs,
                source_hash_sha256=source_hash,
                input_hash_sha256="",
                observed_at=observed_at,
                reconciled_at=calculated_at,
                quarantine_reason=quarantine_reason,
            )
            rows.append(row)
        return rows

    def _research_rows(
        self,
        signals: list[dict[str, Any]],
        outcomes: list[dict[str, Any]],
        benchmark: dict[str, float],
        calculated_at: str,
    ) -> list[PerformanceRow]:
        outcome_by_signal = {str(row.get("signal_id") or ""): row for row in outcomes}
        rows: list[PerformanceRow] = []
        for raw in signals:
            signal_id = str(raw.get("signal_id") or "")
            market_date = str(raw.get("market_date") or "")
            ticker = str(raw.get("ticker") or "").upper()
            payload = _payload(raw)
            outcome = outcome_by_signal.get(signal_id)
            outcome_payload = _payload(outcome or {})
            entry = safe_float((outcome or {}).get("entry_price"))
            exit_price = safe_float((outcome or {}).get("close_price"))
            gross_return = percentage_from_prices(entry, exit_price)
            outcome_status = str((outcome or {}).get("outcome_status") or "").lower()
            no_trade = outcome_status == "not_triggered" or bool(
                str(raw.get("no_trade_reason") or "").strip()
            )
            if no_trade:
                record_status = RecordStatus.NO_TRADE
            elif gross_return is not None and outcome_status in {
                "complete_sourced",
                "complete",
                "audited",
            }:
                record_status = RecordStatus.REALIZED
            else:
                record_status = RecordStatus.MISSING_OUTCOME
            notional_cents = None
            gross_pnl_cents = None
            gross_return_pct = gross_return
            explicit_fees = _optional_money(outcome_payload, "fees_cents", "fees")
            explicit_slippage = _optional_money(
                outcome_payload, "slippage_cost_cents", "slippage_cost"
            )
            net_pnl_cents = None
            if (
                record_status == RecordStatus.REALIZED
                and gross_pnl_cents is not None
                and explicit_fees is not None
                and explicit_slippage is not None
            ):
                net_pnl_cents = gross_pnl_cents - explicit_fees - explicit_slippage
            benchmark_return = benchmark.get(market_date)
            source_hash = str(
                outcome_payload.get("source_bar_hash_sha256")
                or payload.get("source_bar_hash_sha256")
                or stable_hash({"signal": raw, "outcome": outcome or {}})
            )
            source_refs = _source_refs(
                signal_id,
                raw.get("source_url"),
                outcome_payload.get("source_url"),
                outcome_payload.get("source_bar_hash_sha256"),
            )
            rows.append(
                PerformanceRow(
                    record_id=f"research_signal:{signal_id}",
                    market_date=market_date,
                    ticker=ticker,
                    cohort=Cohort.ALPHAOPS_RESEARCH,
                    strategy_id=str(payload.get("strategy_id") or self.strategy_id),
                    strategy_version=str(payload.get("model_version") or self.strategy_version),
                    signal_id=signal_id or None,
                    rank=_int_or_none(raw.get("rank")),
                    record_status=record_status,
                    entry_price=entry,
                    exit_price=exit_price,
                    quantity=None,
                    notional_cents=notional_cents,
                    gross_pnl_cents=gross_pnl_cents,
                    gross_return_pct=gross_return_pct,
                    fees_cents=explicit_fees,
                    slippage_cents=explicit_slippage,
                    net_pnl_cents=net_pnl_cents,
                    return_pct=None,
                    benchmark_return_pct=benchmark_return,
                    excess_return_pct=None,
                    source_refs=source_refs,
                    source_hash_sha256=source_hash,
                    input_hash_sha256="",
                    observed_at=str(
                        outcome_payload.get("source_last_bar_at")
                        or outcome_payload.get("imported_at")
                        or ""
                    )
                    or None,
                    reconciled_at=calculated_at,
                    quarantine_reason=None,
                )
            )
        return rows

    def _strategy_trade_rows(
        self,
        trades: list[dict[str, Any]],
        benchmark: dict[str, float],
        calculated_at: str,
    ) -> list[PerformanceRow]:
        rows: list[PerformanceRow] = []
        for raw in trades:
            payload = _payload(raw)
            market_date = str(raw.get("market_date") or "")
            ticker = str(raw.get("ticker") or "").upper()
            entry = safe_float(raw.get("entry_fill_price"))
            exit_price = safe_float(raw.get("exit_fill_price"))
            quantity = safe_float(raw.get("quantity"))
            status = (
                RecordStatus.REALIZED
                if exit_price is not None and entry is not None
                else RecordStatus.UNREALIZED
            )
            if not str(raw.get("trade_id") or "") or not market_date or not ticker:
                status = RecordStatus.QUARANTINED
            fees_cents = money_to_cents(raw.get("fees"))
            slippage_cents = money_to_cents(raw.get("slippage_cost"))
            net_pnl_cents = money_to_cents(raw.get("net_pnl"))
            gross_pnl_cents = money_to_cents(
                (exit_price - entry) * quantity
                if entry is not None and exit_price is not None and quantity is not None
                else None
            )
            gross_return_pct = percentage_from_prices(entry, exit_price)
            notional_cents = money_to_cents(raw.get("notional"))
            return_pct = safe_float(raw.get("net_return_pct"))
            benchmark_return = benchmark.get(market_date)
            source_hash = str(raw.get("source_bar_hash_sha256") or stable_hash(raw))
            rows.append(
                PerformanceRow(
                    record_id=f"strategy_trade:{raw.get('trade_id')}",
                    market_date=market_date,
                    ticker=ticker,
                    cohort=normalize_cohort(
                        payload.get("cohort") or raw.get("cohort"),
                        default=Cohort.HISTORICAL_BACKTEST,
                    ),
                    strategy_id=str(raw.get("strategy_id") or self.strategy_id),
                    strategy_version=str(raw.get("strategy_version") or self.strategy_version),
                    signal_id=str(raw.get("signal_id") or "") or None,
                    rank=_int_or_none(payload.get("rank")),
                    record_status=status,
                    entry_price=entry,
                    exit_price=exit_price,
                    quantity=quantity,
                    notional_cents=notional_cents,
                    gross_pnl_cents=gross_pnl_cents,
                    gross_return_pct=gross_return_pct,
                    fees_cents=fees_cents,
                    slippage_cents=slippage_cents,
                    net_pnl_cents=net_pnl_cents,
                    return_pct=return_pct,
                    benchmark_return_pct=benchmark_return,
                    excess_return_pct=_excess(return_pct, benchmark_return),
                    source_refs=_source_refs(
                        raw.get("trade_id"), raw.get("source_bar_hash_sha256")
                    ),
                    source_hash_sha256=source_hash,
                    input_hash_sha256="",
                    observed_at=str(raw.get("exit_time") or raw.get("created_at") or "") or None,
                    reconciled_at=calculated_at,
                    quarantine_reason="missing_trade_identity"
                    if status == RecordStatus.QUARANTINED
                    else None,
                )
            )
        return rows

    def _persist(
        self,
        connection: sqlite3.Connection,
        rows: list[PerformanceRow],
        daily: list[dict[str, Any]],
        issues: list[dict[str, Any]],
        *,
        market_date: str | None,
    ) -> None:
        if market_date:
            dates = {row.market_date for row in rows}
            dates.add(market_date)
            for day in dates:
                connection.execute(
                    "DELETE FROM portfolio_performance_rows WHERE market_date = ?", (day,)
                )
                connection.execute(
                    "DELETE FROM performance_reconciliation_issues WHERE market_date = ?", (day,)
                )
                connection.execute(
                    "DELETE FROM portfolio_daily_performance WHERE market_date = ?", (day,)
                )
        else:
            # The daily publisher performs a full rebuild. Clearing the
            # canonical read model first prevents stale rows surviving when
            # raw inputs are empty or an upstream table disappears.
            connection.execute("DELETE FROM portfolio_performance_rows")
            connection.execute("DELETE FROM performance_reconciliation_issues")
            connection.execute("DELETE FROM portfolio_daily_performance")
        for row in rows:
            connection.execute(
                """
                INSERT INTO portfolio_performance_rows (
                    record_id, market_date, ticker, cohort, strategy_id, strategy_version,
                    signal_id, rank, record_status, entry_price, exit_price, quantity,
                    notional_cents, gross_pnl_cents, gross_return_pct, fees_cents,
                    slippage_cents,
                    net_pnl_cents, return_pct, benchmark_return_pct, excess_return_pct,
                    source_refs_json, source_hash_sha256, input_hash_sha256,
                    observed_at, reconciled_at, quarantine_reason, payload_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    row.record_id,
                    row.market_date,
                    row.ticker,
                    row.cohort.value,
                    row.strategy_id,
                    row.strategy_version,
                    row.signal_id,
                    row.rank,
                    row.record_status.value,
                    row.entry_price,
                    row.exit_price,
                    row.quantity,
                    row.notional_cents,
                    row.gross_pnl_cents,
                    row.gross_return_pct,
                    row.fees_cents,
                    row.slippage_cents,
                    row.net_pnl_cents,
                    row.return_pct,
                    row.benchmark_return_pct,
                    row.excess_return_pct,
                    json.dumps(list(row.source_refs), sort_keys=True),
                    row.source_hash_sha256,
                    row.input_hash_sha256,
                    row.observed_at,
                    row.reconciled_at,
                    row.quarantine_reason,
                    json.dumps(row.to_dict(), sort_keys=True),
                ),
            )
        for daily_row in daily:
            connection.execute(
                """
                INSERT INTO portfolio_daily_performance (
                    performance_id, market_date, cohort, strategy_id, strategy_version,
                    status, gross_pnl_cents, fees_cents, slippage_cents, gross_return_pct,
                    net_pnl_cents,
                    allocated_capital_cents, return_pct, benchmark_return_pct,
                    excess_return_pct, realized_trade_count, unrealized_trade_count,
                    missing_outcome_count, quarantined_count, source_hash_sha256,
                    input_hash_sha256, calculated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    daily_row["performance_id"],
                    daily_row["market_date"],
                    daily_row["cohort"],
                    daily_row["strategy_id"],
                    daily_row["strategy_version"],
                    daily_row["status"],
                    daily_row["gross_pnl_cents"],
                    daily_row["fees_cents"],
                    daily_row["slippage_cents"],
                    daily_row["gross_return_pct"],
                    daily_row["net_pnl_cents"],
                    daily_row["allocated_capital_cents"],
                    daily_row["return_pct"],
                    daily_row["benchmark_return_pct"],
                    daily_row["excess_return_pct"],
                    daily_row["realized_trade_count"],
                    daily_row["unrealized_trade_count"],
                    daily_row["missing_outcome_count"],
                    daily_row["quarantined_count"],
                    daily_row["source_hash_sha256"],
                    daily_row["input_hash_sha256"],
                    daily_row["calculated_at"],
                    json.dumps(daily_row, sort_keys=True),
                ),
            )
        for issue in issues:
            connection.execute(
                """
                INSERT INTO performance_reconciliation_issues
                (
                    issue_id, record_id, market_date, severity, issue_code, message,
                    created_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue["issue_id"],
                    issue.get("record_id"),
                    issue["market_date"],
                    issue["severity"],
                    issue["issue_code"],
                    issue["message"],
                    issue["created_at"],
                    json.dumps(issue, sort_keys=True),
                ),
            )


def _select_rows(
    connection: sqlite3.Connection, table: str, market_date: str | None
) -> list[dict[str, Any]]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if not exists:
        return []
    if market_date:
        try:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE market_date = ? ORDER BY market_date ASC, rowid ASC",
                (market_date,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid ASC").fetchall()
    else:
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid ASC").fetchall()
    return [dict(row) for row in rows]


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _benchmark_lookup(rows: Iterable[dict[str, Any]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        if symbol not in {"SPY", "^GSPC", "QQQ", "BENCHMARK"}:
            continue
        value = safe_float(row.get("return_close"))
        day = str(row.get("market_date") or "")
        if value is not None and day:
            grouped[day].append(value)
    return {day: round(sum(values) / len(values), 4) for day, values in grouped.items()}


def _fill_costs(rows: Iterable[dict[str, Any]]) -> dict[str, int | None]:
    slippage = 0
    found = False
    for row in rows:
        bps = as_decimal(row.get("slippage_bps"))
        notional = as_decimal(row.get("gross_notional"))
        if bps is None or notional is None:
            continue
        found = True
        slippage += money_to_cents(notional * bps / 10000) or 0
    return {"slippage_cents": slippage if found else None}


def _optional_money(payload: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in payload:
            return money_to_cents(payload.get(key))
    return None


def _position_quarantine(
    raw: dict[str, Any],
    market_date: str,
    ticker: str,
    quantity: float | None,
    notional_cents: int | None,
) -> str | None:
    if not market_date or not ticker:
        return "missing_trade_identity"
    if quantity is not None and quantity <= 0:
        return "non_positive_quantity"
    if notional_cents is not None and notional_cents <= 0:
        return "non_positive_notional"
    return None


def _source_refs(*values: Any) -> tuple[str, ...]:
    refs = {str(value).strip() for value in values if value is not None and str(value).strip()}
    return tuple(sorted(refs))


def _replace_input_hash(row: PerformanceRow, input_hash: str) -> PerformanceRow:
    return PerformanceRow(
        **{
            **row.to_dict(),
            "cohort": row.cohort,
            "record_status": row.record_status,
            "source_refs": row.source_refs,
            "input_hash_sha256": input_hash,
        }
    )


def _excess(return_pct: float | None, benchmark: float | None) -> float | None:
    if return_pct is None or benchmark is None:
        return None
    return round(return_pct - benchmark, 4)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _aggregate_daily(
    rows: list[PerformanceRow], calculated_at: str, input_hash: str
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[PerformanceRow]] = defaultdict(list)
    for row in rows:
        grouped[(row.market_date, row.cohort.value, row.strategy_id, row.strategy_version)].append(
            row
        )
    output: list[dict[str, Any]] = []
    for (market_date, cohort, strategy_id, strategy_version), group in sorted(grouped.items()):
        realized = [row for row in group if row.record_status == RecordStatus.REALIZED]
        unrealized = [row for row in group if row.record_status == RecordStatus.UNREALIZED]
        missing = [row for row in group if row.record_status == RecordStatus.MISSING_OUTCOME]
        quarantined = [row for row in group if row.record_status == RecordStatus.QUARANTINED]
        no_trade = [row for row in group if row.record_status == RecordStatus.NO_TRADE]
        gross = _sum_optional(row.gross_pnl_cents for row in realized)
        net = _sum_if_complete(row.net_pnl_cents for row in realized)
        fees = _sum_if_complete(row.fees_cents for row in realized)
        slippage = _sum_if_complete(row.slippage_cents for row in realized)
        capital = _sum_optional(row.notional_cents for row in realized)
        net_return = round(net / capital * 100.0, 4) if net is not None and capital else None
        gross_return_values = [
            row.gross_return_pct for row in realized if row.gross_return_pct is not None
        ]
        gross_return = (
            round(gross / capital * 100.0, 4)
            if gross is not None and capital
            else (
                round(sum(gross_return_values) / len(gross_return_values), 4)
                if gross_return_values
                else None
            )
        )
        benchmark_values = [
            row.benchmark_return_pct for row in realized if row.benchmark_return_pct is not None
        ]
        benchmark = (
            round(sum(benchmark_values) / len(benchmark_values), 4) if benchmark_values else None
        )
        status = (
            "NO_TRADE"
            if not realized and not unrealized and not missing and not quarantined and no_trade
            else "COMPLETE"
        )
        if missing or unrealized:
            status = "PARTIAL"
        if quarantined:
            status = "DEGRADED"
        source_hash = stable_hash(sorted(row.source_hash_sha256 for row in group))
        output.append(
            {
                "performance_id": f"{market_date}:{cohort}:{strategy_id}:{strategy_version}",
                "market_date": market_date,
                "cohort": cohort,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "status": status,
                "gross_pnl_cents": gross,
                "fees_cents": fees,
                "slippage_cents": slippage,
                "net_pnl_cents": net,
                "allocated_capital_cents": capital,
                "return_pct": net_return,
                "gross_return_pct": gross_return,
                "benchmark_return_pct": benchmark,
                "excess_return_pct": _excess(net_return, benchmark),
                "realized_trade_count": len(realized),
                "unrealized_trade_count": len(unrealized),
                "missing_outcome_count": len(missing),
                "quarantined_count": len(quarantined),
                "no_trade_count": len(no_trade),
                "source_hash_sha256": source_hash,
                "input_hash_sha256": input_hash,
                "calculated_at": calculated_at,
                "cost_status": "complete"
                if fees is not None and slippage is not None
                else "missing_cost_component",
                "return_basis": "net_after_costs"
                if net_return is not None
                else "gross_observed_or_missing",
            }
        )
    return output


def _sum_optional(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _sum_if_complete(values: Iterable[int | None]) -> int | None:
    values_list = list(values)
    return (
        sum(value for value in values_list if value is not None)
        if values_list and all(value is not None for value in values_list)
        else None
    )


def _issues(rows: Iterable[PerformanceRow], created_at: str) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in rows:
        if row.record_status in {RecordStatus.MISSING_OUTCOME, RecordStatus.UNREALIZED}:
            issues.append(
                {
                    "issue_id": stable_hash([row.record_id, row.record_status.value]),
                    "record_id": row.record_id,
                    "market_date": row.market_date,
                    "severity": "warning",
                    "issue_code": row.record_status.value,
                    "message": (
                        "Outcome is not a realized, after-cost observation; it is excluded "
                        "from realized totals."
                    ),
                    "created_at": created_at,
                }
            )
        if row.record_status == RecordStatus.QUARANTINED:
            issues.append(
                {
                    "issue_id": stable_hash([row.record_id, row.quarantine_reason]),
                    "record_id": row.record_id,
                    "market_date": row.market_date,
                    "severity": "error",
                    "issue_code": "quarantined",
                    "message": row.quarantine_reason or "Record failed reconciliation checks.",
                    "created_at": created_at,
                }
            )
    return issues


def _overall_status(rows: list[PerformanceRow], daily: list[dict[str, Any]]) -> str:
    if not rows:
        return "NO_DATA"
    if any(item["status"] == "DEGRADED" for item in daily):
        return "DEGRADED"
    if any(item["status"] == "PARTIAL" for item in daily):
        return "PARTIAL"
    return "COMPLETE"


def _public_daily(row: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "market_date",
        "cohort",
        "strategy_id",
        "strategy_version",
        "status",
        "gross_pnl_cents",
        "fees_cents",
        "slippage_cents",
        "net_pnl_cents",
        "allocated_capital_cents",
        "return_pct",
        "gross_return_pct",
        "benchmark_return_pct",
        "excess_return_pct",
        "realized_trade_count",
        "unrealized_trade_count",
        "missing_outcome_count",
        "quarantined_count",
        "no_trade_count",
        "source_hash_sha256",
        "input_hash_sha256",
        "calculated_at",
        "cost_status",
        "return_basis",
    )
    return {key: row.get(key) for key in allowed}


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    row["source_refs"] = _json_list(row.pop("source_refs_json", "[]"))
    return row


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
