"""A loopback Alpaca-paper-API emulator that exercises the real adapter.

This is not a mock of ``PaperBrokerClient`` - it is a tiny HTTP server bound
to 127.0.0.1 that speaks enough of the Alpaca paper-trading REST surface for
``PaperBrokerClient`` to submit real bracket orders, look them up by
client_order_id, read positions/account, and cancel/close - all over real
TCP loopback, through real JSON request/response handling.

Fills are never automatic. The scenario script calls ``fill_order`` on the
shared :class:`BrokerState` explicitly, in whatever order and at whatever
price it chooses. State is persisted to a JSON file under the sandbox's
``emulator`` directory on every mutation, independently of whatever SQLite
file the application (``PaperExecutionStore``) uses - so an application
restart mid-test cannot erase what the broker already accepted.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PAPER_ACCOUNT_NUMBER = "PA3E2ESYNTH01"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EmulatorError(RuntimeError):
    """Raised for an operation the emulator deliberately does not support."""


@dataclass
class EmulatedOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    status: str = "accepted"
    order_class: str = "bracket"
    legs: list[dict[str, Any]] = field(default_factory=list)
    limit_price: float | None = None
    take_profit_price: float | None = None
    stop_price: float | None = None
    submitted_at: str = field(default_factory=_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "client_order_id": self.client_order_id,
            "symbol": self.symbol,
            "side": self.side,
            "qty": str(self.qty),
            "filled_qty": str(self.filled_qty),
            "filled_avg_price": (
                f"{self.filled_avg_price:.4f}" if self.filled_avg_price is not None else None
            ),
            "status": self.status,
            "order_class": self.order_class,
            "legs": self.legs,
        }


class BrokerState:
    """The emulator's mutable state: account, positions, orders.

    This is deliberately independent of anything the strategy code writes -
    it represents what a *real broker* would hold, mutated only through the
    same request shapes ``PaperBrokerClient`` sends, plus explicit
    scenario-driven ``fill_order`` calls that stand in for market fills.
    """

    def __init__(self, *, persist_path: Path, starting_cash: float) -> None:
        self._lock = threading.RLock()
        self._persist_path = persist_path
        self.account: dict[str, Any] = {
            "account_number": PAPER_ACCOUNT_NUMBER,
            "equity": starting_cash,
            "cash": starting_cash,
            "last_equity": starting_cash,
            "long_market_value": 0.0,
            "trading_blocked": False,
            "account_blocked": False,
        }
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, EmulatedOrder] = {}  # by broker order id
        self.orders_by_coid: dict[str, str] = {}  # client_order_id -> broker id
        self.clock = {"is_open": True, "next_open": None, "next_close": None}
        self.events: list[dict[str, Any]] = []
        self._persist()

    # -- persistence -----------------------------------------------------

    def _persist(self) -> None:
        payload = {
            "account": self.account,
            "positions": self.positions,
            "orders": {oid: asdict(o) for oid, o in self.orders.items()},
            "orders_by_coid": self.orders_by_coid,
            "events": self.events,
        }
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._persist_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def _log(self, kind: str, **detail: Any) -> None:
        self.events.append({"at": _now(), "kind": kind, "detail": detail})

    # -- API surface used by PaperBrokerClient ---------------------------

    def get_account(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.account)

    def get_clock(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.clock)

    def get_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(p) for p in self.positions.values()]

    def get_open_orders(self) -> list[dict[str, Any]]:
        with self._lock:
            return [o.to_payload() for o in self.orders.values() if o.status not in _TERMINAL]

    def get_orders_since(self) -> list[dict[str, Any]]:
        with self._lock:
            return [o.to_payload() for o in self.orders.values()]

    def find_by_coid(self, coid: str) -> dict[str, Any] | None:
        with self._lock:
            oid = self.orders_by_coid.get(coid)
            if oid is None:
                return None
            return self.orders[oid].to_payload()

    def submit_bracket_buy(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            coid = str(body.get("client_order_id") or "")
            if coid in self.orders_by_coid:
                raise EmulatorError(f"duplicate client_order_id {coid}")
            oid = str(uuid.uuid4())
            qty = float(body["qty"])
            order = EmulatedOrder(
                id=oid,
                client_order_id=coid,
                symbol=str(body["symbol"]),
                side=str(body["side"]),
                qty=qty,
                status="accepted",
                order_class=str(body.get("order_class") or "bracket"),
                limit_price=float(body["limit_price"]),
                take_profit_price=float(body["take_profit"]["limit_price"]),
                stop_price=float(body["stop_loss"]["stop_price"]),
                legs=[
                    {"id": str(uuid.uuid4()), "symbol": body["symbol"], "status": "held",
                     "type": "limit", "role": "take_profit"},
                    {"id": str(uuid.uuid4()), "symbol": body["symbol"], "status": "held",
                     "type": "stop", "role": "stop_loss"},
                ],
            )
            self.orders[oid] = order
            self.orders_by_coid[coid] = oid
            self._log("order_submitted", client_order_id=coid, symbol=order.symbol, qty=qty)
            self._persist()
            return order.to_payload()

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            order = self.orders.get(order_id)
            if order is None:
                raise EmulatorError(f"unknown order id {order_id}")
            if order.status in _TERMINAL:
                raise EmulatorError(f"order {order_id} already terminal: {order.status}")
            order.status = "canceled"
            self._log("order_canceled", order_id=order_id)
            self._persist()

    def close_position(self, symbol: str, *, fill_price: float) -> dict[str, Any]:
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                raise EmulatorError(f"no open position for {symbol}")
            qty = float(pos["qty"])
            oid = str(uuid.uuid4())
            order = EmulatedOrder(
                id=oid,
                client_order_id=f"close-{symbol}-{oid[:8]}",
                symbol=symbol,
                side="sell",
                qty=qty,
                filled_qty=qty,
                filled_avg_price=fill_price,
                status="filled",
            )
            self.orders[oid] = order
            self.orders_by_coid[order.client_order_id] = oid
            self._apply_fill_economics(symbol=symbol, side="sell", qty=qty, price=fill_price)
            del self.positions[symbol]
            self._log("position_closed", symbol=symbol, qty=qty, price=fill_price)
            self._persist()
            return order.to_payload()

    # -- scenario-driven fill control (not part of the real Alpaca API) --

    def fill_order(
        self,
        client_order_id: str,
        *,
        leg: str,
        qty: float,
        price: float,
        fee: float = 0.0,
    ) -> dict[str, Any]:
        """Explicitly fill an order (or one of its bracket legs).

        ``leg`` is one of "entry", "target", "stop". Nothing here runs on a
        timer or on a quote tick - the scenario decides exactly when and at
        what price a fill happens.
        """

        with self._lock:
            oid = self.orders_by_coid.get(client_order_id)
            if oid is None:
                raise EmulatorError(f"unknown client_order_id {client_order_id}")
            order = self.orders[oid]
            if order.status in _TERMINAL and leg == "entry":
                raise EmulatorError(f"order {client_order_id} already terminal")

            if leg == "entry":
                order.filled_qty = qty
                order.filled_avg_price = price
                order.status = "filled"
                self._apply_fill_economics(
                    symbol=order.symbol, side="buy", qty=qty, price=price, fee=fee
                )
                self.positions[order.symbol] = {
                    "symbol": order.symbol,
                    "qty": qty,
                    "avg_entry_price": price,
                    "market_value": qty * price,
                    "unrealized_pl": 0.0,
                }
            elif leg in ("target", "stop"):
                pos = self.positions.get(order.symbol)
                if pos is None:
                    raise EmulatorError(f"no open position for {order.symbol} to exit")
                del self.positions[order.symbol]
                self._apply_fill_economics(
                    symbol=order.symbol, side="sell", qty=qty, price=price, fee=fee
                )
                exit_order = EmulatedOrder(
                    id=str(uuid.uuid4()),
                    client_order_id=f"{client_order_id}-{leg}",
                    symbol=order.symbol,
                    side="sell",
                    qty=qty,
                    filled_qty=qty,
                    filled_avg_price=price,
                    status="filled",
                )
                self.orders[exit_order.id] = exit_order
                self.orders_by_coid[exit_order.client_order_id] = exit_order.id
                for order_leg in order.legs:
                    order_leg["status"] = "filled" if order_leg["role"] == f"{leg}_loss" or (
                        leg == "target" and order_leg["role"] == "take_profit"
                    ) else "canceled"
            else:
                raise EmulatorError(f"unsupported leg {leg!r}")

            self._log(
                "fill", client_order_id=client_order_id, leg=leg, qty=qty, price=price, fee=fee
            )
            self._persist()
            return order.to_payload()

    def _apply_fill_economics(
        self, *, symbol: str, side: str, qty: float, price: float, fee: float = 0.0
    ) -> None:
        notional = qty * price
        if side == "buy":
            self.account["cash"] -= notional + fee
        else:
            self.account["cash"] += notional - fee
        position_value = sum(
            p["qty"] * p.get("avg_entry_price", 0.0) for p in self.positions.values()
        )
        self.account["long_market_value"] = position_value
        self.account["equity"] = self.account["cash"] + position_value


_TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "done_for_day"})


class _Handler(BaseHTTPRequestHandler):
    state: BrokerState  # set per-server via a subclass factory

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # silence default stderr logging; evidence is captured separately

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        try:
            if parsed.path == "/v2/account":
                self._send_json(self.state.get_account())
            elif parsed.path == "/v2/clock":
                self._send_json(self.state.get_clock())
            elif parsed.path == "/v2/positions":
                self._send_json(self.state.get_positions())
            elif parsed.path == "/v2/orders":
                if qs.get("status") == ["open"]:
                    self._send_json(self.state.get_open_orders())
                else:
                    self._send_json(self.state.get_orders_since())
            elif parsed.path == "/v2/orders:by_client_order_id":
                coid = (qs.get("client_order_id") or [""])[0]
                found = self.state.find_by_coid(coid)
                if found is None:
                    self._send_json({"message": "order not found"}, status=404)
                else:
                    self._send_json(found)
            elif parsed.path == "/v2/calendar":
                self._send_json([])
            else:
                self._send_json({"message": "not found"}, status=404)
        except EmulatorError as exc:
            self._send_json({"message": str(exc)}, status=422)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/v2/orders":
                body = self._read_body()
                self._send_json(self.state.submit_bracket_buy(body), status=200)
            else:
                self._send_json({"message": "not found"}, status=404)
        except EmulatorError as exc:
            self._send_json({"message": str(exc)}, status=422)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/v2/orders/"):
                order_id = parsed.path.rsplit("/", 1)[-1]
                self.state.cancel_order(order_id)
                self._send_json({}, status=200)
            elif parsed.path.startswith("/v2/positions/"):
                symbol = parsed.path.rsplit("/", 1)[-1]
                pos = self.state.positions.get(symbol)
                if pos is None:
                    self._send_json({"message": "no position"}, status=404)
                    return
                price = float(pos.get("avg_entry_price", 0.0))
                self._send_json(self.state.close_position(symbol, fill_price=price), status=200)
            else:
                self._send_json({"message": "not found"}, status=404)
        except EmulatorError as exc:
            self._send_json({"message": str(exc)}, status=422)


class FakeAlpacaServer:
    """Owns the loopback HTTP server thread for one sandbox run."""

    def __init__(self, *, persist_path: Path, starting_cash: float = 100_000.0) -> None:
        self.state = BrokerState(persist_path=persist_path, starting_cash=starting_cash)
        handler_cls = type("BoundHandler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"
