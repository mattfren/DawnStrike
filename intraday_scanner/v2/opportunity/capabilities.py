"""Sanitized point-in-time provider capability receipts for opportunity research."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity


class CapabilityState(str, Enum):
    """Explicit provider truth; no state is inferred from another capability."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProviderCapabilityReceipt(OpportunityContract):
    capability_receipt_id: str
    provider: str
    feed: str
    entitlement_identity: str
    decision_at: datetime
    observed_at: datetime
    bars: CapabilityState
    trades: CapabilityState
    quotes: CapabilityState
    consolidated_nbbo: CapabilityState
    aggressor_classification: CapabilityState
    corporate_actions: CapabilityState
    halts: CapabilityState
    historical_coverage: CapabilityState
    coverage_start: datetime | None
    coverage_end: datetime | None
    source_identity: str
    method: str
    limitations: tuple[str, ...]
    bounded_coverage: bool = True
    research_only: bool = True
    schema_version: str = "v2.opportunity.provider_capability_receipt.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.capability_receipt_id, "capability_receipt_id"),
            (self.provider, "provider"),
            (self.feed, "feed"),
            (self.entitlement_identity, "entitlement_identity"),
            (self.source_identity, "source_identity"),
            (self.method, "method"),
        ):
            _require_text(value, name)
        _require_sanitized_identity(self.entitlement_identity, "entitlement_identity")
        _require_sanitized_identity(self.source_identity, "source_identity")
        if self.observed_at > self.decision_at:
            raise ValueError("provider capability cannot be observed after decision_at")
        if (self.coverage_start is None) is not (self.coverage_end is None):
            raise ValueError("historical coverage bounds must be present together")
        if self.coverage_start is not None and self.coverage_end is not None:
            if self.coverage_start > self.coverage_end:
                raise ValueError("historical coverage interval is reversed")
            if self.coverage_end > self.decision_at:
                raise ValueError("historical coverage cannot extend after decision_at")
            if self.coverage_end > self.observed_at:
                raise ValueError("historical coverage cannot extend after observed_at")
        if (
            self.historical_coverage is CapabilityState.AVAILABLE
            and self.coverage_start is None
        ):
            raise ValueError("available historical coverage requires causal bounds")
        if (
            self.historical_coverage is not CapabilityState.AVAILABLE
            and self.coverage_start is not None
        ):
            raise ValueError("unavailable historical coverage cannot carry bounds")
        if _is_iex_like(self.provider, self.feed) and (
            self.consolidated_nbbo is CapabilityState.AVAILABLE
        ):
            raise ValueError("IEX feed cannot claim consolidated SIP/NBBO availability")
        if (
            self.consolidated_nbbo is CapabilityState.AVAILABLE
            and self.quotes is not CapabilityState.AVAILABLE
        ):
            raise ValueError("consolidated NBBO requires available quote capability")
        if (
            self.aggressor_classification is CapabilityState.AVAILABLE
            and self.trades is not CapabilityState.AVAILABLE
        ):
            raise ValueError("aggressor classification requires available trade prints")
        if (
            self.aggressor_classification is CapabilityState.AVAILABLE
            and "ohlcv" in self.method.lower()
        ):
            raise ValueError("OHLCV cannot establish aggressor classification")
        if not self.bounded_coverage:
            raise ValueError("package-002 capability receipts must remain bounded")
        if not self.research_only:
            raise ValueError("provider capability receipts must remain research_only")
        if len(self.limitations) != len(set(self.limitations)):
            raise ValueError("duplicate provider capability limitation")
        for limitation in self.limitations:
            _require_sanitized(limitation)
        expected = stable_identity("provider-capability", _identity_payload(self))
        if self.capability_receipt_id != expected:
            raise ValueError("provider capability receipt identity does not match content")


def build_provider_capability_receipt(
    *,
    provider: str,
    feed: str,
    entitlement_identity: str,
    decision_at: datetime,
    observed_at: datetime,
    bars: CapabilityState,
    trades: CapabilityState,
    quotes: CapabilityState,
    consolidated_nbbo: CapabilityState,
    aggressor_classification: CapabilityState,
    corporate_actions: CapabilityState,
    halts: CapabilityState,
    historical_coverage: CapabilityState,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
    source_identity: str,
    method: str,
    limitations: tuple[str, ...],
) -> ProviderCapabilityReceipt:
    """Build a content-bound, explicitly bounded capability receipt."""

    values = {
        "provider": provider,
        "feed": feed,
        "entitlement_identity": entitlement_identity,
        "decision_at": decision_at,
        "observed_at": observed_at,
        "bars": bars,
        "trades": trades,
        "quotes": quotes,
        "consolidated_nbbo": consolidated_nbbo,
        "aggressor_classification": aggressor_classification,
        "corporate_actions": corporate_actions,
        "halts": halts,
        "historical_coverage": historical_coverage,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "source_identity": source_identity,
        "method": method,
        "limitations": limitations,
        "bounded_coverage": True,
        "research_only": True,
        "schema_version": "v2.opportunity.provider_capability_receipt.v1",
    }
    return ProviderCapabilityReceipt(
        capability_receipt_id=stable_identity("provider-capability", values),
        provider=provider,
        feed=feed,
        entitlement_identity=entitlement_identity,
        decision_at=decision_at,
        observed_at=observed_at,
        bars=bars,
        trades=trades,
        quotes=quotes,
        consolidated_nbbo=consolidated_nbbo,
        aggressor_classification=aggressor_classification,
        corporate_actions=corporate_actions,
        halts=halts,
        historical_coverage=historical_coverage,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        source_identity=source_identity,
        method=method,
        limitations=limitations,
    )


def _identity_payload(receipt: ProviderCapabilityReceipt) -> dict[str, object]:
    return {
        name: value
        for name, value in receipt.__dict__.items()
        if name != "capability_receipt_id"
    }


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be blank")


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?:api[_-]?key|secret|access[_-]?token|token|password|authorization)"
    r"\s*[:=]\s*\S+"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+\S+")
_CREDENTIAL_URL = re.compile(
    r"(?i)https?://(?:[^/@\s:]+:[^/@\s]+@|[^\s]*(?:api[_-]?key|token|secret)=[^\s&]+)"
)
_PRIVATE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/Users/|/home/)")
_PRIVATE_HOST = re.compile(
    r"(?i)(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)"
)


def _require_sanitized(value: str) -> None:
    _require_text(value, "limitation")
    if _contains_private_value(value):
        raise ValueError("provider capability limitation contains a private value")


def _require_sanitized_identity(value: str, field_name: str) -> None:
    if _contains_private_value(value):
        raise ValueError(f"{field_name} contains a private or secret value")


def _contains_private_value(value: str) -> bool:
    return any(
        pattern.search(value)
        for pattern in (
            _SECRET_ASSIGNMENT,
            _BEARER_VALUE,
            _CREDENTIAL_URL,
            _PRIVATE_PATH,
            _PRIVATE_HOST,
        )
    )


def _is_iex_like(provider: str, feed: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", f"{provider} {feed}".lower())
    return "iex" in normalized.split()


__all__ = [
    "CapabilityState",
    "ProviderCapabilityReceipt",
    "build_provider_capability_receipt",
]
