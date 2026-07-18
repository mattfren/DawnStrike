from __future__ import annotations

from pathlib import Path

from intraday_scanner.runtime_paths import (
    operational_runtime_root,
    primary_checkout_root,
    runtime_artifact_path,
)


def test_primary_checkout_root_keeps_normal_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    assert primary_checkout_root(repo) == repo.resolve()


def test_primary_checkout_root_resolves_linked_worktree(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    worktree = tmp_path / "linked"
    git_dir = primary / ".git" / "worktrees" / "linked"
    git_dir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")

    assert primary_checkout_root(worktree) == primary.resolve()
    assert runtime_artifact_path(worktree, "data", "shadow_real.sqlite") == (
        primary / "data" / "shadow_real.sqlite"
    ).resolve()


def test_runtime_root_environment_override_wins(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    configured = tmp_path / "retained-runtime"

    resolved = operational_runtime_root(
        repo,
        environ={"DAWNSTRIKE_RUNTIME_ROOT": str(configured)},
    )

    assert resolved == configured.resolve()


def test_relative_runtime_root_override_is_repo_anchored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = operational_runtime_root(
        repo,
        environ={"DAWNSTRIKE_RUNTIME_ROOT": "../runtime"},
    )

    assert resolved == (repo / "../runtime").resolve()
