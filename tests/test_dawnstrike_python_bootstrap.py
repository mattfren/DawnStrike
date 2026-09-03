from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_BOOTSTRAP = ROOT / "scripts" / "dawnstrike_python_bootstrap.py"
PRODUCTION_GIT_PATH = r"C:\Program Files\Git\cmd\git.exe"
PRODUCTION_GIT_SHA256 = (
    "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"  # pragma: allowlist secret
)
PRODUCTION_RECORD_SET_SHA256 = (
    "447a0d12feffcfd6c353d9acb4cfd1e5cc1b35e3548cd7e9ad58666516b4b3af"  # pragma: allowlist secret
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
    requirements: dict[str, str] = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s\\]+)",
            line.strip(),
        )
        if match:
            requirements[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    installed = {
        re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower(): dist
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name")
    }
    rows = []
    for name, version in sorted(requirements.items()):
        dist = installed[name]
        assert dist.version == version
        record = next(item for item in dist.files or () if str(item).endswith(".dist-info/RECORD"))
        record_path = Path(dist.locate_file(record)).resolve(strict=True)
        rows.append(f"{name}\0{version}\0{hashlib.sha256(record_path.read_bytes()).hexdigest()}\n")
    host_record_set = hashlib.sha256("".join(rows).encode()).hexdigest()

    source = SOURCE_BOOTSTRAP.read_text(encoding="utf-8")
    source = (
        source.replace(
            f'_APPROVED_GIT = Path(r"{PRODUCTION_GIT_PATH}")',
            f"_APPROVED_GIT = Path({str(git_path)!r})",
            1,
        )
        .replace(PRODUCTION_GIT_SHA256, git_sha256, 1)
        .replace(PRODUCTION_RECORD_SET_SHA256, host_record_set, 1)
    )
    assert repr(str(git_path)) in source
    assert git_sha256 in source
    assert host_record_set in source
    destination.write_text(source, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _release(
    tmp_path: Path,
    *,
    metadata_race_fixture: bool = False,
    source_dispatch_race_fixture: bool = False,
) -> tuple[Path, str]:
    root = tmp_path / "release"
    (root / "scripts").mkdir(parents=True)
    (root / "intraday_scanner").mkdir()
    bootstrap = root / "scripts" / SOURCE_BOOTSTRAP.name
    _copy_bootstrap_for_host(bootstrap)
    if metadata_race_fixture:
        source = bootstrap.read_text(encoding="utf-8")
        anchor = (
            "    metadata_snapshots, metadata_handles, forbidden_absent, metadata_guard = (\n"
            "        _validated_git_metadata(root)\n"
            "    )\n"
        )
        assert source.count(anchor) == 1
        source = source.replace("import sys\n", "import sys\nimport time\n", 1).replace(
            anchor,
            anchor
            + "    print('DAWNSTRIKE_TEST_BOOTSTRAP_METADATA_READY', flush=True)\n"
            + "    time.sleep(3)\n",
            1,
        )
        bootstrap.write_text(source, encoding="utf-8")
    if source_dispatch_race_fixture:
        source = bootstrap.read_text(encoding="utf-8")
        anchor = (
            "    source_guard = retry_budget.admit(root, args.expected_sha)\n"
        )
        assert source.count(anchor) == 1
        source = source.replace("import sys\n", "import sys\nimport time\n", 1).replace(
            anchor,
            anchor
            + "    print('DAWNSTRIKE_TEST_BOOTSTRAP_SOURCE_GUARD_READY', flush=True)\n"
            + "    time.sleep(3)\n",
            1,
        )
        bootstrap.write_text(source, encoding="utf-8")
    (root / "intraday_scanner" / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.pyc\n*.csv\n", encoding="utf-8")
    shutil.copy2(ROOT / ".gitattributes", root / ".gitattributes")
    shutil.copy2(ROOT / "requirements.lock", root / "requirements.lock")
    (root / "scripts" / "target.py").write_text("print('BOOTSTRAP_OK')\n", encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(root)],
        check=True,
        capture_output=True,
    )
    _git(root, "config", "user.email", "bootstrap-test@example.invalid")
    _git(root, "config", "user.name", "Bootstrap Test")
    _git(root, "config", "core.autocrlf", "true")
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


def _amend_release(root: Path) -> str:
    _git(root, "add", ".")
    _git(root, "commit", "--amend", "--no-edit")
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""
    return _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("preloaded", [False, True])
def test_bootstrap_runs_only_clean_exact_release(tmp_path: Path, preloaded: bool) -> None:
    root, sha = _release(tmp_path)

    result = _run(root, sha, preloaded=preloaded)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BOOTSTRAP_OK"


def test_exact_source_admission_retries_one_transient_then_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = __import__(
        "scripts.dawnstrike_python_bootstrap", fromlist=["_assert_exact_source"]
    )
    admitted_guard = object()
    calls = 0

    def admit(_root: Path, _expected_sha: str) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(bootstrap._RETRYABLE_EXACT_SOURCE_ADMISSION_FAILURE)
        return admitted_guard

    monkeypatch.setattr(bootstrap, "_assert_exact_source", admit)

    result = bootstrap._assert_exact_source_with_bounded_retry(Path("release"), "a" * 40)

    assert result is admitted_guard
    assert calls == 2


def test_exact_source_admission_persistent_notification_fails_after_one_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = __import__(
        "scripts.dawnstrike_python_bootstrap", fromlist=["_assert_exact_source"]
    )
    calls = 0

    def reject(_root: Path, _expected_sha: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError(bootstrap._RETRYABLE_EXACT_SOURCE_ADMISSION_FAILURE)

    monkeypatch.setattr(bootstrap, "_assert_exact_source", reject)

    with pytest.raises(
        RuntimeError, match="release Git metadata changed during source verification"
    ):
        bootstrap._assert_exact_source_with_bounded_retry(Path("release"), "a" * 40)

    assert calls == 2


def test_final_pre_dispatch_notification_restarts_exact_admission_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = __import__("scripts.dawnstrike_python_bootstrap", fromlist=["main"])
    root = tmp_path / "release"
    script = root / "scripts" / "target.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('fixture')\n", encoding="utf-8")
    events = {
        "admissions": 0,
        "dependency_checks": 0,
        "package_checks": 0,
        "audit_installs": 0,
        "dispatches": 0,
    }
    closed: list[int] = []

    class Guard:
        source_bytes: dict[str, bytes] = {}
        git_contract: dict[str, str] = {"contract": "identical"}

        def __init__(self, attempt: int) -> None:
            self.attempt = attempt
            self.checks = 0

        def assert_unchanged(self) -> None:
            self.checks += 1
            if self.attempt == 1:
                raise RuntimeError(bootstrap._RETRYABLE_EXACT_SOURCE_ADMISSION_FAILURE)

        def close(self) -> None:
            if self.attempt not in closed:
                closed.append(self.attempt)

    guards = [Guard(1), Guard(2)]

    def admit(_root: Path, _expected_sha: str) -> Guard:
        guard = guards[events["admissions"]]
        events["admissions"] += 1
        return guard

    def check_dependencies(*_args: object):
        events["dependency_checks"] += 1
        return frozenset(), frozenset(), {}

    def check_package(_root: Path) -> None:
        events["package_checks"] += 1

    def install_audit(_root: Path, _guard: Guard) -> None:
        events["audit_installs"] += 1

    def dispatch(_script: Path, _source_bytes: dict[str, bytes], _argv: list[str]) -> None:
        events["dispatches"] += 1

    monkeypatch.setattr(bootstrap, "_release_root", lambda _raw: root)
    monkeypatch.setattr(bootstrap, "_assert_exact_source", admit)
    monkeypatch.setattr(bootstrap, "_install_verified_release_importer", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_append_governed_dependencies", lambda: ())
    monkeypatch.setattr(bootstrap, "_assert_locked_dependencies", check_dependencies)
    monkeypatch.setattr(bootstrap, "_install_verified_dependency_importers", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_install_verified_git_dispatch_guard", install_audit)
    monkeypatch.setattr(bootstrap, "_assert_package_from", check_package)
    monkeypatch.setattr(bootstrap, "_run_verified_release_script", dispatch)
    original_path = list(sys.path)
    original_argv = list(sys.argv)
    try:
        result = bootstrap.main(
            [
                "--release-root",
                str(root),
                "--expected-sha",
                "a" * 40,
                "--script",
                str(script),
                "--",
            ]
        )
    finally:
        sys.path[:] = original_path
        sys.argv[:] = original_argv

    assert result == 0
    assert events == {
        "admissions": 2,
        "dependency_checks": 2,
        "package_checks": 2,
        "audit_installs": 1,
        "dispatches": 1,
    }
    assert closed == [1, 2]


def test_bootstrap_never_retries_a_post_dispatch_lifetime_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = __import__("scripts.dawnstrike_python_bootstrap", fromlist=["main"])
    root = tmp_path / "release"
    script = root / "scripts" / "target.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('fixture')\n", encoding="utf-8")
    events = {"admissions": 0, "dispatches": 0, "closes": 0, "dispatched": False}

    class Guard:
        source_bytes: dict[str, bytes] = {}
        git_contract: dict[str, str] = {}

        def assert_unchanged(self) -> None:
            if events["dispatched"]:
                raise RuntimeError("release Git metadata changed during dispatched target lifetime")

        def close(self) -> None:
            events["closes"] += 1

    guard = Guard()

    def admit(_root: Path, _expected_sha: str) -> Guard:
        events["admissions"] += 1
        return guard

    def dispatch(_script: Path, _source_bytes: dict[str, bytes], _argv: list[str]) -> None:
        events["dispatches"] += 1
        events["dispatched"] = True

    monkeypatch.setattr(bootstrap, "_release_root", lambda _raw: root)
    monkeypatch.setattr(bootstrap, "_assert_exact_source", admit)
    monkeypatch.setattr(bootstrap, "_install_verified_release_importer", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_append_governed_dependencies", lambda: ())
    monkeypatch.setattr(
        bootstrap,
        "_assert_locked_dependencies",
        lambda *_args: (frozenset(), frozenset(), {}),
    )
    monkeypatch.setattr(bootstrap, "_install_verified_dependency_importers", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_install_verified_git_dispatch_guard", lambda *_args: None)
    monkeypatch.setattr(bootstrap, "_assert_package_from", lambda _root: None)
    monkeypatch.setattr(bootstrap, "_run_verified_release_script", dispatch)
    original_path = list(sys.path)
    original_argv = list(sys.argv)
    try:
        with pytest.raises(
            RuntimeError,
            match="release Git metadata changed during dispatched target lifetime",
        ):
            bootstrap.main(
                [
                    "--release-root",
                    str(root),
                    "--expected-sha",
                    "a" * 40,
                    "--script",
                    str(script),
                    "--",
                ]
            )
    finally:
        sys.path[:] = original_path
        sys.argv[:] = original_argv

    assert events == {"admissions": 1, "dispatches": 1, "closes": 1, "dispatched": True}


@pytest.mark.parametrize("mutation", ["tracked", "hidden", "ignored_python", "wrong_sha"])
def test_bootstrap_rejects_runtime_identity_tampering(tmp_path: Path, mutation: str) -> None:
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


def test_bootstrap_keeps_tracked_source_guarded_through_dispatch(tmp_path: Path) -> None:
    root, _ = _release(tmp_path, source_dispatch_race_fixture=True)
    guarded_module = root / "intraday_scanner" / "guarded_race.py"
    guarded_module.write_text("print('SAFE_TRACKED_MODULE')\n", encoding="utf-8")
    (root / "scripts" / "target.py").write_text(
        "import intraday_scanner.guarded_race\n", encoding="utf-8"
    )
    sha = _amend_release(root)
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_BOOTSTRAP_SOURCE_GUARD_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"source guard race did not synchronize: {marker!r} {stdout!r} {stderr!r}")
    if sys.platform == "win32":
        with pytest.raises(PermissionError):
            guarded_module.write_text("print('HOSTILE_TRACKED_MODULE')\n", encoding="utf-8")
    else:
        guarded_module.write_text("print('HOSTILE_TRACKED_MODULE')\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert stdout.strip() == "SAFE_TRACKED_MODULE"
    assert "HOSTILE_TRACKED_MODULE" not in stdout
    if sys.platform == "win32":
        assert process.returncode == 0, (marker, stdout, stderr)
    else:
        assert process.returncode != 0
        assert "release tracked file changed" in stderr


def test_bootstrap_uses_captured_exact_commit_requirements_after_admission(
    tmp_path: Path,
) -> None:
    root, sha = _release(tmp_path, source_dispatch_race_fixture=True)
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_BOOTSTRAP_SOURCE_GUARD_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"requirements race did not synchronize: {marker!r} {stdout!r} {stderr!r}")
    hostile = "attacker-package==9.9.9 --hash=sha256:" + ("0" * 64) + "\n"
    requirements = root / "requirements.lock"
    if sys.platform == "win32":
        with pytest.raises(PermissionError):
            requirements.write_text(hostile, encoding="utf-8")
    else:
        requirements.write_text(hostile, encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert stdout.strip() == "BOOTSTRAP_OK"
    if sys.platform == "win32":
        assert process.returncode == 0, (marker, stdout, stderr)
    else:
        assert process.returncode != 0
        assert "release tracked file changed" in stderr


@pytest.mark.skipif(sys.platform != "win32", reason="production checkout lock is Windows")
def test_bootstrap_handle_locks_every_tracked_non_python_file_through_dispatch(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path, source_dispatch_race_fixture=True)
    authority = root / "vercel.json"
    authority.write_text('{"version":"admitted"}\n', encoding="utf-8")
    (root / "scripts" / "target.py").write_text(
        "from pathlib import Path\n"
        "print(Path(__file__).resolve().parents[1].joinpath('vercel.json').read_text())\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_BOOTSTRAP_SOURCE_GUARD_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"tracked-file race did not synchronize: {marker!r} {stdout!r} {stderr!r}")
    with pytest.raises(PermissionError):
        authority.write_text('{"version":"hostile"}\n', encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, (marker, stdout, stderr)
    assert stdout.strip() == '{"version":"admitted"}'


def test_bootstrap_rejects_new_release_root_python_shadow_after_admission(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path, source_dispatch_race_fixture=True)
    (root / "scripts" / "target.py").write_text(
        "import fractions\nprint('TRUSTED_TARGET_CONTINUED')\n", encoding="utf-8"
    )
    sha = _amend_release(root)
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_BOOTSTRAP_SOURCE_GUARD_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"source guard race did not synchronize: {marker!r} {stdout!r} {stderr!r}")
    (root / "fractions.py").write_text("print('HOSTILE_SHADOW')\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, (marker, stdout, stderr)
    assert "HOSTILE_SHADOW" not in stdout
    assert "release source import is not exact-commit owned" in stderr


def test_bootstrap_rejects_commondir_created_during_dispatched_target(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    git_path = Path(shutil.which("git") or "").resolve(strict=True)
    continue_signal = tmp_path / "continue-dispatched-target"
    (root / "scripts" / "target.py").write_text(
        "import os, subprocess, time\n"
        "from pathlib import Path\n"
        "print('DAWNSTRIKE_TEST_DISPATCHED_TARGET_READY', flush=True)\n"
        f"signal = Path({str(continue_signal)!r})\n"
        "while not signal.exists():\n"
        "    time.sleep(0.01)\n"
        "env = {k: v for k, v in os.environ.items() "
        "if not k.upper().startswith('GIT_')}\n"
        "root = Path(__file__).resolve().parents[1]\n"
        f"result = subprocess.run([{str(git_path)!r}, '-C', str(root), "
        "'config', '--get', 'dawnstrike.attack'], env=env, capture_output=True, "
        "text=True, check=False)\n"
        "print('HOSTILE_METADATA_RESULT=' + result.stdout.strip())\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)
    hostile_common = tmp_path / "hostile-common-dispatch"
    shutil.copytree(root / ".git", hostile_common)
    hostile_config = hostile_common / "config"
    hostile_config.write_text(
        hostile_config.read_text(encoding="utf-8")
        + "\n[dawnstrike]\n\tattack = HOSTILE_METADATA_WON\n",
        encoding="utf-8",
    )
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_DISPATCHED_TARGET_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"dispatched target race did not synchronize: {marker!r} {stdout!r} {stderr!r}")
    (root / ".git" / "commondir").write_text(str(hostile_common), encoding="utf-8")
    continue_signal.write_text("continue\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode != 0, (marker, stdout, stderr)
    assert "HOSTILE_METADATA_WON" not in stdout
    assert "release Git metadata" in stderr


def test_bootstrap_rejects_approved_git_dispatch_that_strips_exact_binding(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    git_path = Path(shutil.which("git") or "").resolve(strict=True)
    (root / "scripts" / "target.py").write_text(
        "import os, subprocess\n"
        "env = {k: v for k, v in os.environ.items() "
        "if not k.upper().startswith('GIT_')}\n"
        f"subprocess.run([{str(git_path)!r}, '--version'], env=env, check=True)\n"
        "print('UNBOUND_APPROVED_GIT_RAN')\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode != 0
    assert "UNBOUND_APPROVED_GIT_RAN" not in result.stdout
    assert "Git subprocess is forbidden after exact-source admission" in result.stderr


def test_bootstrap_rejects_approved_git_dispatch_even_with_exact_binding(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    git_path = Path(shutil.which("git") or "").resolve(strict=True)
    (root / "scripts" / "target.py").write_text(
        "import os, subprocess\n"
        f"subprocess.run([{str(git_path)!r}, '--version'], env=dict(os.environ), check=True)\n"
        "print('BOUND_APPROVED_GIT_RAN')\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode != 0
    assert "BOUND_APPROVED_GIT_RAN" not in result.stdout
    assert "Git subprocess is forbidden after exact-source admission" in result.stderr


@pytest.mark.parametrize(
    "injection",
    [
        "env['GIT_OBJECT_DIRECTORY'] = str(root / 'hostile-objects')",
        "env['git_dir'] = str(root / '.git')",
    ],
)
def test_bootstrap_rejects_extra_or_duplicate_git_dispatch_metadata(
    tmp_path: Path, injection: str
) -> None:
    root, _ = _release(tmp_path)
    git_path = Path(shutil.which("git") or "").resolve(strict=True)
    (root / "scripts" / "target.py").write_text(
        "import os, subprocess\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "env = dict(os.environ)\n"
        f"{injection}\n"
        f"subprocess.run([{str(git_path)!r}, '--version'], env=env, check=True)\n"
        "print('REDIRECTED_APPROVED_GIT_RAN')\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode != 0
    assert "REDIRECTED_APPROVED_GIT_RAN" not in result.stdout
    assert "Git subprocess is forbidden after exact-source admission" in result.stderr


def test_bootstrap_clears_git_environment_and_marks_descendants_fail_closed(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    (root / "scripts" / "target.py").write_text(
        "import os\n"
        "git_keys = sorted(key for key in os.environ if key.upper().startswith('GIT_'))\n"
        "print('GIT_KEYS=' + ','.join(git_keys))\n"
        "print('DESCENDANT_SENTINEL=' + "
        "os.environ.get('DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED', ''))\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode == 0, result.stderr
    assert "GIT_KEYS=" in result.stdout
    assert "GIT_DIR" not in result.stdout
    assert "DESCENDANT_SENTINEL=1" in result.stdout


def test_unbootstrapped_descendant_cannot_fall_back_to_mutable_git(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    shutil.copy2(ROOT / "intraday_scanner" / "approved_tools.py", root / "intraday_scanner")
    (root / "scripts" / "target.py").write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "child = (\n"
        '    "import sys; sys.path.insert(0, sys.argv[1]); "\n'
        '    "from intraday_scanner.approved_tools import run_git; "\n'
        "    \"print(run_git(sys.argv[1], 'rev-parse', 'HEAD').stdout)\"\n"
        ")\n"
        "result = subprocess.run(\n"
        "    [sys.executable, '-I', '-B', '-S', '-c', child, str(root)],\n"
        "    capture_output=True, text=True, check=False,\n"
        ")\n"
        "print('CHILD_RC=' + str(result.returncode))\n"
        "print(result.stderr, end='')\n"
        "if result.returncode == 0:\n"
        "    raise RuntimeError('unbootstrapped descendant unexpectedly ran Git')\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode == 0, result.stderr
    assert "CHILD_RC=1" in result.stdout
    assert "admitted descendant without its own snapshot" in result.stdout
    assert sha not in result.stdout


def test_bootstrap_production_git_caller_consumes_only_admitted_identity(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    shutil.copy2(ROOT / "intraday_scanner" / "approved_tools.py", root / "intraday_scanner")
    (root / "scripts" / "target.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from intraday_scanner.approved_tools import run_git\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "ref = root / '.git' / 'refs' / 'heads' / 'main'\n"
        "def hostile_after_bootstrap_hook(event, args):\n"
        "    if event != 'subprocess.Popen' or len(args) != 4:\n"
        "        return\n"
        "    command = args[1]\n"
        "    tokens = command if isinstance(command, (list, tuple)) else [command]\n"
        "    if 'rev-parse' not in [str(token) for token in tokens]:\n"
        "        return\n"
        "    ref.write_text('f' * 40 + '\\n', encoding='ascii')\n"
        "    print('HOSTILE_LATE_AUDIT_HOOK_RAN', flush=True)\n"
        "sys.addaudithook(hostile_after_bootstrap_hook)\n"
        "value = run_git(root, 'rev-parse', 'HEAD').stdout.strip()\n"
        "print('CONSUMED_REF=' + value)\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode == 0, result.stderr
    assert "HOSTILE_LATE_AUDIT_HOOK_RAN" not in result.stdout
    assert f"CONSUMED_REF={sha}" in result.stdout
    assert _git(root, "rev-parse", "HEAD") == sha


def test_bootstrap_serves_public_web_inventory_and_bytes_from_admitted_commit(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    shutil.copy2(ROOT / "intraday_scanner" / "approved_tools.py", root / "intraday_scanner")
    (root / "web" / "assets").mkdir(parents=True)
    (root / "web" / "index.html").write_bytes(b"<html>exact</html>\n")
    (root / "web" / "favicon.svg").write_bytes(b"<svg>exact</svg>\n")
    (root / "web" / "assets" / "app.js").write_bytes(b"const exact = true;\n")
    (root / "scripts" / "target.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from intraday_scanner.approved_tools import read_git_bytes, run_git\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "sys.addaudithook(lambda event, args: "
        "print('UNEXPECTED_GIT_SPAWN', flush=True) if event == 'subprocess.Popen' else None)\n"
        "sha = run_git(root, 'rev-parse', 'HEAD').stdout.strip()\n"
        "names = read_git_bytes(\n"
        "    root, 'ls-tree', '-r', '--name-only', '-z', sha, '--', 'web/assets'\n"
        ").decode()\n"
        "payload = read_git_bytes(root, 'show', sha + ':web/assets/app.js')\n"
        "print('WEB_NAMES=' + names.replace('\\0', ','))\n"
        "print('WEB_BYTES=' + payload.decode().strip())\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode == 0, result.stderr
    assert "UNEXPECTED_GIT_SPAWN" not in result.stdout
    assert "WEB_NAMES=web/assets/app.js," in result.stdout
    assert "WEB_BYTES=const exact = true;" in result.stdout


def test_bootstrap_rejects_raw_git_before_a_later_hostile_audit_hook(
    tmp_path: Path,
) -> None:
    root, _ = _release(tmp_path)
    git_path = Path(shutil.which("git") or "").resolve(strict=True)
    (root / "scripts" / "target.py").write_text(
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[1]\n"
        "ref = root / '.git' / 'refs' / 'heads' / 'main'\n"
        "def hostile_after_bootstrap_hook(event, args):\n"
        "    if event == 'subprocess.Popen':\n"
        "        ref.write_text('f' * 40 + '\\n', encoding='ascii')\n"
        "        print('HOSTILE_LATE_AUDIT_HOOK_RAN', flush=True)\n"
        "sys.addaudithook(hostile_after_bootstrap_hook)\n"
        f"subprocess.run([{str(git_path)!r}, '-C', str(root), 'rev-parse', 'HEAD'], check=True)\n"
        "print('RAW_GIT_RAN')\n",
        encoding="utf-8",
    )
    sha = _amend_release(root)

    result = _run(root, sha)

    assert result.returncode != 0
    assert "HOSTILE_LATE_AUDIT_HOOK_RAN" not in result.stdout
    assert "RAW_GIT_RAN" not in result.stdout
    assert "Git subprocess is forbidden after exact-source admission" in result.stderr
    assert _git(root, "rev-parse", "HEAD") == sha


def test_bootstrap_rejects_config_worktree_created_after_metadata_validation(
    tmp_path: Path,
) -> None:
    root, sha = _release(tmp_path, metadata_race_fixture=True)
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_BOOTSTRAP_METADATA_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(
            f"bootstrap metadata race did not synchronize: {marker!r} {stdout!r} {stderr!r}"
        )
    if sys.platform == "win32":
        with pytest.raises(PermissionError):
            (root / ".git" / "config").write_text(
                '[url "https://attacker.invalid/"]\n\tinsteadOf = https://github.com/\n',
                encoding="utf-8",
            )
    (root / ".git" / "config.worktree").write_text(
        "[core]\n\tbare = true\n\tworktree = C:/hostile-worktree\n",
        encoding="utf-8",
    )
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode != 0, (marker, stdout, stderr)
    assert "BOOTSTRAP_OK" not in stdout
    assert "release Git metadata" in stderr


def test_bootstrap_rejects_commondir_created_after_metadata_validation(
    tmp_path: Path,
) -> None:
    root, sha = _release(tmp_path, metadata_race_fixture=True)
    hostile_common = tmp_path / "hostile-common"
    shutil.copytree(root / ".git", hostile_common)
    hostile_config = hostile_common / "config"
    hostile_config.write_text(
        hostile_config.read_text(encoding="utf-8")
        + '\n[url "https://attacker.invalid/"]\n\tinsteadOf = https://github.com/\n',
        encoding="utf-8",
    )
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            sha,
            "--script",
            str(root / "scripts" / "target.py"),
            "--",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_BOOTSTRAP_METADATA_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(
            f"bootstrap metadata race did not synchronize: {marker!r} {stdout!r} {stderr!r}"
        )
    (root / ".git" / "commondir").write_text(str(hostile_common), encoding="utf-8")
    stdout, stderr = process.communicate(timeout=20)
    assert process.returncode != 0, (marker, stdout, stderr)
    assert "BOOTSTRAP_OK" not in stdout
    assert "release Git metadata" in stderr


def test_bootstrap_rejects_linked_worktree_pointer(tmp_path: Path) -> None:
    root, sha = _release(tmp_path)
    linked_metadata = tmp_path / "linked-metadata"
    (root / ".git").rename(linked_metadata)
    (root / ".git").write_text(f"gitdir: {linked_metadata}\n", encoding="utf-8")

    result = _run(root, sha)

    assert result.returncode != 0
    assert "self-contained clone" in result.stderr


def test_bootstrap_rejects_enabled_worktree_config_extension(tmp_path: Path) -> None:
    root, sha = _release(tmp_path)
    _git(root, "config", "extensions.worktreeConfig", "true")

    result = _run(root, sha)

    assert result.returncode != 0
    assert "requires the Git worktree config extension disabled" in result.stderr


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
    assert source.count('"extensions.worktreeConfig=false"') == 3


def test_distribution_record_swap_cannot_change_captured_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prefix = tmp_path / "prefix"
    record_path = prefix / "fixture-1.0.dist-info" / "RECORD"
    record_path.parent.mkdir(parents=True)
    encoded = base64.urlsafe_b64encode(b"x" * 32).rstrip(b"=").decode("ascii")
    benign = (f"fixture/__init__.py,sha256={encoded},1\nfixture-1.0.dist-info/RECORD,,\n").encode()
    hostile = (f"hostile.py,sha256={encoded},1\nfixture-1.0.dist-info/RECORD,,\n").encode()
    record_path.write_bytes(benign)
    hostile_path = prefix / "hostile-record"
    hostile_path.write_bytes(hostile)

    class FakeDistribution:
        _path = record_path.parent

        @property
        def files(self) -> object:
            pytest.fail("RECORD was reopened through Distribution.files")

        def locate_file(self, relative: object) -> Path:
            return prefix.joinpath(*PurePosixPath(str(relative)).parts)

        def read_text(self, _name: str) -> None:
            return None

    real_sha256 = hashlib.sha256
    raced = False

    def replace_after_digest_input(data: bytes = b"", *args: object, **kwargs: object):
        nonlocal raced
        if data == benign and not raced:
            raced = True
            try:
                os.replace(hostile_path, record_path)
            except PermissionError:
                pass
        return real_sha256(data, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", replace_after_digest_input)
    try:
        owned, _hashes, _top, _digest = __import__(
            "scripts.dawnstrike_python_bootstrap", fromlist=["_read_distribution_record"]
        )._read_distribution_record(FakeDistribution(), prefix, "fixture")
    except RuntimeError as exc:
        assert "RECORD changed" in str(exc)
    else:
        assert os.path.normcase(str(prefix / "fixture" / "__init__.py")) in owned
        assert os.path.normcase(str(prefix / "hostile.py")) not in owned
    assert raced is True
