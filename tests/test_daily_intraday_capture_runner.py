from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest


def _module():
    path = Path("scripts/run_daily_intraday_capture.py").resolve()
    spec = importlib.util.spec_from_file_location("run_daily_intraday_capture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_daily_session_uses_regular_and_early_close_calendar() -> None:
    module = _module()
    regular = module.build_expected_session(date(2026, 8, 31))
    early = module.build_expected_session(date(2026, 11, 27))

    assert regular["start_utc"] == "2026-08-31T13:30:00+00:00"
    assert regular["end_utc"] == "2026-08-31T20:00:00+00:00"
    assert regular["exchange_session_id"] == "XNYS:2026-08-31:regular"
    assert regular["capture_end_utc"] == "2026-08-31T14:00:00+00:00"
    assert early["end_utc"] == "2026-11-27T18:00:00+00:00"
    assert early["capture_end_utc"] == "2026-11-27T15:00:00+00:00"
    assert early["calendar_status"] == "early_close"


def test_daily_session_skips_closed_market_date() -> None:
    assert _module().build_expected_session(date(2026, 9, 7)) is None


def test_runner_accepts_registered_execute_action_without_provider_call(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_daily_intraday_capture.py",
            "--candidate-sha",
            "a" * 40,
            "--repo-root",
            str(tmp_path),
            "--db-path",
            r"C:\r\dawnstrike-forward-db\staging.sqlite",
            "--evidence-root",
            r"C:\r\dawnstrike-forward-evidence",
            "--run-root",
            r"C:\r\dawnstrike-forward-runs",
            "--output-root",
            r"C:\r\dawnstrike-forward-output",
            "--session-root",
            r"C:\r\dawnstrike-forward-sessions",
            "--symbols-manifest",
            r"C:\r\dawnstrike-capture-config-20260830\symbols.json",
            "--symbols-manifest-sha256",
            "b" * 64,
            "--entitlement-receipt",
            r"C:\r\dawnstrike-capture-config-20260830\entitlement.json",
            "--entitlement-receipt-sha256",
            "c" * 64,
            "--source-config",
            r"C:\r\dawnstrike-capture-config-20260830\web_sources.yaml",
            "--source-config-sha256",
            "d" * 64,
            "--env-file",
            str(tmp_path / "secrets" / "runtime.env"),
            "--market-date",
            "2026-09-07",
            "--max-pages",
            "100",
            "--retries",
            "3",
            "--execute",
        ],
    )

    assert module.main() == 0


def test_daily_session_receipt_is_write_once(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "session.json"
    payload = module.build_expected_session(date(2026, 8, 31))
    module._write_once_json(path, payload)
    module._write_once_json(path, payload)
    assert json.loads(path.read_text(encoding="utf-8"))["market_date"] == "2026-08-31"

    with pytest.raises(RuntimeError, match="identity conflicts"):
        module._write_once_json(path, {**payload, "market_date": "2026-09-01"})
