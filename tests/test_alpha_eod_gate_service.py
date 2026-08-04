from __future__ import annotations

import json
from pathlib import Path

from intraday_scanner.alpha.v5_policy import alphaops_strategy_contract
from intraday_scanner.services.alpha_eod_gate_service import evaluate_alpha_eod_gate
from intraday_scanner.services.alpha_official_cohort_service import (
    build_official_cohort_row,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DAY = "2026-08-04"


def test_exact_delivered_official_no_trade_skips_outcome_stages(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _freeze_selection(store, ticker="NO_TRADE", decision="no_trade")
    capture, gap = _artifacts(tmp_path, gap_status="DEGRADED", eligible=5, missing=2)

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
    assert result["official_membership_sha256"]
    assert result["missing_truth_is_zero"] is False
    assert result["warnings"]


def test_orphan_no_trade_selection_cannot_authorize_skip(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    row = _selection(ticker="NO_TRADE", decision="no_trade")
    store.persist_signal_selections([row])
    capture, gap = _artifacts(tmp_path)

    result = _evaluate(db_path, capture, gap)

    assert result["status"] == "BLOCKED"
    assert any("lacks Telegram delivery proof" in error for error in result["errors"])


def test_missing_official_cohort_never_becomes_no_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    capture, gap = _artifacts(tmp_path)

    result = _evaluate(db_path, capture, gap)

    assert result["status"] == "BLOCKED"
    assert any("cohort is absent" in error for error in result["errors"])


def test_official_signal_requires_its_exact_signal_id_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _freeze_selection(store, ticker="ABCD", decision="clean_edge")
    _persist_outcome(store, signal_id="different-signal-ABCD", ticker="ABCD")
    capture, gap = _artifacts(tmp_path, gap_status="COMPLETE", eligible=1, missing=0)

    result = _evaluate(db_path, capture, gap, capture_exit_code=0)

    assert result["status"] == "BLOCKED"
    assert any("requires one exact sourced outcome" in error for error in result["errors"])


def test_exact_official_signal_outcome_passes_even_if_shadow_gap_is_incomplete(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    row = _freeze_selection(store, ticker="ABCD", decision="clean_edge")
    _persist_outcome(store, signal_id=str(row["signal_id"]), ticker="ABCD")
    capture, gap = _artifacts(tmp_path, gap_status="DEGRADED", eligible=4, missing=3)

    result = _evaluate(db_path, capture, gap)

    assert result["status"] == "COMPLETE"
    assert result["official_outcomes_required"] is True
    assert result["exact_outcome_signal_ids"] == [row["signal_id"]]
    assert result["warnings"]


def test_exact_outcome_without_source_coverage_stays_blocked(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    row = _freeze_selection(store, ticker="ABCD", decision="clean_edge")
    _persist_outcome(
        store,
        signal_id=str(row["signal_id"]),
        ticker="ABCD",
        source_coverage_complete=False,
    )
    capture, gap = _artifacts(tmp_path, gap_status="COMPLETE", eligible=1, missing=0)

    result = _evaluate(db_path, capture, gap)

    assert result["status"] == "BLOCKED"
    assert any("complete source coverage" in error for error in result["errors"])


def _evaluate(
    db_path: Path,
    capture: Path,
    gap: Path,
    *,
    capture_exit_code: int = 2,
) -> dict[str, object]:
    return evaluate_alpha_eod_gate(
        db_path=db_path,
        market_date=DAY,
        capture_exit_code=capture_exit_code,
        capture_result_path=capture,
        outcome_gap_path=gap,
    )


def _freeze_selection(
    store: SQLiteScanStore,
    *,
    ticker: str,
    decision: str,
) -> dict[str, object]:
    row = _selection(ticker=ticker, decision=decision)
    cohort = build_official_cohort_row(
        market_date=DAY,
        strategy_id=str(row["strategy_id"]),
        strategy_version=str(row["strategy_version"]),
        scan_id=str(row["scan_id"]),
        event_key=str(row["event_key"]),
        body_sha256=str(row["body_sha256"]),
        claimed_at=str(row["selected_at"]),
        selections=[row],
    )
    store.persist_official_signal_cohort(cohort, [row])
    store.persist_notification_deliveries(
        [
            {
                "membership_id": f"delivery-{ticker}",
                "selection_id": row["selection_id"],
                "scan_id": row["scan_id"],
                "signal_id": row["signal_id"],
                "ticker": ticker,
                "strategy_id": row["strategy_id"],
                "strategy_version": row["strategy_version"],
                "cohort": row["cohort"],
                "decision": decision,
                "selected_at": row["selected_at"],
                "event_key": row["event_key"],
                "channel": "telegram",
                "delivery_status": "delivered",
                "attempted_at": f"{DAY}T13:01:00Z",
                "delivered_at": f"{DAY}T13:01:00Z",
                "body_sha256": row["body_sha256"],
            }
        ]
    )
    return row


def _selection(*, ticker: str, decision: str) -> dict[str, object]:
    strategy_id, strategy_version = alphaops_strategy_contract(
        f"{DAY}T12:00:00-04:00"
    )
    return {
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


def _persist_outcome(
    store: SQLiteScanStore,
    *,
    signal_id: str,
    ticker: str,
    source_coverage_complete: bool = True,
) -> None:
    store.persist_signal_outcomes(
        [
            {
                "signal_id": signal_id,
                "market_date": DAY,
                "ticker": ticker,
                "outcome_source": "alpaca",
                "outcome_status": "not_triggered",
                "validated_against_signal_timestamp": True,
                "automatic_sourced_data": True,
                "source_coverage_complete": source_coverage_complete,
                "no_lookahead": True,
                "research_only": True,
                "broker_execution_enabled": False,
                "source_bar_hash_sha256": "a" * 64,
            }
        ]
    )


def _artifacts(
    tmp_path: Path,
    *,
    gap_status: str = "NO_ELIGIBLE",
    eligible: int = 0,
    missing: int = 0,
) -> tuple[Path, Path]:
    capture = _write_json(
        tmp_path / "capture.json",
        {
            "market_date": DAY,
            "status": "partial",
            "missing_values_are_zero": False,
            "research_only": True,
            "broker_execution_enabled": False,
        },
    )
    gap = _write_json(
        tmp_path / "gap.json",
        {
            "market_date": DAY,
            "status": gap_status,
            "eligible_candidate_count": eligible,
            "missing_outcome_count": missing,
            "missing_truth_is_zero": False,
            "research_only": True,
            "broker_execution_enabled": False,
        },
    )
    return capture, gap


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
