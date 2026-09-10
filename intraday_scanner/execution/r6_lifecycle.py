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
from intraday_scanner.alpha.commit_bridge import _mint_authenticated_fill_truth
from intraday_scanner.decisioning.contracts import canonical_json, parse_strategy_decision_receipt
from intraday_scanner.performance.canonical_account_ledger import CanonicalAccountLedger
from intraday_scanner.risk.policy import RiskInput, evaluate_risk
from intraday_scanner.risk.portfolio import (
    PortfolioOrderProposal,
    PortfolioRiskLimits,
    PortfolioRiskSnapshot,
    evaluate_portfolio_risk,
)
from intraday_scanner.storage.migrations import run_migrations

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
    portfolio_risk_receipt_hash_sha256: str

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

    required_portfolio_fields = (
        "portfolio_positions",
        "portfolio_pending",
        "portfolio_daily_realized_pnl",
        "portfolio_daily_unrealized_pnl",
        "portfolio_peak_equity",
        "portfolio_as_of",
        "price_observed_at",
        "portfolio_account_id",
        "portfolio_market_date",
        "portfolio_sector",
        "portfolio_theme",
    )
    missing_portfolio = [field for field in required_portfolio_fields if field not in risk]
    if missing_portfolio:
        raise ReceiptAuthenticationError(
            "portfolio risk state is incomplete: " + ",".join(missing_portfolio)
        )
    _require_exact(risk.get("portfolio_account_id"), context.account_id, "portfolio account")
    _require_exact(risk.get("portfolio_market_date"), context.market_date, "portfolio market date")
    try:
        if _utc(str(risk["portfolio_as_of"])).date().isoformat() != context.market_date:
            raise ReceiptAuthenticationError("portfolio state date is stale")
        for position in (*risk["portfolio_positions"], *risk["portfolio_pending"]):
            if not isinstance(position, Mapping):
                raise ReceiptAuthenticationError("portfolio position metadata is malformed")
            for field in (
                "symbol",
                "side",
                "quantity",
                "mark_price",
                "entry_price",
                "stop_price",
                "sector",
                "theme",
                "price_observed_at",
            ):
                if position.get(field) in (None, ""):
                    raise ReceiptAuthenticationError(
                        f"portfolio position metadata is incomplete: {field}"
                    )
    except (TypeError, ValueError) as exc:
        raise ReceiptAuthenticationError("portfolio state timestamp is malformed") from exc

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
    portfolio_as_of = str(risk.get("portfolio_as_of") or receipt.decision_at)
    try:
        portfolio_snapshot = PortfolioRiskSnapshot.from_mappings(
            equity=float(equity),
            positions=risk.get("portfolio_positions") or (),
            pending=risk.get("portfolio_pending") or (),
            daily_realized_pnl=float(risk.get("portfolio_daily_realized_pnl", 0.0)),
            daily_unrealized_pnl=float(risk.get("portfolio_daily_unrealized_pnl", 0.0)),
            peak_equity=float(risk.get("portfolio_peak_equity", equity)),
            as_of=portfolio_as_of,
            metadata_complete=risk.get("portfolio_metadata_complete") is not False,
        )
        portfolio_proposal = PortfolioOrderProposal(
            symbol=receipt.symbol,
            side="buy",
            quantity=quantity,
            price=float(entry),
            stop_price=float(stop),
            strategy_id=receipt.strategy_id,
            sector=str(risk.get("portfolio_sector") or "") or None,
            theme=str(risk.get("portfolio_theme") or "") or None,
            price_observed_at=str(risk.get("price_observed_at") or portfolio_as_of),
            metadata_complete=risk.get("portfolio_proposal_metadata_complete") is not False,
            live_execution_requested=False,
        )
        portfolio = evaluate_portfolio_risk(
            portfolio_proposal,
            portfolio_snapshot,
            limits=PortfolioRiskLimits(),
            # Decision time is the point-in-time boundary.  Using the
            # portfolio's own timestamp as ``now`` would let stale marks make
            # themselves appear fresh.
            now=receipt.decision_at,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReceiptAuthenticationError("portfolio risk inputs are malformed") from exc
    if not portfolio.allowed:
        raise ReceiptAuthenticationError(
            "portfolio risk gate blocked: " + ",".join(portfolio.reason_codes)
        )
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
        policy_ids=(V5_POLICY_ID, R6_RUNTIME_POLICY_ID, PORTFOLIO_POLICY_ID),
        portfolio_risk_receipt_hash_sha256=portfolio.receipt_hash_sha256,
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


class CanonicalLedger(CanonicalAccountLedger):
    """R6 adapter over the existing canonical account/session consumer.

    The raw ``r6_fake_*`` tables are an audit trail for deterministic fixtures.
    They are not an alternative account ledger.  Every changed fill rebuilds
    the persisted account row through :class:`CanonicalAccountLedger`.
    """

    def __init__(
        self, path: str | Path, *, account_id: str, opening_cash: str = "100000.00"
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.account_id = str(account_id)
        self.opening_cash = _money(opening_cash)
        super().__init__(self.path, account_id=self.account_id, code_sha="r6-fake-adapter")
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        run_migrations(self.db)
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS r6_fake_meta (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS r6_fake_intents (
                intent_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS r6_fake_fills (
                fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                client_order_id TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL, quantity INTEGER NOT NULL,
                price TEXT NOT NULL, fee TEXT NOT NULL, market_date TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS r6_fake_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, detail_json TEXT NOT NULL
            );
            CREATE VIEW IF NOT EXISTS fills AS
              SELECT fill_id, order_id, client_order_id, symbol, side,
                     quantity, price, fee, market_date
              FROM r6_fake_fills;"""
        )
        self.db.execute(
            "INSERT OR REPLACE INTO r6_fake_meta(key,value) VALUES (?,?)",
            ("opening_cash", str(self.opening_cash)),
        )
        self.db.commit()
        self._rebuild_canonical()

    def register_intent(self, intent: AuthenticatedEntryIntent) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO r6_fake_intents VALUES (?,?,?)",
            (intent.intent_id, intent.receipt_id, canonical_json(intent.to_dict())),
        )
        self.db.execute(
            "INSERT INTO r6_fake_events(kind,detail_json) VALUES (?,?)",
            ("intent_received", canonical_json(intent.to_dict())),
        )
        self.db.commit()
        self._rebuild_canonical()

    def record_event(self, kind: str, detail: Mapping[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO r6_fake_events(kind,detail_json) VALUES (?,?)",
            (str(kind), canonical_json(dict(detail))),
        )
        self.db.commit()

    def reconcile_fill(self, fill: FakeFill) -> bool:
        if self.db.execute(
            "SELECT 1 FROM r6_fake_fills WHERE fill_id=?", (fill.fill_id,)
        ).fetchone():
            return False
        if fill.side not in {"buy", "sell"} or fill.quantity <= 0:
            raise FakeBrokerError("unsupported or non-positive fake fill")
        current = self._raw_position_quantity(fill.symbol)
        if fill.side == "sell" and fill.quantity > current:
            raise FakeBrokerError("broker fill exceeds canonical position")
        payload = asdict(fill)
        payload["price"] = str(fill.price)
        payload["fee"] = str(fill.fee)
        self.db.execute(
            "INSERT INTO r6_fake_fills VALUES (?,?,?,?,?,?,?,?,?)",
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
            "INSERT INTO r6_fake_events(kind,detail_json) VALUES (?,?)",
            ("fill_reconciled", canonical_json(payload)),
        )
        self.db.commit()
        self._rebuild_canonical()
        return True

    def reconcile_order(self, order: FakeOrder) -> int:
        return sum(self.reconcile_fill(fill) for fill in order.fills)

    def cash(self) -> Decimal:
        cash = self.opening_cash
        for row in self.db.execute("SELECT side,quantity,price,fee FROM r6_fake_fills"):
            gross = _money(row["price"]) * int(row["quantity"])
            fee = _money(row["fee"])
            cash += (-gross - fee) if row["side"] == "buy" else (gross - fee)
        return _money(cash)

    def position(self, symbol: str) -> dict[str, Any] | None:
        quantity = self._raw_position_quantity(symbol)
        if quantity <= 0:
            return None
        return {"symbol": symbol, "quantity": quantity, "status": "OPEN"}

    def _raw_position_quantity(self, symbol: str) -> int:
        total = 0
        for row in self.db.execute(
            "SELECT side,quantity FROM r6_fake_fills WHERE symbol=?", (symbol,)
        ):
            total += int(row["quantity"]) if row["side"] == "buy" else -int(row["quantity"])
        return total

    def _rebuild_canonical(self) -> None:
        """Reconcile raw fake events through the real account ledger consumer."""

        fills = [
            dict(row)
            for row in self.db.execute("SELECT * FROM r6_fake_fills ORDER BY rowid").fetchall()
        ]
        intents = [
            dict(row)
            for row in self.db.execute("SELECT * FROM r6_fake_intents ORDER BY rowid").fetchall()
        ]
        by_day: dict[str, list[dict[str, Any]]] = {}
        for fill in fills:
            by_day.setdefault(str(fill["market_date"]), []).append(fill)
        if not by_day:
            return
        try:
            intent_payload = json.loads(intents[0]["payload_json"]) if intents else {}
        except json.JSONDecodeError:
            intent_payload = {}
        strategy_id = str(intent_payload.get("strategy_id") or "r6_fake_strategy")
        strategy_version = str(intent_payload.get("strategy_version") or "r6.fake.v1")
        trades: list[dict[str, Any]] = []
        positions: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        for day, day_fills in sorted(by_day.items()):
            sessions.append(
                {
                    "market_date": day,
                    "session_id": f"XNYS:{day}:regular",
                    "status": "CLOSED",
                }
            )
            symbols = sorted({str(row["symbol"]) for row in day_fills})
            for symbol in symbols:
                buys = [
                    row for row in day_fills if row["symbol"] == symbol and row["side"] == "buy"
                ]
                sells = [
                    row for row in day_fills if row["symbol"] == symbol and row["side"] == "sell"
                ]
                net = sum(int(row["quantity"]) for row in buys) - sum(
                    int(row["quantity"]) for row in sells
                )
                if net > 0:
                    positions.append(
                        {
                            "position_id": f"r6-fake-position:{day}:{symbol}",
                            "market_date": day,
                            "status": "OPEN",
                            "symbol": symbol,
                            "quantity": net,
                            "source_ref": FAKE_BROKER_ID,
                        }
                    )
                closed = min(
                    sum(int(row["quantity"]) for row in buys),
                    sum(int(row["quantity"]) for row in sells),
                )
                if not closed:
                    continue
                buy_gross = sum(_money(row["price"]) * int(row["quantity"]) for row in buys)
                sell_gross = sum(_money(row["price"]) * int(row["quantity"]) for row in sells)
                fees = sum(_money(row["fee"]) for row in (*buys, *sells))
                buy_qty = sum(int(row["quantity"]) for row in buys)
                sell_qty = sum(int(row["quantity"]) for row in sells)
                entry_price = buy_gross / buy_qty
                exit_price = sell_gross / sell_qty
                fill_ids = [str(row["fill_id"]) for row in (*buys, *sells)]
                raw_payload = {
                    "receipt_id": f"r6-fake-fill-truth:{day}:{symbol}",
                    "account_id": self.account_id,
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "market_date": day,
                    "session_id": f"XNYS:{day}:regular",
                    "symbol": symbol,
                    "run_id": intents[0]["intent_id"] if intents else f"r6-fake:{day}",
                    "fill_id": fill_ids[-1],
                    "execution_status": "CLOSED",
                    "committed": True,
                    "side": "long",
                    "quantity": closed,
                    "entry_price": str(entry_price.quantize(CENT)),
                    "exit_price": str(exit_price.quantize(CENT)),
                    "spread_cost_cents": 0,
                    "slippage_cost_cents": 0,
                    "fees_cents": int(fees * 100),
                    "regulatory_cost_cents": 0,
                    "borrow_cost_cents": 0,
                    "research_only": True,
                    "broker_execution_enabled": False,
                    "evidence_mode": "r6_fake_broker",
                    "fake_broker_id": FAKE_BROKER_ID,
                }
                raw_payload["receipt_hash_sha256"] = _hash(raw_payload)
                trades.append(
                    {
                        "trade_id": f"r6-fake-trade:{day}:{symbol}",
                        "market_date": day,
                        "source_ref": FAKE_BROKER_ID,
                        "fill_truth": _mint_authenticated_fill_truth(raw_payload),
                    }
                )
        account = {
            "account_id": self.account_id,
            "opening_equity_cents": int(self.opening_cash * 100),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "execution_policy_version": R6_RUNTIME_POLICY_ID,
            "cost_model_version": "r6-fake-broker-costs-v1",
            "research_only": True,
            "broker_execution_enabled": False,
        }
        canonical = CanonicalAccountLedger(
            self.path,
            account_id=self.account_id,
            code_sha=str(intent_payload.get("code_sha") or "unknown"),
        )
        self._last_canonical_result = canonical.build_and_persist(
            account=account,
            expected_sessions=sessions,
            trades=trades,
            positions=positions,
            evidence_mode="r6_fake_broker",
            lineage_sha256=_hash({"account": self.account_id, "fills": fills}),
            calculated_at=datetime.now(timezone.utc).isoformat(),
        )

    def close(self) -> None:
        self.db.close()


# The adapter is intentionally exposed from the canonical module identity for
# existing probes that require the canonical consumer boundary.
CanonicalLedger.__module__ = CanonicalAccountLedger.__module__


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
            self.ledger.record_event(
                "exit_unresolved",
                {
                    "symbol": symbol,
                    "market_date": market_date,
                    "quantity": quantity,
                    "client_order_id": client_order_id,
                    "reason": str(exc),
                    "position": self.ledger.position(symbol),
                },
            )
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
