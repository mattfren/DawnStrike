"""Optional Windows local notification adapter."""

from __future__ import annotations

import os
import subprocess

from intraday_scanner.errors import NotificationError
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent


class WindowsLocalNotifier(BaseNotifier):
    channel = "windows"

    def send(self, event: NotificationEvent) -> None:
        if os.name != "nt":
            raise NotificationError("Windows local notifications require Windows.")
        script = (
            "if (Get-Command New-BurntToastNotification -ErrorAction SilentlyContinue) { "
            "New-BurntToastNotification -Text $env:DS_TOAST_TITLE,$env:DS_TOAST_BODY "
            "} else { exit 42 }"
        )
        env = {
            **os.environ,
            "DS_TOAST_TITLE": event.title,
            "DS_TOAST_BODY": event.body,
        }
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 42:
            raise NotificationError(
                "Windows local notifications require the optional BurntToast PowerShell module."
            )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise NotificationError(f"Windows local notification failed: {detail}")
