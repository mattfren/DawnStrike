"""Typed errors for discovery-metric persistence."""

from intraday_scanner.errors import StorageError


class OpportunityMetricStoreError(StorageError):
    pass


class OpportunityMetricConflictError(OpportunityMetricStoreError):
    pass


class OpportunityMetricIntegrityError(OpportunityMetricStoreError):
    pass


class OpportunityMetricReadOnlyError(OpportunityMetricStoreError):
    pass


class OpportunityMetricStaleParentError(OpportunityMetricIntegrityError):
    pass
