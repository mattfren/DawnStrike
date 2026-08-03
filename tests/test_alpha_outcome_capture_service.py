from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.alpha.outcome_labeler import label_outcomes
from intraday_scanner.alpha.v5_policy import alphaops_strategy_contract
from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import DataProviderError, SnapshotValidationError
from intraday_scanner.services.alpha_outcome_capture_service import (
    capture_sourced_alpha_outcomes,
)
from intraday_scanner.services.alpha_paper_reconciliation_service import (
    reconcile_alpha_paper_trades,
)
from intraday_scanner.services.learning_service import run_alpha_learning
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

EASTERN = ZoneInfo("America/New_York")
DAY = "2026-07-13"


def test_sourced_eod_capture_is_trigger_aware_persisted_and_learning_ready(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outcomes.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    payload = _chart_payload(_contiguous_bars(overrides={
        "09:30": (9.80, 9.95, 9.75, 9.90),
        "09:31": (9.90, 10.15, 9.88, 10.10),
        "09:32": (10.10, 10.25, 10.05, 10.20),
        "09:36": (10.20, 10.35, 10.15, 10.30),
        "09:46": (10.30, 10.45, 10.25, 10.40),
        "12:00": (10.40, 10.55, 10.35, 10.50),
        "15:59": (10.50, 10.65, 10.45, 10.60),
    }))

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        persist=True,
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: payload,
    )

    assert result["status"] == "complete"
    assert result["learning_eligible_count"] == 1
    outcome = result["outcomes"][0]
    assert outcome["entry_time"] == "2026-07-13T13:31:00Z"
    assert outcome["entry_price"] == 10.0
    assert outcome["price_1m"] == 10.2
    assert outcome["price_5m"] == 10.3
    assert outcome["price_15m"] == 10.4
    assert outcome["lunch_price"] == 10.5
    assert outcome["close_price"] == 10.6
    assert outcome["high_after_entry"] == 10.65
    assert outcome["low_after_entry"] == 9.88
    assert outcome["planned_first_touch_outcome"] == "target_1"
    assert outcome["source_bar_hash_sha256"]
    assert outcome["no_lookahead"] is True
    assert outcome["broker_execution_enabled"] is False
    assert outcome["source_coverage_complete"] is True
    assert outcome["coverage_expected_minute_count"] == 390
    assert outcome["coverage_observed_minute_count"] == 390

    stored = store.load_signal_outcomes(signal_id="signal-1")
    assert len(stored) == 1
    assert stored[0]["outcome_status"] == "complete_sourced"
    events = store.load_signal_events(signal_id="signal-1")
    assert any(row["event_type"] == "OUTCOME_CAPTURED" for row in events)

    reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reconciliation",
        config=ScannerConfig(),
    )
    learning = run_alpha_learning(store)
    assert learning["status"] == "complete"
    assert learning["labels_created"] == 1
    assert learning["sourced_outcomes_considered"] == 1
    label = store.load_alpha_outcome_labels()[0]
    assert label["planned_first_touch_outcome"] == "target_1"
    assert label["outcome_source"] == "yahoo_finance_chart"
    assert label["learning_eligible"] is True
    assert run_alpha_learning(store)["status"] == "no_new_eligible_outcomes"

    artifact = json.loads(
        (tmp_path / "capture" / "alpha_outcome_capture.json").read_text("utf-8")
    )
    bars = json.loads(
        (tmp_path / "capture" / "alpha_outcome_source_bars.json").read_text("utf-8")
    )
    assert artifact["missing_values_are_zero"] is False
    assert artifact["source_requests"][0]["source_bar_hash_sha256"]
    assert len(bars["NOVA"]) == 390


def test_sourced_capture_refuses_to_infer_an_outcome_before_close(tmp_path: Path) -> None:
    db_path = tmp_path / "early.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    called = False

    def unexpected_fetch(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T15:59:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=unexpected_fetch,
    )

    assert result["status"] == "session_incomplete"
    assert called is False
    assert store.load_signal_outcomes() == []


def test_sourced_capture_fails_closed_without_exact_session_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "missing-selection.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_historical_signals([_signal()])

    with pytest.raises(SnapshotValidationError, match="selection evidence is absent"):
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            config=ScannerConfig(),
            fetcher=lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
        )


def test_sourced_capture_fails_closed_on_partially_persisted_selection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "partial-selection.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_signal_selections(
        [
            {
                "selection_id": "selection-missing-signal",
                "scan_id": "scan-partial",
                "signal_id": "missing-historical-signal",
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": "alphaops_v4",
                "strategy_version": "dawnstrike-alphaops-v4",
                "cohort": "official_telegram",
                "decision": "clean_edge",
                "selected_at": f"{DAY}T13:00:00Z",
                "event_key": "alphaops:partial:alpha_morning_watch",
                "body_sha256": "partial-body-hash",
            }
        ]
    )

    with pytest.raises(SnapshotValidationError, match="partially persisted"):
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            config=ScannerConfig(),
        )


def test_sourced_capture_allows_explicit_recorded_no_trade(tmp_path: Path) -> None:
    db_path = tmp_path / "no-trade.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_signal_selections(
        [
            {
                "selection_id": "selection-no-trade",
                "scan_id": "scan-no-trade",
                "signal_id": f"no_trade:{DAY}",
                "ticker": "NO_TRADE",
                "rank": 0,
                "strategy_id": "alphaops_v4",
                "strategy_version": "dawnstrike-alphaops-v4",
                "cohort": "official_telegram",
                "decision": "no_trade",
                "selected_at": f"{DAY}T13:00:00Z",
                "event_key": "alphaops:no-trade:alpha_no_trade",
                "body_sha256": "no-trade-body-hash",
            }
        ]
    )

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
    )

    assert result["status"] == "no_targets"
    assert result["signal_count"] == 0


def test_verified_not_triggered_is_persisted_but_never_learned(tmp_path: Path) -> None:
    db_path = tmp_path / "not-triggered.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    payload = _chart_payload(_contiguous_bars(
        default=(9.50, 9.80, 9.40, 9.55),
        overrides={"15:59": (9.65, 9.80, 9.60, 9.75)},
    ))

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: payload,
    )

    outcome = store.load_signal_outcomes()[0]
    assert result["not_triggered_count"] == 1
    assert outcome["outcome_status"] == "not_triggered"
    assert outcome["learning_eligible"] is False
    assert outcome["entry_price"] is None
    assert outcome["close_price"] is None
    learning = run_alpha_learning(store)
    assert learning["status"] == "complete"
    assert learning["sourced_outcomes_considered"] == 0
    assert learning["return_learning_eligible"] is False


def test_missing_source_bar_truth_remains_unresolved_and_null(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    payload = _chart_payload(_contiguous_bars(overrides={
        "09:30": (9.80, None, 9.75, 9.90),
        "15:59": (10.10, 10.20, 10.00, 10.15),
    }))

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: payload,
    )

    assert result["status"] == "partial"
    assert result["ineligible_count"] == 1
    assert result["diagnostics"][0]["status"] == "ineligible_incomplete_source_bars"
    assert store.load_signal_outcomes() == []
    assert result["required_stage_failed"] is True
    assert result["capture_attempts"]["terminal_missing_count"] == 1
    attempts = store.load_outcome_capture_attempts(market_date=DAY)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "terminal_missing"
    assert attempts[0]["learning_eligible"] is False
    assert attempts[0]["error_code"] == "ineligible_incomplete_source_bars"


def test_bounded_secondary_provider_fallback_captures_full_attribution(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "fallback.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    rows = _contiguous_bars(overrides={
        "09:30": (9.80, 9.95, 9.75, 9.90),
        "09:31": (9.90, 10.15, 9.88, 10.10),
        "09:32": (10.10, 10.25, 10.05, 10.20),
        "15:59": (10.20, 10.40, 10.15, 10.30),
    })
    primary_calls = 0

    def unavailable_primary(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal primary_calls
        primary_calls += 1
        raise DataProviderError("primary unavailable")

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=unavailable_primary,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
        provider_attempt_limit=2,
    )

    assert result["status"] == "complete"
    assert primary_calls == 6  # two bounded attempts for NOVA, SPY, and IWM
    outcome = result["outcomes"][0]
    assert outcome["outcome_source"] == "alpaca_market_data_iex"
    assert outcome["source_lineage"][0]["status"] == "provider_error"
    assert outcome["source_lineage"][-1]["status"] == "ok"
    assert outcome["entry_opportunity"] is True
    assert outcome["fill_status"] == "modeled_legacy_trigger"
    assert outcome["exit_reason"] == "target_1"
    assert outcome["holding_duration_minutes"] == 1
    assert outcome["max_favorable_excursion_pct"] == 4.0
    assert outcome["max_adverse_excursion_pct"] == -1.2
    assert outcome["time_to_mfe_minutes"] == 388
    assert outcome["time_to_mae_minutes"] == 0
    assert outcome["benchmark_return_pct"] is not None
    assert outcome["secondary_benchmark_return_pct"] is not None
    assert outcome["excess_return_pct"] is not None
    assert outcome["attribution_complete"] is True
    assert result["required_stage_failed"] is False


def test_alpaca_first_mode_preserves_yahoo_as_secondary_reconciliation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpaca-first.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    rows = _contiguous_bars(overrides={
        "09:30": (9.80, 9.95, 9.75, 9.90),
        "09:31": (9.90, 10.15, 9.88, 10.10),
        "15:59": (10.20, 10.40, 10.15, 10.30),
    })
    yahoo_calls = 0

    def unavailable_yahoo(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal yahoo_calls
        yahoo_calls += 1
        raise DataProviderError("secondary unavailable")

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(
            alpaca_api_key_id="test-key-id",
            alpaca_api_secret_key="test-secret",
            outcome_capture_provider_order="alpaca,yahoo",
        ),
        fetcher=unavailable_yahoo,
        fallback_fetcher=lambda *_args, **_kwargs: rows,
        provider_attempt_limit=2,
    )

    assert result["status"] == "complete"
    assert yahoo_calls == 6  # Two bounded reconciliation attempts for NOVA, SPY, and IWM.
    outcome = result["outcomes"][0]
    assert outcome["outcome_source"] == "alpaca_market_data_iex"
    assert outcome["source_lineage"][0]["source"] == "alpaca_market_data_iex"
    assert outcome["source_lineage"][-1]["source"] == "yahoo_finance_chart"
    assert outcome["source_lineage"][-1]["status"] == "provider_error"
    assert outcome["source_lineage"][0]["source_coverage_complete"] is True


def test_v5_complete_candidate_without_allowed_entry_is_recorded_as_non_fill(
    tmp_path: Path,
) -> None:
    day = "2026-07-31"
    db_path = tmp_path / "v5-non-fill.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal(day)])

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=day,
        requested_at=f"{day}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(
            _contiguous_bars(
                day=day,
                overrides={
                    "09:31": (9.90, 10.15, 9.88, 10.10),
                    "09:32": (10.10, 10.25, 10.05, 10.20),
                },
            )
        ),
    )

    outcome = result["outcomes"][0]
    assert outcome["strategy_id"] == "alphaops_v5"
    assert outcome["fill_status"] == "not_filled_official_policy"
    assert outcome["non_fill_reason"] == "no_eligible_enter_long_intent"
    assert outcome["learning_eligible"] is False
    assert outcome["modeled_fees"] is None
    assert result["required_stage_failed"] is False


def test_outcome_matching_does_not_reuse_a_ticker_outcome_across_scans() -> None:
    signals = [
        {"signal_id": "a", "scan_id": "scan-a", "ticker": "NOVA"},
        {"signal_id": "b", "scan_id": "scan-b", "ticker": "NOVA"},
    ]
    outcomes = [{
        "signal_id": "b",
        "scan_id": "scan-b",
        "ticker": "NOVA",
        "entry_price": 10.0,
        "high_after_entry": 11.0,
        "low_after_entry": 9.5,
        "close_price": 10.5,
        "source": "test_source",
    }]

    labels = label_outcomes(signals, outcomes)

    assert [row["signal_id"] for row in labels] == ["b"]


def test_sourced_capture_uses_published_early_close(tmp_path: Path) -> None:
    early_day = "2026-11-27"
    db_path = tmp_path / "early-close.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal(early_day)])
    payload = _chart_payload(_contiguous_bars(
        day=early_day,
        close_clock="12:59",
        overrides={
            "09:30": (9.95, 10.10, 9.90, 10.05),
            "12:59": (10.05, 10.30, 10.00, 10.25),
        },
    ))

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=early_day,
        requested_at=f"{early_day}T13:05:00-05:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: payload,
    )

    assert result["status"] == "complete"
    assert result["market_session"]["status"] == "early_close"
    assert result["market_session"]["close_time_et"] == "13:00"
    assert result["outcomes"][0]["close_price_observed_at"] == "2026-11-27T17:59:00Z"


def test_sparse_or_gapped_bars_never_become_conclusive(tmp_path: Path) -> None:
    complete = _contiguous_bars()
    cases = {
        "sparse": [complete[0], complete[150], complete[-1]],
        "missing_start": complete[1:],
        "missing_middle": complete[:100] + complete[101:],
        "missing_final": complete[:-1],
    }
    expected = {
        "sparse": "ineligible_bar_gap",
        "missing_start": "ineligible_missing_start_bar",
        "missing_middle": "ineligible_bar_gap",
        "missing_final": "ineligible_missing_final_bar",
    }
    for name, bars in cases.items():
        db_path = tmp_path / f"{name}.sqlite"
        store = SQLiteScanStore(db_path)
        _persist_selected_signals(store, [_signal()])
        result = capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / name,
            config=ScannerConfig(),
            fetcher=lambda *_args, _bars=bars, **_kwargs: _chart_payload(_bars),
        )

        assert result["status"] == "partial"
        assert result["diagnostics"][0]["status"] == expected[name]
        assert result["not_triggered_count"] == 0
        assert store.load_signal_outcomes() == []


def test_malformed_or_naive_recommendation_timestamp_is_ineligible_not_exception(
    tmp_path: Path,
) -> None:
    for name, generated_at in {
        "malformed": "not-a-timestamp",
        "timezone_naive": f"{DAY}T10:00:00",
    }.items():
        db_path = tmp_path / f"{name}.sqlite"
        store = SQLiteScanStore(db_path)
        signal = {**_signal(), "generated_at": generated_at}
        _persist_selected_signals(store, [signal])

        result = capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / name,
            config=ScannerConfig(),
            fetcher=lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
        )

        assert result["status"] == "partial"
        assert result["diagnostics"][0]["status"] == (
            "ineligible_missing_recommendation_timestamp"
        )
        assert store.load_signal_outcomes() == []


def test_malformed_ohlc_never_becomes_conclusive(tmp_path: Path) -> None:
    db_path = tmp_path / "malformed-ohlc.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    bars = _contiguous_bars(overrides={
        "10:15": (9.90, 9.80, 9.85, 9.88),
    })

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(bars),
    )

    assert result["status"] == "partial"
    assert result["diagnostics"][0]["status"] == "ineligible_malformed_ohlc"
    assert store.load_signal_outcomes() == []


def test_all_eligible_same_ticker_signals_are_captured(tmp_path: Path) -> None:
    db_path = tmp_path / "multiple-signals.sqlite"
    store = SQLiteScanStore(db_path)
    second = {
        **_signal(),
        "signal_id": "signal-2",
        "scan_id": "scan-2",
        "alpha_signal_id": "alpha-2",
        "generated_at": f"{DAY}T13:45:00Z",
    }
    _persist_selected_signals(store, [_signal(), second])

    result = capture_sourced_alpha_outcomes(
        db_path=db_path,
        market_date=DAY,
        requested_at=f"{DAY}T16:05:00-04:00",
        out_dir=tmp_path / "capture",
        config=ScannerConfig(),
        fetcher=lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
    )

    assert result["signal_count"] == 2
    assert {row["signal_id"] for row in result["outcomes"]} == {
        "signal-1",
        "signal-2",
    }


def test_replaced_outcome_event_identity_tracks_source_evidence_revision(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "event-revision.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    first_bars = _contiguous_bars()

    def capture(bars: list[dict[str, Any]]) -> None:
        capture_sourced_alpha_outcomes(
            db_path=db_path,
            market_date=DAY,
            requested_at=f"{DAY}T16:05:00-04:00",
            out_dir=tmp_path / "capture",
            persist=True,
            replace=True,
            config=ScannerConfig(),
            fetcher=lambda *_args, **_kwargs: _chart_payload(bars),
        )

    capture(first_bars)
    capture(first_bars)
    assert len(store.load_signal_events(signal_id="signal-1")) == 1

    revised_bars = list(first_bars)
    revised_bars[-1] = {**revised_bars[-1], "close": 9.93}
    capture(revised_bars)

    events = [
        row
        for row in store.load_signal_events(signal_id="signal-1")
        if row["event_type"] == "OUTCOME_RESOLVED"
    ]
    assert len(events) == 2
    assert len({row["event_id"] for row in events}) == 2


def test_outcome_and_event_persistence_is_atomic(tmp_path: Path) -> None:
    db_path = tmp_path / "atomic.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    outcome = {
        "signal_id": "signal-1",
        "market_date": DAY,
        "ticker": "NOVA",
        "outcome_source": "test",
        "imported_at": f"{DAY}T20:05:00Z",
        "outcome_status": "complete_sourced",
    }
    invalid_event = {
        "event_id": "event-1",
        "signal_id": "signal-1",
        "event_type": "OUTCOME_CAPTURED",
        "event_timestamp": f"{DAY}T20:05:00Z",
        "source": "test",
        "payload_json": {"not_json_serializable": object()},
    }

    with pytest.raises(TypeError):
        store.persist_signal_outcomes_with_events([outcome], [invalid_event])

    assert store.load_signal_outcomes() == []
    assert store.load_signal_events(signal_id="signal-1") == []


def test_existing_sourced_outcome_repairs_a_missing_audit_event(tmp_path: Path) -> None:
    db_path = tmp_path / "repair.sqlite"
    store = SQLiteScanStore(db_path)
    _persist_selected_signals(store, [_signal()])
    common = {
        "db_path": db_path,
        "market_date": DAY,
        "requested_at": f"{DAY}T16:05:00-04:00",
        "config": ScannerConfig(),
        "fetcher": lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
    }
    preview = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "preview",
        persist=False,
    )
    store.persist_signal_outcomes(preview["outcomes"])
    assert store.load_signal_events(signal_id="signal-1") == []

    repaired = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "repair",
        persist=True,
    )

    assert repaired["status"] == "already_captured"
    assert repaired["audit_events"]["inserted"] == 1
    assert len(store.load_signal_events(signal_id="signal-1")) == 1


def test_mixed_repair_and_new_event_accounting_is_aggregated(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed-repair.sqlite"
    store = SQLiteScanStore(db_path)
    second = {
        **_signal(),
        "signal_id": "signal-2",
        "scan_id": "scan-2",
        "alpha_signal_id": "alpha-2",
        "generated_at": f"{DAY}T13:45:00Z",
    }
    _persist_selected_signals(store, [_signal(), second])
    common = {
        "db_path": db_path,
        "market_date": DAY,
        "requested_at": f"{DAY}T16:05:00-04:00",
        "config": ScannerConfig(),
        "fetcher": lambda *_args, **_kwargs: _chart_payload(_contiguous_bars()),
    }
    preview = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "preview",
        persist=False,
    )
    first = next(row for row in preview["outcomes"] if row["signal_id"] == "signal-1")
    store.persist_signal_outcomes([first])

    result = capture_sourced_alpha_outcomes(
        **common,
        out_dir=tmp_path / "capture",
        persist=True,
    )

    assert result["audit_events"] == {
        "inserted": 2,
        "skipped": 0,
        "repaired_inserted": 1,
        "repaired_skipped": 0,
        "new_inserted": 1,
        "new_skipped": 0,
    }
    assert len(store.load_signal_outcomes()) == 2


def _signal(day: str = DAY) -> dict[str, Any]:
    return {
        "signal_id": "signal-1",
        "scan_id": "scan-1",
        "alpha_signal_id": "alpha-1",
        "generated_at": f"{day}T13:00:00Z",
        "market_date": day,
        "ticker": "NOVA",
        "company": "Nova Research",
        "rank": 1,
        "source": "public_web",
        "source_url": "https://example.test/nova",
        "source_confidence": 90.0,
        "data_source_kind": "public_free_shadow",
        "model_version": "dawnstrike-alphaops-v4",
        "config_hash": "config-hash",
        "primary_setup": "gap-breakout",
        "setup_grade": "A",
        "signal_label": "WATCH",
        "entry_watch_level": 10.0,
        "entry_trigger_type": "breakout_confirmation",
        "entry_condition": "Watch above 10.0",
        "confirmation_condition": "Sustained volume",
        "exit_line": 9.8,
        "invalidation_level": 9.8,
        "target_1": 10.2,
        "target_2": 10.5,
        "risk_flags_json": [],
        "avoid_reasons_json": [],
        "catalyst_summary": "Sourced catalyst",
        "telegram_event_key": "",
        "was_alerted": True,
        "no_trade_reason": "",
        "raw_payload_json": {
            "can_alert": True,
            "trade_plan_blocks_alert": False,
            "setup_key": "gap-breakout",
            "alpha_score": 80.0,
        },
    }


def _persist_selected_signals(
    store: SQLiteScanStore,
    signals: list[dict[str, Any]],
) -> None:
    store.persist_historical_signals(signals)
    selections: list[dict[str, Any]] = []
    deliveries: list[dict[str, Any]] = []
    for index, signal in enumerate(signals, 1):
        signal_id = str(signal["signal_id"])
        day = str(signal["market_date"])
        selection_id = f"selection-{signal_id}"
        generated_at = str(signal.get("generated_at") or "")
        selected_at = (
            generated_at if generated_at.startswith(day) else f"{day}T13:00:00Z"
        )
        strategy_id, strategy_version = alphaops_strategy_contract(selected_at)
        common = {
            "selection_id": selection_id,
            "scan_id": str(signal.get("scan_id") or ""),
            "signal_id": signal_id,
            "ticker": str(signal.get("ticker") or ""),
            "rank": int(signal.get("rank") or index),
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "cohort": "official_telegram",
            "decision": "clean_edge",
            "selected_at": selected_at,
            "event_key": f"alphaops:{signal_id}:alpha_morning_watch",
            "body_sha256": f"body-hash-{signal_id}",
        }
        selections.append(common)
        deliveries.append({
            **common,
            "membership_id": f"delivery-{signal_id}",
            "channel": "telegram",
            "delivery_status": "delivered",
            "attempted_at": selected_at,
            "delivered_at": selected_at,
        })
    store.persist_signal_selections(selections)
    store.persist_notification_deliveries(deliveries)


def _bar(
    clock: str,
    open_price: float,
    high: float | None,
    low: float,
    close: float,
    *,
    day: str = DAY,
) -> dict[str, Any]:
    observed_at = datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=EASTERN)
    return {
        "timestamp": int(observed_at.timestamp()),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": 1_000.0,
    }


def _contiguous_bars(
    *,
    day: str = DAY,
    close_clock: str = "15:59",
    default: tuple[float, float | None, float, float] = (9.90, 9.95, 9.90, 9.92),
    overrides: dict[str, tuple[float, float | None, float, float]] | None = None,
) -> list[dict[str, Any]]:
    current = datetime.fromisoformat(f"{day}T09:30:00").replace(tzinfo=EASTERN)
    end = datetime.fromisoformat(f"{day}T{close_clock}:00").replace(tzinfo=EASTERN)
    selected = overrides or {}
    rows: list[dict[str, Any]] = []
    while current <= end:
        clock = current.strftime("%H:%M")
        open_price, high, low, close = selected.get(clock, default)
        rows.append(_bar(clock, open_price, high, low, close, day=day))
        current += timedelta(minutes=1)
    return rows


def _chart_payload(bars: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "chart": {
            "result": [{
                "timestamp": [row["timestamp"] for row in bars],
                "indicators": {
                    "quote": [{
                        "open": [row["open"] for row in bars],
                        "high": [row["high"] for row in bars],
                        "low": [row["low"] for row in bars],
                        "close": [row["close"] for row in bars],
                        "volume": [row["volume"] for row in bars],
                    }]
                },
            }],
            "error": None,
        }
    }
