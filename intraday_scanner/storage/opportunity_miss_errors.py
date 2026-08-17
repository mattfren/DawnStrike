"""Typed errors for append-only missed-opportunity persistence."""

from intraday_scanner.errors import StorageError


class OpportunityMissStoreError(StorageError):
    """Base error raised by the missed-opportunity store."""


class OpportunityMissConflictError(OpportunityMissStoreError):
    """A requested append conflicts with immutable miss history."""


class OpportunityMissIntegrityError(OpportunityMissStoreError):
    """Stored miss data, schema, or parent evidence is invalid."""


class OpportunityMissReadOnlyError(OpportunityMissStoreError):
    """A write was attempted through a read-only miss store."""


class OpportunityMissStaleParentError(OpportunityMissIntegrityError):
    """A current replay is bound to an outcome head that has advanced."""
