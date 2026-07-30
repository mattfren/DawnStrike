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
from intraday_scanner.performance.paper_ops import load_paper_ops
from intraday_scanner.storage.migrations import run_migrations


class CanonicalPerformanceService:
    """Build one deterministic, cohort-separated performance read model.

    The service consumes existing signal, outcome, paper-position, fill, and
    benchmark tables. It never turns an absent price into zero and never
    blends official paper rows with research or backtest rows.
    """

    DEFAULT_STRATEGY_ID = "alphaops_v4"
    DEFAULT_STRATEGY_VERSION = "dawnstrike-alphaops-v4"
    CALCULATION_VERSION = "dawnstrike-performance-v2"
    EXECUTION_POLICY_VERSION = "unregistered-policy"

    def __init__(
        self,
        db_path: str | Path,
        *,
        strategy_id: str = DEFAULT_STRATEGY_ID,
        strategy_version: str = DEFAULT_STRATEGY_VERSION,
        paper_ops_root: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.strategy_id = strategy_id
        self.strategy_version = strategy_version
        self.paper_ops_root = Path(paper_ops_root) if paper_ops_root is not None else None

    def reconcile(
        self,
        *,
        market_date: str | None = None,
        persist: bool = True,
        now: str | None = None,
    ) -> dict[str, Any]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            if persist:
                run_migrations(connection)
            inputs = self._read_inputs(connection, market_date=market_date)
            input_hash = stable_hash(inputs["hash_inputs"])
            calculated_at = (
                now
                or _existing_calculated_at(connection, input_hash)
                or _utc_now()
            )
            benchmark = inputs["benchmark"]
            rows = [
                *self._position_rows(
                    inputs["positions"], inputs["fills"], benchmark, calculated_at
                ),
                *self._research_rows(
                    inputs["signals"], inputs["outcomes"], benchmark, calculated_at
                ),
                *self._strategy_trade_rows(inputs["strategy_trades"], benchmark, calculated_at),
                *self._paper_ops_rows(inputs["paper_ops"]["rows"], benchmark, calculated_at),
            ]
            rows.sort(
                key=lambda item: (
                    item.market_date,
                    item.cohort.value,
                    item.rank or 999999,
                    item.record_id,
                )
            )
            rows = [_replace_input_hash(row, input_hash) for row in rows]
            daily = _aggregate_daily(
                rows,
                calculated_at,
                input_hash,
                [*inputs["equity"], *inputs["paper_ops"]["equity"]],
            )
            _add_cumulative_metrics(daily)
            issues = _issues(rows, calculated_at)
            issues.extend(
                {
                    **issue,
                    "created_at": calculated_at,
                }
                for issue in inputs["paper_ops"]["issues"]
            )
            if persist:
                self._persist(connection, rows, daily, issues, market_date=market_date)
            row_payload = [row.to_dict() for row in rows]
            output_hash = stable_hash({"rows": row_payload, "daily": daily, "issues": issues})
            return {
                "status": _overall_status(rows, daily),
                "market_date": market_date,
                "calculated_at": calculated_at,
                "input_hash_sha256": input_hash,
                "output_hash_sha256": output_hash,
                "row_count": len(rows),
                "daily_count": len(daily),
                "issue_count": len(issues),
                "rows": row_payload,
                "daily": daily,
                "issues": issues,
                "paper_ops_reconciliation": inputs["paper_ops"],
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
                    ORDER BY CASE cohort
                               WHEN 'official_forward_paper' THEN 0
                               WHEN 'alphaops_signal_research' THEN 1
                               WHEN 'shadow_challenger' THEN 2
                               WHEN 'historical_backtest' THEN 3
                               ELSE 4
                             END,
                             market_date DESC, strategy_id ASC
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
                           quarantine_reason, execution_policy_version,
                           trade_count, open_position_count, unrealized_pnl_cents,
                           record_type
                    FROM portfolio_performance_rows
                    ORDER BY CASE cohort
                               WHEN 'official_forward_paper' THEN 0
                               WHEN 'alphaops_signal_research' THEN 1
                               WHEN 'shadow_challenger' THEN 2
                               WHEN 'historical_backtest' THEN 3
                               ELSE 4
                             END,
                             market_date DESC, rank ASC, record_id ASC
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
        equity_rows = _select_rows(connection, "portfolio_equity_observations", market_date)
        paper_ops = load_paper_ops(self.paper_ops_root, market_date=market_date)
        benchmark = _benchmark_lookup(benchmark_rows)
        return {
            "signals": signals,
            "outcomes": outcomes,
            "positions": positions,
            "fills": fills,
            "strategy_trades": strategy_trades,
            "equity": equity_rows,
            "paper_ops": {
                **paper_ops,
                "rows": paper_ops["rows"],
                "equity": paper_ops["equity"],
            },
            "benchmark": benchmark,
            "hash_inputs": {
                "signals": signals,
                "outcomes": outcomes,
                "positions": positions,
                "fills": fills,
                "strategy_trades": strategy_trades,
                "equity": equity_rows,
                "benchmark": benchmark_rows,
                "paper_ops": paper_ops["hash_inputs"],
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
                execution_policy_version=str(
                    payload.get("execution_policy_version") or self.EXECUTION_POLICY_VERSION
                ),
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
                    execution_policy_version=str(
                        outcome_payload.get("execution_policy_version")
                        or payload.get("execution_policy_version")
                        or self.EXECUTION_POLICY_VERSION
                    ),
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
                    execution_policy_version=str(
                        raw.get("execution_policy_version")
                        or payload.get("execution_policy_version")
                        or self.EXECUTION_POLICY_VERSION
                    ),
                )
            )
        return rows

    def _paper_ops_rows(
        self,
        source_rows: list[dict[str, Any]],
        benchmark: dict[str, float],
        calculated_at: str,
    ) -> list[PerformanceRow]:
        rows: list[PerformanceRow] = []
        for raw in source_rows:
            market_date = str(raw.get("market_date") or "")
            benchmark_return = benchmark.get(market_date)
            return_pct = safe_float(raw.get("return_pct"))
            rows.append(
                PerformanceRow(
                    record_id=str(raw["record_id"]),
                    market_date=market_date,
                    ticker=str(raw.get("ticker") or "PORTFOLIO"),
                    cohort=raw["cohort"],
                    strategy_id=str(raw.get("strategy_id") or ""),
                    strategy_version=str(raw.get("strategy_version") or ""),
                    signal_id=None,
                    rank=None,
                    record_status=RecordStatus(str(raw.get("record_status") or "quarantined")),
                    entry_price=None,
                    exit_price=None,
                    quantity=None,
                    notional_cents=_int_or_none(raw.get("notional_cents")),
                    gross_pnl_cents=_int_or_none(raw.get("gross_pnl_cents")),
                    gross_return_pct=safe_float(raw.get("gross_return_pct")),
                    fees_cents=_int_or_none(raw.get("fees_cents")),
                    slippage_cents=_int_or_none(raw.get("slippage_cents")),
                    net_pnl_cents=_int_or_none(raw.get("net_pnl_cents")),
                    return_pct=return_pct,
                    benchmark_return_pct=benchmark_return,
                    excess_return_pct=_excess(return_pct, benchmark_return),
                    source_refs=tuple(raw.get("source_refs") or ()),
                    source_hash_sha256=str(raw.get("source_hash_sha256") or stable_hash(raw)),
                    input_hash_sha256="",
                    observed_at=raw.get("observed_at"),
                    reconciled_at=calculated_at,
                    quarantine_reason=raw.get("quarantine_reason"),
                    execution_policy_version=str(
                        raw.get("execution_policy_version") or "unknown-paper-ops-policy"
                    ),
                    trade_count=max(_int_or_none(raw.get("trade_count")) or 0, 0),
                    open_position_count=max(_int_or_none(raw.get("open_position_count")) or 0, 0),
                    unrealized_pnl_cents=_int_or_none(raw.get("unrealized_pnl_cents")),
                    record_type="portfolio_observation",
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
                    observed_at, reconciled_at, quarantine_reason,
                    execution_policy_version, trade_count, open_position_count,
                    unrealized_pnl_cents, record_type, payload_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
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
                    row.execution_policy_version,
                    row.trade_count,
                    row.open_position_count,
                    row.unrealized_pnl_cents,
                    row.record_type,
                    json.dumps(row.to_dict(), sort_keys=True),
                ),
            )
        for daily_row in daily:
            connection.execute(
                """
                INSERT INTO portfolio_daily_performance (
                    performance_id, market_date, cohort, strategy_id, strategy_version,
                    status, gross_pnl_cents, fees_cents, slippage_cents, gross_return_pct,
                    unrealized_pnl_cents, opening_equity_cents, ending_equity_cents,
                    net_pnl_cents, allocated_capital_cents, return_pct, cumulative_return_pct,
                    benchmark_return_pct, excess_return_pct, drawdown_pct, exposure_cents,
                    realized_trade_count, unrealized_trade_count,
                    missing_outcome_count, quarantined_count, source_hash_sha256,
                    input_hash_sha256, calculated_at, execution_policy_version,
                    calculation_version, evidence_state, coverage_json, source_refs_json,
                    payload_json
                ) VALUES (
                    :performance_id, :market_date, :cohort, :strategy_id, :strategy_version,
                    :status, :gross_pnl_cents, :fees_cents, :slippage_cents,
                    :gross_return_pct, :unrealized_pnl_cents, :opening_equity_cents,
                    :ending_equity_cents, :net_pnl_cents, :allocated_capital_cents,
                    :return_pct, :cumulative_return_pct, :benchmark_return_pct,
                    :excess_return_pct, :drawdown_pct, :exposure_cents,
                    :realized_trade_count, :unrealized_trade_count, :missing_outcome_count,
                    :quarantined_count, :source_hash_sha256, :input_hash_sha256,
                    :calculated_at, :execution_policy_version, :calculation_version,
                    :evidence_state, :coverage_json, :source_refs_json, :payload_json
                )
                """,
                {
                    **daily_row,
                    "coverage_json": json.dumps(daily_row["coverage"], sort_keys=True),
                    "source_refs_json": json.dumps(daily_row["source_refs"], sort_keys=True),
                    "payload_json": json.dumps(daily_row, sort_keys=True),
                },
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
                f"SELECT * FROM {table} WHERE market_date <= ? ORDER BY market_date ASC, rowid ASC",
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
    rows: list[PerformanceRow],
    calculated_at: str,
    input_hash: str,
    equity_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[PerformanceRow]] = defaultdict(list)
    equity = _equity_lookup(equity_rows)
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
        explicit_no_trade = bool(no_trade) and not realized and not unrealized and not missing
        gross = _sum_optional(row.gross_pnl_cents for row in realized)
        net = _sum_if_complete(row.net_pnl_cents for row in realized)
        fees = _sum_if_complete(row.fees_cents for row in realized)
        slippage = _sum_if_complete(row.slippage_cents for row in realized)
        if explicit_no_trade:
            gross = net = fees = slippage = 0
        capital = _sum_optional(row.notional_cents for row in realized)
        key = (market_date, cohort, strategy_id, strategy_version)
        equity_observation = equity.get(key, {})
        opening_equity = _int_or_none(equity_observation.get("opening_equity_cents"))
        ending_equity = _int_or_none(equity_observation.get("ending_equity_cents"))
        net_return = _portfolio_return(net, opening_equity)
        gross_return = _portfolio_return(gross, opening_equity)
        benchmark_values = [
            row.benchmark_return_pct for row in realized if row.benchmark_return_pct is not None
        ]
        benchmark = (
            round(sum(benchmark_values) / len(benchmark_values), 4) if benchmark_values else None
        )
        status = "NO_TRADE" if explicit_no_trade and not quarantined else "COMPLETE"
        if (
            missing
            or unrealized
            or (realized and (net is None or fees is None or slippage is None))
        ):
            status = "PARTIAL"
        if realized and benchmark is None:
            status = "PARTIAL"
        if quarantined:
            status = "DEGRADED"
        evidence_state = {
            "COMPLETE": "complete",
            "NO_TRADE": "no_trade",
            "PARTIAL": "pending",
            "DEGRADED": "degraded",
        }[status]
        eligible_count = len(group) - len(no_trade)
        observed_count = len(realized)
        excluded_count = len(quarantined)
        missing_count = len(missing) + len(unrealized)
        coverage_pct = (
            round(observed_count / eligible_count * 100.0, 4) if eligible_count else 100.0
        )
        exposure = _sum_if_complete(row.notional_cents for row in group)
        unrealized_values = [row.gross_pnl_cents for row in unrealized]
        unrealized_values.extend(
            row.unrealized_pnl_cents
            for row in group
            if row.record_type == "portfolio_observation"
            and row.unrealized_pnl_cents is not None
            and row.record_status != RecordStatus.QUARANTINED
        )
        unrealized_pnl = _sum_if_complete(unrealized_values)
        source_refs = _source_refs(*(ref for row in group for ref in row.source_refs))
        source_hash = stable_hash(sorted(row.source_hash_sha256 for row in group))
        policies = sorted({row.execution_policy_version for row in group})
        execution_policy_version = policies[0] if len(policies) == 1 else "mixed"
        realized_trade_count = sum(
            row.trade_count if row.record_type == "portfolio_observation" else 1
            for row in realized
        )
        unrealized_trade_count = sum(
            row.open_position_count
            for row in group
            if row.record_type == "portfolio_observation"
            and row.record_status != RecordStatus.QUARANTINED
        ) + sum(1 for row in unrealized if row.record_type != "portfolio_observation")
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
                "opening_equity_cents": opening_equity,
                "ending_equity_cents": ending_equity,
                "unrealized_pnl_cents": unrealized_pnl,
                "cumulative_return_pct": None,
                "drawdown_pct": None,
                "exposure_cents": exposure,
                "realized_trade_count": realized_trade_count,
                "unrealized_trade_count": unrealized_trade_count,
                "missing_outcome_count": len(missing),
                "quarantined_count": len(quarantined),
                "no_trade_count": len(no_trade),
                "source_hash_sha256": source_hash,
                "input_hash_sha256": input_hash,
                "calculated_at": calculated_at,
                "generated_at": calculated_at,
                "calculation_version": CanonicalPerformanceService.CALCULATION_VERSION,
                "execution_policy_version": execution_policy_version,
                "evidence_state": evidence_state,
                "coverage": {
                    "eligible_count": eligible_count,
                    "observed_count": observed_count,
                    "missing_count": missing_count,
                    "excluded_count": excluded_count,
                    "coverage_pct": coverage_pct,
                },
                "source_refs": list(source_refs),
                "cost_status": "complete"
                if fees is not None and slippage is not None
                else "missing_cost_component",
                "return_basis": "net_after_costs"
                if net_return is not None
                else "gross_observed_or_missing",
            }
        )
    return output


def _equity_lookup(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    output: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        cohort = normalize_cohort(row.get("cohort"), default=Cohort.OFFICIAL_FORWARD_PAPER).value
        key = (
            str(row.get("market_date") or ""),
            cohort,
            str(row.get("strategy_id") or CanonicalPerformanceService.DEFAULT_STRATEGY_ID),
            str(
                row.get("strategy_version") or CanonicalPerformanceService.DEFAULT_STRATEGY_VERSION
            ),
        )
        output[key] = row
    return output


def _portfolio_return(pnl_cents: int | None, opening_equity_cents: int | None) -> float | None:
    if pnl_cents is None or opening_equity_cents is None or opening_equity_cents <= 0:
        return None
    return round(pnl_cents / opening_equity_cents * 100.0, 4)


def _add_cumulative_metrics(daily: list[dict[str, Any]]) -> None:
    states: dict[tuple[str, str, str], dict[str, float | bool]] = {}
    for row in sorted(
        daily,
        key=lambda item: (
            str(item.get("cohort") or ""),
            str(item.get("strategy_id") or ""),
            str(item.get("strategy_version") or ""),
            str(item.get("market_date") or ""),
        ),
    ):
        key = (
            str(row.get("cohort") or ""),
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
        )
        state = states.setdefault(key, {"wealth": 1.0, "peak": 1.0, "invalid": False})
        daily_return = row.get("return_pct")
        if bool(state["invalid"]) or not isinstance(daily_return, (int, float)):
            state["invalid"] = True
            row["cumulative_return_pct"] = None
            row["drawdown_pct"] = None
            continue
        state["wealth"] = float(state["wealth"]) * (1.0 + float(daily_return) / 100.0)
        state["peak"] = max(float(state["peak"]), float(state["wealth"]))
        row["cumulative_return_pct"] = round((float(state["wealth"]) - 1.0) * 100.0, 4)
        row["drawdown_pct"] = round(
            (float(state["wealth"]) / float(state["peak"]) - 1.0) * 100.0, 4
        )


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
        if row.record_type == "portfolio_observation":
            continue
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
    payload = _json_object(row.pop("payload_json", "{}"))
    payload.update(row)
    if not isinstance(payload.get("coverage"), dict):
        payload["coverage"] = _json_object(payload.get("coverage_json", "{}"))
    if not isinstance(payload.get("source_refs"), list):
        payload["source_refs"] = _json_list(payload.get("source_refs_json", "[]"))
    payload["generated_at"] = payload.get("generated_at") or payload.get("calculated_at")
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
        "opening_equity_cents",
        "ending_equity_cents",
        "unrealized_pnl_cents",
        "cumulative_return_pct",
        "benchmark_return_pct",
        "excess_return_pct",
        "drawdown_pct",
        "exposure_cents",
        "realized_trade_count",
        "unrealized_trade_count",
        "missing_outcome_count",
        "quarantined_count",
        "no_trade_count",
        "source_hash_sha256",
        "input_hash_sha256",
        "calculated_at",
        "generated_at",
        "calculation_version",
        "execution_policy_version",
        "evidence_state",
        "coverage",
        "source_refs",
        "cost_status",
        "return_basis",
    )
    return {key: payload.get(key) for key in allowed}


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    row["source_refs"] = _json_list(row.pop("source_refs_json", "[]"))
    return row


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _existing_calculated_at(connection: sqlite3.Connection, input_hash: str) -> str | None:
    """Reuse the calculation timestamp when the canonical inputs are unchanged."""

    try:
        row = connection.execute(
            """
            SELECT calculated_at
            FROM portfolio_daily_performance
            WHERE input_hash_sha256 = ? AND calculated_at IS NOT NULL
            ORDER BY calculated_at ASC, performance_id ASC
            LIMIT 1
            """,
            (input_hash,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT reconciled_at
                FROM portfolio_performance_rows
                WHERE input_hash_sha256 = ? AND reconciled_at IS NOT NULL
                ORDER BY reconciled_at ASC, record_id ASC
                LIMIT 1
                """,
                (input_hash,),
            ).fetchone()
    except sqlite3.Error:
        return None
    value = str(row[0]) if row and row[0] is not None else ""
    return value or None
