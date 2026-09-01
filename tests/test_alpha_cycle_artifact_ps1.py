from __future__ import annotations

import json
import os
import shutil
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
PROCESS_RUNNER = ROOT / "scripts" / "dawnstrike_process_runner.ps1"
MARKET_DATE = "2026-08-04"
RELEASE_SHA = "a" * 40


def _initialize_git_runtime(runtime: Path) -> str:
    runtime.mkdir()
    subprocess.run(["git", "init", str(runtime)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(runtime), "config", "user.email", "test@example.test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(runtime), "config", "user.name", "Dawnstrike Test"],
        check=True,
        capture_output=True,
    )
    shutil.copytree(
        ROOT / "scripts",
        runtime / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(ROOT / ".gitattributes", runtime / ".gitattributes")
    (runtime / "app.py").write_text("print('clean')\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(runtime), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(runtime), "commit", "-m", "fixture"],
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "-C", str(runtime), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _resolve_release_sha(runtime: Path, log_root: Path) -> subprocess.CompletedProcess[str]:
    expected_sha = subprocess.run(
        ["git", "-C", str(runtime), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    command = (
        f". '{PROCESS_RUNNER}'; "
        "try { "
        f"$sha = Resolve-DawnstrikeReleaseSha -RuntimeRoot '{runtime}' "
        f"-LogRoot '{log_root}' -ExpectedSha '{expected_sha}'; "
        '[Console]::Out.WriteLine("RESOLVED=$sha"); exit 0 '
        "} catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }"
    )
    environment = os.environ.copy()
    # Codex runs pytest under PowerShell 7. Its inherited PSModulePath omits
    # Windows PowerShell's built-in modules, while the scheduled task starts
    # powershell.exe with the normal PS 5.1 module path.
    windows_modules = (
        Path(environment.get("WINDIR", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "Modules"
    )
    environment["PSModulePath"] = os.pathsep.join(
        [str(windows_modules), environment.get("PSModulePath", "")]
    )
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _valid_payload(
    signal_count: Any = 0, research_symbols: list[str] | None = None
) -> dict[str, Any]:
    research_symbols = ["NOVA"] if research_symbols is None else research_symbols
    return {
        "status": "no_trade" if signal_count == 0 else "complete",
        "scan_id": "scan-current-attempt",
        "code_sha": RELEASE_SHA,
        "source_summary": {"code_sha": RELEASE_SHA},
        "signal_count": signal_count,
        "run_contract": {
            "schema_version": "alphaops.run_contract.v1",
            "producer": "alphaops",
            "producer_run_id": "scan-current-attempt",
            "market_date": MARKET_DATE,
            "code_sha": RELEASE_SHA,
            "source_status": "success",
            "selection_outcome": "data_ineligible",
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
    allow_core_shortfall: bool = False,
    release_sha: str = "",
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
    core_switch = "-AllowCoreShortfall " if allow_core_shortfall else ""
    release_switch = f"-ReleaseSha '{release_sha}' " if release_sha else ""
    command = (
        f". '{HELPER}'; "
        f"$receipt = Get-Content -LiteralPath '{receipt}' -Raw | ConvertFrom-Json; "
        "try { "
        f"$result = Test-DawnstrikeAlphaCycleArtifact -ArtifactPath '{artifact}' "
        f"-ProcessReceipt $receipt -MarketDate '{MARKET_DATE}' "
        f"{release_switch}{core_switch}; "
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


def test_release_sha_resolver_accepts_only_exact_clean_git_runtime(tmp_path: Path) -> None:
    if shutil.which("git.exe") is None:
        pytest.skip("Git for Windows is required for release-SHA execution proof.")
    runtime = tmp_path / "clean-runtime"
    expected_sha = _initialize_git_runtime(runtime)

    result = _resolve_release_sha(runtime, tmp_path / "clean-logs")

    assert result.returncode == 0, result.stderr
    assert f"RESOLVED={expected_sha}" in result.stdout


@pytest.mark.parametrize("mutation", ["tracked", "staged", "untracked"])
def test_release_sha_resolver_rejects_uncommitted_runtime_bytes(
    tmp_path: Path,
    mutation: str,
) -> None:
    if shutil.which("git.exe") is None:
        pytest.skip("Git for Windows is required for release-SHA execution proof.")
    runtime = tmp_path / f"{mutation}-runtime"
    _initialize_git_runtime(runtime)
    if mutation == "untracked":
        (runtime / "runtime_override.py").write_text("print('untracked')\n", encoding="utf-8")
    else:
        (runtime / "app.py").write_text("print('modified')\n", encoding="utf-8")
        if mutation == "staged":
            subprocess.run(
                ["git", "-C", str(runtime), "add", "app.py"],
                check=True,
                capture_output=True,
            )

    result = _resolve_release_sha(runtime, tmp_path / f"{mutation}-logs")

    assert result.returncode == 1
    assert "release checkout is not clean" in result.stderr


@pytest.mark.parametrize("signal_count", [None, -1, 0.5])
def test_artifact_rejects_noncanonical_signal_counts(tmp_path: Path, signal_count: Any) -> None:
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
def test_artifact_rejects_missing_or_malformed_json(tmp_path: Path, payload: str | None) -> None:
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
    assert json.loads(result.stdout)["selection_outcome"] == "data_ineligible"


def test_artifact_accepts_lane_local_core_data_unavailable_shortfall(
    tmp_path: Path,
) -> None:
    payload = _valid_payload()
    payload["run_contract"]["core_universe_status"] = "DATA_UNAVAILABLE"

    strict = _validate(tmp_path, payload)
    assert strict.returncode == 1
    assert "core universe is not READY" in strict.stderr

    result = _validate(tmp_path, payload, allow_core_shortfall=True)

    assert result.returncode == 0, result.stderr


def test_artifact_binds_exact_scheduled_release_sha_across_cycle_and_contract(
    tmp_path: Path,
) -> None:
    payload = _valid_payload()

    accepted = _validate(tmp_path, payload, release_sha=RELEASE_SHA)
    assert accepted.returncode == 0, accepted.stderr

    for path in (
        ("code_sha",),
        ("source_summary", "code_sha"),
        ("run_contract", "code_sha"),
    ):
        hostile = _valid_payload()
        target = hostile
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = "b" * 40
        rejected = _validate(tmp_path, hostile, release_sha=RELEASE_SHA)
        assert rejected.returncode == 1
        assert "release identity" in rejected.stderr


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


def test_prior_artifact_is_restored_only_when_attempt_produced_no_replacement(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "alpha_cycle.json"
    archive = tmp_path / "attempt_archive" / "alpha_cycle.prior.json"
    archive.parent.mkdir()
    archive.write_text("prior", encoding="utf-8")
    command = (
        f". '{HELPER}'; "
        f"$restored = Restore-DawnstrikePriorAlphaCycleArtifact -ArtifactPath '{artifact}' "
        f"-ArchivePath '{archive}'; "
        "$restored | ConvertTo-Json -Compress"
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) is True
    assert artifact.read_text(encoding="utf-8") == "prior"
    assert not archive.exists()

    archive.write_text("older", encoding="utf-8")
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) is False
    assert artifact.read_text(encoding="utf-8") == "prior"
    assert archive.read_text(encoding="utf-8") == "older"


def test_invalid_replacement_is_quarantined_before_prior_artifact_is_restored(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "alpha_cycle.json"
    archive = tmp_path / "attempt_archive" / "alpha_cycle.prior.json"
    archive.parent.mkdir()
    artifact.write_text("invalid replacement", encoding="utf-8")
    archive.write_text("prior canonical", encoding="utf-8")
    command = (
        f". '{HELPER}'; "
        f"$restored = Restore-DawnstrikePriorAlphaCycleArtifact -ArtifactPath '{artifact}' "
        f"-ArchivePath '{archive}' -QuarantineReplacement; "
        "$restored | ConvertTo-Json -Compress"
    )

    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) is True
    assert artifact.read_text(encoding="utf-8") == "prior canonical"
    assert not archive.exists()
    quarantined = list(archive.parent.glob("alpha_cycle.invalid.*.json"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "invalid replacement"


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
