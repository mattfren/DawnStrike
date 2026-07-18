"""Resolve read-only operational data when the UI runs from a Git worktree.

Scheduled Dawnstrike jobs write their retained evidence in the primary checkout.
Release and review servers commonly run from linked worktrees, so cwd-relative
paths can otherwise select a new, empty runtime without raising an error.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

RUNTIME_ROOT_ENV = "DAWNSTRIKE_RUNTIME_ROOT"


def primary_checkout_root(repo_root: str | Path) -> Path:
    """Return the primary checkout for ``repo_root`` when it is a linked worktree."""

    root = Path(repo_root).expanduser().resolve()
    git_marker = root / ".git"
    if git_marker.is_dir() or not git_marker.is_file():
        return root

    try:
        marker = git_marker.read_text(encoding="utf-8").strip()
    except OSError:
        return root
    prefix = "gitdir:"
    if not marker.lower().startswith(prefix):
        return root

    raw_git_dir = marker[len(prefix) :].strip()
    git_dir = Path(raw_git_dir).expanduser()
    if not git_dir.is_absolute():
        git_dir = (root / git_dir).resolve()
    else:
        git_dir = git_dir.resolve()

    for candidate in (git_dir, *git_dir.parents):
        if candidate.name == ".git":
            primary = candidate.parent
            if primary.is_dir():
                return primary.resolve()
            break
    return root


def operational_runtime_root(
    repo_root: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the authoritative local runtime root for read-only UI consumers.

    An explicit ``DAWNSTRIKE_RUNTIME_ROOT`` always wins. Otherwise linked
    worktrees use their primary checkout, where the scheduled jobs retain the
    production SQLite database and PaperOps artifacts.
    """

    environment = os.environ if environ is None else environ
    root = Path(repo_root).expanduser().resolve()
    configured = str(environment.get(RUNTIME_ROOT_ENV) or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        return candidate.resolve()
    return primary_checkout_root(root)


def runtime_artifact_path(
    repo_root: str | Path,
    *parts: str,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return an artifact path below the authoritative operational root."""

    return operational_runtime_root(repo_root, environ=environ).joinpath(*parts)


__all__ = [
    "RUNTIME_ROOT_ENV",
    "operational_runtime_root",
    "primary_checkout_root",
    "runtime_artifact_path",
]
