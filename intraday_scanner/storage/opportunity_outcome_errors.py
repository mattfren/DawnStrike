"""Internal typed errors shared by outcome persistence modules."""

from intraday_scanner.errors import StorageError


class OpportunityOutcomeStoreError(StorageError):
    """Base error raised by the downstream outcome store."""


class OpportunityOutcomeConflictError(OpportunityOutcomeStoreError):
    """An append conflicts with immutable outcome history."""


class OpportunityOutcomeIntegrityError(OpportunityOutcomeStoreError):
    """Stored outcome data or its schema fails exact verification."""


class OpportunityOutcomeReadOnlyError(OpportunityOutcomeStoreError):
    """A write was attempted through a read-only outcome store."""
