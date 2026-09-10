"""Isolated execution research primitives.

The R6 module in this package is deliberately network-free.  It models a
fake broker and a canonical account ledger for authenticated lifecycle tests;
the production paper adapter remains outside this packet.
"""

from .r6_lifecycle import (
    AuthenticatedEntryIntent,
    CanonicalLedger,
    FakeBroker,
    FakeBrokerError,
    R6Lifecycle,
    R6RiskSettings,
    ReceiptAuthenticationError,
    authenticate_entry_intent,
)

__all__ = [
    "AuthenticatedEntryIntent",
    "CanonicalLedger",
    "FakeBroker",
    "FakeBrokerError",
    "R6Lifecycle",
    "R6RiskSettings",
    "ReceiptAuthenticationError",
    "authenticate_entry_intent",
]
