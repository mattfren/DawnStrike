"""Canonical one-percent total-account/session truth contract.

This module is intentionally independent of strategy scoring and position sizing.
It defines the accounting identity and the evidence states used by both V5 and
V6 account ledgers.  Missing evidence remains missing; it is never represented
as a zero return.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

ACCOUNT_SESSION_CONTRACT_VERSION = "dawnstrike.account_session_target.v1"
TARGET_NET_RETURN = Decimal("0.01")
TARGET_NET_RETURN_PCT = Decimal("1.00")
RESEARCH_ONLY = True
BROKER_EXECUTION_ENABLED = False


class AccountSessionStatus(str, Enum):
    """Evidence status for one expected account/session row."""

    COMPLETE_TARGET_MET = "COMPLETE_TARGET_MET"
    COMPLETE_TARGET_NOT_MET = "COMPLETE_TARGET_NOT_MET"
    NO_TRADE = "NO_TRADE"
    PENDING = "PENDING"
    MISSING = "MISSING"
    DEGRADED = "DEGRADED"
    QUARANTINED = "QUARANTINED"


# Short alias for callers that use the contract's plain-language name.
SessionStatus = AccountSessionStatus


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def compute_net_total_account_return(
    *,
    beginning_equity_cents: int | Decimal | str | None,
    ending_equity_cents: int | Decimal | str | None,
    external_flow_cents: int | Decimal | str | None = 0,
) -> Decimal | None:
    """Return ``(ending - beginning - flows) / beginning`` as a Decimal.

    Values are cents so the identity is exact for ledger persistence.  A
    missing value returns ``None`` and an invalid/non-positive opening equity
    raises, preventing a caller from manufacturing a result.
    """

    beginning = _decimal(beginning_equity_cents)
    ending = _decimal(ending_equity_cents)
    flows = _decimal(external_flow_cents)
    if beginning is None or ending is None or flows is None:
        return None
    if beginning <= 0:
        raise ValueError("beginning equity must be positive")
    return (ending - beginning - flows) / beginning


def account_session_return_pct(
    *,
    beginning_equity_cents: int | Decimal | str | None,
    ending_equity_cents: int | Decimal | str | None,
    external_flow_cents: int | Decimal | str | None = 0,
) -> Decimal | None:
    """Return the canonical identity in percentage points (1% is ``1.00``)."""

    value = compute_net_total_account_return(
        beginning_equity_cents=beginning_equity_cents,
        ending_equity_cents=ending_equity_cents,
        external_flow_cents=external_flow_cents,
    )
    return value * Decimal("100") if value is not None else None


def _receipt_is_authoritative(receipt: Any) -> bool:
    if not isinstance(receipt, dict):
        return False
    receipt_id = str(receipt.get("receipt_id") or receipt.get("id") or "").strip()
    if not receipt_id:
        return False
    if receipt.get("authoritative") is False:
        return False
    if receipt.get("status") in {"UNTRUSTED", "CONFLICT", "QUARANTINED"}:
        return False
    return bool(receipt.get("authoritative", True))


@dataclass(frozen=True, slots=True)
class AccountSessionValidation:
    """Deterministic validation result for a canonical account/session row."""

    status: AccountSessionStatus
    net_return: Decimal | None
    reasons: tuple[str, ...] = ()
    research_only: bool = RESEARCH_ONLY
    broker_execution_enabled: bool = BROKER_EXECUTION_ENABLED
    contract_version: str = ACCOUNT_SESSION_CONTRACT_VERSION

    @property
    def valid(self) -> bool:
        return self.status in {
            AccountSessionStatus.COMPLETE_TARGET_MET,
            AccountSessionStatus.COMPLETE_TARGET_NOT_MET,
            AccountSessionStatus.NO_TRADE,
        }

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        result["net_return"] = (
            str(self.net_return) if self.net_return is not None else None
        )
        return result


def validate_account_session(
    *,
    expected_session: bool,
    beginning_equity_cents: int | Decimal | str | None,
    ending_equity_cents: int | Decimal | str | None,
    external_flow_cents: int | Decimal | str | None = 0,
    authoritative_receipt: dict[str, Any] | None = None,
    no_trade: bool = False,
    pending: bool = False,
    degraded: bool = False,
    quarantined: bool = False,
) -> AccountSessionValidation:
    """Validate one expected session while preserving null versus zero.

    ``NO_TRADE`` is only valid with an authoritative session receipt.  A
    no-trade assertion without that receipt is ``MISSING`` rather than zero.
    """

    if not expected_session:
        return AccountSessionValidation(
            AccountSessionStatus.MISSING,
            None,
            ("expected_market_session_missing",),
        )
    if quarantined:
        return AccountSessionValidation(
            AccountSessionStatus.QUARANTINED,
            None,
            ("evidence_quarantined",),
        )
    if degraded:
        return AccountSessionValidation(
            AccountSessionStatus.DEGRADED,
            None,
            ("evidence_degraded",),
        )
    if pending:
        return AccountSessionValidation(
            AccountSessionStatus.PENDING,
            None,
            ("session_not_finalized",),
        )
    if no_trade:
        if not _receipt_is_authoritative(authoritative_receipt):
            return AccountSessionValidation(
                AccountSessionStatus.MISSING,
                None,
                ("authoritative_no_trade_receipt_missing",),
            )
        return AccountSessionValidation(AccountSessionStatus.NO_TRADE, Decimal("0"))

    net_return = compute_net_total_account_return(
        beginning_equity_cents=beginning_equity_cents,
        ending_equity_cents=ending_equity_cents,
        external_flow_cents=external_flow_cents,
    )
    if net_return is None:
        return AccountSessionValidation(
            AccountSessionStatus.MISSING,
            None,
            ("account_equity_evidence_missing",),
        )
    status = (
        AccountSessionStatus.COMPLETE_TARGET_MET
        if net_return >= TARGET_NET_RETURN
        else AccountSessionStatus.COMPLETE_TARGET_NOT_MET
    )
    return AccountSessionValidation(status, net_return)


@dataclass(frozen=True, slots=True)
class AccountSessionTarget:
    """Persistable identity and target fields for a ledger session row."""

    account_id: str
    market_date: str
    expected_session_id: str
    status: AccountSessionStatus
    beginning_equity_cents: int | None
    ending_equity_cents: int | None
    external_flow_cents: int | None
    net_return: Decimal | None
    authoritative_receipt_id: str | None = None
    experiment_id: str | None = None
    arm_id: str | None = None
    evidence_mode: str = "forward_observed"
    lineage_sha256: str | None = None
    research_only: bool = RESEARCH_ONLY
    broker_execution_enabled: bool = BROKER_EXECUTION_ENABLED
    contract_version: str = ACCOUNT_SESSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.market_date.strip():
            raise ValueError("account_id and market_date are required")
        if not self.expected_session_id.strip():
            raise ValueError("expected_session_id is required")
        if not self.research_only or self.broker_execution_enabled:
            raise ValueError("account session contract is research-only")
        if self.status is AccountSessionStatus.NO_TRADE:
            if not self.authoritative_receipt_id:
                raise ValueError("NO_TRADE requires an authoritative receipt")
            if self.net_return != Decimal("0"):
                raise ValueError("NO_TRADE return must be exactly zero")
        if self.status is AccountSessionStatus.MISSING and self.net_return is not None:
            raise ValueError("MISSING sessions cannot have a return")

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        result["net_return"] = str(self.net_return) if self.net_return is not None else None
        return result


__all__ = [
    "ACCOUNT_SESSION_CONTRACT_VERSION",
    "TARGET_NET_RETURN",
    "TARGET_NET_RETURN_PCT",
    "RESEARCH_ONLY",
    "BROKER_EXECUTION_ENABLED",
    "AccountSessionStatus",
    "SessionStatus",
    "AccountSessionValidation",
    "AccountSessionTarget",
    "compute_net_total_account_return",
    "account_session_return_pct",
    "validate_account_session",
]
