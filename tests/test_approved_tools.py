"""Hostile checks for production Git executable and configuration admission."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from intraday_scanner import approved_tools


@pytest.mark.skipif(sys.platform != "win32", reason="production host is Windows")
def test_run_git_disables_repo_local_fsmonitor_execution(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git = str(approved_tools.approved_git_path())
    subprocess.run([git, "init", "-q", str(repository)], check=True)
    sentinel = tmp_path / "fsmonitor-executed.txt"
    hook = tmp_path / "hostile-fsmonitor.cmd"
    hook.write_text(f"@echo executed>{sentinel}\r\n@exit /b 0\r\n", encoding="utf-8")
    subprocess.run(
        [git, "-C", str(repository), "config", "--local", "core.fsmonitor", str(hook)],
        check=True,
    )

    with pytest.raises(approved_tools.ApprovedToolError, match="not governed"):
        approved_tools.run_git(repository, "status", "--porcelain=v1")
    assert not sentinel.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="production host is Windows")
def test_ambient_git_config_injection_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git = str(approved_tools.approved_git_path())
    subprocess.run([git, "init", "-q", str(repository)], check=True)
    sentinel = tmp_path / "ambient-fsmonitor-executed.txt"
    hook = tmp_path / "ambient-fsmonitor.cmd"
    hook.write_text(f"@echo executed>{sentinel}\r\n@exit /b 0\r\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook))

    result = approved_tools.run_git(repository, "status", "--porcelain=v1")
    assert result.returncode == 0
    assert not sentinel.exists()


def test_operational_python_git_calls_use_approved_helper() -> None:
    paths = (
        Path("scripts/build_public.py"),
        Path("scripts/verify_daily_prepublication.py"),
        Path("scripts/validate_web_source_config.py"),
        Path("intraday_scanner/services/daily_run_service.py"),
        Path("intraday_scanner/v2/paper_ops/universe_handoff.py"),
    )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "run_git" in source
        assert '["git"' not in source
