from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Executable PowerShell artifact contracts require Windows PowerShell.",
)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "alpha_cycle_artifact.ps1"
MARKET_DATE = "2026-08-04"


def _valid_payload(
    signal_count: Any = 0, research_symbols: list[str] | None = None
) -> dict[str, Any]:
    research_symbols = ["NOVA"] if research_symbols is None else research_symbols
    return {
        "status": "no_trade" if signal_count == 0 else "complete",
        "scan_id": "scan-current-attempt",
        "signal_count": signal_count,
        "run_contract": {
            "schema_version": "alphaops.run_contract.v1",
            "producer": "alphaops",
            "producer_run_id": "scan-current-attempt",
            "market_date": MARKET_DATE,
            "source_status": "success",
            "signal_count": signal_count,
            "research_candidate_count": len(research_symbols),
            "research_symbols": research_symbols,
        },
    }


def _validate(
    tmp_path: Path,
    payload: dict[str, Any] | str | None,
    *,
    started_at: datetime | None = None,
) -> subprocess.CompletedProcess[str]:
    artifact = tmp_path / "alpha_cycle.json"
    receipt = tmp_path / "receipt.json"
    if payload is not None:
        artifact.write_text(
            payload if isinstance(payload, str) else json.dumps(payload),
            encoding="utf-8",
        )
    receipt.write_text(
        json.dumps(
            {
                "started_at": (
                    started_at or datetime.now(timezone.utc) - timedelta(seconds=30)
                ).isoformat()
            }
        ),
        encoding="utf-8",
    )
    command = (
        f". '{HELPER}'; "
        f"$receipt = Get-Content -LiteralPath '{receipt}' -Raw | ConvertFrom-Json; "
        "try { "
        f"$result = Test-DawnstrikeAlphaCycleArtifact -ArtifactPath '{artifact}' "
        f"-ProcessReceipt $receipt -MarketDate '{MARKET_DATE}'; "
        "$result | ConvertTo-Json -Compress; exit 0 "
        "} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("signal_count", [None, -1, 0.5])
def test_artifact_rejects_noncanonical_signal_counts(
    tmp_path: Path, signal_count: Any
) -> None:
    result = _validate(tmp_path, _valid_payload(signal_count))

    assert result.returncode == 1
    assert "nonnegative integer" in result.stderr


def test_artifact_rejects_missing_signal_count(tmp_path: Path) -> None:
    payload = _valid_payload()
    del payload["signal_count"]

    result = _validate(tmp_path, payload)

    assert result.returncode == 1
    assert "missing signal_count" in result.stderr


@pytest.mark.parametrize("payload", [None, "{not-json"])
def test_artifact_rejects_missing_or_malformed_json(
    tmp_path: Path, payload: str | None
) -> None:
    result = _validate(tmp_path, payload)

    assert result.returncode == 1
    assert result.stderr


def test_artifact_rejects_stale_same_day_file(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        _valid_payload(),
        started_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )

    assert result.returncode == 1
    assert "predates the current process attempt" in result.stderr


@pytest.mark.parametrize("signal_count", [0, 3])
def test_artifact_accepts_current_zero_and_positive_counts(
    tmp_path: Path, signal_count: int
) -> None:
    result = _validate(tmp_path, _valid_payload(signal_count))

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["signal_count"] == signal_count
    assert json.loads(result.stdout)["research_symbols"] == ["NOVA"]


def test_artifact_rejects_inconsistent_research_candidate_universe(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["run_contract"]["research_candidate_count"] = 2

    result = _validate(tmp_path, payload)

    assert result.returncode == 1
    assert "research candidate count is inconsistent" in result.stderr


def test_artifact_can_validate_a_current_session_without_process_receipt(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "alpha_cycle.json"
    artifact.write_text(json.dumps(_valid_payload(0)), encoding="utf-8")
    command = (
        f". '{HELPER}'; "
        f"$result = Test-DawnstrikeAlphaCycleArtifact -ArtifactPath '{artifact}' "
        f"-MarketDate '{MARKET_DATE}'; "
        "$result | ConvertTo-Json -Compress"
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["signal_count"] == 0
    assert result["process_started_at_utc"] is None


def test_artifact_rejects_identity_and_source_failures(tmp_path: Path) -> None:
    payload = _valid_payload()
    payload["run_contract"]["producer_run_id"] = "different-scan"
    payload["run_contract"]["source_status"] = "source_failed"

    result = _validate(tmp_path, payload)

    assert result.returncode == 1
    assert "identity" in result.stderr


def test_optional_scenario_failure_keeps_core_complete_but_task_nonzero() -> None:
    command = (
        f". '{HELPER}'; "
        "$result = Resolve-DawnstrikeMorningOutcome -CoreExitCode 0 "
        "-ScenarioExitCode 1 -RecordStageFailed $false; "
        "$result | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["core_status"] == "COMPLETE"
    assert result["core_exit_code"] == 0
    assert result["final_exit_code"] == 1
