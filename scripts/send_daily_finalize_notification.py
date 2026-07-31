"""Send an optional, idempotent daily-finalize Telegram digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib import parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--deployment-url", default="")
    args = parser.parse_args()
    payload = json.loads(Path(args.result_file).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("daily finalize result must be an object")
    deployment_url = args.deployment_url or str(payload.get("deployment_url") or "")
    message = _message(payload, deployment_url)
    event_key = "dawnstrike:daily-finalize:telegram:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    store = SQLiteScanStore(Path(args.db_path))
    existing = store.load_notification(event_key)
    if existing is not None and (
        existing.get("sent") is True
        or existing.get("channel") == "telegram:sent"
    ):
        print(
            json.dumps(
                {
                    "deduplicated": True,
                    "event_key": event_key,
                    "status": "already_sent",
                    "sent": False,
                },
                sort_keys=True,
            )
        )
        return 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("INTRADAY_TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("INTRADAY_TELEGRAM_CHAT_ID")
    sent = False
    status = "not_configured"
    if token and chat_id:
        body = parse.urlencode(
            {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
        ).encode()
        with request.urlopen(
            request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body),
            timeout=20,
        ) as response:
            response.read()
        sent = True
        status = "sent"
    store.record_notification_delivery(
        event_key=event_key,
        channel=f"telegram:{status}",
        payload={
            **payload,
            "deployment_url": deployment_url,
            "sent": sent,
        },
    )
    print(
        json.dumps(
            {
                "deduplicated": False,
                "event_key": event_key,
                "status": status,
                "sent": sent,
            },
            sort_keys=True,
        )
    )
    return 0


def _message(payload: dict[str, object], deployment_url: str) -> str:
    status = str(payload.get("status") or "FAILED").upper()
    market_date = str(payload.get("market_date") or "not reported")
    readiness = payload.get("readiness")
    readiness_status = readiness.get("status") if isinstance(readiness, dict) else "not reported"
    next_action = ""
    if isinstance(readiness, dict):
        next_action = str(readiness.get("reason") or "")
    daily_run = (
        readiness.get("daily_run")
        if isinstance(readiness, dict)
        and isinstance(readiness.get("daily_run"), dict)
        else {}
    )
    run = (
        daily_run.get("run")
        if isinstance(daily_run.get("run"), dict)
        else {}
    )
    failed_stage = str(run.get("failed_stage") or "")
    failure_reason = str(run.get("failure_reason") or "")
    failure_line = (
        f"\nFailed stage: {failed_stage} · {failure_reason or 'reason not reported'}"
        if failed_stage
        else ""
    )
    suffix = f"\n{deployment_url}" if deployment_url else ""
    return (
        f"Dawnstrike daily finalize · {market_date}\n"
        f"Result: {status} · readiness: {readiness_status}\n"
        f"Next action: {next_action or 'Review the stage manifest.'}"
        f"{failure_line}{suffix}"
    )[:3900]


if __name__ == "__main__":
    raise SystemExit(main())
