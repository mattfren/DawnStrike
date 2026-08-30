from __future__ import annotations

import importlib.util
import json
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
    assert early["end_utc"] == "2026-11-27T18:00:00+00:00"
    assert early["calendar_status"] == "early_close"


def test_daily_session_skips_closed_market_date() -> None:
    assert _module().build_expected_session(date(2026, 9, 7)) is None


def test_daily_session_receipt_is_write_once(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "session.json"
    payload = module.build_expected_session(date(2026, 8, 31))
    module._write_once_json(path, payload)
    module._write_once_json(path, payload)
    assert json.loads(path.read_text(encoding="utf-8"))["market_date"] == "2026-08-31"

    with pytest.raises(RuntimeError, match="identity conflicts"):
        module._write_once_json(path, {**payload, "market_date": "2026-09-01"})
