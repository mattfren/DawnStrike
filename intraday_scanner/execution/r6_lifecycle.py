"""Network-free authenticated strategy-to-fake-broker lifecycle.

R6 is an engineering packet, not paper-fill or economic evidence.  The
adapter accepts only a persisted, typed strategy receipt that has passed the
real alert-gate consumer and is bound to the exact source, config, date,
account, host, code and plan supplied by the caller.  The fake broker and
ledger are intentionally distinct from synthetic watchers and broker-paper
clients.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.alert_gate import apply_alert_gate, validate_strategy_receipt_envelope
from intraday_scanner.decisioning.contracts import canonical_json, parse_strategy_decision_receipt
from intraday_scanner.risk.policy import RiskInput, evaluate_risk

SCHEMA_VERSION = "dawnstrike.r6.fake_execution.v1"
FAKE_BROKER_ID = "dawnstrike-r6-fake-broker-v1"
R6_RUNTIME_POLICY_ID = "dawnstrike-runtime-fake-equivalent-v1"
V5_POLICY_ID = "dawnstrike-risk-policy-v1"
PORTFOLIO_POLICY_ID = "dawnstrike-portfolio-risk-v1"
ET = ZoneInfo("America/New_York")
CENT = Decimal("0.01")


class ReceiptAuthenticationError(ValueError):
    """The strategy receipt or one of its exact identity bindings is invalid."""


class FakeBrokerError(RuntimeError):
    """A deterministic fake-broker refusal or unresolved acknowledgement."""


def _money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid monetary value: {value!r}") from exc
    if not result.is_finite():
        raise ValueError("monetary values must be finite")
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps require a timezone")
    return parsed.astimezone(timezone.utc)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class R6RiskSettings:
    """Runtime-equivalent fake gate; it does not replace V5 or portfolio gates."""

    risk_pct: Decimal = Decimal("0.5")
    max_position_pct: Decimal = Decimal("10")
    max_concurrent_positions: int = 3
    max_entries_per_day: int = 5
    daily_loss_limit_pct: Decimal = Decimal("2")

    @property
    def policy_id(self) -> str:
        return R6_RUNTIME_POLICY_ID


@dataclass(frozen=True, slots=True)
class ReceiptContext:
    source_identity: str
    source_config_hash_sha256: str
    market_date: str
    account_id: str
    host_id: str
    code_sha: str
    plan_hash_sha256: str


@dataclass(frozen=True, slots=True)
class AuthenticatedEntryIntent:
    intent_id: str
    receipt_id: str
    source_identity: str
    source_config_hash_sha256: str
    market_date: str
    account_id: str
    host_id: str
    code_sha: str
    plan_hash_sha256: str
    strategy_id: str
    strategy_version: str
    symbol: str
    side: str
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    quantity: int
    decision_at: str
    policy_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        output = asdict(self)
        for key in ("entry_price", "stop_price", "target_price"):
            output[key] = str(output[key])
        output["policy_ids"] = list(self.policy_ids)
        return output


def _context_from(value: ReceiptContext | Mapping[str, Any]) -> ReceiptContext:
    return value if isinstance(value, ReceiptContext) else ReceiptContext(**dict(value))


def _require_exact(actual: Any, expected: Any, field: str) -> None:
    if str(actual or "") != str(expected or ""):
        raise ReceiptAuthenticationError(f"{field} identity mismatch")


def authenticate_entry_intent(
    row: Mapping[str, Any],
    context: ReceiptContext | Mapping[str, Any],
    *,
    settings: R6RiskSettings | None = None,
) -> AuthenticatedEntryIntent:
    """Validate a genuine source-bound row through the actual alert consumer."""

    context = _context_from(context)
    candidate = dict(row)
    try:
        gated = apply_alert_gate(candidate)
    except (TypeError, ValueError, KeyError) as exc:
        raise ReceiptAuthenticationError("alert-gate consumer rejected malformed row") from exc
    if not validate_strategy_receipt_envelope(gated):
        raise ReceiptAuthenticationError(
            "strategy receipt is not authenticated by the real consumer"
        )
    try:
        receipt = parse_strategy_decision_receipt(
            gated["strategy_decision_receipt"], require_v2=True
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ReceiptAuthenticationError("strategy receipt is not a valid v2 receipt") from exc
    if gated.get("alert_gate_status") not in {"PASS", "ALERT_OK"}:
        raise ReceiptAuthenticationError("alert gate did not admit the entry")
    if gated.get("manual_confirmation_required") is not False:
        raise ReceiptAuthenticationError("manual confirmation is required")
    if receipt.paper_entry_eligible is not True:
        raise ReceiptAuthenticationError("receipt paper-entry eligibility is false")
    if receipt.research_only is not True or receipt.broker_execution_enabled is not False:
        raise ReceiptAuthenticationError("receipt safety boundary is invalid")

    _require_exact(receipt.source_identity, context.source_identity, "source")
    _require_exact(receipt.market_date, context.market_date, "market date")
    _require_exact(receipt.code_sha, context.code_sha, "code")
    _require_exact(receipt.plan_hash_sha256, context.plan_hash_sha256, "plan")
    _require_exact(gated.get("source_identity"), context.source_identity, "row source")
    _require_exact(
        gated.get("source_config_hash_sha256"), context.source_config_hash_sha256, "config"
    )
    _require_exact(gated.get("market_date"), context.market_date, "row market date")
    _require_exact(gated.get("account_id"), context.account_id, "account")
    _require_exact(gated.get("host_id"), context.host_id, "host")
    _require_exact(gated.get("plan_hash_sha256"), context.plan_hash_sha256, "row plan")

    try:
        input_payload = json.loads(receipt.input_payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReceiptAuthenticationError("receipt input payload is not canonical JSON") from exc
    for field, expected in (
        ("source_identity", context.source_identity),
        ("source_config_hash_sha256", context.source_config_hash_sha256),
        ("market_date", context.market_date),
        ("account_id", context.account_id),
        ("host_id", context.host_id),
        ("plan_hash_sha256", context.plan_hash_sha256),
    ):
        _require_exact(input_payload.get(field), expected, f"input {field}")

    decision_at = _utc(receipt.decision_at)
    local = decision_at.astimezone(ET)
    if local.time() >= time(15, 30):
        raise ReceiptAuthenticationError("V5 entry cutoff 15:30 ET is exclusive")
    if receipt.entry_reference is None or receipt.stop is None or receipt.target is None:
        raise ReceiptAuthenticationError("receipt lacks executable levels")
    entry, stop, target = map(_money, (receipt.entry_reference, receipt.stop, receipt.target))
    if not stop < entry < target:
        raise ReceiptAuthenticationError("receipt levels are not a long plan")

    risk = gated.get("r6_risk_inputs")
    if not isinstance(risk, Mapping):
        raise ReceiptAuthenticationError("independent risk inputs are missing")
    fake = settings or R6RiskSettings()
    equity = _money(risk.get("equity"))
    cash = _money(risk.get("cash"))
    open_positions = int(risk.get("open_positions", 0))
    entries_today = int(risk.get("entries_today", 0))
    day_loss_pct = _money(risk.get("day_loss_pct", 0))
    if open_positions >= fake.max_concurrent_positions:
        raise ReceiptAuthenticationError("fake runtime max concurrent positions reached")
    if entries_today >= fake.max_entries_per_day:
        raise ReceiptAuthenticationError("fake runtime max entries per day reached")
    if day_loss_pct <= -fake.daily_loss_limit_pct:
        raise ReceiptAuthenticationError("fake runtime daily loss limit reached")
    per_share_risk = entry - stop
    quantity = int(
        (equity * fake.risk_pct / Decimal("100") / per_share_risk).to_integral_value(
            rounding="ROUND_FLOOR"
        )
    )
    quantity = min(
        quantity,
        int(
            (equity * fake.max_position_pct / Decimal("100") / entry).to_integral_value(
                rounding="ROUND_FLOOR"
            )
        ),
    )
    quantity = min(quantity, int((cash / entry).to_integral_value(rounding="ROUND_FLOOR")))
    # Preserve the stricter V5 per-trade ceiling when both surfaces apply.
    quantity = min(
        quantity,
        int(
            (equity * Decimal("0.25") / Decimal("100") / per_share_risk).to_integral_value(
                rounding="ROUND_FLOOR"
            )
        ),
    )
    if quantity <= 0:
        raise ReceiptAuthenticationError("fake runtime risk gate produced no quantity")

    # V5 is an independent stricter surface.  All inputs must be present and
    # this call is never replaced by the wider fake-runtime defaults.
    v5 = evaluate_risk(
        RiskInput(
            ticker=receipt.symbol,
            decision_time=receipt.decision_at,
            equity_cents=int(equity * 100),
            entry_price=float(entry),
            stop_price=float(stop),
            proposed_notional_cents=int(entry * quantity * 100),
            daily_realized_loss_cents=int(day_loss_pct * equity),
            ticker_notional_cents=int(_money(risk.get("ticker_notional", 0)) * 100),
            correlated_position_count=int(risk.get("correlated_positions", 0)),
            halt_status=str(risk.get("halt_status") or ""),
            corporate_action_status=str(risk.get("corporate_action_status") or ""),
            sec_risk_status=str(risk.get("sec_risk_status") or ""),
            source_quality_status=str(risk.get("source_quality_status") or ""),
            spread_bps=float(risk.get("spread_bps"))
            if risk.get("spread_bps") is not None
            else None,
            available_cash_cents=int(cash * 100),
            session_status=str(risk.get("session_status") or ""),
        )
    )
    if not v5.allowed_for_paper:
        raise ReceiptAuthenticationError("V5 risk gate blocked: " + ",".join(v5.reasons))
    intent_id = (
        "intent-"
        + _hash({"receipt": receipt.receipt_hash_sha256, "account": context.account_id})[:24]
    )
    return AuthenticatedEntryIntent(
        intent_id=intent_id,
        receipt_id=receipt.receipt_id,
        source_identity=context.source_identity,
        source_config_hash_sha256=context.source_config_hash_sha256,
        market_date=context.market_date,
        account_id=context.account_id,
        host_id=context.host_id,
        code_sha=context.code_sha,
        plan_hash_sha256=context.plan_hash_sha256,
        strategy_id=receipt.strategy_id,
        strategy_version=receipt.strategy_version,
        symbol=receipt.symbol,
        side="buy",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=quantity,
        decision_at=receipt.decision_at,
        policy_ids=(V5_POLICY_ID, R6_RUNTIME_POLICY_ID),
    )


@dataclass(frozen=True, slots=True)
class FakeFill:
    fill_id: str
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    price: Decimal
    fee: Decimal
    market_date: str
    acknowledged: bool = True


@dataclass(frozen=True, slots=True)
class FakeOrder:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    quantity: int
    status: str
    fills: tuple[FakeFill, ...] = ()
    reason: str = ""


class FakeBroker:
    """Deterministic in-memory broker.  It has no URL, credentials, or network."""

    broker_id = FAKE_BROKER_ID

    def __init__(self, *, cash: str = "100000.00", fee_bps: str = "10") -> None:
        self.cash = _money(cash)
        self.fee_bps = _money(fee_bps)
        self.orders: dict[str, FakeOrder] = {}
        self.positions: dict[str, int] = {}
        self._order_seq = 0
        self._fill_seq = 0
        self.ambiguous_client_ids: set[str] = set()
        self.entry_enabled = True
        self.reject_exits = False

    def find_by_client_order_id(self, client_order_id: str) -> FakeOrder | None:
        return self.orders.get(client_order_id)

    def submit_entry(
        self, intent: AuthenticatedEntryIntent, *, fills: tuple[tuple[int, str], ...] = ()
    ) -> FakeOrder:
        existing = self.find_by_client_order_id(intent.intent_id)
        if existing is not None:
            return existing
        if not self.entry_enabled:
            raise FakeBrokerError("entry kill switch engaged")
        if not fills:
            fills = ((intent.quantity, str(intent.entry_price)),)
        if sum(quantity for quantity, _ in fills) > intent.quantity:
            raise FakeBrokerError("fill quantity exceeds order quantity")
        self._order_seq += 1
        order_id = f"fake-order-{self._order_seq}"
        built: list[FakeFill] = []
        for quantity, price in fills:
            self._fill_seq += 1
            built.append(
                self._fill(
                    order_id,
                    intent.intent_id,
                    intent.symbol,
                    "buy",
                    quantity,
                    price,
                    intent.market_date,
                )
            )
        status = (
            "filled"
            if sum(item.quantity for item in built) == intent.quantity
            else "partially_filled"
        )
        order = FakeOrder(
            order_id, intent.intent_id, intent.symbol, "buy", intent.quantity, status, tuple(built)
        )
        self.orders[intent.intent_id] = order
        if intent.intent_id in self.ambiguous_client_ids:
            raise FakeBrokerError("ambiguous acknowledgement; reconcile by client ID")
        return order

    def submit_exit(
        self, *, symbol: str, market_date: str, client_order_id: str, quantity: int, price: str
    ) -> FakeOrder:
        existing = self.find_by_client_order_id(client_order_id)
        if existing is not None:
            return existing
        current = self.positions.get(symbol, 0)
        if self.reject_exits:
            raise FakeBrokerError("close rejected; position remains unresolved")
        if quantity <= 0 or quantity > current:
            raise FakeBrokerError("exit quantity exceeds current position")
        self._order_seq += 1
        self._fill_seq += 1
        order_id = f"fake-order-{self._order_seq}"
        fill = self._fill(order_id, client_order_id, symbol, "sell", quantity, price, market_date)
        order = FakeOrder(order_id, client_order_id, symbol, "sell", quantity, "filled", (fill,))
        self.orders[client_order_id] = order
        return order

    def fill_remaining(self, client_order_id: str, *, price: str, market_date: str) -> FakeOrder:
        """Advance a partial entry to a full fill in the deterministic fixture."""

        existing = self.orders.get(client_order_id)
        if existing is None or existing.side != "buy":
            raise FakeBrokerError("entry order not found")
        filled = sum(item.quantity for item in existing.fills)
        remaining = existing.quantity - filled
        if remaining <= 0:
            return existing
        self._fill_seq += 1
        fill = self._fill(
            existing.order_id,
            client_order_id,
            existing.symbol,
            "buy",
            remaining,
            price,
            market_date,
        )
        updated = FakeOrder(
            existing.order_id,
            existing.client_order_id,
            existing.symbol,
            existing.side,
            existing.quantity,
            "filled",
            (*existing.fills, fill),
        )
        self.orders[client_order_id] = updated
        return updated

    def _fill(
        self,
        order_id: str,
        client_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: str,
        market_date: str,
    ) -> FakeFill:
        amount = _money(price)
        fee = (amount * quantity * self.fee_bps / Decimal("10000")).quantize(
            CENT, rounding=ROUND_HALF_UP
        )
        current = self.positions.get(symbol, 0)
        self.positions[symbol] = current + quantity if side == "buy" else current - quantity
        if self.positions[symbol] <= 0:
            self.positions.pop(symbol, None)
        return FakeFill(
            f"fake-fill-{self._fill_seq}",
            order_id,
            client_id,
            symbol,
            side,
            quantity,
            amount,
            fee,
            market_date,
        )


class CanonicalLedger:
    """Durable cash/position/fill ledger with fill-idempotent reconciliation."""

    def __init__(
        self, path: str | Path, *, account_id: str, opening_cash: str = "100000.00"
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.account_id = account_id
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS account (
                account_id TEXT PRIMARY KEY, cash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                fill_id TEXT PRIMARY KEY, order_id TEXT, client_order_id TEXT,
                symbol TEXT, side TEXT, quantity INTEGER, price TEXT, fee TEXT,
                market_date TEXT
            );
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL,
                cost_basis TEXT NOT NULL, realized_pnl TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY, receipt_id TEXT, payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, detail_json TEXT
            );"""
        )
        self.db.execute(
            "INSERT OR IGNORE INTO account VALUES (?,?)", (account_id, str(_money(opening_cash)))
        )
        self.db.commit()

    def register_intent(self, intent: AuthenticatedEntryIntent) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO intents VALUES (?,?,?)",
            (intent.intent_id, intent.receipt_id, canonical_json(intent.to_dict())),
        )
        self.db.commit()

    def reconcile_fill(self, fill: FakeFill) -> bool:
        if self.db.execute("SELECT 1 FROM fills WHERE fill_id=?", (fill.fill_id,)).fetchone():
            return False
        account = self.db.execute(
            "SELECT cash FROM account WHERE account_id=?", (self.account_id,)
        ).fetchone()
        if account is None:
            raise FakeBrokerError("ledger account is missing")
        cash = _money(account["cash"])
        position = self.db.execute(
            "SELECT * FROM positions WHERE symbol=?", (fill.symbol,)
        ).fetchone()
        qty = int(position["quantity"]) if position else 0
        basis = _money(position["cost_basis"]) if position else Decimal("0")
        realized = _money(position["realized_pnl"]) if position else Decimal("0")
        gross = fill.price * fill.quantity
        if fill.side == "buy":
            cash -= gross + fill.fee
            basis = basis + gross + fill.fee
            qty += fill.quantity
        elif fill.side == "sell":
            if fill.quantity > qty:
                raise FakeBrokerError("broker fill exceeds canonical position")
            cash += gross - fill.fee
            cost = (basis / qty * fill.quantity) if qty else Decimal("0")
            realized += gross - fill.fee - cost
            basis -= cost
            qty -= fill.quantity
        else:
            raise FakeBrokerError("unsupported fill side")
        self.db.execute(
            "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
            (
                fill.fill_id,
                fill.order_id,
                fill.client_order_id,
                fill.symbol,
                fill.side,
                fill.quantity,
                str(fill.price),
                str(fill.fee),
                fill.market_date,
            ),
        )
        self.db.execute(
            "UPDATE account SET cash=? WHERE account_id=?", (str(cash), self.account_id)
        )
        if qty:
            self.db.execute(
                """INSERT INTO positions VALUES (?,?,?,?)
                ON CONFLICT(symbol) DO UPDATE SET
                quantity=excluded.quantity,
                cost_basis=excluded.cost_basis,
                realized_pnl=excluded.realized_pnl""",
                (fill.symbol, qty, str(basis), str(realized)),
            )
        else:
            self.db.execute("DELETE FROM positions WHERE symbol=?", (fill.symbol,))
        event = asdict(fill)
        event["price"] = str(fill.price)
        event["fee"] = str(fill.fee)
        self.db.execute(
            "INSERT INTO events(kind,detail_json) VALUES (?,?)",
            ("fill_reconciled", canonical_json(event)),
        )
        self.db.commit()
        return True

    def reconcile_order(self, order: FakeOrder) -> int:
        return sum(self.reconcile_fill(fill) for fill in order.fills)

    def cash(self) -> Decimal:
        row = self.db.execute(
            "SELECT cash FROM account WHERE account_id=?", (self.account_id,)
        ).fetchone()
        return _money(row["cash"] if row else "0")

    def position(self, symbol: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM positions WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.db.close()


class R6Lifecycle:
    """Drive authenticated intent, fake fills, exits and honest recovery."""

    def __init__(self, broker: FakeBroker, ledger: CanonicalLedger) -> None:
        self.broker = broker
        self.ledger = ledger

    def enter(
        self, intent: AuthenticatedEntryIntent, *, fills: tuple[tuple[int, str], ...] = ()
    ) -> dict[str, Any]:
        self.ledger.register_intent(intent)
        existing = self.broker.find_by_client_order_id(intent.intent_id)
        if existing is not None:
            self.ledger.reconcile_order(existing)
            return {
                "status": "DEDUPLICATED",
                "order_id": existing.order_id,
                "reconciled_fills": len(existing.fills),
            }
        try:
            order = self.broker.submit_entry(intent, fills=fills)
        except FakeBrokerError as exc:
            if "ambiguous" not in str(exc).lower():
                return {"status": "ENTRY_REJECTED", "reason": str(exc)}
            order = self.broker.find_by_client_order_id(intent.intent_id)
            if order is None:
                return {
                    "status": "ENTRY_UNRESOLVED",
                    "reason": "ambiguous acknowledgement not found by client ID",
                }
            self.ledger.reconcile_order(order)
            return {"status": "RECONCILED_AFTER_AMBIGUOUS_ACK", "order_id": order.order_id}
        reconciled = self.ledger.reconcile_order(order)
        return {
            "status": "ENTRY_FILLED" if order.status == "filled" else "ENTRY_PARTIAL",
            "order_id": order.order_id,
            "reconciled_fills": reconciled,
        }

    def exit(
        self, *, symbol: str, market_date: str, quantity: int, price: str, client_order_id: str
    ) -> dict[str, Any]:
        try:
            order = self.broker.submit_exit(
                symbol=symbol,
                market_date=market_date,
                client_order_id=client_order_id,
                quantity=quantity,
                price=price,
            )
        except FakeBrokerError as exc:
            return {
                "status": "EXIT_UNRESOLVED",
                "reason": str(exc),
                "position": self.ledger.position(symbol),
            }
        self.ledger.reconcile_order(order)
        return {
            "status": "EXIT_FILLED",
            "order_id": order.order_id,
            "position": self.ledger.position(symbol),
        }

    def manage_before_close(
        self, *, symbol: str, market_date: str, close_at: str, now: str, price: str
    ) -> dict[str, Any]:
        minutes = (_utc(close_at) - _utc(now)).total_seconds() / 60
        position = self.ledger.position(symbol)
        if position is None:
            return {"status": "FLAT", "minutes_to_close": minutes}
        if minutes > 10:
            return {
                "status": "HELD_UNTIL_EXIT_RULE",
                "minutes_to_close": minutes,
                "position": position,
            }
        result = self.exit(
            symbol=symbol,
            market_date=market_date,
            quantity=int(position["quantity"]),
            price=price,
            client_order_id=f"exit-{symbol}-{market_date}",
        )
        result["minutes_to_close"] = minutes
        return result
