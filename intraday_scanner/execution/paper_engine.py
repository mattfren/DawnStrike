"""The paper execution vertical slice: signal -> risk -> order -> fill -> exit -> P&L.

Every order carries a deterministic ``client_order_id`` derived from the signal
identity, so a retry after an ambiguous timeout reconciles against the broker
instead of creating a second position.

Records live in their own SQLite store, deliberately separate from the forward
performance ledger, so connectivity smoke tests can never contaminate strategy
statistics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.execution.paper_broker import (
    BrokerOrder,
    PaperBrokerClient,
    PaperBrokerError,
)
from intraday_scanner.execution.risk_gate import RiskDecision, RiskSettings, evaluate_entry

SCHEMA_VERSION = "dawnstrike.paper_execution.v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def client_order_id(*, symbol: str, market_date: str, strategy_version: str, seq: int = 0) -> str:
    """Deterministic and idempotent: the same signal always yields the same id.

    Alpaca caps client_order_id at 128 chars; this is far shorter and stable
    across process restarts, which is what makes retry-safety possible.
    """

    seed = f"{symbol}|{market_date}|{strategy_version}|{seq}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"ds-{market_date}-{symbol}-{digest}"


@dataclass
class EntryPlan:
    symbol: str
    market_date: str
    entry: float
    stop: float
    target: float
    strategy_version: str
    signal_id: str = ""
    data_age_seconds: float = 0.0


class PaperExecutionStore:
    """Durable record of every decision, order, fill and exit."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY, market_date TEXT, symbol TEXT,
                approved INTEGER, reason TEXT, qty INTEGER, notional REAL,
                entry REAL, stop REAL, target REAL, planned_r REAL,
                strategy_version TEXT, decided_at TEXT, detail_json TEXT);
            CREATE TABLE IF NOT EXISTS orders (
                client_order_id TEXT PRIMARY KEY, broker_order_id TEXT,
                market_date TEXT, symbol TEXT, side TEXT, qty REAL,
                filled_qty REAL, filled_avg_price REAL, status TEXT,
                order_class TEXT, submitted_at TEXT, updated_at TEXT, raw_json TEXT);
            CREATE TABLE IF NOT EXISTS equity_marks (
                market_date TEXT PRIMARY KEY, opening_equity REAL, ending_equity REAL,
                cash REAL, position_value REAL, day_pnl REAL, day_return_pct REAL,
                marked_at TEXT, detail_json TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT, detail_json TEXT);
            """
        )
        self._db.commit()

    def record_decision(self, plan: EntryPlan, decision: RiskDecision) -> None:
        did = f"{plan.market_date}:{plan.symbol}:{plan.strategy_version}"
        per_share = plan.entry - plan.stop
        self._db.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                did,
                plan.market_date,
                plan.symbol,
                1 if decision.approved else 0,
                decision.reason,
                decision.qty,
                decision.notional,
                plan.entry,
                plan.stop,
                plan.target,
                round((plan.target - plan.entry) / per_share, 4) if per_share > 0 else None,
                plan.strategy_version,
                utc_now(),
                json.dumps(decision.detail, sort_keys=True),
            ),
        )
        self._db.commit()

    def upsert_order(
        self, order: BrokerOrder, market_date: str, submitted_at: str | None = None
    ) -> None:
        existing = self._db.execute(
            "SELECT submitted_at FROM orders WHERE client_order_id=?", (order.client_order_id,)
        ).fetchone()
        self._db.execute(
            "INSERT OR REPLACE INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order.client_order_id,
                order.id,
                market_date,
                order.symbol,
                order.side,
                order.qty,
                order.filled_qty,
                order.filled_avg_price,
                order.status,
                order.order_class,
                (existing["submitted_at"] if existing else None) or submitted_at or utc_now(),
                utc_now(),
                json.dumps(order.raw, sort_keys=True),
            ),
        )
        self._db.commit()

    def log(self, kind: str, **detail: Any) -> None:
        self._db.execute(
            "INSERT INTO events (at, kind, detail_json) VALUES (?,?,?)",
            (utc_now(), kind, json.dumps(detail, sort_keys=True, default=str)),
        )
        self._db.commit()

    def entries_today(self, market_date: str) -> int:
        row = self._db.execute(
            "SELECT COUNT(*) c FROM orders WHERE market_date=? AND side='buy'", (market_date,)
        ).fetchone()
        return int(row["c"] if row else 0)

    def mark_equity(
        self, market_date: str, account: dict[str, Any], opening: float | None
    ) -> dict[str, Any]:
        equity = float(account.get("equity") or 0.0)
        cash = float(account.get("cash") or 0.0)
        pos_value = float(account.get("long_market_value") or 0.0)
        open_eq = opening if opening is not None else float(account.get("last_equity") or equity)
        day_pnl = equity - open_eq
        pct = (day_pnl / open_eq * 100.0) if open_eq else 0.0
        self._db.execute(
            "INSERT OR REPLACE INTO equity_marks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                market_date,
                open_eq,
                equity,
                cash,
                pos_value,
                day_pnl,
                pct,
                utc_now(),
                json.dumps({"source": "broker_account"}, sort_keys=True),
            ),
        )
        self._db.commit()
        return {"opening_equity": open_eq, "ending_equity": equity, "day_return_pct": pct}

    def day_return_pct(self, market_date: str) -> float:
        row = self._db.execute(
            "SELECT day_return_pct FROM equity_marks WHERE market_date=?", (market_date,)
        ).fetchone()
        return float(row["day_return_pct"]) if row and row["day_return_pct"] is not None else 0.0


class PaperExecutionEngine:
    """Coordinates the broker, the risk gate and the store."""

    def __init__(
        self,
        *,
        client: PaperBrokerClient,
        store: PaperExecutionStore,
        settings: RiskSettings | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.settings = settings or RiskSettings.from_env()

    # -- preflight ------------------------------------------------------

    def preflight(self) -> dict[str, Any]:
        """Must pass before any entry. Fails closed on anything unexpected."""

        account = self.client.assert_paper_account()
        clock = self.client.get_clock()
        return {
            "paper_account": True,
            "account_number_prefix": str(account.get("account_number", ""))[:2],
            "equity": float(account.get("equity") or 0),
            "cash": float(account.get("cash") or 0),
            "market_open": bool(clock.get("is_open")),
            "next_open": clock.get("next_open"),
            "next_close": clock.get("next_close"),
        }

    # -- entry ----------------------------------------------------------

    def submit_entry(self, plan: EntryPlan) -> dict[str, Any]:
        """Idempotent entry. Reconciles before submitting, never blind-retries."""

        coid = client_order_id(
            symbol=plan.symbol,
            market_date=plan.market_date,
            strategy_version=plan.strategy_version,
        )

        existing = self.client.find_by_client_order_id(coid)
        if existing is not None:
            self.store.upsert_order(existing, plan.market_date)
            self.store.log("entry_deduplicated", client_order_id=coid, status=existing.status)
            return {"submitted": False, "reason": "already_submitted", "order": existing}

        account = self.client.assert_paper_account()
        positions = self.client.get_positions()
        decision = evaluate_entry(
            entry=plan.entry,
            stop=plan.stop,
            target=plan.target,
            account=account,
            open_positions=len(positions),
            entries_today=self.store.entries_today(plan.market_date),
            day_pnl_pct=self.store.day_return_pct(plan.market_date),
            data_age_seconds=plan.data_age_seconds,
            settings=self.settings,
        )
        self.store.record_decision(plan, decision)
        if not decision.approved:
            self.store.log("entry_refused", symbol=plan.symbol, reason=decision.reason)
            return {"submitted": False, "reason": decision.reason, "decision": decision}

        try:
            order = self.client.submit_bracket_buy(
                symbol=plan.symbol,
                qty=decision.qty,
                limit_price=plan.entry,
                take_profit=plan.target,
                stop_loss=plan.stop,
                client_order_id=coid,
            )
        except PaperBrokerError as exc:
            # Ambiguous outcomes are never retried here: reconcile() will pick
            # the order up by client_order_id on the next pass.
            self.store.log("entry_submit_failed", symbol=plan.symbol, error=str(exc), coid=coid)
            return {"submitted": False, "reason": "submit_failed", "error": str(exc)}

        self.store.upsert_order(order, plan.market_date, submitted_at=utc_now())
        self.store.log("entry_submitted", symbol=plan.symbol, coid=coid, qty=decision.qty)
        return {"submitted": True, "reason": "submitted", "order": order, "decision": decision}

    # -- reconciliation & management ------------------------------------

    def reconcile(self, market_date: str) -> dict[str, Any]:
        """Refresh local state from the broker. Safe to run at any time."""

        orders = self.client.get_orders_since(f"{market_date}T00:00:00Z")
        # Adopt only orders whose id encodes THIS market date. The broker's
        # `after` filter is a timestamp, so without this an order left over
        # from an earlier session would be counted against today's entry limit.
        mine = f"ds-{market_date}-"
        for order in orders:
            if order.client_order_id.startswith(mine):
                self.store.upsert_order(order, market_date)
        positions = self.client.get_positions()
        account = self.client.assert_paper_account()
        marks = self.store.mark_equity(market_date, account, opening=None)
        partial = [o for o in orders if o.is_partial and o.client_order_id.startswith(mine)]
        self.store.log(
            "reconciled",
            orders=len(orders),
            orders_for_this_date=sum(
                1 for o in orders if o.client_order_id.startswith(mine)
            ),
            positions=len(positions),
            partial_fills=len(partial),
        )
        adopted = [o for o in orders if o.client_order_id.startswith(mine)]
        return {
            "orders": len(adopted),
            "orders_seen": len(orders),
            "positions": len(positions),
            "partial_fills": [o.client_order_id for o in partial],
            **marks,
        }

    def manage_positions(self, *, market_date: str, force_exit: bool) -> dict[str, Any]:
        """Manage open positions. Deliberately independent of entry gating.

        Stale data or a disabled entry switch must never strand an open
        position: this method still runs, and still exits, in those states.
        """

        positions = self.client.get_positions()
        actions: list[dict[str, Any]] = []
        for pos in positions:
            symbol = str(pos.get("symbol"))
            if force_exit:
                try:
                    order = self.client.close_position(symbol)
                    self.store.upsert_order(order, market_date)
                    actions.append({"symbol": symbol, "action": "time_exit_submitted"})
                    self.store.log("time_exit", symbol=symbol)
                except PaperBrokerError as exc:
                    actions.append(
                        {"symbol": symbol, "action": "time_exit_failed", "error": str(exc)}
                    )
            else:
                actions.append({"symbol": symbol, "action": "held_broker_bracket"})
        return {"positions": len(positions), "actions": actions}
