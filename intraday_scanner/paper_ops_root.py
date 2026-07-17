"""Canonical production-root selection for PaperOps artifact consumers.

PaperOps itself keeps explicit ``output_root`` injection so replay, tests, and
other isolated runs never mutate production state by accident.  Read-only
production consumers use this module to agree on the live artifact tree.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

PAPER_OPS_ROOT_ENV = "DAWNSTRIKE_PAPER_OPS_ROOT"
DEFAULT_PAPER_OPS_PRODUCTION_ROOT = Path("data/v2_paper_ops_live")
LEGACY_PAPER_OPS_ROOT = Path("data/v2_paper_ops")


def production_paper_ops_root(
    *,
    repo_root: str | Path | None = None,
    override: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Return the configured PaperOps production root.

    Precedence is explicit ``override``, ``DAWNSTRIKE_PAPER_OPS_ROOT``, then
    ``data/v2_paper_ops_live``.  Relative values are anchored to ``repo_root``
    when one is supplied.  Callers that already accept a custom root should
    pass it as ``override``; this keeps tests and isolated research runs fully
    injectable.
    """

    environment = os.environ if environ is None else environ
    configured = override
    if configured is None:
        configured = environment.get(PAPER_OPS_ROOT_ENV) or DEFAULT_PAPER_OPS_PRODUCTION_ROOT
    raw = str(configured).strip()
    if not raw:
        raise ValueError("PaperOps root override must not be blank.")
    root = Path(raw).expanduser()
    if repo_root is not None and not root.is_absolute():
        root = Path(repo_root) / root
    return root


def paper_ops_artifact_path(
    *parts: str,
    repo_root: str | Path | None = None,
    override: str | Path | None = None,
) -> Path:
    """Return a path below the configured production root."""

    return production_paper_ops_root(repo_root=repo_root, override=override).joinpath(*parts)
