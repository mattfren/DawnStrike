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

from intraday_scanner.network_safety import open_allowlisted_url
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from scripts.public_artifact_inventory import assert_contained_no_reparse


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate daily finalize result field: {key}")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--expected-build-attempt-id", required=True)
    parser.add_argument("--deployment-url", default="")
    args = parser.parse_args()
    db_path = Path(os.path.abspath(args.db_path))
    state_root = db_path.parent
    result_path = Path(os.path.abspath(args.result_file))
    result_path.relative_to(state_root)
    assert_contained_no_reparse(state_root, result_path)
    before = result_path.stat()
    raw = result_path.read_text(encoding="utf-8")
    after = result_path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ValueError("daily finalize result changed while it was read")
    payload = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    if not isinstance(payload, dict):
        raise ValueError("daily finalize result must be an object")
    expected_attempt = args.expected_build_attempt_id.strip().lower()
    if (
        len(expected_attempt) != 32
        or any(character not in "0123456789abcdef" for character in expected_attempt)
        or payload.get("build_attempt_id") != expected_attempt
    ):
        raise ValueError("daily finalize result is not bound to this build attempt")
    if payload.get("status") != "COMPLETE":
        raise ValueError("daily finalize result is not COMPLETE")
    if payload.get("broker_execution_enabled") is not False:
        raise ValueError("daily finalize result does not preserve the no-broker boundary")
    deployment_url = args.deployment_url or str(payload.get("deployment_url") or "")
    message = _message(payload, deployment_url)
    event_key = (
        "dawnstrike:daily-finalize:telegram:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
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
    sent = False
    status = "not_configured"
    if token and chat_id:
        body = parse.urlencode(
            {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
        ).encode()
        with open_allowlisted_url(
            request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body),
            timeout=20,
            allowed_hosts=("api.telegram.org",),
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
        if isinstance(readiness, dict) and isinstance(readiness.get("daily_run"), dict)
        else {}
    )
    run = daily_run.get("run") if isinstance(daily_run.get("run"), dict) else {}
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
