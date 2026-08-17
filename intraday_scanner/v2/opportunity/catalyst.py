"""Injected point-in-time catalyst evidence for the deterministic opportunity core."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CatalystEvidence:
    symbol: str
    state: str
    observed_at: datetime
    available_at: datetime
    source_identity: str
    payload_hash_sha256: str

    def __post_init__(self) -> None:
        if not self.symbol.strip() or self.symbol != self.symbol.upper():
            raise ValueError("catalyst symbol must be normalized uppercase")
        if not self.state.strip() or not self.source_identity.strip():
            raise ValueError("catalyst state and source identity are required")
        for value, name in (
            (self.observed_at, "observed_at"),
            (self.available_at, "available_at"),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"catalyst {name} must be timezone-aware")
        if self.observed_at > self.available_at:
            raise ValueError("catalyst cannot be available before it was observed")
        if len(self.payload_hash_sha256) != 64:
            raise ValueError("catalyst payload hash must be SHA-256")
        int(self.payload_hash_sha256, 16)

    @classmethod
    def from_payload(
        cls,
        *,
        symbol: str,
        state: str,
        observed_at: datetime,
        available_at: datetime,
        source_identity: str,
        payload: bytes,
    ) -> CatalystEvidence:
        return cls(
            symbol=symbol.strip().upper(),
            state=state.strip(),
            observed_at=observed_at,
            available_at=available_at,
            source_identity=source_identity.strip(),
            payload_hash_sha256=hashlib.sha256(payload).hexdigest(),
        )


@dataclass(frozen=True)
class InjectedCatalystAdapter:
    evidence_by_symbol: Mapping[str, CatalystEvidence]

    def evidence_at(self, symbol: str, *, decision_at: datetime) -> CatalystEvidence | None:
        evidence = self.evidence_by_symbol.get(symbol.strip().upper())
        if evidence is None:
            return None
        if evidence.available_at > decision_at or evidence.observed_at > decision_at:
            raise ValueError("post-cutoff catalyst evidence is not causal")
        return evidence


__all__ = ["CatalystEvidence", "InjectedCatalystAdapter"]
