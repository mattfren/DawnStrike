"""Independent accounting oracle for the synthetic E2E rehearsal.

Deliberately does not import or call anything from
``intraday_scanner.execution.paper_engine`` or ``paper_broker`` for its
arithmetic - it replays the same event list (order + fills) through its own
``Decimal``-based ledger, so a bug shared between the production P&L code and
the check would not go unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal


def D(value: float | str | Decimal) -> Decimal:
    return Decimal(str(value))


@dataclass
class Fill:
    side: str  # "buy" | "sell"
    qty: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")


@dataclass
class OracleLedger:
    """A from-scratch replay of one symbol's round trip."""

    starting_cash: Decimal
    fills: list[Fill] = field(default_factory=list)

    def apply(self, fill: Fill) -> None:
        self.fills.append(fill)

    @property
    def cash(self) -> Decimal:
        cash = self.starting_cash
        for f in self.fills:
            notional = f.qty * f.price
            if f.side == "buy":
                cash -= notional + f.fee
            elif f.side == "sell":
                cash += notional - f.fee
            else:
                raise ValueError(f"unknown side {f.side!r}")
        return cash

    @property
    def position_qty(self) -> Decimal:
        qty = Decimal("0")
        for f in self.fills:
            qty += f.qty if f.side == "buy" else -f.qty
        return qty

    def realized_pnl(self) -> tuple[Decimal, Decimal]:
        """Gross and net realized P&L for a fully round-tripped position.

        Only valid once ``position_qty`` is back to zero. Gross ignores
        fees; net includes every fee on every fill.
        """

        if self.position_qty != 0:
            raise ValueError("position is not flat; realized P&L is undefined")
        buys = [f for f in self.fills if f.side == "buy"]
        sells = [f for f in self.fills if f.side == "sell"]
        buy_notional = sum((f.qty * f.price for f in buys), Decimal("0"))
        sell_notional = sum((f.qty * f.price for f in sells), Decimal("0"))
        total_fees = sum((f.fee for f in self.fills), Decimal("0"))
        gross = sell_notional - buy_notional
        net = gross - total_fees
        return (
            gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN),
            net.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN),
        )

    def final_equity(self) -> Decimal:
        if self.position_qty != 0:
            raise ValueError("final_equity is only defined once flat")
        return self.cash.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    def mfe_diagnostic(self, *, high_water_price: Decimal, entry_price: Decimal, qty: Decimal) -> Decimal:
        """Maximum favorable excursion, as a diagnostic only.

        This number never feeds into realized_pnl/final_equity - a positive
        MFE on a trade that closed at a loss must stay a loss.
        """

        return ((high_water_price - entry_price) * qty).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )


@dataclass
class OracleComparison:
    field_name: str
    expected: Decimal
    observed: Decimal

    @property
    def matches(self) -> bool:
        return self.expected == self.observed

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "field": self.field_name,
            "expected": str(self.expected),
            "observed": str(self.observed),
            "matches": self.matches,
        }


def compare(expected: dict[str, Decimal], observed: dict[str, Decimal]) -> list[OracleComparison]:
    results = []
    for key, exp in expected.items():
        obs = observed.get(key)
        if obs is None:
            raise KeyError(f"observed accounting is missing field {key!r}")
        results.append(OracleComparison(key, D(exp), D(obs)))
    return results
