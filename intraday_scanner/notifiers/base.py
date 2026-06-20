"""Notification abstractions for research alerts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NotificationEvent:
    event_key: str
    title: str
    body: str
    channel_hint: str
    ticker: str | None = None
    payload: dict[str, Any] | None = None


class BaseNotifier(ABC):
    channel: str

    @abstractmethod
    def send(self, event: NotificationEvent) -> None:
        """Send one notification event or raise NotificationError."""
