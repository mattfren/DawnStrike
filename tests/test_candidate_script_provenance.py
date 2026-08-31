from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECT_CANDIDATE_SCRIPTS = (
    "scripts/state_preparation.py",
    "scripts/prepare_dawnstrike_state.py",
    "scripts/runtime_activation_contract.py",
    "scripts/verify_public_artifact.py",
)


def _write_stale_import_tree(root: Path) -> None:
    for package in (
        root / "intraday_scanner",
        root / "intraday_scanner" / "storage",
        root / "scripts",
    ):
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    marker = "raise RuntimeError('STALE_RUNTIME_MODULE_LOADED')\n"
    (root / "intraday_scanner" / "storage" / "migrations.py").write_text(
        marker, encoding="utf-8"
    )
    (root / "scripts" / "state_preparation.py").write_text(marker, encoding="utf-8")
    (root / "scripts" / "public_lineage.py").write_text(marker, encoding="utf-8")


@pytest.mark.parametrize("relative_script", DIRECT_CANDIDATE_SCRIPTS)
def test_direct_candidate_contract_scripts_ignore_stale_runtime_imports(
    tmp_path: Path, relative_script: str
) -> None:
    stale_root = tmp_path / "stale-runtime"
    _write_stale_import_tree(stale_root)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(stale_root)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / relative_script), "--help"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "STALE_RUNTIME_MODULE_LOADED" not in result.stdout + result.stderr

