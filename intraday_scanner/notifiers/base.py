"""Notification abstractions for research alerts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
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
    def send(self, event: NotificationEvent) -> Mapping[str, Any] | None:
        """Send one notification event and optionally return transport evidence.

        Network-backed adapters should return the exact transmitted text/bytes
        digest plus any provider acknowledgement identifiers.  ``None`` remains
        supported for legacy/local adapters, but callers that require delivery
        proof must treat it as unverified rather than inventing a receipt.
        """
