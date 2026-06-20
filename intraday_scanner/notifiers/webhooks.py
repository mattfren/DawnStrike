"""Webhook-based notification adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import NotificationError
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent


class DiscordWebhookNotifier(BaseNotifier):
    channel = "discord"

    def __init__(self, config: ScannerConfig):
        self.webhook_url = config.discord_webhook_url
        self.timeout_seconds = config.request_timeout_seconds

    def send(self, event: NotificationEvent) -> None:
        if not self.webhook_url:
            raise NotificationError(
                "Discord webhook notifier requires INTRADAY_DISCORD_WEBHOOK_URL"
            )
        _post_json(
            self.webhook_url,
            {"content": f"**{event.title}**\n{event.body}"},
            timeout_seconds=self.timeout_seconds,
        )


class TelegramNotifier(BaseNotifier):
    channel = "telegram"

    def __init__(self, config: ScannerConfig):
        self.bot_token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self.timeout_seconds = config.request_timeout_seconds

    def send(self, event: NotificationEvent) -> None:
        if not self.bot_token or not self.chat_id:
            raise NotificationError(
                "Telegram notifier requires INTRADAY_TELEGRAM_BOT_TOKEN and "
                "INTRADAY_TELEGRAM_CHAT_ID"
            )
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        _post_json(
            url,
            {"chat_id": self.chat_id, "text": f"{event.title}\n{event.body}"},
            timeout_seconds=self.timeout_seconds,
        )


def _post_json(url: str, payload: dict[str, object], *, timeout_seconds: float) -> None:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            if response.status >= 400:
                raise NotificationError(f"Webhook request failed with HTTP {response.status}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NotificationError(f"Webhook notification failed: {exc}") from exc
