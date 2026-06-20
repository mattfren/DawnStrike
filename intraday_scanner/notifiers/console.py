"""Console notifier for offline tests and local research runs."""

from __future__ import annotations

from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent


class ConsoleNotifier(BaseNotifier):
    channel = "console"

    def send(self, event: NotificationEvent) -> None:
        ticker = f" [{event.ticker}]" if event.ticker else ""
        print(f"[{self.channel}]{ticker} {event.title}: {event.body}")
