"""Fail-closed, non-mutating preflight for PaperOps observer commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class PaperOpsObserverBlocked(RuntimeError):
    """An observer cannot safely inspect the requested PaperOps tree."""

    status: str
    detail: str

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}"


def require_observer_tree(
    output_root: Path,
    *,
    required_files: tuple[str, ...] = (),
    nonempty_files: tuple[str, ...] = (),
) -> None:
    """Validate an existing tree without making directories, locks, or repairs."""

    root = Path(output_root)
    if not root.is_dir():
        raise PaperOpsObserverBlocked("MISSING_INPUT", f"PaperOps root does not exist: {root}")
    required = (
        "ledger",
        "state",
        "calendar",
        "reports",
        "manifests",
        "logs",
        "exports",
        "reconciliation",
    )
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise PaperOpsObserverBlocked(
            "MISSING_INPUT",
            f"PaperOps tree is incomplete; missing directories: {', '.join(missing)}",
        )
    journal = root / "state" / "paper_transaction_pending.json"
    if journal.exists():
        raise PaperOpsObserverBlocked(
            "BLOCKED_PENDING_RECOVERY",
            f"Pending transaction journal retained for explicit writer recovery: {journal}",
        )
    for relative in required_files:
        if not (root / relative).is_file():
            raise PaperOpsObserverBlocked(
                "MISSING_INPUT", f"Required PaperOps input is absent: {relative}"
            )
    for relative in nonempty_files:
        path = root / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise PaperOpsObserverBlocked(
                "MISSING_INPUT", f"Required PaperOps input is empty: {relative}"
            )
