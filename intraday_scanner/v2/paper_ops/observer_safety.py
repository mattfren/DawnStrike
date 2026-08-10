"""Fail-closed, non-mutating preflight for PaperOps observer commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PaperOpsObserverBlocked(RuntimeError):
    """An observer cannot safely inspect the requested PaperOps tree."""

    status: str
    detail: str

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}"


def require_observer_tree(output_root: Path) -> None:
    """Validate an existing tree without making directories, locks, or repairs."""

    root = Path(output_root)
    if not root.is_dir():
        raise PaperOpsObserverBlocked("MISSING_INPUT", f"PaperOps root does not exist: {root}")
    required = ("ledger", "state", "calendar", "manifests", "reconciliation")
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
