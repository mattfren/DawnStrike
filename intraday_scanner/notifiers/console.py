"""Console notifier for offline tests and local research runs."""

from __future__ import annotations

import hashlib
import sys
from typing import Any

from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent


class ConsoleNotifier(BaseNotifier):
    channel = "console"

    def send(self, event: NotificationEvent) -> dict[str, Any]:
        ticker = f" [{event.ticker}]" if event.ticker else ""
        transmitted = f"[{self.channel}]{ticker} {event.title}: {event.body}"
        _safe_print(transmitted)
        encoded = transmitted.encode("utf-8")
        return {
            "transmitted_text": transmitted,
            "transmitted_byte_count": len(encoded),
            "transmitted_bytes_sha256": hashlib.sha256(encoded).hexdigest(),
            "transport": "console",
        }


def _safe_print(message: str) -> None:
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        safe = message.encode(encoding, errors="backslashreplace").decode(encoding)
        print(safe)
