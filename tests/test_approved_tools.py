"""Hostile checks for production Git executable and configuration admission."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from intraday_scanner import approved_tools


def _install_admitted_contract(
    monkeypatch: pytest.MonkeyPatch, repository: Path
) -> dict[str, object]:
    sha = "a" * 40
    contract: dict[str, object] = {
        "schema_version": "dawnstrike.exact_git_contract.v1",
        "root": os.path.normcase(os.path.abspath(repository)),
        "candidate_sha": sha,
        "candidate_tree": "b" * 40,
        "origin_url": "https://github.com/mattfren/DawnStrike.git",
        "origin_main_sha": sha,
        "git_executable_sha256": "c" * 64,
        "clean": True,
        "tracked_inventory": (
            ("100644", "1" * 40, "requirements.lock"),
            ("100644", "d" * 40, "web/index.html"),
            ("100644", "e" * 40, "web/favicon.svg"),
            ("100644", "f" * 40, "web/assets/app.js"),
        ),
        "release_authority_blobs": MappingProxyType({"requirements.lock": b"locked"}),
        "public_web_inventory": (
            ("100644", "d" * 40, "web/index.html"),
            ("100644", "e" * 40, "web/favicon.svg"),
            ("100644", "f" * 40, "web/assets/app.js"),
        ),
        "public_web_blobs": MappingProxyType(
            {
                "web/index.html": b"index",
                "web/favicon.svg": b"icon",
                "web/assets/app.js": b"asset",
            }
        ),
    }
    monkeypatch.setattr(
        sys,
        "_dawnstrike_exact_git_contract_v1",
        MappingProxyType(contract),
        raising=False,
    )
    monkeypatch.setenv("DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED", "1")
    return contract


def test_admitted_release_authority_is_served_without_a_path_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "runtime"
    _install_admitted_contract(monkeypatch, repository)

    assert approved_tools.read_admitted_release_bytes(repository, "requirements.lock") == b"locked"
    with pytest.raises(approved_tools.ApprovedToolError, match="not allowlisted"):
        approved_tools.read_admitted_release_bytes(repository, "vercel.json")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("rev-parse", "HEAD"), "a" * 40 + "\n"),
        (("rev-parse", "HEAD^{tree}"), "b" * 40 + "\n"),
        (("rev-parse", "a" * 40 + "^{tree}"), "b" * 40 + "\n"),
        (("status", "--porcelain", "--untracked-files=all"), ""),
        (
            (
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
            "",
        ),
        (("rev-parse", "refs/remotes/origin/main"), "a" * 40 + "\n"),
        (
            ("remote", "get-url", "origin"),
            "https://github.com/mattfren/DawnStrike.git\n",
        ),
    ],
)
def test_all_production_text_git_call_shapes_use_only_admitted_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: tuple[str, ...],
    expected: str,
) -> None:
    repository = tmp_path / "runtime"
    _install_admitted_contract(monkeypatch, repository)
    monkeypatch.setattr(
        approved_tools.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("post-admission Git subprocess attempted"),
    )

    completed = approved_tools.run_git(repository, *arguments)

    assert completed.stdout == expected


def test_all_production_byte_git_call_shapes_use_only_admitted_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "runtime"
    _install_admitted_contract(monkeypatch, repository)
    monkeypatch.setattr(
        approved_tools.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("post-admission Git subprocess attempted"),
    )

    inventory = approved_tools.read_git_bytes(
        repository,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        "a" * 40,
        "--",
        "web/assets",
    )
    blob = approved_tools.read_git_bytes(repository, "show", f"{'a' * 40}:web/assets/app.js")

    assert inventory == b"web/assets/app.js\0"
    assert blob == b"asset"


def test_admitted_git_contract_rejects_uncovered_command_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "runtime"
    _install_admitted_contract(monkeypatch, repository)
    monkeypatch.setattr(
        approved_tools.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("post-admission Git subprocess attempted"),
    )

    with pytest.raises(approved_tools.ApprovedToolError, match="not covered"):
        approved_tools.run_git(repository, "log", "-1")


def test_admitted_descendant_without_its_own_contract_never_falls_back_to_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "runtime"
    monkeypatch.delattr(sys, "_dawnstrike_exact_git_contract_v1", raising=False)
    monkeypatch.setenv("DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED", "1")
    monkeypatch.setattr(
        approved_tools.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("post-admission Git subprocess attempted"),
    )

    with pytest.raises(approved_tools.ApprovedToolError, match="admitted descendant"):
        approved_tools.run_git(repository, "rev-parse", "HEAD")


def test_sanitized_git_environment_binds_only_exact_repository_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    monkeypatch.setenv("git_dir", str(tmp_path / "hostile-git-dir"))
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(tmp_path / "hostile-objects"))

    environment = approved_tools.sanitized_git_environment(repository)
    git_environment = {
        key.upper(): value for key, value in environment.items() if key.upper().startswith("GIT_")
    }

    resolved = repository.resolve(strict=True)
    git_dir = str(resolved / ".git")
    assert git_environment == {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_DIR": git_dir,
        "GIT_COMMON_DIR": git_dir,
        "GIT_WORK_TREE": str(resolved),
    }


def test_run_git_accepts_only_platform_native_filemode(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git = str(approved_tools.approved_git_path())
    subprocess.run([git, "init", "-q", str(repository)], check=True)
    native = "false" if os.name == "nt" else "true"
    opposite = "true" if native == "false" else "false"

    subprocess.run(
        [git, "-C", str(repository), "config", "--local", "core.filemode", native],
        check=True,
    )
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "remote",
            "add",
            "origin",
            "https://github.com/mattfren/DawnStrike",
        ],
        check=True,
    )
    assert approved_tools.run_git(repository, "status", "--porcelain=v1").returncode == 0

    subprocess.run(
        [git, "-C", str(repository), "config", "--local", "core.filemode", opposite],
        check=True,
    )
    with pytest.raises(approved_tools.ApprovedToolError, match="core.filemode"):
        approved_tools.run_git(repository, "status", "--porcelain=v1")

    subprocess.run(
        [git, "-C", str(repository), "config", "--local", "core.filemode", native],
        check=True,
    )
    subprocess.run(
        [
            git,
            "-C",
            str(repository),
            "remote",
            "set-url",
            "origin",
            "https://github.com/attacker/DawnStrike.git",
        ],
        check=True,
    )
    with pytest.raises(approved_tools.ApprovedToolError, match="remote.origin.url"):
        approved_tools.run_git(repository, "status", "--porcelain=v1")


def test_run_git_accepts_only_disabled_local_gc(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git = str(approved_tools.approved_git_path())
    subprocess.run([git, "init", "-q", str(repository)], check=True)
    subprocess.run(
        [git, "-C", str(repository), "config", "--local", "gc.auto", "0"],
        check=True,
    )
    assert approved_tools.run_git(repository, "status", "--porcelain=v1").returncode == 0

    subprocess.run(
        [git, "-C", str(repository), "config", "--local", "gc.auto", "1"],
        check=True,
    )
    with pytest.raises(approved_tools.ApprovedToolError, match="gc.auto"):
        approved_tools.run_git(repository, "status", "--porcelain=v1")


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
    expected = {
        "scripts/build_public.py": {
            (
                "read_git_bytes",
                "'ls-tree'",
                "'-r'",
                "'--name-only'",
                "'-z'",
                "source_sha",
                "'--'",
                "'web/assets'",
            ),
            ("read_git_bytes", "'show'", "f'{source_sha}:{source_name}'"),
            ("run_git", "'rev-parse'", "'HEAD'"),
            ("run_git", "'status'", "'--porcelain'", "'--untracked-files=all'"),
        },
        "scripts/verify_daily_prepublication.py": {
            ("run_git", "'rev-parse'", "'HEAD'"),
        },
        "scripts/validate_web_source_config.py": {
            ("run_git", "'rev-parse'", "'HEAD'"),
        },
        "intraday_scanner/services/daily_run_service.py": {
            ("run_git", "'rev-parse'", "'HEAD'"),
        },
        "intraday_scanner/v2/paper_ops/universe_handoff.py": {
            ("run_git", "'rev-parse'", "'HEAD'"),
            (
                "run_git",
                "'status'",
                "'--porcelain=v1'",
                "'--untracked-files=all'",
                "'--ignore-submodules=none'",
            ),
        },
    }
    observed_by_file: dict[str, set[tuple[str, ...]]] = {}
    for path in sorted((*Path("scripts").glob("*.py"), *Path("intraday_scanner").rglob("*.py"))):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        observed = {
            (node.func.id, *(ast.unparse(argument) for argument in node.args[1:]))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"run_git", "read_git_bytes"}
        }
        if observed:
            observed_by_file[path.as_posix()] = observed
    assert observed_by_file == expected

    for relative in expected:
        source = Path(relative).read_text(encoding="utf-8")
        assert "subprocess.run" not in source
        assert "subprocess.Popen" not in source
        assert "subprocess.check_output" not in source


def test_every_direct_operational_git_spawn_is_in_an_explicit_boundary() -> None:
    expected = {
        ("scripts/capture_source_test_identity.py", "_checkout_blob_entries"): 1,
        ("scripts/capture_source_test_identity.py", "_git"): 1,
        ("scripts/capture_source_test_identity.py", "_head_blob_entries"): 1,
        ("scripts/run_detect_secrets_tracked.py", "tracked_files"): 1,
        ("scripts/dawnstrike_python_bootstrap.py", "_assert_exact_source_locked"): 2,
        ("scripts/dawnstrike_python_bootstrap.py", "_git_process"): 1,
        ("intraday_scanner/approved_tools.py", "_assert_local_git_config_safe"): 1,
        ("intraday_scanner/approved_tools.py", "read_git_bytes"): 1,
        ("intraday_scanner/approved_tools.py", "run_git"): 1,
        ("intraday_scanner/services/capture_operations.py", "_git_identity"): 3,
        ("intraday_scanner/services/scheduler_doctor_service.py", "_runtime_git_clean"): 6,
        ("intraday_scanner/services/scheduler_doctor_service.py", "_runtime_git_origin_sha"): 1,
        ("intraday_scanner/services/scheduler_doctor_service.py", "_runtime_git_value"): 1,
    }
    observed: dict[tuple[str, str], int] = {}

    class DirectGitSpawnVisitor(ast.NodeVisitor):
        function = "<module>"

        def __init__(self) -> None:
            self.git_command_names: list[set[str]] = [set()]

        @staticmethod
        def _contains_git_marker(node: ast.AST) -> bool:
            expression = ast.unparse(node).casefold()
            return any(
                marker in expression for marker in ("'git'", '"git"', "git_path", "approved_git")
            )

        def _is_git_command(self, node: ast.AST) -> bool:
            return (
                isinstance(node, ast.Name) and node.id in self.git_command_names[-1]
            ) or self._contains_git_marker(node)

        def _record_assignment(self, target: ast.AST, value: ast.AST) -> None:
            if not self._is_git_command(value):
                return
            for node in ast.walk(target):
                if isinstance(node, ast.Name):
                    self.git_command_names[-1].add(node.id)

        def visit_Assign(self, node: ast.Assign) -> None:
            for target in node.targets:
                self._record_assignment(target, node.value)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                self._record_assignment(node.target, node.value)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            previous = self.function
            self.function = node.name
            self.git_command_names.append(set())
            self.generic_visit(node)
            self.git_command_names.pop()
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
                and node.args
                and self._is_git_command(node.args[0])
            ):
                key = (relative, self.function)
                observed[key] = observed.get(key, 0) + 1
            self.generic_visit(node)

    paths = (
        Path("app.py"),
        *Path("scripts").glob("*.py"),
        *Path("intraday_scanner").rglob("*.py"),
    )
    for path in sorted(paths):
        relative = path.as_posix()
        DirectGitSpawnVisitor().visit(ast.parse(path.read_text(encoding="utf-8")))

    assert observed == expected
