"""Notification adapters and dispatch helpers."""

from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent
from intraday_scanner.notifiers.console import ConsoleNotifier
from intraday_scanner.notifiers.service import (
    audit_summary_events,
    build_notifiers,
    dispatch_events,
    scan_events_from_payload,
)
from intraday_scanner.notifiers.windows import WindowsLocalNotifier

__all__ = [
    "BaseNotifier",
    "ConsoleNotifier",
    "NotificationEvent",
    "WindowsLocalNotifier",
    "audit_summary_events",
    "build_notifiers",
    "dispatch_events",
    "scan_events_from_payload",
]
