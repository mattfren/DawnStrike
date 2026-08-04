from __future__ import annotations

import json
from pathlib import Path

from intraday_scanner.alpha.v5_policy import alphaops_strategy_contract
from intraday_scanner.services.alpha_eod_gate_service import evaluate_alpha_eod_gate
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DAY = "2026-08-04"


def test_explicit_official_no_trade_skips_outcome_dependent_stages(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selection(store, ticker="NO_TRADE", decision="no_trade")
    capture = _write_json(
        tmp_path / "capture.json",
        _capture_payload(status="partial"),
    )
    gap = _write_json(
        tmp_path / "gap.json",
        _gap_payload(status="NO_ELIGIBLE", eligible=0, missing=0),
    )

    result = evaluate_alpha_eod_gate(
        db_path=db_path,
        market_date=DAY,
        capture_exit_code=2,
        capture_result_path=capture,
        outcome_gap_path=gap,
    )

    assert result["status"] == "NO_ELIGIBLE"
    assert result["reason_code"] == "official_no_trade"
    assert result["official_outcomes_required"] is False
    assert result["capture_exit_code"] == 2
    assert result["missing_truth_is_zero"] is False


def test_missing_official_selection_never_becomes_no_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    capture = _write_json(tmp_path / "capture.json", _capture_payload(status="no_targets"))
    gap = _write_json(
        tmp_path / "gap.json",
        _gap_payload(status="NO_ELIGIBLE", eligible=0, missing=0),
    )

    result = evaluate_alpha_eod_gate(
        db_path=db_path,
        market_date=DAY,
        capture_exit_code=0,
        capture_result_path=capture,
        outcome_gap_path=gap,
    )

    assert result["status"] == "BLOCKED"
    assert "selection evidence is absent" in result["errors"][0]


def test_official_signal_requires_complete_capture_and_gap(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selection(store, ticker="ABCD", decision="clean_edge")
    capture = _write_json(tmp_path / "capture.json", _capture_payload(status="complete"))
    gap = _write_json(
        tmp_path / "gap.json",
        _gap_payload(status="COMPLETE", eligible=1, missing=0),
    )

    result = evaluate_alpha_eod_gate(
        db_path=db_path,
        market_date=DAY,
        capture_exit_code=2,
        capture_result_path=capture,
        outcome_gap_path=gap,
    )

    assert result["status"] == "COMPLETE"
    assert result["official_outcomes_required"] is True
    assert result["official_signal_count"] == 1
    assert result["warnings"]


def test_official_signal_with_terminal_gap_stays_blocked(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selection(store, ticker="ABCD", decision="clean_edge")
    capture = _write_json(tmp_path / "capture.json", _capture_payload(status="partial"))
    gap = _write_json(
        tmp_path / "gap.json",
        _gap_payload(status="DEGRADED", eligible=1, missing=1),
    )

    result = evaluate_alpha_eod_gate(
        db_path=db_path,
        market_date=DAY,
        capture_exit_code=2,
        capture_result_path=capture,
        outcome_gap_path=gap,
    )

    assert result["status"] == "BLOCKED"
    assert result["official_outcomes_required"] is True
    assert any("outcomes are not complete" in error for error in result["errors"])


def _persist_selection(
    store: SQLiteScanStore,
    *,
    ticker: str,
    decision: str,
) -> None:
    strategy_id, strategy_version = alphaops_strategy_contract(
        f"{DAY}T12:00:00-04:00"
    )
    store.persist_signal_selections(
        [
            {
                "selection_id": f"selection-{ticker}",
                "scan_id": "scan-eod-gate",
                "signal_id": f"signal-{ticker}",
                "ticker": ticker,
                "rank": 0 if ticker == "NO_TRADE" else 1,
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "cohort": "official_telegram",
                "decision": decision,
                "selected_at": f"{DAY}T13:00:00Z",
                "event_key": "alphaops:eod-gate",
                "body_sha256": "eod-gate-body",
            }
        ]
    )


def _capture_payload(*, status: str) -> dict[str, object]:
    return {
        "market_date": DAY,
        "status": status,
        "missing_values_are_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _gap_payload(
    *,
    status: str,
    eligible: int,
    missing: int,
) -> dict[str, object]:
    return {
        "market_date": DAY,
        "status": status,
        "eligible_candidate_count": eligible,
        "missing_outcome_count": missing,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
