"""Alpaca PAPER-ONLY broker client.

Paper-only is enforced in four independent ways, so no single mistake can
route an order to live:

1. ``PAPER_BASE`` is the only base URL this module will construct.
2. Every request goes through ``open_allowlisted_url`` with an allowlist of
   exactly one host; the live host is never in it.
3. ``assert_paper_account`` requires the broker to report an account number
   with Alpaca's ``PA`` paper prefix.
4. ``_reject_live`` refuses any URL that is not the paper base, so a caller
   cannot pass one in.

This module submits and reads orders. It holds no strategy logic and makes no
sizing decisions - see ``risk_gate.py``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from intraday_scanner.network_safety import open_allowlisted_url

PAPER_BASE = "https://paper-api.alpaca.markets"
PAPER_HOST = "paper-api.alpaca.markets"
LIVE_HOSTS = ("api.alpaca.markets",)
PAPER_ACCOUNT_PREFIX = "PA"


class PaperBrokerError(RuntimeError):
    """Any refusal or broker failure on the paper path."""


class LiveTradingRefused(PaperBrokerError):
    """Raised when anything attempts to leave the paper endpoint."""


def _reject_live(url: str) -> None:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host in LIVE_HOSTS:
        raise LiveTradingRefused(f"live trading host is refused by construction: {host}")
    if host != PAPER_HOST:
        raise LiveTradingRefused(f"only {PAPER_HOST} may be contacted, got: {host!r}")


@dataclass(frozen=True)
class BrokerOrder:
    """The subset of an Alpaca order this product reasons about."""

    id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    status: str
    order_class: str
    legs: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.status in {"filled", "canceled", "expired", "rejected", "done_for_day"}

    @property
    def is_partial(self) -> bool:
        return 0 < self.filled_qty < self.qty


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _order(payload: dict[str, Any]) -> BrokerOrder:
    return BrokerOrder(
        id=str(payload.get("id") or ""),
        client_order_id=str(payload.get("client_order_id") or ""),
        symbol=str(payload.get("symbol") or ""),
        side=str(payload.get("side") or ""),
        qty=_as_float(payload.get("qty")),
        filled_qty=_as_float(payload.get("filled_qty")),
        filled_avg_price=(
            _as_float(payload.get("filled_avg_price"))
            if payload.get("filled_avg_price") not in (None, "")
            else None
        ),
        status=str(payload.get("status") or ""),
        order_class=str(payload.get("order_class") or "simple"),
        legs=tuple(payload.get("legs") or ()),
        raw=payload,
    )


class PaperBrokerClient:
    """Read/write access to the Alpaca paper trading API, and nothing else."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        timeout: float = 20.0,
        retries: int = 3,
    ) -> None:
        self.base_url = PAPER_BASE
        self._key = api_key or os.environ.get("ALPACA_API_KEY_ID", "")
        self._secret = secret_key or os.environ.get("ALPACA_API_SECRET_KEY", "")
        self._timeout = timeout
        self._retries = max(1, retries)
        if not self._key or not self._secret:
            raise PaperBrokerError("Alpaca credentials are not present in the environment")

    # -- transport ------------------------------------------------------

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        _reject_live(url)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "APCA-API-KEY-ID": self._key,
                "APCA-API-SECRET-KEY": self._secret,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        last: Exception | None = None
        for attempt in range(1, self._retries + 1):
            try:
                with open_allowlisted_url(
                    request, timeout=self._timeout, allowed_hosts=(PAPER_HOST,)
                ) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
                # 4xx other than 429 are deterministic: never retry them, and
                # never retry a write, because a retried submit that actually
                # succeeded would double the position.
                if exc.code == 429 or (exc.code >= 500 and method == "GET"):
                    last = PaperBrokerError(f"{exc.code} {detail}")
                    if attempt < self._retries:
                        time.sleep(min(2 ** attempt, 8))
                        continue
                raise PaperBrokerError(f"{method} {path} -> {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                # A write that timed out may still have been accepted. The
                # caller must reconcile by client_order_id rather than resubmit.
                if method != "GET":
                    raise PaperBrokerError(
                        f"{method} {path} outcome is ambiguous; "
                        f"reconcile before resubmitting: {exc}"
                    ) from exc
                if attempt < self._retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
        raise PaperBrokerError(f"{method} {path} failed: {last}")

    # -- reads ----------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def get_clock(self) -> dict[str, Any]:
        return self._request("GET", "/v2/clock")

    def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v2/calendar?start={start}&end={end}")

    def get_positions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/positions")

    def get_open_orders(self) -> list[BrokerOrder]:
        payload = self._request("GET", "/v2/orders?status=open&nested=true&limit=500")
        return [_order(o) for o in payload]

    def get_orders_since(self, after_iso: str) -> list[BrokerOrder]:
        q = urllib.parse.urlencode(
            {"status": "all", "after": after_iso, "nested": "true", "limit": "500"}
        )
        return [_order(o) for o in self._request("GET", f"/v2/orders?{q}")]

    def find_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        """The reconciliation primitive: did this order already reach the broker?"""

        q = urllib.parse.urlencode({"client_order_id": client_order_id})
        try:
            return _order(self._request("GET", f"/v2/orders:by_client_order_id?{q}"))
        except PaperBrokerError as exc:
            if "404" in str(exc):
                return None
            raise

    # -- writes ---------------------------------------------------------

    def submit_bracket_buy(
        self,
        *,
        symbol: str,
        qty: int,
        limit_price: float,
        take_profit: float,
        stop_loss: float,
        client_order_id: str,
        time_in_force: str = "day",
    ) -> BrokerOrder:
        """Submit a long bracket entry with broker-managed protection.

        Long only. Extended hours is never requested: Alpaca rejects bracket
        orders outside regular hours, and the strategy enters after the open.
        """

        if qty <= 0:
            raise PaperBrokerError("refusing to submit a non-positive quantity")
        if not (stop_loss < limit_price < take_profit):
            raise PaperBrokerError(
                f"incoherent bracket: stop {stop_loss} < entry {limit_price} < target {take_profit}"
            )
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "limit_price": f"{limit_price:.4f}",
            "time_in_force": time_in_force,
            "order_class": "bracket",
            "extended_hours": False,
            "client_order_id": client_order_id,
            "take_profit": {"limit_price": f"{take_profit:.4f}"},
            "stop_loss": {"stop_price": f"{stop_loss:.4f}"},
        }
        return _order(self._request("POST", "/v2/orders", body))

    def cancel_order(self, order_id: str) -> None:
        self._request("DELETE", f"/v2/orders/{order_id}")

    def close_position(self, symbol: str) -> BrokerOrder:
        return _order(self._request("DELETE", f"/v2/positions/{symbol}"))

    # -- safety ---------------------------------------------------------

    def assert_paper_account(self) -> dict[str, Any]:
        """Fail closed unless the broker itself says this is a paper account."""

        account = self.get_account()
        number = str(account.get("account_number") or "")
        if not number.startswith(PAPER_ACCOUNT_PREFIX):
            raise LiveTradingRefused(
                f"account number {number[:3]}... lacks the paper prefix "
                f"{PAPER_ACCOUNT_PREFIX!r}; refusing to trade"
            )
        if account.get("trading_blocked") or account.get("account_blocked"):
            raise PaperBrokerError("broker reports the account is blocked")
        return account
