from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from intraday_scanner.alpha.path_replay import PathTruthStatus
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_POLICY_VERSION,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
)
from intraday_scanner.cli import main as cli_main
from intraday_scanner.config import ScannerConfig
from intraday_scanner.services.alpha_paper_reconciliation_service import (
    ALPHAOPS_STRATEGY_ID,
    ALPHAOPS_STRATEGY_VERSION,
    DELIVERED_COHORT,
    _reconcile_selection,
    reconcile_alpha_paper_trades,
)
from intraday_scanner.services.learning_service import (
    load_production_alpha_learning_labels,
    run_alpha_learning,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

DAY = "2026-07-15"
SIGNAL_ID = "scan-paper:1:NOVA"
SELECTION_ID = "selection-paper-nova"


def _tree(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_dir()
        )
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
    return directories, files


def test_paper_reconciliation_preserves_canonical_path_truth_status() -> None:
    evaluation, trade, labels = _reconcile_selection(
        selection={
            "selection_id": SELECTION_ID,
            "signal_id": SIGNAL_ID,
            "ticker": "NOVA",
            "market_date": DAY,
            "strategy_id": ALPHAOPS_STRATEGY_ID,
            "cohort": DELIVERED_COHORT,
        },
        signal={"signal_id": SIGNAL_ID, "ticker": "NOVA", "market_date": DAY},
        outcome={
            "outcome_status": "complete_sourced",
            "source_coverage_complete": True,
            "source_bar_hash_sha256": "bars",
            "path_truth_status": PathTruthStatus.ENTRY_BAR_AMBIGUOUS.value,
            "path_replay_id": "replay-1",
        },
        delivery=None,
        reconciled_at="2026-07-15T20:00:00+00:00",
        notional_per_trade=1000.0,
        fee_bps=1.0,
        slippage_bps=50.0,
    )

    assert trade is None
    assert labels == []
    assert evaluation["path_truth_status"] == PathTruthStatus.ENTRY_BAR_AMBIGUOUS.value
    assert evaluation["reconciliation_status"] == "unresolved"


def test_not_triggered_is_resolved_activation_evidence_with_no_trade_return(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_selection(store)
    _persist_outcome(store, outcome_status="not_triggered")

    first = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
        persist=True,
        config=ScannerConfig(slippage_bps=50.0),
    )
    second = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
        persist=True,
        config=ScannerConfig(slippage_bps=50.0),
    )

    labels = store.load_strategy_learning_labels()
    scorecards = store.load_daily_strategy_scorecards()
    assert first["status"] == "complete"
    assert first["closed_trade_count"] == 0
    assert first["not_triggered_count"] == 1
    assert first["unresolved_count"] == 0
    assert first["evaluations"][0]["terminal_state"] == "not_triggered"
    assert first["evaluations"][0]["net_return_pct"] is None
    assert store.load_strategy_paper_trades() == []
    assert [(row["label_family"], row["label_value"]) for row in labels] == [
        ("activation", 0.0)
    ]
    official = next(row for row in scorecards if row["cohort"] == DELIVERED_COHORT)
    assert official["delivered_count"] == 1
    assert official["resolved_count"] == 1
    assert official["not_triggered_count"] == 1
    assert official["closed_count"] == 0
    assert official["average_net_return_pct"] is None
    assert official["return_on_allocated_capital_pct"] is None
    assert second["persistence"]["evaluations"]["updated"] == 1
    assert second["persistence"]["learning_labels"]["updated"] == 1


def test_complete_sourced_outcome_creates_exact_paper_entry_exit_and_return(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_selection(store)
    _persist_outcome(store, outcome_status="complete_sourced")

    result = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
        persist=True,
        notional_per_trade=1_000.0,
        fee_bps=1.0,
        config=ScannerConfig(slippage_bps=50.0),
    )

    trade = store.load_strategy_paper_trades()[0]
    labels = {row["label_family"]: row for row in store.load_strategy_learning_labels()}
    assert result["status"] == "complete"
    assert result["triggered_count"] == 1
    assert result["closed_trade_count"] == 1
    assert trade["entry_time"] == f"{DAY}T13:31:00Z"
    assert trade["exit_time"] == f"{DAY}T14:04:00Z"
    assert trade["exit_reason"] == "target_1"
    assert trade["entry_fill_price"] == pytest.approx(10.05)
    assert trade["exit_fill_price"] == pytest.approx(10.945)
    expected_quantity = 1_000.0 / 10.05
    expected_fees = (10.05 * expected_quantity + 10.945 * expected_quantity) / 10_000
    expected_net = (10.945 - 10.05) * expected_quantity - expected_fees
    assert trade["net_pnl"] == pytest.approx(expected_net, abs=0.0001)
    assert trade["net_return_pct"] == pytest.approx(expected_net / 10, abs=0.0001)
    assert labels["activation"]["label_value"] == 1.0
    assert labels["trade_return"]["label_value"] == trade["net_return_pct"]
    assert result["evaluations"][0]["source_bar_hash_sha256"] == "bars-hash"
    assert result["evaluations"][0]["reconciliation_status"] == "resolved"


@pytest.mark.parametrize("via_cli", (False, True))
def test_reconciliation_preview_preserves_existing_database_bytes(
    tmp_path: Path,
    via_cli: bool,
) -> None:
    db_root = tmp_path / "db-root"
    db_path = db_root / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_selection(store)
    _persist_outcome(store, outcome_status="complete_sourced")
    before = _tree(db_root)
    out_dir = tmp_path / "reports"

    if via_cli:
        assert (
            cli_main(
                [
                    "alpha-paper-reconcile",
                    "--db-path",
                    str(db_path),
                    "--market-date",
                    DAY,
                    "--out-dir",
                    str(out_dir),
                ]
            )
            == 0
        )
    else:
        result = reconcile_alpha_paper_trades(
            db_path=db_path,
            market_date=DAY,
            out_dir=out_dir,
            persist=False,
            config=ScannerConfig(slippage_bps=50.0),
        )
        assert result["status"] == "complete"

    assert (out_dir / DAY / "reconciliation.json").is_file()
    assert _tree(db_root) == before


def test_reconciliation_correction_atomically_removes_stale_trade_and_return_label(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_selection(store)
    _persist_outcome(store, outcome_status="complete_sourced")
    reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
        config=ScannerConfig(slippage_bps=50.0),
    )
    store.persist_manual_outcomes(
        [
            {
                "outcome_key": "manual-must-not-learn",
                "scan_id": "scan-paper",
                "signal_id": SIGNAL_ID,
                "ticker": "NOVA",
                "recommendation_timestamp": f"{DAY}T13:10:00Z",
                "uploaded_at": f"{DAY}T21:00:00Z",
                "source": "manual_upload",
                "entry": 10.0,
                "high": 999.0,
                "low": 1.0,
                "close": 999.0,
            }
        ]
    )
    first_learning = run_alpha_learning(store)
    assert first_learning["total_return_labels"] == 1
    assert first_learning["manual_outcomes_considered"] == 1
    assert first_learning["legacy_outcomes_excluded_from_production_learning"] is True
    canonical = load_production_alpha_learning_labels(store)
    assert len(canonical) == 1
    assert canonical[0]["planned_first_touch_return_pct"] != 9_890.0
    assert canonical[0]["return_measure"] == "reconciled_net_after_cost_return_pct"
    assert store.load_strategy_paper_trades()
    assert any(
        row["label_family"] == "trade_return"
        for row in store.load_strategy_learning_labels()
    )

    _persist_outcome(store, outcome_status="not_triggered")
    corrected = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
        config=ScannerConfig(slippage_bps=50.0),
    )
    second_learning = run_alpha_learning(store)

    assert corrected["persistence"]["trades"]["deleted"] == 1
    assert corrected["persistence"]["learning_labels"]["deleted"] == 1
    assert store.load_strategy_paper_trades() == []
    assert [
        row["label_family"] for row in store.load_strategy_learning_labels()
    ] == ["activation"]
    assert second_learning["total_return_labels"] == 0
    assert second_learning["return_learning_eligible"] is False
    assert store.load_alpha_outcome_labels() == []


def test_reconciliation_stale_cleanup_rolls_back_with_failed_batch(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_selection(store)
    _persist_outcome(store, outcome_status="complete_sourced")
    reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
    )
    original_evaluation = store.load_strategy_evaluations()[0]
    corrected_evaluation = {
        **original_evaluation,
        "terminal_state": "not_triggered",
        "activated": False,
        "filled": False,
        "closed": False,
        "trade_return_eligible": False,
        "net_return_pct": None,
    }
    invalid_label = {
        "label_id": "invalid-label",
        "evaluation_id": original_evaluation["evaluation_id"],
        "signal_id": SIGNAL_ID,
        "market_date": DAY,
        "ticker": "NOVA",
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": DELIVERED_COHORT,
        "label_family": "activation",
        "label_value": 0.0,
        "eligible": True,
        "source_bar_hash_sha256": "bars-hash",
        "created_at": f"{DAY}T21:00:00Z",
        "not_json_serializable": object(),
    }

    with pytest.raises(TypeError, match="JSON serializable"):
        store.persist_strategy_reconciliation(
            evaluations=[corrected_evaluation],
            paper_trades=[],
            learning_labels=[invalid_label],
            scorecards=[],
        )

    assert len(store.load_strategy_paper_trades()) == 1
    assert {
        row["label_family"] for row in store.load_strategy_learning_labels()
    } == {"activation", "trade_return"}
    assert store.load_strategy_evaluations()[0]["terminal_state"] == "filled_and_closed"


def test_official_cohort_ignores_delivered_non_telegram_channel(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_selection(store, persist_delivery=False)
    _persist_outcome(store, outcome_status="complete_sourced")
    store.persist_notification_deliveries(
        [
            {
                "membership_id": "delivery-paper-nova-console",
                "selection_id": SELECTION_ID,
                "scan_id": "scan-paper",
                "signal_id": SIGNAL_ID,
                "ticker": "NOVA",
                "strategy_id": ALPHAOPS_STRATEGY_ID,
                "strategy_version": ALPHAOPS_STRATEGY_VERSION,
                "cohort": DELIVERED_COHORT,
                "decision": "probability_fallback",
                "selected_at": f"{DAY}T13:10:00+00:00",
                "event_key": "alphaops:scan-paper:alpha_morning_watch",
                "channel": "console",
                "delivery_status": "delivered",
                "attempted_at": f"{DAY}T13:10:06+00:00",
                "delivered_at": f"{DAY}T13:10:06+00:00",
                "body_sha256": "body-hash",
            },
        ]
    )

    result = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
    )
    official = next(
        row for row in result["scorecards"] if row["cohort"] == DELIVERED_COHORT
    )
    selected = next(
        row for row in result["scorecards"] if row["cohort"] == "algorithm_selected"
    )

    assert result["evaluations"][0]["delivered"] is False
    assert official["selected_count"] == 0
    assert official["delivered_count"] == 0
    assert official["closed_count"] == 0
    assert selected["selected_count"] == 1
    assert selected["closed_count"] == 1
    learning = run_alpha_learning(store)
    assert learning["total_return_labels"] == 0
    assert learning["canonical_return_label_diagnostics"]["excluded_not_delivered"] == 1


def test_explicit_no_trade_day_is_complete_and_distinct_from_a_missed_run(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    selection_id = "selection-no-trade"
    store.persist_signal_selections(
        [
            {
                "selection_id": selection_id,
                "scan_id": "scan-no-trade",
                "signal_id": f"no_trade:scan-no-trade:{DAY}",
                "ticker": "NO_TRADE",
                "rank": 0,
                "strategy_id": ALPHAOPS_STRATEGY_ID,
                "strategy_version": ALPHAOPS_STRATEGY_VERSION,
                "cohort": DELIVERED_COHORT,
                "decision": "no_trade",
                "selected_at": f"{DAY}T13:10:00+00:00",
                "event_key": "alphaops:scan-no-trade:alpha_no_trade",
                "body_sha256": "body-hash",
            }
        ]
    )
    store.persist_notification_deliveries(
        [
            {
                "membership_id": "delivery-no-trade",
                "selection_id": selection_id,
                "scan_id": "scan-no-trade",
                "signal_id": f"no_trade:scan-no-trade:{DAY}",
                "ticker": "NO_TRADE",
                "strategy_id": ALPHAOPS_STRATEGY_ID,
                "strategy_version": ALPHAOPS_STRATEGY_VERSION,
                "cohort": DELIVERED_COHORT,
                "decision": "no_trade",
                "selected_at": f"{DAY}T13:10:00+00:00",
                "event_key": "alphaops:scan-no-trade:alpha_no_trade",
                "channel": "telegram",
                "delivery_status": "delivered",
                "attempted_at": f"{DAY}T13:10:01+00:00",
                "delivered_at": f"{DAY}T13:10:01+00:00",
                "body_sha256": "body-hash",
            }
        ]
    )

    result = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
        persist=True,
    )

    assert result["status"] == "complete"
    assert result["selection_evidence_status"] == "explicit_no_trade"
    assert result["selection_count"] == 0
    assert result["no_trade_count"] == 1
    assert result["paper_trades"] == []
    official = next(
        row for row in result["scorecards"] if row["cohort"] == DELIVERED_COHORT
    )
    assert official["session_status"] == "explicit_no_trade"
    assert official["no_trade_count"] == 1
    assert official["return_on_allocated_capital_pct"] is None


def test_missing_exact_selection_evidence_fails_visible(tmp_path: Path) -> None:
    result = reconcile_alpha_paper_trades(
        db_path=tmp_path / "empty.sqlite",
        market_date=DAY,
        out_dir=tmp_path / "reports",
        persist=True,
    )

    assert result["status"] == "failed"
    assert result["selection_evidence_status"] == "missing_selection_evidence"
    assert all(
        row["reconciliation_status"] == "failed" for row in result["scorecards"]
    )


def test_v5_selection_without_allowed_entry_remains_research_only(
    tmp_path: Path,
) -> None:
    day = "2026-07-31"
    db_path = tmp_path / "v5-blocked.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_v5_reconciliation(store, day=day, with_entry_intent=False)

    result = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=day,
        out_dir=tmp_path / "reports",
        persist=True,
    )

    assert result["status"] == "complete"
    assert result["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    assert result["execution_policy_version"] == ALPHAOPS_V5_POLICY_VERSION
    assert result["closed_trade_count"] == 0
    assert result["evaluations"][0]["terminal_state"] == "research_only_policy_blocked"
    assert result["evaluations"][0]["trade_return_eligible"] is False
    assert store.load_strategy_paper_trades() == []
    assert store.load_strategy_learning_labels() == []


def test_v5_reconciliation_uses_risk_sized_entry_not_fixed_notional(
    tmp_path: Path,
) -> None:
    day = "2026-07-31"
    db_path = tmp_path / "v5-allowed.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_v5_reconciliation(store, day=day, with_entry_intent=True)

    result = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=day,
        out_dir=tmp_path / "reports",
        persist=True,
    )

    trade = store.load_strategy_paper_trades()[0]
    assert result["status"] == "complete"
    assert result["closed_trade_count"] == 1
    assert trade["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    assert trade["strategy_version"] == ALPHAOPS_V5_STRATEGY_VERSION
    assert trade["account_id"] == ALPHAOPS_V5_ACCOUNT_ID
    assert trade["quantity"] == 216
    assert trade["notional"] == pytest.approx(10.10025 * 216, abs=0.0001)
    assert trade["notional"] != 1_000
    assert trade["execution_policy_version"] == ALPHAOPS_V5_POLICY_VERSION
    assert trade["decision_fingerprint"] == "f" * 64
    learning = run_alpha_learning(store)
    assert learning["total_return_labels"] == 1
    assert learning["return_learning_eligible"] is True
    production_label = load_production_alpha_learning_labels(store)[0]
    assert production_label["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    assert production_label["trade_id"] == trade["trade_id"]


def _seed_selection(
    store: SQLiteScanStore,
    *,
    persist_delivery: bool = True,
) -> None:
    store.persist_historical_signals(
        [
            {
                "signal_id": SIGNAL_ID,
                "scan_id": "scan-paper",
                "alpha_signal_id": SIGNAL_ID,
                "generated_at": f"{DAY}T13:10:00+00:00",
                "market_date": DAY,
                "ticker": "NOVA",
                "rank": 1,
                "model_version": ALPHAOPS_STRATEGY_VERSION,
                "signal_label": "WATCH",
                "entry_watch_level": 10.0,
                "invalidation_level": 9.0,
                "target_1": 11.0,
                "raw_payload_json": {"setup_key": "grade:A|gap:clean"},
            }
        ]
    )
    body_hash = hashlib.sha256(b"1) NOVA - Opportunity").hexdigest()
    store.persist_signal_selections(
        [
            {
                "selection_id": SELECTION_ID,
                "scan_id": "scan-paper",
                "signal_id": SIGNAL_ID,
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": ALPHAOPS_STRATEGY_ID,
                "strategy_version": ALPHAOPS_STRATEGY_VERSION,
                "cohort": DELIVERED_COHORT,
                "decision": "probability_fallback",
                "selected_at": f"{DAY}T13:10:00+00:00",
                "event_key": "alphaops:scan-paper:alpha_morning_watch",
                "body_sha256": body_hash,
            }
        ]
    )
    if not persist_delivery:
        return
    store.persist_notification_deliveries(
        [
            {
                "membership_id": "delivery-paper-nova",
                "selection_id": SELECTION_ID,
                "scan_id": "scan-paper",
                "signal_id": SIGNAL_ID,
                "ticker": "NOVA",
                "strategy_id": ALPHAOPS_STRATEGY_ID,
                "strategy_version": ALPHAOPS_STRATEGY_VERSION,
                "cohort": DELIVERED_COHORT,
                "decision": "probability_fallback",
                "selected_at": f"{DAY}T13:10:00+00:00",
                "event_key": "alphaops:scan-paper:alpha_morning_watch",
                "channel": "telegram",
                "delivery_status": "delivered",
                "attempted_at": f"{DAY}T13:10:05+00:00",
                "delivered_at": f"{DAY}T13:10:05+00:00",
                "body_sha256": body_hash,
            }
        ]
    )


def _persist_outcome(store: SQLiteScanStore, *, outcome_status: str) -> None:
    row = {
        "signal_id": SIGNAL_ID,
        "market_date": DAY,
        "ticker": "NOVA",
        "outcome_source": "yahoo_finance_chart",
        "source_url": "https://query1.finance.yahoo.test/NOVA",
        "source_bar_hash_sha256": "bars-hash",
        "source_bar_count": 390,
        "source_coverage_complete": True,
        "validated_against_signal_timestamp": True,
        "outcome_status": outcome_status,
        "imported_at": f"{DAY}T20:05:00+00:00",
        "automatic_sourced_data": True,
        "no_lookahead": True,
    }
    if outcome_status == "complete_sourced":
        row.update(
            {
                "entry_time": f"{DAY}T13:31:00Z",
                "entry_price": 10.0,
                "target_price": 11.0,
                "invalidation_price": 9.0,
                "target_touched_at": f"{DAY}T14:04:00Z",
                "invalidation_touched_at": None,
                "planned_first_touch_outcome": "target_1",
                "close_price": 10.8,
                "close_price_observed_at": f"{DAY}T19:59:00Z",
                "high_after_entry": 11.2,
                "low_after_entry": 9.8,
                "learning_eligible": True,
            }
        )
    else:
        row.update(
            {
                "entry_time": None,
                "entry_price": None,
                "learning_eligible": False,
            }
        )
    store.persist_signal_outcomes([row], replace=True)


def _seed_v5_reconciliation(
    store: SQLiteScanStore,
    *,
    day: str,
    with_entry_intent: bool,
) -> None:
    signal_id = "scan-v5:1:NOVA"
    selection_id = "selection-v5-nova"
    store.persist_historical_signals(
        [
            {
                "signal_id": signal_id,
                "scan_id": "scan-v5",
                "generated_at": f"{day}T12:10:00+00:00",
                "market_date": day,
                "ticker": "NOVA",
                "rank": 1,
                "model_version": ALPHAOPS_V5_STRATEGY_VERSION,
                "signal_label": "WATCH",
                "entry_watch_level": 10.0,
                "invalidation_level": 9.0,
                "target_1": 12.75,
            }
        ]
    )
    store.persist_signal_selections(
        [
            {
                "selection_id": selection_id,
                "scan_id": "scan-v5",
                "signal_id": signal_id,
                "ticker": "NOVA",
                "rank": 1,
                "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
                "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
                "cohort": DELIVERED_COHORT,
                "decision": "clean_edge",
                "selected_at": f"{day}T12:10:00+00:00",
                "event_key": "alphaops:scan-v5:alpha_morning_watch",
                "body_sha256": "v5-body-hash",
            }
        ]
    )
    store.persist_notification_deliveries(
        [
            {
                "membership_id": "delivery-v5-nova",
                "selection_id": selection_id,
                "scan_id": "scan-v5",
                "signal_id": signal_id,
                "ticker": "NOVA",
                "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
                "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
                "cohort": DELIVERED_COHORT,
                "decision": "clean_edge",
                "selected_at": f"{day}T12:10:00+00:00",
                "event_key": "alphaops:scan-v5:alpha_morning_watch",
                "channel": "telegram",
                "delivery_status": "delivered",
                "attempted_at": f"{day}T12:10:05+00:00",
                "delivered_at": f"{day}T12:10:05+00:00",
                "body_sha256": "v5-body-hash",
            }
        ]
    )
    store.persist_signal_outcomes(
        [
            {
                "signal_id": signal_id,
                "market_date": day,
                "ticker": "NOVA",
                "outcome_source": "yahoo_finance_chart",
                "source_url": "https://query1.finance.yahoo.test/NOVA",
                "source_bar_hash_sha256": "v5-bars-hash",
                "source_bar_count": 390,
                "source_coverage_complete": True,
                "validated_against_signal_timestamp": True,
                "outcome_status": "complete_sourced",
                "entry_time": f"{day}T13:31:00Z",
                "entry_price": 10.0,
                "target_price": 12.75,
                "invalidation_price": 9.0,
                "target_touched_at": f"{day}T16:04:00Z",
                "planned_first_touch_outcome": "target_1",
                "close_price": 11.0,
                "close_price_observed_at": f"{day}T19:59:00Z",
                "high_after_entry": 13.0,
                "low_after_entry": 9.8,
                "learning_eligible": True,
                "imported_at": f"{day}T20:05:00Z",
            }
        ]
    )
    if not with_entry_intent:
        return
    intent = {
        "intent_id": "v5-entry-intent",
        "signal_id": signal_id,
        "market_date": day,
        "ticker": "NOVA",
        "mode": "paper_execute",
        "lifecycle_state": "ENTRY_TRIGGERED",
        "action": "ENTER_LONG",
        "decision_time": f"{day}T14:00:00+00:00",
        "decision_price": 10.05,
        "trigger_price": 10.0,
        "stop_price": 9.0,
        "target_price": 12.75,
        "quantity": 216,
        "notional": 10.10025 * 216,
        "risk_amount": 249.534,
        "reason": "V5 policy passed.",
        "created_at": f"{day}T14:00:00+00:00",
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "cohort": DELIVERED_COHORT,
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "execution_policy_version": ALPHAOPS_V5_POLICY_VERSION,
        "decision_fingerprint": "f" * 64,
        "official_paper_eligible": True,
        "decision_trace": {
            "eligible_for_official_paper": True,
            "computed": {"expected_entry_price": 10.10025},
        },
    }
    intent["payload_json"] = dict(intent)
    store.persist_trade_intents([intent])
