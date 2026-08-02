from __future__ import annotations

from intraday_scanner.services.alpha_v6_learning_service import (
    run_alpha_v6_daily_monitor,
    run_alpha_v6_weekly_training,
)
from intraday_scanner.services.alpha_v6_research_service import (
    build_alpha_v6_research_packet,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_daily_v6_monitor_never_refits_or_writes_model_evaluation(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")

    result = run_alpha_v6_daily_monitor(store, market_date="2026-08-01")
    repeated = run_alpha_v6_daily_monitor(store, market_date="2026-08-01")

    assert result["model_refit_performed"] is False
    assert result["challenger_evaluation_performed"] is False
    assert store.load_alpha_v6_model_runs() == []
    assert store.load_alpha_v6_evaluations() == []
    receipts = store.load_alpha_v6_operational_receipts(receipt_kind="daily_monitor")
    assert len(receipts) == 1
    assert receipts[0]["as_of_date"]
    assert (
        result["operational_receipt"]["receipt_id"]
        == repeated["operational_receipt"]["receipt_id"]
    )
    assert repeated["operational_receipt"]["inserted"] is False


def test_weekly_v6_training_is_the_only_refit_path(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")

    result = run_alpha_v6_weekly_training(store, code_sha="c" * 40)

    assert result["model_refit_performed"] is True
    assert len(store.load_alpha_v6_model_runs()) == 1
    assert len(store.load_alpha_v6_evaluations()) == 1
    assert len(store.load_alpha_v6_operational_receipts(receipt_kind="weekly_training")) == 1


def test_research_packet_reads_persisted_evidence_without_triggering_training(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")

    packet = build_alpha_v6_research_packet(store, code_sha="c" * 40)

    assert packet["latest_training"] is None
    assert packet["latest_evaluation"] is None
    assert packet["failure_attribution"]["experiment_persistence"]["persistence_skipped"]
    assert store.load_alpha_v6_model_runs() == []
    assert store.load_alpha_v6_evaluations() == []
