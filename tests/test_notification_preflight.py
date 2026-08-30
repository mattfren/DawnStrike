from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from intraday_scanner.errors import NotificationError
from intraday_scanner.services import alpha_cycle_service

TELEGRAM_ENV_ALIASES = (
    "TELEGRAM_BOT_TOKEN",
    "INTRADAY_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "INTRADAY_TELEGRAM_CHAT_ID",
)


def _clear_telegram_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in TELEGRAM_ENV_ALIASES:
        monkeypatch.delenv(name, raising=False)


def _assert_preflight_receipt(path: Path, *, stage: str) -> None:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt == {
        "broker_execution_enabled": False,
        "channel": "telegram",
        "error_code": "notification_credentials_missing",
        "market_date": "2026-08-27",
        "message": receipt["message"],
        "missing_fields": ["bot_token", "chat_id"],
        "recorded_at": receipt["recorded_at"],
        "research_only": True,
        "schema_version": "dawnstrike.notification_preflight.v1",
        "stage": stage,
        "status": "FAILED",
    }
    assert "NOTIFICATION_PREFLIGHT_FAILED" in receipt["message"]
    assert "bot_token" in receipt["message"]
    assert "chat_id" in receipt["message"]
    assert "SECRET" not in receipt["message"]


def test_alpha_morning_missing_both_telegram_alias_pairs_fails_before_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_telegram_aliases(monkeypatch)
    collected = False

    def unexpected_collection(**_kwargs: object) -> dict[str, object]:
        nonlocal collected
        collected = True
        raise AssertionError("notification preflight must precede collection")

    monkeypatch.setattr(alpha_cycle_service, "web_auto_collect", unexpected_collection)

    with pytest.raises(NotificationError, match="NOTIFICATION_PREFLIGHT_FAILED"):
        alpha_cycle_service.alpha_cycle(
            config_path="missing.yaml",
            db_path=tmp_path / "shadow.sqlite",
            out_dir=tmp_path / "morning",
            notify="telegram",
            as_of=datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc),
            market_date="2026-08-27",
            code_sha="a" * 40,
        )

    assert collected is False
    assert not (tmp_path / "shadow.sqlite").exists()
    _assert_preflight_receipt(
        tmp_path / "morning" / "notification-preflight-alpha_cycle-2026-08-27.json",
        stage="alpha_cycle",
    )


def test_alpha_monitor_missing_both_telegram_alias_pairs_fails_before_store_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_telegram_aliases(monkeypatch)

    with pytest.raises(NotificationError, match="NOTIFICATION_PREFLIGHT_FAILED"):
        alpha_cycle_service.alpha_monitor(
            db_path=tmp_path / "shadow.sqlite",
            notify="telegram",
            as_of=datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc),
        )

    assert not (tmp_path / "shadow.sqlite").exists()
    _assert_preflight_receipt(
        tmp_path / "receipts" / "notification-preflight-alpha_monitor-2026-08-27.json",
        stage="alpha_monitor",
    )


def test_scheduled_wrappers_propagate_notification_preflight_stage_code() -> None:
    morning = Path("scripts/run_alphaops_morning.ps1").read_text(encoding="utf-8")
    monitor = Path("scripts/run_alphaops_monitor.ps1").read_text(encoding="utf-8")
    helper = Path("scripts/alpha_cycle_artifact.ps1").read_text(encoding="utf-8")

    assert "notification-preflight-$Stage-$MarketDate.json" in helper
    assert "notification_credentials_missing" in helper
    assert "Resolve-DawnstrikeNotificationFailureCode" in morning
    assert "Resolve-DawnstrikeNotificationFailureCode" in monitor
    assert "-ProcessReceipt $alphaCycle" in morning
    assert "-ProcessReceipt $monitor" in monitor
    assert "-FallbackErrorCode \"alpha_cycle_failed\"" in morning
    assert "-FallbackErrorCode \"alpha_monitor_failed\"" in monitor


def test_stale_same_date_preflight_receipt_cannot_relabel_current_child_failure(
    tmp_path: Path,
) -> None:
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    receipt = {
        "schema_version": "dawnstrike.notification_preflight.v1",
        "status": "FAILED",
        "stage": "alpha_monitor",
        "error_code": "notification_credentials_missing",
        "channel": "telegram",
        "market_date": "2026-08-27",
        "missing_fields": ["bot_token", "chat_id"],
        "message": "nonsecret stale diagnostic",
        "research_only": True,
        "broker_execution_enabled": False,
        "recorded_at": "2026-08-27T14:00:00+00:00",
    }
    (receipt_root / "notification-preflight-alpha_monitor-2026-08-27.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    helper = (Path("scripts") / "alpha_cycle_artifact.ps1").resolve()
    ps = str(helper).replace("'", "''")
    root = str(receipt_root).replace("'", "''")
    command = (
        f". '{ps}'; "
        "$p = [pscustomobject]@{started_at='2026-08-27T15:00:00+00:00'; "
        "completed_at='2026-08-27T15:01:00+00:00'}; "
        f"Resolve-DawnstrikeNotificationFailureCode -ReceiptRoot '{root}' "
        "-Stage 'alpha_monitor' -MarketDate '2026-08-27' "
        "-FallbackErrorCode 'alpha_monitor_failed' -ProcessReceipt $p"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "alpha_monitor_failed"
