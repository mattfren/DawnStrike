"""SMTP email notifier."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import NotificationError
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent


class EmailNotifier(BaseNotifier):
    channel = "email"

    def __init__(self, config: ScannerConfig):
        self.host = config.email_smtp_host
        self.port = config.email_smtp_port
        self.username = config.email_username
        self.password = config.email_password
        self.sender = config.email_from
        self.recipient = config.email_to
        self.timeout_seconds = config.request_timeout_seconds

    def send(self, event: NotificationEvent) -> None:
        missing = [
            name
            for name, value in {
                "INTRADAY_EMAIL_SMTP_HOST": self.host,
                "INTRADAY_EMAIL_FROM": self.sender,
                "INTRADAY_EMAIL_TO": self.recipient,
            }.items()
            if not value
        ]
        if missing:
            raise NotificationError(
                "Email notifier missing required setting(s): " + ", ".join(missing)
            )

        message = EmailMessage()
        message["Subject"] = event.title
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(event.body)

        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout_seconds) as smtp:
                smtp.starttls()
                if self.username and self.password:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotificationError(f"Email notification failed: {exc}") from exc
