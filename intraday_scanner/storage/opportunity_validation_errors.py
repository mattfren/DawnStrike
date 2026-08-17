"""Typed errors for durable validation evidence."""

from intraday_scanner.errors import StorageError


class OpportunityValidationStoreError(StorageError):
    pass


class OpportunityValidationConflictError(OpportunityValidationStoreError):
    pass


class OpportunityValidationIntegrityError(OpportunityValidationStoreError):
    pass


class OpportunityValidationReadOnlyError(OpportunityValidationStoreError):
    pass


__all__ = [
    "OpportunityValidationConflictError",
    "OpportunityValidationIntegrityError",
    "OpportunityValidationReadOnlyError",
    "OpportunityValidationStoreError",
]
