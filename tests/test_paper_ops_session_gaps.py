from __future__ import annotations

import json
from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.session_gaps import (
    load_forward_session_gaps,
    record_forward_session_gap,
)


def test_terminal_forward_gap_is_append_only_idempotent_and_never_zero(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paths = PaperOpsPaths.create(root)

    first = record_forward_session_gap(
        output_root=root,
        market_date="2026-07-31",
        reason_code="scheduler_run_absent",
    )
    second = record_forward_session_gap(
        output_root=root,
        market_date="2026-07-31",
        reason_code="scheduler_run_absent",
    )
    rows, errors = load_forward_session_gaps(paths)

    assert first["status"] == "recorded"
    assert second["status"] == "already_recorded"
    assert errors == []
    assert len(rows) == 1
    assert rows[0]["status"] == "TERMINAL_MISSING"
    assert rows[0]["missing_truth_is_zero"] is False
    assert "return" not in rows[0]


def test_terminal_forward_gap_rejects_any_existing_session_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paths = PaperOpsPaths.create(root)
    (paths.calendar / "strategy_daily_returns.csv").write_text(
        "date,mode,strategy_id\n2026-07-31,forward,alpha\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="forward evidence exists: calendar rows"):
        record_forward_session_gap(
            output_root=root,
            market_date="2026-07-31",
            reason_code="scheduler_run_absent",
        )


def test_terminal_forward_gap_integrity_tampering_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths = PaperOpsPaths.create(root)
    record_forward_session_gap(
        output_root=root,
        market_date="2026-07-31",
        reason_code="scheduler_run_absent",
    )
    ledger = paths.state / "forward_session_gaps.jsonl"
    row = json.loads(ledger.read_text(encoding="utf-8"))
    row["reason_code"] = "tampered"
    ledger.write_text(json.dumps(row) + "\n", encoding="utf-8")

    rows, errors = load_forward_session_gaps(paths)

    assert rows == []
    assert any("record_id integrity mismatch" in error for error in errors)
