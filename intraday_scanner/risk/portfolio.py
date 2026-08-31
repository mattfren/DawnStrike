"""Centralized portfolio risk authority for research and paper proposals.

This module is deliberately independent of broker clients.  A strategy may
propose a paper entry, but the proposal is not admitted until this authority
has evaluated the complete account snapshot.  Missing facts are rejection
conditions; callers must never turn an unavailable mark into zero.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

PORTFOLIO_RISK_SCHEMA = "dawnstrike.portfolio_risk_receipt.v1"
PORTFOLIO_RISK_POLICY_VERSION = "dawnstrike-portfolio-risk-v1"


@dataclass(frozen=True, slots=True)
class PortfolioRiskLimits:
    """Hard portfolio limits. Percentages are expressed as fractions."""

    max_gross_exposure_pct: float = 0.30
    max_net_exposure_pct: float = 0.30
    max_symbol_exposure_pct: float = 0.10
    max_sector_exposure_pct: float = 0.20
    max_theme_exposure_pct: float = 0.20
    max_open_risk_pct: float = 0.0075
    max_daily_loss_pct: float = 0.01
    max_drawdown_pct: float = 0.08
    max_simultaneous_positions: int = 3
    max_price_age_seconds: int = 300
    # This is a reporting objective only. It is never consulted by admission.
    daily_return_target_pct: float = 0.01
    policy_version: str = PORTFOLIO_RISK_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "max_gross_exposure_pct",
            "max_net_exposure_pct",
            "max_symbol_exposure_pct",
            "max_sector_exposure_pct",
            "max_theme_exposure_pct",
            "max_open_risk_pct",
            "max_daily_loss_pct",
            "max_drawdown_pct",
            "daily_return_target_pct",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if self.max_simultaneous_positions < 1 or self.max_price_age_seconds < 1:
            raise ValueError("position and price-age limits must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioPosition:
    symbol: str
    side: str
    quantity: int
    mark_price: float | None
    entry_price: float | None = None
    stop_price: float | None = None
    sector: str | None = None
    theme: str | None = None
    price_observed_at: str | None = None
    strategy_id: str | None = None
    risk_amount: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PortfolioPosition:
        symbol = str(value.get("symbol") or value.get("ticker") or "").strip().upper()
        side = str(value.get("side") or value.get("direction") or "").strip().lower()
        quantity = _int(value.get("quantity"))
        mark = _number(value.get("mark_price", value.get("last_mark_price")))
        return cls(
            symbol=symbol,
            side=side,
            quantity=quantity,
            mark_price=mark,
            entry_price=_number(value.get("entry_price", value.get("entry"))),
            stop_price=_number(value.get("stop_price", value.get("stop"))),
            sector=_text(value.get("sector")),
            theme=_text(value.get("theme", value.get("correlation_group"))),
            price_observed_at=_text(
                value.get("price_observed_at", value.get("last_mark_at"))
            ),
            strategy_id=_text(value.get("strategy_id")),
            risk_amount=_number(value.get("risk_amount", value.get("max_loss_estimate"))),
        )


@dataclass(frozen=True, slots=True)
class PortfolioRiskSnapshot:
    equity: float | None
    positions: tuple[PortfolioPosition, ...] = ()
    pending: tuple[PortfolioPosition, ...] = ()
    daily_realized_pnl: float | None = None
    daily_unrealized_pnl: float | None = None
    peak_equity: float | None = None
    as_of: str | None = None
    metadata_complete: bool = False

    @classmethod
    def from_mappings(
        cls,
        *,
        equity: float | None,
        positions: Sequence[Mapping[str, Any]] = (),
        pending: Sequence[Mapping[str, Any]] = (),
        daily_realized_pnl: float | None = None,
        daily_unrealized_pnl: float | None = None,
        peak_equity: float | None = None,
        as_of: str | None = None,
        metadata_complete: bool = False,
    ) -> PortfolioRiskSnapshot:
        return cls(
            equity=equity,
            positions=tuple(PortfolioPosition.from_mapping(row) for row in positions),
            pending=tuple(PortfolioPosition.from_mapping(row) for row in pending),
            daily_realized_pnl=daily_realized_pnl,
            daily_unrealized_pnl=daily_unrealized_pnl,
            peak_equity=peak_equity,
            as_of=as_of,
            metadata_complete=metadata_complete,
        )


@dataclass(frozen=True, slots=True)
class PortfolioOrderProposal:
    symbol: str
    side: str
    quantity: int
    price: float | None
    stop_price: float | None
    strategy_id: str
    sector: str | None = None
    theme: str | None = None
    price_observed_at: str | None = None
    metadata_complete: bool = False
    live_execution_requested: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PortfolioOrderProposal:
        return cls(
            symbol=str(value.get("symbol") or value.get("ticker") or "").strip().upper(),
            side=str(value.get("side") or value.get("direction") or "").strip().lower(),
            quantity=_int(value.get("quantity")),
            price=_number(value.get("price", value.get("entry"))),
            stop_price=_number(value.get("stop_price", value.get("stop"))),
            strategy_id=str(value.get("strategy_id") or "").strip(),
            sector=_text(value.get("sector")),
            theme=_text(value.get("theme", value.get("correlation_group"))),
            price_observed_at=_text(value.get("price_observed_at", value.get("signal_time"))),
            metadata_complete=bool(value.get("metadata_complete", False)),
            live_execution_requested=bool(value.get("live_execution_requested", False)),
        )


@dataclass(frozen=True, slots=True)
class PortfolioRiskDecision:
    allowed: bool
    action: str
    reason_codes: tuple[str, ...]
    policy_version: str
    computed: dict[str, float | int | None]
    receipt_hash_sha256: str
    schema_version: str = PORTFOLIO_RISK_SCHEMA

    @property
    def blocked(self) -> bool:
        return not self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "reason_codes": list(self.reason_codes),
        }


def evaluate_portfolio_risk(
    proposal: PortfolioOrderProposal | Mapping[str, Any],
    snapshot: PortfolioRiskSnapshot,
    *,
    limits: PortfolioRiskLimits | None = None,
    now: str | datetime | None = None,
) -> PortfolioRiskDecision:
    """Evaluate one proposal against the aggregate account state.

    The result is deterministic for the supplied inputs.  A 1% daily target
    is included in the receipt for measurement, but cannot make a proposal
    pass or relax any limit.
    """

    limits = limits or PortfolioRiskLimits()
    proposal = (
        proposal
        if isinstance(proposal, PortfolioOrderProposal)
        else PortfolioOrderProposal.from_mapping(proposal)
    )
    reasons: list[str] = []
    computed: dict[str, float | int | None] = {
        "daily_return_target_pct": limits.daily_return_target_pct,
    }
    equity = _finite_positive(snapshot.equity)
    if not snapshot.metadata_complete:
        reasons.append("PORTFOLIO_METADATA_UNKNOWN")
    if equity is None:
        reasons.append("EQUITY_UNKNOWN")
    else:
        computed["equity"] = equity
    if proposal.live_execution_requested:
        reasons.append("LIVE_EXECUTION_DISABLED")
    if not proposal.symbol:
        reasons.append("SYMBOL_UNKNOWN")
    if proposal.side not in {"long", "short", "buy", "sell"}:
        reasons.append("SIDE_UNKNOWN")
    if proposal.quantity <= 0:
        reasons.append("QUANTITY_INVALID")
    price = _finite_positive(proposal.price)
    stop = _finite_positive(proposal.stop_price)
    if price is None:
        reasons.append("PROPOSAL_PRICE_UNKNOWN")
    if stop is None or price is not None and stop == price:
        reasons.append("PROPOSAL_STOP_UNKNOWN")
    if not proposal.metadata_complete:
        reasons.append("PROPOSAL_METADATA_UNKNOWN")
    _price_age_gate(
        reasons,
        observed_at=proposal.price_observed_at,
        as_of=now or snapshot.as_of,
        max_age_seconds=limits.max_price_age_seconds,
        missing_code="PROPOSAL_PRICE_TIMESTAMP_UNKNOWN",
    )

    all_positions = (*snapshot.positions, *snapshot.pending)
    for position in all_positions:
        _price_age_gate(
            reasons,
            observed_at=position.price_observed_at,
            as_of=now or snapshot.as_of,
            max_age_seconds=limits.max_price_age_seconds,
            missing_code="POSITION_PRICE_TIMESTAMP_UNKNOWN",
        )
    aggregate = _aggregate_positions(all_positions, reasons)
    proposal_notional = None if price is None else abs(price * proposal.quantity)
    proposal_sign = -1.0 if proposal.side in {"short", "sell"} else 1.0
    proposal_net = None if proposal_notional is None else proposal_sign * proposal_notional
    proposal_risk = (
        None
        if price is None or stop is None
        else abs(price - stop) * proposal.quantity
    )
    if proposal_risk is None:
        reasons.append("PROPOSAL_RISK_UNKNOWN")
    if (
        equity is not None
        and proposal_notional is not None
        and proposal_net is not None
        and proposal_risk is not None
    ):
        gross = aggregate["gross"] + proposal_notional
        net = aggregate["net"] + proposal_net
        open_risk = aggregate["risk"] + proposal_risk
        computed.update(
            {
                "gross_exposure": gross,
                "net_exposure": net,
                "open_risk": open_risk,
                "gross_exposure_pct": gross / equity,
                "net_exposure_pct": abs(net) / equity,
                "open_risk_pct": open_risk / equity,
                "simultaneous_positions": len(all_positions) + 1,
            }
        )
        if gross > equity * limits.max_gross_exposure_pct:
            reasons.append("GROSS_EXPOSURE_LIMIT")
        if abs(net) > equity * limits.max_net_exposure_pct:
            reasons.append("NET_EXPOSURE_LIMIT")
        if open_risk > equity * limits.max_open_risk_pct:
            reasons.append("OPEN_RISK_LIMIT")
        if len(all_positions) + 1 > limits.max_simultaneous_positions:
            reasons.append("SIMULTANEOUS_POSITION_LIMIT")
        _concentration_gate(
            reasons,
            all_positions,
            proposal,
            proposal_notional,
            equity,
            limits,
        )
    else:
        reasons.append("PORTFOLIO_EXPOSURE_UNKNOWN")

    if (
        snapshot.daily_realized_pnl is None
        or snapshot.daily_unrealized_pnl is None
        or _number(snapshot.daily_realized_pnl) is None
        or _number(snapshot.daily_unrealized_pnl) is None
    ):
        reasons.append("DAILY_PNL_UNKNOWN")
    elif equity is not None:
        daily_pnl = float(snapshot.daily_realized_pnl) + float(snapshot.daily_unrealized_pnl)
        computed["daily_pnl"] = daily_pnl
        computed["daily_loss_pct"] = max(0.0, -daily_pnl / equity)
        if daily_pnl <= -(equity * limits.max_daily_loss_pct):
            reasons.append("DAILY_LOSS_LIMIT")
    if snapshot.peak_equity is None or _finite_positive(snapshot.peak_equity) is None:
        reasons.append("DRAWDOWN_UNKNOWN")
    elif equity is not None:
        drawdown = max(0.0, (float(snapshot.peak_equity) - equity) / float(snapshot.peak_equity))
        computed["drawdown_pct"] = drawdown
        if drawdown > limits.max_drawdown_pct:
            reasons.append("DRAWDOWN_LIMIT")

    unique_reasons = tuple(dict.fromkeys(reasons))
    body = {
        "schema_version": PORTFOLIO_RISK_SCHEMA,
        "policy_version": limits.policy_version,
        "proposal": _canonical(proposal),
        "snapshot": _canonical(snapshot),
        "limits": _canonical(limits),
        "allowed": not unique_reasons,
        "reason_codes": list(unique_reasons),
        "computed": computed,
    }
    receipt_hash = hashlib.sha256(_json_bytes(body)).hexdigest()
    return PortfolioRiskDecision(
        allowed=not unique_reasons,
        action="PAPER_ALLOW" if not unique_reasons else "PAPER_BLOCK",
        reason_codes=unique_reasons,
        policy_version=limits.policy_version,
        computed=computed,
        receipt_hash_sha256=receipt_hash,
    )


class PortfolioRiskAuthority:
    """Small callable façade used by strategy and PaperOps entry paths."""

    def __init__(self, limits: PortfolioRiskLimits | None = None) -> None:
        self.limits = limits or PortfolioRiskLimits()

    def evaluate(
        self,
        proposal: PortfolioOrderProposal | Mapping[str, Any],
        snapshot: PortfolioRiskSnapshot,
        *,
        now: str | datetime | None = None,
    ) -> PortfolioRiskDecision:
        return evaluate_portfolio_risk(proposal, snapshot, limits=self.limits, now=now)


def _aggregate_positions(
    positions: Sequence[PortfolioPosition], reasons: list[str]
) -> dict[str, float]:
    gross = net = risk = 0.0
    for position in positions:
        if not position.symbol or position.quantity <= 0:
            reasons.append("POSITION_METADATA_UNKNOWN")
        if position.side not in {"long", "short", "buy", "sell"}:
            reasons.append("POSITION_SIDE_UNKNOWN")
        mark = _finite_positive(position.mark_price)
        if mark is None:
            reasons.append("POSITION_PRICE_UNKNOWN")
            continue
        notional = abs(mark * position.quantity)
        sign = -1.0 if position.side in {"short", "sell"} else 1.0
        gross += notional
        net += sign * notional
        if position.risk_amount is None:
            entry = _finite_positive(position.entry_price)
            stop = _finite_positive(position.stop_price)
            if entry is None or stop is None or entry == stop:
                reasons.append("POSITION_RISK_UNKNOWN")
            else:
                risk += abs(entry - stop) * position.quantity
        else:
            parsed_risk = _number(position.risk_amount)
            if parsed_risk is None or parsed_risk < 0:
                reasons.append("POSITION_RISK_UNKNOWN")
            else:
                risk += parsed_risk
    return {"gross": gross, "net": net, "risk": risk}


def _concentration_gate(
    reasons: list[str],
    positions: Sequence[PortfolioPosition],
    proposal: PortfolioOrderProposal,
    proposal_notional: float,
    equity: float,
    limits: PortfolioRiskLimits,
) -> None:
    rows = [
        (
            position.symbol,
            position.sector,
            position.theme,
            abs(position.mark_price or 0) * position.quantity,
        )
        for position in positions
    ]
    rows.append((proposal.symbol, proposal.sector, proposal.theme, proposal_notional))
    for index, (label, limit, unknown_code) in enumerate(
        (
            (proposal.symbol, limits.max_symbol_exposure_pct, "SYMBOL_METADATA_UNKNOWN"),
            (proposal.sector, limits.max_sector_exposure_pct, "SECTOR_METADATA_UNKNOWN"),
            (proposal.theme, limits.max_theme_exposure_pct, "THEME_METADATA_UNKNOWN"),
        )
    ):
        if label is None or not str(label).strip():
            # Symbol is mandatory; sector/theme are optional "where known".
            if index == 0:
                reasons.append(unknown_code)
            continue
        total = sum(
            value
            for symbol, sector, theme, value in rows
            if (symbol, sector, theme)[index] == label
        )
        if total > equity * limit:
            kind = "SYMBOL" if index == 0 else "SECTOR" if index == 1 else "THEME"
            reasons.append(f"{kind}_CONCENTRATION_LIMIT")


def _price_age_gate(
    reasons: list[str],
    *,
    observed_at: str | None,
    as_of: str | datetime | None,
    max_age_seconds: int,
    missing_code: str,
) -> None:
    if not observed_at or as_of is None:
        reasons.append(missing_code)
        return
    try:
        observed = _datetime(observed_at)
        current = (
            _datetime(as_of)
            if not isinstance(as_of, datetime)
            else as_of.astimezone(timezone.utc)
        )
    except (TypeError, ValueError):
        reasons.append(missing_code)
        return
    age_seconds = (current - observed).total_seconds()
    if age_seconds < 0:
        reasons.append("FUTURE_PRICE_TIMESTAMP")
    elif age_seconds > max_age_seconds:
        reasons.append("STALE_PRICE")


def _canonical(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return {key: _canonical(item) for key, item in asdict(value).items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    return value


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _finite_positive(value: Any) -> float | None:
    parsed = _number(value)
    return parsed if parsed is not None and parsed > 0 else None


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = [
    "PORTFOLIO_RISK_POLICY_VERSION",
    "PORTFOLIO_RISK_SCHEMA",
    "PortfolioOrderProposal",
    "PortfolioPosition",
    "PortfolioRiskAuthority",
    "PortfolioRiskDecision",
    "PortfolioRiskLimits",
    "PortfolioRiskSnapshot",
    "evaluate_portfolio_risk",
]
