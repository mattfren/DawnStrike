"""Console notifier for offline tests and local research runs."""

from __future__ import annotations

import sys

from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent


class ConsoleNotifier(BaseNotifier):
    channel = "console"

    def send(self, event: NotificationEvent) -> None:
        ticker = f" [{event.ticker}]" if event.ticker else ""
        _safe_print(f"[{self.channel}]{ticker} {event.title}: {event.body}")


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe = message.encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe)
