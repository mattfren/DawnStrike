"""Durable storage adapters."""

from importlib import import_module

from intraday_scanner.storage.opportunity_store import (
    CANONICAL_OPPORTUNITY_ARTIFACT_FAMILIES,
    OPPORTUNITY_DATABASE_SCHEMA_VERSION,
    OpportunityArtifactFamily,
    OpportunityArtifactFamilyCount,
    OpportunityPersistenceConflictError,
    OpportunityPersistenceIntegrityError,
    OpportunityPersistenceReceipt,
    OpportunityStore,
    OpportunityStoreError,
    OpportunityStoreReadOnlyError,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

__all__ = [
    "CANONICAL_OPPORTUNITY_ARTIFACT_FAMILIES",
    "OPPORTUNITY_DATABASE_SCHEMA_VERSION",
    "OpportunityArtifactFamily",
    "OpportunityArtifactFamilyCount",
    "OpportunityPersistenceConflictError",
    "OpportunityPersistenceIntegrityError",
    "OpportunityPersistenceReceipt",
    "OpportunityStore",
    "OpportunityStoreError",
    "OpportunityStoreReadOnlyError",
    "OpportunityValidationConflictError",
    "OpportunityValidationIntegrityError",
    "OpportunityValidationReadOnlyError",
    "OpportunityValidationStore",
    "OpportunityValidationStoreError",
    "ValidationPersistenceReceipt",
    "ValidationPersistenceReplay",
    "ValidationPersistenceStatus",
    "SQLiteScanStore",
]

_VALIDATION_EXPORTS = frozenset(
    {
        "OpportunityValidationConflictError",
        "OpportunityValidationIntegrityError",
        "OpportunityValidationReadOnlyError",
        "OpportunityValidationStore",
        "OpportunityValidationStoreError",
        "ValidationPersistenceReceipt",
        "ValidationPersistenceReplay",
        "ValidationPersistenceStatus",
    }
)


def __getattr__(name: str):
    """Keep validation/robustness code out of real-time imports until requested."""

    if name in _VALIDATION_EXPORTS:
        module = import_module(
            "intraday_scanner.storage.opportunity_validation_store"
        )
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
