"""Explicit mutating application of PaperOps evidence governance."""

from __future__ import annotations

from pathlib import Path

from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
    _recover_pending_transaction,
)
from intraday_scanner.v2.paper_ops.storage import exclusive_file_lock
from intraday_scanner.v2.paper_ops.strategy_evidence import (
    _update_governance_overlay,
    score_strategy_evidence,
)


def apply_evidence_governance(
    *, output_root: Path = Path("data/v2_paper_ops")
) -> dict[str, object]:
    """Apply the current evidence score as a writer-only, idempotent overlay."""

    paths = PaperOpsPaths.create(output_root)
    with exclusive_file_lock(paths.state / ".paper_governance.lock"):
        _recover_pending_transaction(paths)
        result = score_strategy_evidence(output_root=output_root)
        _update_governance_overlay(paths, result.scores)
    return {"status": "applied", "score_count": len(result.scores)}
