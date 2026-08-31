"""Canonical one-percent total-account/session truth contract.

This module is intentionally independent of strategy scoring and position sizing.
It defines the accounting identity and the evidence states used by both V5 and
V6 account ledgers.  Missing evidence remains missing; it is never represented
as a zero return.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from intraday_scanner.alpha.commit_bridge import (
    has_authenticated_no_trade_receipt,
)
from intraday_scanner.market_calendar import canonical_regular_session_id

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
    """Only store-mediated typed receipts can authorize NO_TRADE."""

    return has_authenticated_no_trade_receipt(receipt)


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
        result["net_return"] = str(self.net_return) if self.net_return is not None else None
        return result


def validate_account_session(
    *,
    expected_session: bool,
    beginning_equity_cents: int | Decimal | str | None,
    ending_equity_cents: int | Decimal | str | None,
    external_flow_cents: int | Decimal | str | None = 0,
    authoritative_receipt: object | None = None,
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
        try:
            canonical_session_id = canonical_regular_session_id(self.market_date)
        except ValueError as exc:
            raise ValueError("market_date must be ISO YYYY-MM-DD") from exc
        if self.expected_session_id != canonical_session_id:
            raise ValueError("expected_session_id must be the canonical regular-session identity")
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


def evaluate_expected_account_sessions(
    *,
    expected_sessions: list[dict[str, Any]],
    observed_sessions: list[dict[str, Any]],
    account_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate one account across every authoritative expected market session.

    A closed no-trade session is a real zero only when its observation carries
    an authoritative receipt. Missing, partial, degraded, and quarantined
    sessions remain non-evaluable and block completeness.
    """

    target_account_id = str(account_id or "").strip()
    if not target_account_id:
        raise ValueError("account_id is required for exact account/session evaluation")

    normalized_expected: list[dict[str, Any]] = []
    expected_by_date: dict[str, dict[str, Any]] = {}
    expected_ids: set[str] = set()
    for raw_expected in expected_sessions:
        if not isinstance(raw_expected, Mapping):
            raise ValueError("expected account sessions must be mappings")
        expected = dict(raw_expected)
        market_date = str(expected.get("market_date") or "").strip()
        canonical_session_id = canonical_regular_session_id(market_date)
        supplied_session_id = str(
            expected.get("session_id") or expected.get("expected_session_id") or ""
        ).strip()
        if supplied_session_id != canonical_session_id:
            raise ValueError(
                "expected account session must use the canonical regular-session identity"
            )
        expected_account_id = str(expected.get("account_id") or "").strip()
        if expected_account_id and expected_account_id != target_account_id:
            raise ValueError("expected account session account_id does not match target account")
        if market_date in expected_by_date or canonical_session_id in expected_ids:
            raise ValueError(f"duplicate expected account session: {canonical_session_id}")
        expected["market_date"] = market_date
        expected["session_id"] = canonical_session_id
        expected_by_date[market_date] = expected
        expected_ids.add(canonical_session_id)
        normalized_expected.append(expected)

    observed_by_date: dict[str, dict[str, Any]] = {}
    observed_count = 0
    for raw_observed in observed_sessions:
        if not isinstance(raw_observed, Mapping):
            raise ValueError("observed account sessions must be mappings")
        observed = dict(raw_observed)
        observed_account_id = str(observed.get("account_id") or "").strip()
        if observed_account_id != target_account_id:
            # Other accounts are outside this single-account evaluation and
            # must not be allowed to satisfy or shadow its expected sessions.
            continue
        observed_count += 1
        market_date = str(observed.get("market_date") or "").strip()
        supplied_session_id = str(
            observed.get("session_id") or observed.get("expected_session_id") or ""
        ).strip()
        identity_error: str | None = None
        try:
            canonical_session_id = canonical_regular_session_id(market_date)
        except ValueError:
            canonical_session_id = ""
            identity_error = "observed account session market_date is invalid"
        if not identity_error and supplied_session_id != canonical_session_id:
            identity_error = (
                "observed account session must use the canonical regular-session identity"
            )
        expected_for_observed = expected_by_date.get(market_date)
        if (
            not identity_error
            and expected_for_observed is not None
            and supplied_session_id != str(expected_for_observed["session_id"])
        ):
            identity_error = "observed account session does not match expected session identity"
        if market_date in observed_by_date:
            duplicate_id = canonical_session_id or supplied_session_id or market_date
            raise ValueError(f"duplicate observed account session: {duplicate_id}")
        observed["market_date"] = market_date
        observed["session_id"] = canonical_session_id or supplied_session_id
        if identity_error:
            observed["__identity_error"] = identity_error
        observed_by_date[market_date] = observed

    rows: list[dict[str, Any]] = []
    blocking: list[str] = []
    for expected_row in sorted(normalized_expected, key=lambda row: str(row["market_date"])):
        market_date = str(expected_row["market_date"])
        observed_for_date = observed_by_date.get(market_date)
        if observed_for_date is None:
            validation = validate_account_session(
                expected_session=True,
                beginning_equity_cents=None,
                ending_equity_cents=None,
            )
        else:
            status = str(observed_for_date.get("status") or "").upper()
            if observed_for_date.get("__identity_error"):
                validation = AccountSessionValidation(
                    AccountSessionStatus.MISSING,
                    None,
                    (str(observed_for_date["__identity_error"]),),
                )
            else:
                receipt = observed_for_date.get("authoritative_receipt") or observed_for_date.get(
                    "session_receipt"
                )
                no_trade = bool(observed_for_date.get("no_trade") or status == "NO_TRADE")
                receipt_reason = _no_trade_receipt_identity_reason(
                    receipt,
                    account_id=target_account_id,
                    market_date=market_date,
                    session_id=str(expected_row["session_id"]),
                    expected=expected_row,
                    observed=observed_for_date,
                ) if no_trade else None
                if receipt_reason is not None:
                    validation = AccountSessionValidation(
                        AccountSessionStatus.MISSING,
                        None,
                        (receipt_reason,),
                    )
                else:
                    validation = validate_account_session(
                        expected_session=True,
                        beginning_equity_cents=observed_for_date.get("beginning_equity_cents"),
                        ending_equity_cents=observed_for_date.get("ending_equity_cents"),
                        external_flow_cents=observed_for_date.get("external_flow_cents", 0),
                        authoritative_receipt=receipt,
                        no_trade=no_trade,
                        pending=status in {"PENDING", "OPEN"},
                        degraded=status in {"PARTIAL", "DEGRADED"},
                        quarantined=status in {"QUARANTINED", "CONFLICT"},
                    )
        row = {
            "account_id": target_account_id,
            "market_date": market_date,
            "expected_session_id": expected_row["session_id"],
            "status": validation.status.value,
            "net_return": str(validation.net_return) if validation.net_return is not None else None,
            "reasons": list(validation.reasons),
            "target_return_pct": "1.00",
            "research_only": True,
            "broker_execution_enabled": False,
        }
        rows.append(row)
        if not validation.valid:
            blocking.append(market_date + ":" + validation.status.value)
    return {
        "status": "COMPLETE"
        if not blocking and rows
        else "NOT_EVALUABLE_ACCOUNT_SESSION_COMPLETENESS",
        "account_id": target_account_id,
        "expected_session_count": len(normalized_expected),
        "observed_session_count": observed_count,
        "complete_session_count": sum(
            1
            for row in rows
            if row["status"]
            in {
                AccountSessionStatus.COMPLETE_TARGET_MET.value,
                AccountSessionStatus.COMPLETE_TARGET_NOT_MET.value,
                AccountSessionStatus.NO_TRADE.value,
            }
        ),
        "blocking_sessions": blocking,
        "target_return_pct": "1.00",
        "target_is_evaluation_only": True,
        "rows": rows,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _no_trade_receipt_identity_reason(
    receipt: object,
    *,
    account_id: str,
    market_date: str,
    session_id: str,
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> str | None:
    """Return a fail-closed reason when NO_TRADE receipt identity is unsafe."""

    if not _receipt_is_authoritative(receipt):
        return "authoritative_no_trade_receipt_missing"
    if not isinstance(receipt, Mapping):
        return "authoritative_no_trade_receipt_identity_invalid"

    required: dict[str, str] = {
        "account_id": account_id,
        "market_date": market_date,
        "session_id": session_id,
    }
    for field in ("strategy_id", "strategy_version", "experiment_id", "arm_id"):
        expected_value = str(expected.get(field) or "").strip()
        observed_value = str(observed.get(field) or "").strip()
        if expected_value and observed_value and expected_value != observed_value:
            return f"no_trade_{field}_identity_conflict"
        value = expected_value or observed_value
        if value:
            required[field] = value

    for field, wanted in required.items():
        actual = receipt.get(field)
        if field == "session_id" and not actual:
            actual = receipt.get("expected_session_id")
        if str(actual or "").strip() != wanted:
            return f"authoritative_no_trade_{field}_identity_mismatch"
    return None


# Explicit aliases used by research/evaluation callers.
evaluate_account_session_window = evaluate_expected_account_sessions
build_account_session_evaluation = evaluate_expected_account_sessions


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
    "evaluate_expected_account_sessions",
    "evaluate_account_session_window",
    "build_account_session_evaluation",
]
