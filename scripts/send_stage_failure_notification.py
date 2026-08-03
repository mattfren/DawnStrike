"""Send one idempotent Telegram alert for a terminal shared-DAG failure."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib import parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.services.daily_run_service import (
    latest_daily_run_snapshot,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--market-date", required=True)
    args = parser.parse_args()
    snapshot = latest_daily_run_snapshot(db_path=args.db_path)
    run = snapshot.get("run")
    run_payload = run if isinstance(run, dict) else {}
    failed_stage = str(run_payload.get("failed_stage") or "unknown_stage")
    reason = str(run_payload.get("failure_reason") or "No failure reason was recorded.")
    status = str(run_payload.get("status") or "DEGRADED")
    message = (
        f"Dawnstrike required stage failed · {args.market_date}\n"
        f"Stage: {failed_stage}\n"
        f"Status: {status}\n"
        f"Reason: {reason}\n"
        "Readiness remains degraded; missing truth was not converted to zero."
    )[:3900]
    event_key = (
        "dawnstrike:stage-failure:telegram:"
        + hashlib.sha256(f"{args.market_date}:{failed_stage}:{reason}".encode()).hexdigest()
    )
    store = SQLiteScanStore(Path(args.db_path))
    existing = store.load_notification(event_key)
    if existing is not None and (
        existing.get("sent") is True or existing.get("channel") == "telegram:sent"
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
    delivery_status = "not_configured"
    sent = False
    if token and chat_id:
        body = parse.urlencode(
            {
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            }
        ).encode()
        with open_allowlisted_url(
            request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=body,
            ),
            timeout=20,
            allowed_hosts=("api.telegram.org",),
        ) as response:
            response.read()
        delivery_status = "sent"
        sent = True
    store.record_notification_delivery(
        event_key=event_key,
        channel=f"telegram:{delivery_status}",
        payload={
            "market_date": args.market_date,
            "failed_stage": failed_stage,
            "failure_reason": reason,
            "status": status,
            "sent": sent,
            "research_only": True,
        },
        run_id=str(run_payload.get("run_id") or "") or None,
    )
    print(
        json.dumps(
            {
                "event_key": event_key,
                "status": delivery_status,
                "sent": sent,
                "deduplicated": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
