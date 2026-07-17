from __future__ import annotations

from pathlib import Path

import pytest

from intraday_scanner.paper_ops_root import (
    DEFAULT_PAPER_OPS_PRODUCTION_ROOT,
    PAPER_OPS_ROOT_ENV,
    paper_ops_artifact_path,
    production_paper_ops_root,
)


def test_production_root_defaults_to_live_tree() -> None:
    assert production_paper_ops_root(environ={}) == DEFAULT_PAPER_OPS_PRODUCTION_ROOT
    assert production_paper_ops_root(repo_root=Path("repo"), environ={}) == (
        Path("repo") / DEFAULT_PAPER_OPS_PRODUCTION_ROOT
    )


def test_production_root_honors_env_and_explicit_override(tmp_path: Path) -> None:
    environment = {PAPER_OPS_ROOT_ENV: "data/custom-paper"}

    assert production_paper_ops_root(repo_root=tmp_path, environ=environment) == (
        tmp_path / "data/custom-paper"
    )
    explicit = tmp_path / "isolated-paper"
    assert production_paper_ops_root(override=explicit, environ=environment) == explicit
    assert paper_ops_artifact_path(
        "calendar",
        "strategy_daily_returns.csv",
        override=explicit,
    ) == explicit / "calendar/strategy_daily_returns.csv"


def test_production_root_rejects_blank_explicit_override() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        production_paper_ops_root(override=" ", environ={})


def test_eod_chain_passes_one_root_to_every_paperops_phase() -> None:
    script = Path("scripts/run_alphaops_eod_full.bat").read_text(encoding="utf-8")

    assert (
        'if not defined DAWNSTRIKE_PAPER_OPS_ROOT set '
        '"DAWNSTRIKE_PAPER_OPS_ROOT=data\\v2_paper_ops_live"'
    ) in script
    paperops_lines = [
        line.strip()
        for line in script.splitlines()
        if line.strip().startswith("py -m intraday_scanner.v2.paper_ops ")
    ]
    assert paperops_lines
    assert all(
        '--output-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"' in line
        for line in paperops_lines
    )
    commands = [line.split()[3] for line in paperops_lines]
    assert set(commands) == {
        "run-day",
        "reconcile",
        "verify-calendar",
        "verify-source-bars",
        "rebuild-ledger",
        "shadow-init",
        "shadow-run",
        "blotter",
        "verify-blotter",
        "challenger-evaluate",
        "evidence",
    }
    assert commands.count("reconcile") == 2
    assert commands.count("verify-calendar") == 2
    assert commands.count("rebuild-ledger") == 2
    assert commands.count("verify-source-bars") == 2
    assert commands.count("blotter") == 1
    assert commands.count("verify-blotter") == 1
    assert (
        'if not defined DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS set '
        '"DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS=12"'
    ) in script
    assert (
        'if not defined DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS set '
        '"DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS=300"'
    ) in script
    assert "call :RUN_PAPEROPS_FORWARD_WITH_RETRY" in script
    assert ":PAPEROPS_FORWARD_RETRY_LOOP" in script
    assert 'if not "!PAPEROPS_CURRENT_DATE!"=="%RUN_DATE%"' in script
    assert (
        "if !PAPEROPS_ATTEMPT! GEQ %DAWNSTRIKE_PAPEROPS_MAX_ATTEMPTS%"
        in script
    )
    assert (
        "timeout /t %DAWNSTRIKE_PAPEROPS_RETRY_DELAY_SECONDS% /nobreak >nul"
        in script
    )
    assert '--paper-ops-root "%DAWNSTRIKE_PAPER_OPS_ROOT%"' in script
    assert "intraday_scanner.v2.command_center_x3 build" in script
