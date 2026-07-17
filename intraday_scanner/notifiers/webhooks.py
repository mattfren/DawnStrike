"""Webhook-based notification adapters."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import NotificationError
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent
from intraday_scanner.notifiers.telegram_formatter import format_telegram_event


class DiscordWebhookNotifier(BaseNotifier):
    channel = "discord"

    def __init__(self, config: ScannerConfig):
        self.webhook_url = config.discord_webhook_url
        self.timeout_seconds = config.request_timeout_seconds

    def send(self, event: NotificationEvent) -> dict[str, Any]:
        if not self.webhook_url:
            raise NotificationError(
                "Discord webhook notifier requires INTRADAY_DISCORD_WEBHOOK_URL"
            )
        transmitted = f"**{event.title}**\n{event.body}"
        response = _post_json(
            self.webhook_url,
            {"content": transmitted},
            timeout_seconds=self.timeout_seconds,
        )
        return _transport_receipt(
            transmitted=transmitted,
            transport="discord",
            response=response,
        )


class TelegramNotifier(BaseNotifier):
    channel = "telegram"

    def __init__(self, config: ScannerConfig):
        self.config = config
        self.bot_token = config.telegram_bot_token
        self.chat_id = config.telegram_chat_id
        self.timeout_seconds = config.request_timeout_seconds

    def send(self, event: NotificationEvent) -> dict[str, Any]:
        if not self.bot_token or not self.chat_id:
            raise NotificationError(
                "Telegram notifier requires INTRADAY_TELEGRAM_BOT_TOKEN and "
                "INTRADAY_TELEGRAM_CHAT_ID"
            )
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        if self.config.telegram_message_style == "legacy":
            text = f"{event.title}\n{event.body}"
        else:
            text = format_telegram_event(
                event,
                max_morning_chars=self.config.telegram_max_morning_chars,
                max_alert_chars=self.config.telegram_max_alert_chars,
                max_summary_chars=self.config.telegram_max_summary_chars,
                include_debug_fields=self.config.telegram_include_debug_fields,
            )
        response = _post_json(
            url,
            {"chat_id": self.chat_id, "text": text},
            timeout_seconds=self.timeout_seconds,
        )
        result = response.get("result") if isinstance(response, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return {
            **_transport_receipt(
                transmitted=text,
                transport="telegram",
                response=response,
            ),
            "message_id": message_id,
            "telegram_response": response,
        }


def _post_json(
    url: str,
    payload: dict[str, object],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
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
            raw = response.read()
            if not raw:
                return {"http_status": int(response.status)}
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NotificationError(
                    "Webhook response was not valid UTF-8 JSON"
                ) from exc
            if not isinstance(decoded, dict):
                raise NotificationError("Webhook response JSON must be an object")
            return {"http_status": int(response.status), **decoded}
    except (urllib.error.URLError, TimeoutError) as exc:
        raise NotificationError(f"Webhook notification failed: {exc}") from exc


def _transport_receipt(
    *,
    transmitted: str,
    transport: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    encoded = transmitted.encode("utf-8")
    return {
        "transport": transport,
        "transmitted_text": transmitted,
        "transmitted_byte_count": len(encoded),
        "transmitted_bytes_sha256": hashlib.sha256(encoded).hexdigest(),
        "http_status": response.get("http_status"),
        "provider_response": response,
    }
