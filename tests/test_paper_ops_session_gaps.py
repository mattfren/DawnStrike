from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.session_gaps import (
    SIGNING_KEY_ENV,
    load_forward_session_gaps,
    record_forward_session_gap,
)


@pytest.fixture(autouse=True)
def _gap_signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SIGNING_KEY_ENV, "test-only-" + ("gap-key-" * 8))


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
    assert (paths.state / "forward_session_gap_anchors.jsonl").is_file()


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


def test_terminal_gap_strict_schema_rejects_invented_return_even_if_rehashed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paths = PaperOpsPaths.create(root)
    record_forward_session_gap(
        output_root=root,
        market_date="2026-07-31",
        reason_code="scheduler_run_absent",
    )
    ledger = paths.state / "forward_session_gaps.jsonl"
    row = json.loads(ledger.read_text(encoding="utf-8"))
    row["daily_return_pct"] = 0
    row["record_id"] = _record_hash(row, id_field="record_id")
    ledger.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

    rows, errors = load_forward_session_gaps(paths)

    assert rows == []
    assert any("non-canonical fields" in error for error in errors)


def test_terminal_gap_chain_rewrite_is_detected_by_external_anchor(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths = PaperOpsPaths.create(root)
    for market_date in ("2026-07-31", "2026-08-03"):
        record_forward_session_gap(
            output_root=root,
            market_date=market_date,
            reason_code="scheduler_run_absent",
        )
    ledger = paths.state / "forward_session_gaps.jsonl"
    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    rows[0]["reason_code"] = "rewritten_reason"
    rows[0]["record_id"] = _record_hash(rows[0], id_field="record_id")
    rows[1]["previous_record_id"] = rows[0]["record_id"]
    rows[1]["record_id"] = _record_hash(rows[1], id_field="record_id")
    ledger.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    anchors_path = paths.state / "forward_session_gap_anchors.jsonl"
    anchors = [
        json.loads(line)
        for line in anchors_path.read_text(encoding="utf-8").splitlines()
    ]
    anchors[-1]["head_record_id"] = rows[-1]["record_id"]
    anchors[-1]["ledger_sha256"] = hashlib.sha256(ledger.read_bytes()).hexdigest()
    anchor_canonical = {
        key: value
        for key, value in anchors[-1].items()
        if key not in {"anchor_id", "signature_hmac_sha256"}
    }
    anchors[-1]["anchor_id"] = hashlib.sha256(
        json.dumps(anchor_canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    anchors_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in anchors),
        encoding="utf-8",
    )

    accepted, errors = load_forward_session_gaps(paths)

    assert accepted == []
    assert any("signature_hmac_sha256 mismatch" in error for error in errors)


def test_terminal_gap_requires_independent_signing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SIGNING_KEY_ENV)

    with pytest.raises(ValueError, match=SIGNING_KEY_ENV):
        record_forward_session_gap(
            output_root=tmp_path / "paper_ops",
            market_date="2026-07-31",
            reason_code="scheduler_run_absent",
        )


def test_terminal_gap_anchor_tampering_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths = PaperOpsPaths.create(root)
    record_forward_session_gap(
        output_root=root,
        market_date="2026-07-31",
        reason_code="scheduler_run_absent",
    )
    anchors = paths.state / "forward_session_gap_anchors.jsonl"
    row = json.loads(anchors.read_text(encoding="utf-8"))
    row["ledger_sha256"] = "0" * 64
    anchors.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")

    accepted, errors = load_forward_session_gaps(paths)

    assert accepted == []
    assert any("anchor_id integrity mismatch" in error for error in errors)


def _record_hash(row: dict[str, object], *, id_field: str) -> str:
    canonical = {key: value for key, value in row.items() if key != id_field}
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
