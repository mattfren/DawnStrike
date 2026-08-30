"""Project-specific exception types."""


class IntradayScannerError(Exception):
    """Base class for scanner failures."""


class ConfigError(IntradayScannerError):
    """Raised when configuration is invalid or incomplete."""


class DataProviderError(IntradayScannerError):
    """Raised when a market data provider cannot serve requested data."""


class SnapshotValidationError(IntradayScannerError):
    """Raised when snapshot input is malformed."""


class StorageError(IntradayScannerError):
    """Raised when durable storage cannot be read or written."""


class NotificationError(IntradayScannerError):
    """Raised when a notifier cannot send a requested alert."""


class NotificationConfigurationError(NotificationError):
    """Raised before durable work when a requested notifier is incomplete."""

    def __init__(self, message: str, *, missing_fields: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.missing_fields = missing_fields


class MarketCalendarCoverageError(IntradayScannerError):
    """Raised when a requested session falls outside the governed calendar."""
