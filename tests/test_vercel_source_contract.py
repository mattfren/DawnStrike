"""Hostile tests for exact Git identity and immutable Vercel entrypoints."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Vercel PowerShell source-boundary contracts require Windows PowerShell.",
)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "vercel_source_contract.ps1"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _fixture(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Dawnstrike Test")
    (repo / "api").mkdir()
    (repo / "api" / "health.py").write_bytes(b"HEALTH-COMMITTED\n")
    (repo / "api" / "readiness.py").write_bytes(b"READINESS-COMMITTED\n")
    (repo / "scripts").mkdir()
    shutil.copy2(HELPER, repo / "scripts" / HELPER.name)
    shutil.copy2(
        ROOT / "scripts" / "dawnstrike_job_process.ps1",
        repo / "scripts" / "dawnstrike_job_process.ps1",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")


def _powershell(command: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
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
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def test_dirty_api_is_rejected_before_staging(tmp_path: Path) -> None:
    repo, commit, tree = _fixture(tmp_path)
    (repo / "api" / "health.py").write_bytes(b"ATTACKER-WORKTREE\n")
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Assert-VercelGitSourceStable -Root '{repo}' -ExpectedSourceSha '{commit}' "
        f"-ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "not clean" in result.stderr.lower()


def test_head_and_tree_race_is_rejected_after_identity_capture(tmp_path: Path) -> None:
    repo, commit, tree = _fixture(tmp_path)
    (repo / "api" / "health.py").write_bytes(b"NEW-COMMITTED\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "race")
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Assert-VercelGitSourceStable -Root '{repo}' -ExpectedSourceSha '{commit}' "
        f"-ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "head changed" in result.stderr.lower()


def test_staged_api_byte_mismatch_is_rejected(tmp_path: Path) -> None:
    repo, commit, tree = _fixture(tmp_path)
    stage = repo / "build" / "vercel-stage"
    (stage / "api").mkdir(parents=True)
    (stage / "api" / "health.py").write_bytes(b"HEALTH-COMMITTED\n")
    (stage / "api" / "readiness.py").write_bytes(b"READINESS-COMMITTED\n")
    health_hash = hashlib.sha256(b"HEALTH-COMMITTED\n").hexdigest()
    readiness_hash = hashlib.sha256(b"READINESS-COMMITTED\n").hexdigest()
    manifest = (
        '{"schema_version":"dawnstrike.vercel_source_manifest.v1",'
        f'"source_sha":"{commit}","source_tree":"{tree}","api_sha256":{{'
        f'"api/health.py":"{health_hash}",'
        f'"api/readiness.py":"{readiness_hash}"}}}}'
    )
    (stage / "vercel-source-manifest.json").write_text(manifest, encoding="utf-8")
    (stage / "api" / "health.py").write_bytes(b"TAMPERED-STAGE\n")
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Assert-VercelStagedSourceManifest -StageRoot '{stage}' "
        f"-ExpectedSourceSha '{commit}' -ExpectedSourceTree '{tree}'"
    )
    result = _powershell(command)
    assert result.returncode != 0
    assert "staged api bytes" in result.stderr.lower()


def test_git_blob_extraction_is_byte_exact_under_windows_powershell(tmp_path: Path) -> None:
    repo, commit, _tree = _fixture(tmp_path)
    destination = tmp_path / "extracted-health.py"
    command = (
        f". '{repo / 'scripts' / HELPER.name}'; "
        f"Write-VercelGitBlob -Root '{repo}' -Commit '{commit}' "
        f"-RelativePath 'api/health.py' -Destination '{destination}'"
    )
    result = _powershell(command)
    assert result.returncode == 0, result.stderr
    assert destination.read_bytes() == b"HEALTH-COMMITTED\n"
