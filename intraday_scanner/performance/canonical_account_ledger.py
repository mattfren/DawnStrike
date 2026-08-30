"""Canonical total-account/session ledger.

The strategy ledgers in :mod:`account_ledger` are useful attribution views, but
they are not a complete account truth source.  This module supplies the small,
deterministic account-level boundary used by the 1% target work:

* one row per expected market session;
* several strategy/trade inputs aggregate into that one row;
* missing evidence remains ``None`` (never an invented zero);
* a no-trade result is accepted only when a persisted receipt is supplied; and
* the same inputs produce the same hashes and row identity on every rerun.

It intentionally has no broker or order-submission dependency.  Callers pass
already-retained research evidence (normally resolved by CommitBridge).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from intraday_scanner.performance.account_contract import TARGET_NET_RETURN_PCT
from intraday_scanner.storage.migrations import run_migrations

RESEARCH_ONLY = True
BROKER_EXECUTION_ENABLED = False
DEFAULT_TARGET_RETURN_PCT = float(TARGET_NET_RETURN_PCT)


class CanonicalLedgerError(RuntimeError):
    """Base error for fail-closed account-ledger construction."""


class LedgerConflictError(CanonicalLedgerError):
    """Two retained facts claim different values for one canonical identity."""


class LedgerEvidenceError(CanonicalLedgerError):
    """Evidence is insufficient to publish a derived account result."""


@dataclass(frozen=True, slots=True)
class LedgerBuildResult:
    """Result of constructing one account's canonical session rows."""

    account_id: str
    rows: tuple[dict[str, Any], ...]
    input_hash_sha256: str
    source_hash_sha256: str
    coverage: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "rows": [dict(row) for row in self.rows],
            "input_hash_sha256": self.input_hash_sha256,
            "source_hash_sha256": self.source_hash_sha256,
            "coverage": dict(self.coverage),
            "research_only": RESEARCH_ONLY,
            "broker_execution_enabled": BROKER_EXECUTION_ENABLED,
        }


class CanonicalAccountLedger:
    """Build and persist one canonical account/session ledger.

    Input rows are intentionally mapping-based to make this boundary usable by
    V5, V6, historical replay, and forward capture without coupling accounting
    to any single strategy table.  Trade rows must carry ``fill_truth`` (or
    ``fill_truth_authenticated=True``) to be considered realized evidence.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        account_id: str | None = None,
        target_return_pct: float = DEFAULT_TARGET_RETURN_PCT,
        code_sha: str = "unknown",
    ) -> None:
        self.db_path = Path(db_path)
        self.account_id = str(account_id or "").strip()
        if target_return_pct <= 0:
            raise ValueError("target_return_pct must be positive")
        self.target_return_pct = float(target_return_pct)
        self.code_sha = str(code_sha or "unknown")

    def build(
        self,
        *,
        account: Mapping[str, Any] | None = None,
        expected_sessions: Iterable[Mapping[str, Any]],
        trades: Iterable[Mapping[str, Any]] = (),
        positions: Iterable[Mapping[str, Any]] = (),
        marks: Iterable[Mapping[str, Any]] = (),
        no_trade_receipts: Iterable[Mapping[str, Any]] = (),
        external_flows: Iterable[Mapping[str, Any]] = (),
        calculated_at: str | None = None,
        evidence_mode: str = "forward_observed",
        lineage_sha256: str | None = None,
    ) -> LedgerBuildResult:
        account_id = self._resolve_account_id(account)
        sessions = _normalize_sessions(expected_sessions)
        if not sessions:
            raise LedgerEvidenceError("at least one expected market session is required")
        trade_rows = _normalize_rows(trades)
        position_rows = _normalize_rows(positions)
        mark_rows = _normalize_rows(marks)
        receipt_rows = _normalize_rows(no_trade_receipts)
        flow_rows = _normalize_rows(external_flows)
        _reject_conflicting_ids(trade_rows, "trade_id")
        _reject_conflicting_ids(position_rows, "position_id")
        _reject_conflicting_ids(receipt_rows, "receipt_id")

        canonical_inputs = {
            "account": dict(account or {"account_id": account_id}),
            "expected_sessions": sessions,
            "trades": trade_rows,
            "positions": position_rows,
            "marks": mark_rows,
            "no_trade_receipts": receipt_rows,
            "external_flows": flow_rows,
            "target_return_pct": self.target_return_pct,
            "evidence_mode": evidence_mode,
            "lineage_sha256": lineage_sha256,
        }
        input_hash = _hash(canonical_inputs)
        source_hash = _hash(
            {
                "expected_sessions": sessions,
                "trades": trade_rows,
                "positions": position_rows,
                "marks": mark_rows,
                "no_trade_receipts": receipt_rows,
                "external_flows": flow_rows,
            }
        )
        as_at = calculated_at or datetime.now(UTC).isoformat()
        opening_equity = _int_value((account or {}).get("opening_equity_cents"))
        if opening_equity is None:
            opening_equity = _int_value((account or {}).get("opening_equity"))
        if opening_equity is None:
            opening_equity = self._load_opening_equity(account_id)
        if opening_equity is None or opening_equity <= 0:
            raise LedgerEvidenceError("positive account opening equity is required")

        by_day_trades = _group_date(trade_rows)
        by_day_positions = _group_date(position_rows)
        by_day_marks = _group_date(mark_rows)
        by_day_receipts = _group_date(receipt_rows)
        by_day_flows = _group_date(flow_rows)
        rows: list[dict[str, Any]] = []
        carried_equity: int | None = opening_equity
        carry_allowed = True
        for session in sessions:
            day = str(session["market_date"])
            day_trades = by_day_trades.get(day, [])
            day_positions = by_day_positions.get(day, [])
            day_marks = by_day_marks.get(day, [])
            day_receipts = by_day_receipts.get(day, [])
            day_flows = by_day_flows.get(day, [])
            row, next_equity, next_carry = self._build_row(
                account_id=account_id,
                account=account,
                session=session,
                day_trades=day_trades,
                day_positions=day_positions,
                day_marks=day_marks,
                day_receipts=day_receipts,
                day_flows=day_flows,
                beginning=carried_equity if carry_allowed else None,
                input_hash=input_hash,
                source_hash=source_hash,
                calculated_at=as_at,
                evidence_mode=evidence_mode,
                lineage_sha256=lineage_sha256,
            )
            rows.append(row)
            carried_equity = next_equity
            carry_allowed = next_carry
        counts: dict[str, int] = {}
        for row in rows:
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
        return LedgerBuildResult(account_id, tuple(rows), input_hash, source_hash, counts)

    def persist(
        self,
        result: LedgerBuildResult,
        *,
        account: Mapping[str, Any] | None = None,
    ) -> int:
        """Persist rows idempotently and return the number of rows present.

        Repeating the exact build is a no-op.  A changed input hash replaces
        only this derived read model after validating the account identity; raw
        evidence remains append-only in its own tables.
        """

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            run_migrations(connection)
            self._ensure_account(connection, result.account_id, account)
            existing = connection.execute(
                "SELECT input_hash_sha256, source_hash_sha256 FROM paper_account_daily_ledger "
                "WHERE account_id = ? LIMIT 1",
                (result.account_id,),
            ).fetchone()
            if existing is not None and str(existing[0]) == result.input_hash_sha256:
                return int(
                    connection.execute(
                        "SELECT count(*) FROM paper_account_daily_ledger WHERE account_id = ?",
                        (result.account_id,),
                    ).fetchone()[0]
                )
            connection.execute(
                "DELETE FROM paper_account_daily_ledger WHERE account_id = ?",
                (result.account_id,),
            )
            for row in result.rows:
                payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
                values = dict(row)
                values.update(
                    source_refs_json=json.dumps(row["source_refs"], sort_keys=True),
                    payload_json=payload,
                    research_only=1,
                    broker_execution_enabled=0,
                )
                connection.execute(
                    """
                    INSERT INTO paper_account_daily_ledger (
                      ledger_id, account_id, market_date, cohort, strategy_id,
                      strategy_version, execution_policy_version, cost_model_version,
                      status, evidence_state, beginning_equity_cents,
                      external_flow_cents, realized_gross_pnl_cents, fees_cents,
                      slippage_cents, realized_net_pnl_cents,
                      unrealized_pnl_change_cents, cash_cents,
                      position_market_value_cents, ending_equity_cents,
                      market_benchmark_return_pct, cash_benchmark_return_pct,
                      gross_return_pct, net_return_pct, excess_return_pct,
                      accounting_delta_cents, trade_count, open_position_count,
                      source_refs_json, source_hash_sha256, input_hash_sha256,
                      calculated_at, payload_json, target_return_pct, target_status,
                      expected_session_id, experiment_id, arm_id, evidence_mode,
                      lineage_sha256
                    ) VALUES (
                      :ledger_id, :account_id, :market_date, :cohort, :strategy_id,
                      :strategy_version, :execution_policy_version, :cost_model_version,
                      :status, :evidence_state, :beginning_equity_cents,
                      :external_flow_cents, :realized_gross_pnl_cents, :fees_cents,
                      :slippage_cents, :realized_net_pnl_cents,
                      :unrealized_pnl_change_cents, :cash_cents,
                      :position_market_value_cents, :ending_equity_cents,
                      :market_benchmark_return_pct, :cash_benchmark_return_pct,
                      :gross_return_pct, :net_return_pct, :excess_return_pct,
                      :accounting_delta_cents, :trade_count, :open_position_count,
                      :source_refs_json, :source_hash_sha256, :input_hash_sha256,
                      :calculated_at, :payload_json, :target_return_pct, :target_status,
                      :expected_session_id, :experiment_id, :arm_id, :evidence_mode,
                      :lineage_sha256
                    )
                    """,
                    values,
                )
            connection.commit()
            return len(result.rows)

    def build_and_persist(self, **kwargs: Any) -> LedgerBuildResult:
        account = kwargs.get("account")
        result = self.build(**kwargs)
        self.persist(result, account=account)
        return result

    def load(self, *, account_id: str | None = None) -> list[dict[str, Any]]:
        aid = str(account_id or self.account_id).strip()
        if not aid:
            raise ValueError("account_id is required")
        if not self.db_path.exists():
            return []
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM paper_account_daily_ledger WHERE account_id = ? "
                "ORDER BY market_date ASC",
                (aid,),
            ).fetchall()
            output = []
            for row in rows:
                item = dict(row)
                try:
                    item["payload"] = json.loads(str(item.get("payload_json") or "{}"))
                except json.JSONDecodeError:
                    item["payload"] = {}
                output.append(item)
            return output

    def _build_row(
        self,
        *,
        account_id: str,
        account: Mapping[str, Any] | None,
        session: Mapping[str, Any],
        day_trades: list[dict[str, Any]],
        day_positions: list[dict[str, Any]],
        day_marks: list[dict[str, Any]],
        day_receipts: list[dict[str, Any]],
        day_flows: list[dict[str, Any]],
        beginning: int | None,
        input_hash: str,
        source_hash: str,
        calculated_at: str,
        evidence_mode: str,
        lineage_sha256: str | None,
    ) -> tuple[dict[str, Any], int | None, bool]:
        session_status = str(session.get("status") or "EXPECTED").upper()
        declared_beginning = _int_value(
            session.get("beginning_equity_cents")
            if session.get("beginning_equity_cents") is not None
            else session.get("account_start_equity_cents")
        )
        if declared_beginning is not None:
            if beginning is not None and declared_beginning != beginning:
                raise LedgerConflictError(
                    f"{account_id}/{session['market_date']}: beginning equity conflicts "
                    "with prior session"
                )
            beginning = declared_beginning
        declared_ending = _int_value(
            session.get("ending_equity_cents")
            if session.get("ending_equity_cents") is not None
            else session.get("account_end_equity_cents")
        )
        strategy_id = str((account or {}).get("strategy_id") or "account_aggregate")
        strategy_version = str((account or {}).get("strategy_version") or "aggregate.v1")
        policy = str((account or {}).get("execution_policy_version") or "account.v1")
        cost_model = str((account or {}).get("cost_model_version") or "unknown")
        refs = sorted(
            {
                str(value)
                for group in (day_trades, day_positions, day_marks, day_receipts)
                for item in group
                for value in (
                    item.get("trade_id"),
                    item.get("position_id"),
                    item.get("receipt_id"),
                    item.get("source_artifact_hash_sha256"),
                    item.get("source_ref"),
                )
                if str(value or "").strip()
            }
        )
        base = dict(
            ledger_id=_hash({"account_id": account_id, "market_date": session["market_date"]}),
            account_id=account_id,
            market_date=str(session["market_date"]),
            expected_session_id=str(session["session_id"]),
            cohort=str((account or {}).get("cohort") or "account_aggregate"),
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            execution_policy_version=policy,
            cost_model_version=cost_model,
            target_return_pct=self.target_return_pct,
            evidence_mode=evidence_mode,
            lineage_sha256=lineage_sha256,
            source_refs=refs,
            source_hash_sha256=source_hash,
            input_hash_sha256=input_hash,
            calculated_at=calculated_at,
            research_only=True,
            broker_execution_enabled=False,
            experiment_id=(account or {}).get("experiment_id"),
            arm_id=(account or {}).get("arm_id"),
        )
        if session_status in {"CANCELLED", "NOT_EXPECTED"}:
            return (
                {
                    **base,
                    "status": "NOT_EXPECTED",
                    "target_status": "NOT_EXPECTED",
                    "target_shortfall_pct": None,
                    "target_excess_pct": None,
                    "evidence_state": "not_expected",
                    **_empty_financials(),
                    "trade_count": 0,
                    "open_position_count": 0,
                },
                beginning,
                True,
            )
        if session_status in {"HALTED", "QUARANTINED"} or bool(session.get("halted")):
            status = "QUARANTINED" if session_status == "QUARANTINED" else "HALTED"
            return (
                {
                    **base,
                    "status": status,
                    "target_status": status,
                    "target_shortfall_pct": None,
                    "target_excess_pct": None,
                    "evidence_state": status.lower(),
                    **_empty_financials(),
                    "trade_count": len(day_trades),
                    "open_position_count": len(_open_positions(day_positions)),
                },
                beginning,
                False,
            )

        if _has_conflicting_no_trade(day_trades, day_receipts):
            raise LedgerConflictError(
                f"{account_id}/{session['market_date']}: trade and no-trade evidence conflict"
            )
        flows = _sum_int(day_flows, "flow_cents", "external_flow_cents", "amount_cents")
        if flows is None and day_flows:
            return self._degraded_row(
                base, beginning, "external_flow_evidence_incomplete", day_trades, day_positions
            )
        flow = 0 if flows is None else flows
        no_trade = _authoritative_receipt_for(
            day_receipts, account_id, str(session["session_id"]), str(session["market_date"])
        )
        open_positions = _open_positions(day_positions)
        authenticated_trades = [item for item in day_trades if _trade_authenticated(item)]
        incomplete_trades = bool(day_trades) and len(authenticated_trades) != len(day_trades)
        gross = _sum_int(authenticated_trades, "gross_pnl_cents", "gross_pnl")
        fees = _sum_int(authenticated_trades, "fees_cents", "fees")
        slippage = _sum_int(
            authenticated_trades, "slippage_cents", "slippage_cost_cents", "slippage_cost"
        )
        net = _sum_int(authenticated_trades, "net_pnl_cents", "net_pnl")
        # Algebraic completion is valid only when all cost components needed
        # for that identity are present; unknown values remain partial.
        if (
            authenticated_trades
            and net is None
            and gross is not None
            and fees is not None
            and slippage is not None
        ):
            net = gross - fees - slippage
        if (
            authenticated_trades
            and gross is None
            and net is not None
            and fees is not None
            and slippage is not None
        ):
            gross = net + fees + slippage
        if authenticated_trades and any(value is None for value in (gross, fees, slippage, net)):
            return self._degraded_row(
                base, beginning, "fill_truth_financials_incomplete", day_trades, day_positions
            )
        unrealized = _sum_int(day_marks, "unrealized_pnl_change_cents", "unrealized_pnl_change")
        if day_marks and unrealized is None:
            return self._degraded_row(
                base, beginning, "mark_evidence_incomplete", day_trades, day_positions
            )
        if open_positions and unrealized is None:
            return self._degraded_row(
                base, beginning, "open_position_mark_missing", day_trades, day_positions
            )

        complete_trade_day = (
            bool(authenticated_trades) and not incomplete_trades and net is not None
        )
        if no_trade is not None and not day_trades and not open_positions:
            gross = fees = slippage = net = unrealized = 0
            ending = beginning if beginning is not None else None
            status = "AUTHENTICATED_NO_TRADE"
            state = "no_trade"
        elif complete_trade_day and beginning is not None:
            unrealized = unrealized or 0
            ending = beginning + flow + int(net or 0) + int(unrealized)
            status = "TRADE"
            state = "complete"
        elif not day_trades and not open_positions:
            return self._degraded_row(
                base, beginning, "authoritative_session_evidence_missing", day_trades, day_positions
            )
        else:
            return self._degraded_row(
                base, beginning, "account_equity_evidence_missing", day_trades, day_positions
            )
        accounting_delta = (
            ending - (beginning + flow + int(net or 0) + int(unrealized or 0))
            if ending is not None
            and beginning is not None
            and net is not None
            and unrealized is not None
            else None
        )
        if accounting_delta not in {None, 0}:
            return self._degraded_row(
                base, beginning, "accounting_identity_mismatch", day_trades, day_positions
            )
        if declared_ending is not None and ending != declared_ending:
            raise LedgerConflictError(
                f"{account_id}/{session['market_date']}: ending equity conflicts "
                "with accounting inputs"
            )
        net_return = _return_pct(
            (ending - beginning - flow) if ending is not None and beginning is not None else None,
            beginning,
        )
        gross_return = _return_pct(gross, beginning)
        target_status = (
            "NO_TRADE"
            if status == "AUTHENTICATED_NO_TRADE"
            else (
                "TARGET_MET"
                if net_return is not None and net_return >= self.target_return_pct
                else "TARGET_NOT_MET"
            )
        )
        target_shortfall = (
            round(max(0.0, self.target_return_pct - net_return), 8)
            if net_return is not None
            else None
        )
        target_excess = (
            round(net_return - self.target_return_pct, 8) if net_return is not None else None
        )
        next_equity = ending if ending is not None else beginning
        return (
            {
                **base,
                "status": status,
                "target_status": target_status,
                "target_shortfall_pct": target_shortfall,
                "target_excess_pct": target_excess,
                "evidence_state": state,
                "beginning_equity_cents": beginning,
                "external_flow_cents": flow,
                "realized_gross_pnl_cents": gross,
                "fees_cents": fees,
                "slippage_cents": slippage,
                "realized_net_pnl_cents": net,
                "unrealized_pnl_change_cents": unrealized,
                "cash_cents": ending if not open_positions else None,
                "position_market_value_cents": 0
                if not open_positions and ending is not None
                else None,
                "ending_equity_cents": ending,
                "market_benchmark_return_pct": None,
                "cash_benchmark_return_pct": None,
                "gross_return_pct": gross_return,
                "net_return_pct": net_return,
                "excess_return_pct": None,
                "accounting_delta_cents": accounting_delta,
                "trade_count": len(day_trades),
                "open_position_count": len(open_positions),
            },
            next_equity,
            ending is not None,
        )

    def _degraded_row(
        self,
        base: dict[str, Any],
        beginning: int | None,
        reason: str,
        trades: list[dict[str, Any]],
        positions: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int | None, bool]:
        return (
            {
                **base,
                "status": "PARTIAL" if (trades or positions) else "MISSING",
                "target_status": "PARTIAL" if (trades or positions) else "MISSING",
                "target_shortfall_pct": None,
                "target_excess_pct": None,
                "evidence_state": "partial" if (trades or positions) else "missing",
                **_empty_financials(),
                "beginning_equity_cents": beginning,
                "trade_count": len(trades),
                "open_position_count": len(_open_positions(positions)),
                "quarantine_reason": reason,
            },
            None,
            False,
        )

    def _resolve_account_id(self, account: Mapping[str, Any] | None) -> str:
        value = self.account_id or str((account or {}).get("account_id") or "").strip()
        if not value:
            raise ValueError("account_id is required")
        return value

    def _load_opening_equity(self, account_id: str) -> int | None:
        if not self.db_path.exists():
            return None
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT opening_equity_cents FROM paper_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            return _int_value(row[0]) if row else None

    def _ensure_account(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        account: Mapping[str, Any] | None,
    ) -> None:
        row = connection.execute(
            """SELECT strategy_id, strategy_version, activation_timestamp,
                    opening_equity_cents, currency, account_type,
                    execution_policy_version, cost_model_version,
                    research_only, broker_execution_enabled
             FROM paper_accounts WHERE account_id = ?""",
            (account_id,),
        ).fetchone()
        if row is not None:
            if int(row[8]) != 1 or int(row[9]) != 0:
                raise LedgerConflictError("paper account is not research-only")
            return
        if account is None:
            raise LedgerEvidenceError("paper account contract is not persisted")
        opening = _int_value(account.get("opening_equity_cents"))
        if opening is None or opening <= 0:
            raise LedgerEvidenceError("paper account opening equity is required")
        values = {
            "account_id": account_id,
            "strategy_id": str(account.get("strategy_id") or "account_aggregate"),
            "strategy_version": str(account.get("strategy_version") or "aggregate.v1"),
            "activation_timestamp": str(account.get("activation_timestamp") or "unknown"),
            "opening_equity_cents": opening,
            "currency": str(account.get("currency") or "USD"),
            "account_type": str(account.get("account_type") or "simulated_paper"),
            "execution_policy_version": str(
                account.get("execution_policy_version") or "account.v1"
            ),
            "cost_model_version": str(account.get("cost_model_version") or "unknown"),
            "research_only": 1,
            "broker_execution_enabled": 0,
            "created_at": str(account.get("created_at") or datetime.now(UTC).isoformat()),
        }
        values["payload_json"] = json.dumps(values, sort_keys=True)
        connection.execute(
            """INSERT INTO paper_accounts
               (account_id, strategy_id, strategy_version, activation_timestamp,
                opening_equity_cents, currency, account_type,
                execution_policy_version, cost_model_version, research_only,
                broker_execution_enabled, created_at, payload_json)
               VALUES (:account_id, :strategy_id, :strategy_version,
                :activation_timestamp, :opening_equity_cents, :currency,
                :account_type, :execution_policy_version, :cost_model_version,
                :research_only, :broker_execution_enabled, :created_at,
                :payload_json)""",
            values,
        )


def build_canonical_account_ledger(**kwargs: Any) -> LedgerBuildResult:
    """Convenience wrapper around :class:`CanonicalAccountLedger`."""

    db_path = kwargs.pop("db_path", "data/shadow_real.sqlite")
    account_id = kwargs.get("account_id") or (kwargs.get("account") or {}).get("account_id")
    service = CanonicalAccountLedger(db_path, account_id=account_id)
    return service.build(**kwargs)


def persist_canonical_account_ledger(
    result: LedgerBuildResult,
    *,
    db_path: str | Path,
    account: Mapping[str, Any] | None = None,
) -> int:
    """Persist a previously built result."""

    return CanonicalAccountLedger(db_path, account_id=result.account_id).persist(
        result, account=account
    )


def _normalize_sessions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    for raw in rows:
        item = dict(raw)
        day = str(item.get("market_date") or item.get("date") or "")[:10]
        session_id = str(item.get("session_id") or item.get("expected_session_id") or "")
        if not day or not session_id:
            raise LedgerEvidenceError("expected sessions require market_date and session_id")
        old = seen.get(day)
        if old and old != session_id:
            raise LedgerConflictError(f"multiple expected sessions for {day}")
        seen[day] = session_id
        item["market_date"] = day
        item["session_id"] = session_id
        output.append(item)
    return sorted(output, key=lambda row: (str(row["market_date"]), str(row["session_id"])))


def _normalize_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return sorted((dict(row) for row in rows), key=_canonical_json)


def _group_date(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        day = str(row.get("market_date") or row.get("date") or "")[:10]
        if day:
            output.setdefault(day, []).append(dict(row))
    return output


def _reject_conflicting_ids(rows: Sequence[Mapping[str, Any]], key: str) -> None:
    identities: dict[str, str] = {}
    for row in rows:
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        normalized = _canonical_json(row)
        old = identities.get(value)
        if old is not None and old != normalized:
            raise LedgerConflictError(f"conflicting {key}: {value}")
        identities[value] = normalized


def _trade_authenticated(row: Mapping[str, Any]) -> bool:
    if row.get("fill_truth_authenticated") is True:
        return True
    fill_truth = row.get("fill_truth")
    if isinstance(fill_truth, Mapping):
        return bool(
            fill_truth.get("authenticated") is True
            or fill_truth.get("status") in {"AUTHENTICATED", "COMMITTED", "FILLED", "CLOSED"}
        )
    return False


def _authoritative_receipt_for(
    rows: Iterable[Mapping[str, Any]], account_id: str, session_id: str, day: str
) -> dict[str, Any] | None:
    found: dict[str, Any] | None = None
    for row in rows:
        receipt_id = str(row.get("receipt_id") or row.get("id") or "").strip()
        if not receipt_id or row.get("authoritative") is False:
            continue
        if row.get("account_id") and str(row.get("account_id")) != account_id:
            continue
        if row.get("market_date") and str(row.get("market_date"))[:10] != day:
            continue
        if row.get("session_id") and str(row.get("session_id")) != session_id:
            continue
        if str(row.get("status") or "").upper() in {"UNTRUSTED", "CONFLICT", "QUARANTINED"}:
            continue
        if found is not None and _canonical_json(found) != _canonical_json(row):
            raise LedgerConflictError(f"conflicting no-trade receipts for {account_id}/{day}")
        found = dict(row)
    return found


def _has_conflicting_no_trade(
    trades: Iterable[Mapping[str, Any]], receipts: Iterable[Mapping[str, Any]]
) -> bool:
    return bool(list(trades)) and any(
        str(row.get("decision") or row.get("status") or "").upper()
        in {"NO_TRADE", "EXPLICIT_NO_TRADE"}
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
        for row in receipts
    )


def _open_positions(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if str(row.get("status") or "").upper() in {"OPEN", "HELD", "UNREALIZED", "PENDING"}
    ]


def _sum_int(rows: Iterable[Mapping[str, Any]], *keys: str) -> int | None:
    values: list[int] = []
    rows = list(rows)
    if not rows:
        return None
    for row in rows:
        value = None
        for key in keys:
            if row.get(key) is not None:
                value = _int_value(row.get(key))
                break
        if value is None:
            return None
        values.append(value)
    return sum(values)


def _int_value(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        return None
    return int(parsed)


def _return_pct(pnl_cents: int | None, beginning: int | None) -> float | None:
    if pnl_cents is None or beginning is None or beginning <= 0:
        return None
    return round(pnl_cents / beginning * 100.0, 8)


def _empty_financials() -> dict[str, Any]:
    return {
        "beginning_equity_cents": None,
        "external_flow_cents": None,
        "realized_gross_pnl_cents": None,
        "fees_cents": None,
        "slippage_cents": None,
        "realized_net_pnl_cents": None,
        "unrealized_pnl_change_cents": None,
        "cash_cents": None,
        "position_market_value_cents": None,
        "ending_equity_cents": None,
        "market_benchmark_return_pct": None,
        "cash_benchmark_return_pct": None,
        "gross_return_pct": None,
        "net_return_pct": None,
        "excess_return_pct": None,
        "accounting_delta_cents": None,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "CanonicalAccountLedger",
    "CanonicalLedgerError",
    "LedgerConflictError",
    "LedgerEvidenceError",
    "LedgerBuildResult",
    "build_canonical_account_ledger",
    "persist_canonical_account_ledger",
]
