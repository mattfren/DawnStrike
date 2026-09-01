from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_BOOTSTRAP = ROOT / "scripts" / "dawnstrike_python_bootstrap.py"
PRODUCTION_GIT_PATH = r"C:\Program Files\Git\cmd\git.exe"
PRODUCTION_GIT_SHA256 = (
    "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"
)
BOOTSTRAP_PRELOADER = (
    "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); "
    "a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw("
    "RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; "
    "exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
)


def _copy_bootstrap_for_host(destination: Path) -> None:
    """Exercise the bootstrap on CI without weakening its production pin."""

    discovered = shutil.which("git")
    assert discovered is not None
    git_path = Path(discovered).resolve()
    git_sha256 = hashlib.sha256(git_path.read_bytes()).hexdigest()
    source = SOURCE_BOOTSTRAP.read_text(encoding="utf-8")
    source = source.replace(
        f'_APPROVED_GIT = Path(r"{PRODUCTION_GIT_PATH}")',
        f"_APPROVED_GIT = Path({str(git_path)!r})",
        1,
    ).replace(PRODUCTION_GIT_SHA256, git_sha256, 1)
    assert repr(str(git_path)) in source
    assert git_sha256 in source
    destination.write_text(source, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _release(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "release"
    (root / "scripts").mkdir(parents=True)
    (root / "intraday_scanner").mkdir()
    _copy_bootstrap_for_host(root / "scripts" / SOURCE_BOOTSTRAP.name)
    (root / "intraday_scanner" / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.pyc\n*.csv\n", encoding="utf-8")
    (root / "scripts" / "target.py").write_text("print('BOOTSTRAP_OK')\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(root)],
        check=True,
        capture_output=True,
    )
    _git(root, "config", "user.email", "bootstrap-test@example.invalid")
    _git(root, "config", "user.name", "Bootstrap Test")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    # Settle the disposable index stat cache before the bootstrap performs its
    # read-only identity check.  Production materialization is already checked
    # repeatedly by the activation contract; this prevents a racy-clean temp
    # file timestamp from masquerading as a content change in this fixture.
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""
    return root, _git(root, "rev-parse", "HEAD")


def _run(
    root: Path, expected_sha: str, *, preloaded: bool = False
) -> subprocess.CompletedProcess[str]:
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    prefix = [sys.executable, "-I", "-B", "-S"]
    if preloaded:
        prefix.extend(
            [
                "-c",
                BOOTSTRAP_PRELOADER,
                str(bootstrap),
                hashlib.sha256(bootstrap.read_bytes()).hexdigest(),
            ]
        )
    else:
        prefix.append(str(bootstrap))
    return subprocess.run(
        [
            *prefix,
            "--release-root",
            str(root),
            "--expected-sha",
            expected_sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("preloaded", [False, True])
def test_bootstrap_runs_only_clean_exact_release(tmp_path: Path, preloaded: bool) -> None:
    root, sha = _release(tmp_path)

    result = _run(root, sha, preloaded=preloaded)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BOOTSTRAP_OK"


@pytest.mark.parametrize("mutation", ["tracked", "hidden", "ignored_python", "wrong_sha"])
def test_bootstrap_rejects_runtime_identity_tampering(
    tmp_path: Path, mutation: str
) -> None:
    root, sha = _release(tmp_path)
    target = root / "scripts" / "target.py"
    expected_sha = sha
    if mutation == "tracked":
        target.write_text("print('TAMPERED')\n", encoding="utf-8")
    elif mutation == "hidden":
        _git(root, "update-index", "--assume-unchanged", "scripts/target.py")
        target.write_text("print('TAMPERED')\n", encoding="utf-8")
    elif mutation == "ignored_python":
        (root / "intraday_scanner" / "hostile.pyc").write_bytes(b"hostile")
    else:
        expected_sha = "f" * 40

    result = _run(root, expected_sha)

    assert result.returncode != 0
    assert "TAMPERED" not in result.stdout


def test_bootstrap_allows_ignored_nonexecutable_research_data(tmp_path: Path) -> None:
    root, sha = _release(tmp_path)
    (root / "research.csv").write_text("symbol,value\nAAPL,1\n", encoding="utf-8")

    result = _run(root, sha)

    assert result.returncode == 0, result.stderr


def test_preloader_rejects_changed_bootstrap_bytes(tmp_path: Path) -> None:
    root, sha = _release(tmp_path)
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    expected_hash = hashlib.sha256(bootstrap.read_bytes()).hexdigest()
    bootstrap.write_text("print('BYPASS')\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            "-c",
            BOOTSTRAP_PRELOADER,
            str(bootstrap),
            expected_hash,
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "BYPASS" not in result.stdout


def test_source_bootstrap_pins_production_windows_git() -> None:
    source = SOURCE_BOOTSTRAP.read_text(encoding="utf-8")

    assert f'_APPROVED_GIT = Path(r"{PRODUCTION_GIT_PATH}")' in source
    assert PRODUCTION_GIT_SHA256 in source
