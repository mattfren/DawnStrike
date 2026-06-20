"""Notification provider compatibility package."""

from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent
from intraday_scanner.notifiers.console import ConsoleNotifier
from intraday_scanner.notifiers.email import EmailNotifier
from intraday_scanner.notifiers.webhooks import DiscordWebhookNotifier, TelegramNotifier

__all__ = [
    "BaseNotifier",
    "ConsoleNotifier",
    "DiscordWebhookNotifier",
    "EmailNotifier",
    "NotificationEvent",
    "TelegramNotifier",
]
