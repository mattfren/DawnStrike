from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

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
    expected = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    digest = module._write_once_json(path, payload)
    assert digest == hashlib.sha256(expected).hexdigest()
    assert module._write_once_json(path, payload) == digest
    assert json.loads(path.read_text(encoding="utf-8"))["market_date"] == "2026-08-31"

    with pytest.raises(RuntimeError, match="identity conflicts"):
        module._write_once_json(path, {**payload, "market_date": "2026-09-01"})


def test_runner_child_uses_isolated_exact_interpreter(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "_approved_child_python", lambda: Path(sys.executable))
    argv = [
        "run_daily_intraday_capture.py",
        "--candidate-sha",
        "a" * 40,
        "--repo-root",
        str(tmp_path),
        "--db-path",
        str(tmp_path / "capture.sqlite"),
        "--evidence-root",
        str(tmp_path / "evidence"),
        "--run-root",
        str(tmp_path / "runs"),
        "--output-root",
        str(tmp_path / "output"),
        "--session-root",
        str(tmp_path / "sessions"),
        "--symbols-manifest",
        str(tmp_path / "symbols.json"),
        "--symbols-manifest-sha256",
        "b" * 64,
        "--entitlement-receipt",
        str(tmp_path / "entitlement.json"),
        "--entitlement-receipt-sha256",
        "c" * 64,
        "--source-config",
        str(tmp_path / "sources.yaml"),
        "--source-config-sha256",
        "d" * 64,
        "--env-file",
        str(tmp_path / "runtime.env"),
        "--market-date",
        "2026-08-31",
        "--max-pages",
        "100",
        "--retries",
        "3",
        "--execute",
    ]
    monkeypatch.setattr(sys, "argv", argv)

    assert module.main() == 0
    command = captured["command"]
    assert command[:6] == [
        str(Path(sys.executable).resolve(strict=True)),
        "-I",
        "-B",
        "-S",
        "-u",
        "-c",
    ]
    assert command[6] == module._BOOTSTRAP_PRELOADER
    assert Path(command[7]).resolve() == Path("scripts/dawnstrike_python_bootstrap.py").resolve()
    assert command[8] == hashlib.sha256(Path(command[7]).read_bytes()).hexdigest()
    assert command[9:14] == [
        "--release-root",
        str(Path.cwd().resolve()),
        "--expected-sha",
        "a" * 40,
        "--script",
    ]
    assert Path(command[14]).resolve() == Path("scripts/capture_intraday_operations.py").resolve()
    assert command[15] == "--"
    session_index = command.index("--expected-session")
    assert command[session_index + 2] == "--expected-session-sha256"
    assert (
        command[session_index + 3]
        == hashlib.sha256(Path(command[session_index + 1]).read_bytes()).hexdigest()
    )
    assert captured["kwargs"]["cwd"] == tmp_path
