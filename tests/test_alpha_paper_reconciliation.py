from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from intraday_scanner.alpha.canonical_return_truth import canonical_paper_selection_context
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
from tests._alpha_path_truth import (
    canonical_ineligible_outcome,
    canonical_return_outcome,
    canonical_v6_decision,
    causal_identity_from,
    replay_binding_from,
)

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


@pytest.mark.parametrize(
    "missing_key",
    (
        "path_replay_schema_version",
        "path_replay_id",
        "path_replay_policy_version",
        "path_replay_policy_hash_sha256",
        "path_replay_receipt",
        "replay_input_manifest",
        "replay_input_hash_sha256",
        "replay_truth_hash_sha256",
        "replay_receipt_hash_sha256",
        "path_truth_status",
        "path_event",
        "source_bar_hash_sha256",
        "source_coverage_complete",
        "sequence_complete_through_exit",
        "after_cost_return_pct",
        "return_truth_schema_version",
        "return_truth_hash_sha256",
        "cost_schema_version",
        "cost_receipt_id",
        "cost_receipt_hash_sha256",
        "cost_receipt",
        "observed_cost_model_identity",
        "modeled_cost_model_identity",
        "cost_components",
        "benchmark_return_pct",
        "benchmark_symbol",
        "benchmark_source_bar_hash_sha256",
        "benchmark_independent_reconciliation_status",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_symbol",
        "secondary_benchmark_source_bar_hash_sha256",
        "secondary_benchmark_independent_reconciliation_status",
        "independent_reconciliation_status",
        "reconciliation_schema_version",
        "reconciliation_receipt_id",
        "reconciliation_receipt_hash_sha256",
        "reconciliation_receipt",
        "causal_decision_identity",
        "eligibility_policy_version",
        "retrospective_research_eligible",
        "prospective_promotion_eligible",
    ),
)
def test_paper_reconciliation_requires_complete_canonical_return_truth(
    missing_key: str,
) -> None:
    outcome = _canonical_reconciliation_outcome()
    outcome.pop(missing_key)

    evaluation, trade, labels = _reconcile_selection(
        selection=_direct_selection(),
        signal=_direct_signal(),
        outcome=outcome,
        delivery=None,
        reconciled_at="2026-07-15T20:00:00+00:00",
        notional_per_trade=1_000.0,
        fee_bps=1.0,
        slippage_bps=50.0,
    )

    assert trade is None
    assert labels == []
    assert evaluation["reconciliation_status"] == (
        "unresolved" if missing_key == "source_coverage_complete" else "invalid"
    )
    assert evaluation["trade_return_eligible"] is False


@pytest.mark.parametrize(
    ("key", "blank"),
    (
        ("path_replay_schema_version", ""),
        ("path_replay_id", " "),
        ("path_replay_policy_version", ""),
        ("path_replay_policy_hash_sha256", "not-a-hash"),
        ("path_replay_receipt", {}),
        ("replay_input_manifest", {}),
        ("replay_input_hash_sha256", ""),
        ("replay_truth_hash_sha256", ""),
        ("replay_receipt_hash_sha256", ""),
        ("path_truth_status", ""),
        ("path_event", ""),
        ("source_bar_hash_sha256", ""),
        ("source_coverage_complete", None),
        ("sequence_complete_through_exit", None),
        ("return_truth_schema_version", ""),
        ("return_truth_hash_sha256", ""),
        ("cost_schema_version", ""),
        ("cost_receipt_id", ""),
        ("cost_receipt_hash_sha256", ""),
        ("cost_receipt", {}),
        ("observed_cost_model_identity", ""),
        ("modeled_cost_model_identity", ""),
        ("cost_components", {}),
        ("benchmark_symbol", ""),
        ("benchmark_return_pct", None),
        ("benchmark_source_bar_hash_sha256", ""),
        ("benchmark_independent_reconciliation_status", ""),
        ("secondary_benchmark_symbol", ""),
        ("secondary_benchmark_return_pct", None),
        ("secondary_benchmark_source_bar_hash_sha256", ""),
        ("secondary_benchmark_independent_reconciliation_status", ""),
        ("reconciliation_schema_version", ""),
        ("independent_reconciliation_status", ""),
        ("reconciliation_receipt_id", ""),
        ("reconciliation_receipt_hash_sha256", ""),
        ("reconciliation_receipt", {}),
        ("causal_decision_identity", None),
        ("eligibility_policy_version", ""),
        ("retrospective_research_eligible", None),
        ("prospective_promotion_eligible", None),
    ),
)
def test_paper_reconciliation_rejects_blank_current_truth(
    key: str,
    blank: object,
) -> None:
    outcome = {**_canonical_reconciliation_outcome(), key: blank}

    evaluation, trade, labels = _reconcile_selection(
        selection=_direct_selection(),
        signal=_direct_signal(),
        outcome=outcome,
        delivery=None,
        reconciled_at="2026-07-15T20:00:00+00:00",
        notional_per_trade=1_000.0,
        fee_bps=1.0,
        slippage_bps=50.0,
    )

    assert trade is None
    assert labels == []
    assert evaluation["reconciliation_status"] == (
        "unresolved" if key == "source_coverage_complete" else "invalid"
    )


@pytest.mark.parametrize(
    "case",
    (
        "entry_censored",
        "same_censored",
        "missing_interval",
        "halt",
        "source_conflict",
        "corporate_action",
    ),
)
def test_paper_reconciliation_never_turns_censored_path_into_trade(case: str) -> None:
    outcome = canonical_ineligible_outcome(
        market_date=DAY,
        case=case,
        causal_identity=_paper_causal_identity(),
    )

    evaluation, trade, labels = _reconcile_selection(
        selection=_direct_selection(),
        signal=_direct_signal(),
        outcome=outcome,
        delivery=None,
        reconciled_at="2026-07-15T20:00:00+00:00",
        notional_per_trade=1_000.0,
        fee_bps=1.0,
        slippage_bps=50.0,
    )

    assert trade is None
    assert labels == []
    assert evaluation["reconciliation_status"] == "unresolved"


def test_paper_reconciliation_uses_only_canonical_pre_exit_excursions() -> None:
    outcome = {
        **_canonical_reconciliation_outcome(),
        "high_after_entry": 99.0,
        "low_after_entry": 1.0,
    }

    _evaluation, trade, labels = _reconcile_selection(
        selection=_direct_selection(),
        signal=_direct_signal(),
        outcome=outcome,
        delivery=None,
        reconciled_at="2026-07-15T20:00:00+00:00",
        notional_per_trade=1_000.0,
        fee_bps=1.0,
        slippage_bps=50.0,
    )

    assert trade is not None
    assert trade["max_favorable_excursion_pct"] == outcome["mfe_pct"]
    assert trade["max_adverse_excursion_pct"] == outcome["mae_pct"]
    assert {row["label_family"] for row in labels} == {"activation", "trade_return"}


@pytest.mark.parametrize("case", ("ordered_target", "ordered_stop", "timeout"))
def test_paper_reconciliation_copies_canonical_return_truth_without_replay(
    case: str,
) -> None:
    outcome = canonical_return_outcome(
        market_date=DAY,
        case=case,
        causal_identity=_paper_causal_identity(),
    )

    evaluation, trade, labels = _reconcile_selection(
        selection=_direct_selection(),
        signal=_direct_signal(),
        outcome=outcome,
        delivery=None,
        reconciled_at=f"{DAY}T20:00:00+00:00",
        notional_per_trade=99_999.0,
        fee_bps=999.0,
        slippage_bps=999.0,
    )

    assert trade is not None
    assert trade["raw_entry_price"] == outcome["entry_price"]
    assert trade["entry_time"] == outcome["entry_time"]
    assert trade["raw_exit_price"] == outcome["exit_price"]
    assert trade["exit_time"] == outcome["exit_time"]
    assert trade["net_return_pct"] == outcome["after_cost_return_pct"]
    assert trade["max_favorable_excursion_pct"] == outcome["mfe_pct"]
    assert trade["max_adverse_excursion_pct"] == outcome["mae_pct"]
    assert trade["notional"] == outcome["cost_components"]["notional_per_trade"]
    for key in (
        "path_replay_id",
        "path_replay_receipt",
        "return_truth_schema_version",
        "return_truth_hash_sha256",
        "cost_schema_version",
        "cost_receipt_id",
        "cost_receipt_hash_sha256",
        "cost_receipt",
        "observed_cost_model_identity",
        "modeled_cost_model_identity",
        "cost_components",
        "gross_return_pct",
        "after_cost_return_pct",
        "benchmark_symbol",
        "benchmark_return_pct",
        "benchmark_source_bar_hash_sha256",
        "secondary_benchmark_symbol",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_source_bar_hash_sha256",
        "reconciliation_schema_version",
        "reconciliation_receipt_id",
        "reconciliation_receipt_hash_sha256",
        "reconciliation_receipt",
        "causal_decision_identity",
        "eligibility_policy_version",
        "evidence_cohort",
    ):
        assert trade[key] == outcome[key]
    assert evaluation["path_replay_id"] == outcome["path_replay_id"]
    assert evaluation["trade_return_eligible"] is True
    assert {row["label_family"] for row in labels} == {"activation", "trade_return"}


@pytest.mark.parametrize(
    ("key", "wrong"),
    (
        ("path_replay_schema_version", "dawnstrike.path_truth.v999"),
        ("path_replay_policy_version", "attacker-policy"),
        ("path_replay_policy_hash_sha256", "f" * 64),
        ("path_truth_status", "RESOLVED_STOP_FIRST"),
        ("path_event", "STOP"),
        ("return_truth_schema_version", "attacker-return-v9"),
        ("return_truth_hash_sha256", "f" * 64),
        ("cost_schema_version", "attacker-cost-v9"),
        ("cost_receipt_hash_sha256", "f" * 64),
        ("benchmark_symbol", "QQQ"),
        ("benchmark_return_pct", 99.0),
        ("benchmark_source_bar_hash_sha256", "f" * 64),
        ("benchmark_independent_reconciliation_status", "FAILED"),
        ("secondary_benchmark_symbol", "QQQ"),
        ("secondary_benchmark_return_pct", 99.0),
        ("secondary_benchmark_source_bar_hash_sha256", "f" * 64),
        ("secondary_benchmark_independent_reconciliation_status", "PENDING"),
        ("reconciliation_schema_version", "attacker-recon-v9"),
        ("reconciliation_receipt_hash_sha256", "f" * 64),
        ("independent_reconciliation_status", "FAILED"),
        ("eligibility_policy_version", "attacker-eligibility-v9"),
        ("causal_decision_identity", {"kind": "fabricated"}),
    ),
)
def test_paper_reconciliation_rejects_wrong_nonblank_current_truth(
    key: str,
    wrong: object,
) -> None:
    outcome = {**_canonical_reconciliation_outcome(), key: wrong}

    evaluation, trade, labels = _reconcile_selection(
        selection=_direct_selection(),
        signal=_direct_signal(),
        outcome=outcome,
        delivery=None,
        reconciled_at=f"{DAY}T20:00:00+00:00",
        notional_per_trade=1_000.0,
        fee_bps=1.0,
        slippage_bps=50.0,
    )

    assert trade is None
    assert labels == []
    assert evaluation["reconciliation_status"] == "invalid"


def test_paper_reconciliation_rejects_120_pathless_boolean_outcomes() -> None:
    trades = []
    labels = []
    for index in range(120):
        outcome = {
            "outcome_id": f"legacy-outcome-{index}",
            "outcome_status": "complete_sourced",
            "source_coverage_complete": True,
            "source_bar_hash_sha256": "a" * 64,
            "entry_time": f"{DAY}T14:30:00+00:00",
            "entry_price": 10.0,
            "planned_first_touch_outcome": "target_1",
            "target_touched_at": f"{DAY}T15:00:00+00:00",
            "target_price": 11.0,
            "learning_eligible": True,
            "retrospective_research_eligible": True,
            "prospective_promotion_eligible": True,
            "net_return_pct": 99.0,
        }
        evaluation, trade, emitted = _reconcile_selection(
            selection={
                **_direct_selection(),
                "selection_id": f"legacy-selection-{index}",
                "signal_id": f"legacy-signal-{index}",
            },
            signal={**_direct_signal(), "signal_id": f"legacy-signal-{index}"},
            outcome=outcome,
            delivery=None,
            reconciled_at=f"{DAY}T20:00:00+00:00",
            notional_per_trade=1_000.0,
            fee_bps=1.0,
            slippage_bps=50.0,
        )
        assert evaluation["trade_return_eligible"] is False
        trades.append(trade)
        labels.extend(emitted)

    assert trades == [None] * 120
    assert labels == []


def test_legacy_not_triggered_is_quarantined_without_activation_learning(
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
    assert first["status"] == "failed"
    assert first["closed_trade_count"] == 0
    assert first["not_triggered_count"] == 0
    assert first["unresolved_count"] == 0
    assert first["invalid_count"] == 1
    assert first["evaluations"][0]["terminal_state"] == (
        "invalid_canonical_activation_truth"
    )
    assert first["evaluations"][0]["net_return_pct"] is None
    assert store.load_strategy_paper_trades() == []
    assert labels == []
    official = next(row for row in scorecards if row["cohort"] == DELIVERED_COHORT)
    assert official["delivered_count"] == 1
    assert official["resolved_count"] == 0
    assert official["not_triggered_count"] == 0
    assert official["closed_count"] == 0
    assert official["average_net_return_pct"] is None
    assert official["return_on_allocated_capital_pct"] is None
    assert second["persistence"]["evaluations"]["updated"] == 1
    assert second["persistence"]["learning_labels"]["updated"] == 0


def test_legacy_complete_sourced_outcome_cannot_create_paper_return(
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

    assert store.load_strategy_paper_trades() == []
    assert store.load_strategy_learning_labels() == []
    assert result["status"] == "failed"
    assert result["triggered_count"] == 1
    assert result["closed_trade_count"] == 0
    assert result["invalid_count"] == 1
    assert result["evaluations"][0]["reconciliation_status"] == "invalid"
    assert result["evaluations"][0]["trade_return_eligible"] is False


def test_reconciliation_never_rewrites_legacy_paper_positions(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_selection(store)
    _persist_outcome(store, outcome_status="complete_sourced")
    store.persist_paper_positions(
        [
            {
                "position_id": "legacy-position-1",
                "signal_id": SIGNAL_ID,
                "market_date": DAY,
                "ticker": "NOVA",
                "status": "closed",
                "quantity": 100.0,
                "opened_at": f"{DAY}T14:30:00+00:00",
                "closed_at": f"{DAY}T15:00:00+00:00",
                "entry_price": 10.0,
                "exit_price": 10.5,
                "realized_return_pct": 5.0,
                "updated_at": f"{DAY}T15:00:00+00:00",
                "payload_json": {"schema_version": "legacy.paper_position.v1"},
            }
        ]
    )
    before = store.load_paper_positions()

    reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=DAY,
        out_dir=tmp_path / "reports",
        persist=True,
        config=ScannerConfig(slippage_bps=50.0),
    )

    assert store.load_paper_positions() == before


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
            == 1
        )
    else:
        result = reconcile_alpha_paper_trades(
            db_path=db_path,
            market_date=DAY,
            out_dir=out_dir,
            persist=False,
            config=ScannerConfig(slippage_bps=50.0),
        )
        assert result["status"] == "failed"

    assert (out_dir / DAY / "reconciliation.json").is_file()
    assert _tree(db_root) == before


def test_reconciliation_correction_atomically_removes_stale_trade_and_return_label(
    tmp_path: Path,
) -> None:
    day = "2026-08-03"
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_v5_reconciliation(store, day=day, with_entry_intent=True)
    reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=day,
        out_dir=tmp_path / "reports",
        config=ScannerConfig(slippage_bps=50.0),
    )
    store.persist_manual_outcomes(
        [
            {
                "outcome_key": "manual-must-not-learn",
                "scan_id": "scan-1",
                "signal_id": "signal-1",
                "ticker": "NOVA",
                "recommendation_timestamp": f"{day}T13:10:00Z",
                "uploaded_at": f"{day}T21:00:00Z",
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

    corrected_outcome = store.load_signal_outcomes(signal_id="signal-1")[0]
    corrected_outcome.pop("path_replay_receipt", None)
    store.persist_signal_outcomes([corrected_outcome], replace=True)
    corrected = reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=day,
        out_dir=tmp_path / "reports",
        config=ScannerConfig(slippage_bps=50.0),
    )
    second_learning = run_alpha_learning(store)

    assert corrected["persistence"]["trades"]["deleted"] == 1
    assert corrected["persistence"]["learning_labels"]["deleted"] == 2
    assert store.load_strategy_paper_trades() == []
    assert store.load_strategy_learning_labels() == []
    assert second_learning["total_return_labels"] == 0
    assert second_learning["return_learning_eligible"] is False
    assert store.load_alpha_outcome_labels() == []


def test_reconciliation_stale_cleanup_rolls_back_with_failed_batch(
    tmp_path: Path,
) -> None:
    day = "2026-08-03"
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    _seed_v5_reconciliation(store, day=day, with_entry_intent=True)
    reconcile_alpha_paper_trades(
        db_path=db_path,
        market_date=day,
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
        "created_at": f"{day}T21:00:00Z",
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
    assert selected["closed_count"] == 0
    learning = run_alpha_learning(store)
    assert learning["total_return_labels"] == 0
    assert learning["return_learning_eligible"] is False


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
    day = "2026-08-03"
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
    intent = store.load_trade_intents(market_date=day, action="ENTER_LONG")[0]
    assert trade["decision_fingerprint"] == intent["decision_fingerprint"]
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
    body = "\n".join(
        [
            "OFFICIAL PAPER CANDIDATES",
            "1) NOVA - Opportunity",
            "",
            "RESEARCH WATCHLIST",
            "- None",
        ]
    )
    body_hash = hashlib.sha256(body.encode()).hexdigest()
    selection = {
        **_direct_selection(),
        "scan_id": "scan-paper",
        "rank": 1,
        "decision": "probability_fallback",
        "event_key": "alphaops:scan-paper:alpha_morning_watch",
        "body_sha256": body_hash,
    }
    selection["payload_json"] = {
        **selection,
        "signal": {
            "signal_id": SIGNAL_ID,
            "scan_id": "scan-paper",
            "ticker": "NOVA",
            "market_date": DAY,
        },
        "decision_payload": {
            "decision": "probability_fallback",
            "research_only": True,
            "broker_execution_enabled": False,
        },
    }
    store.persist_signal_selections([selection])
    if not persist_delivery:
        return
    delivery = {
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
    delivery["payload_json"] = {
        **delivery,
        "body": body,
        "research_only": True,
    }
    store.persist_notification_deliveries([delivery])


def _direct_selection() -> dict[str, object]:
    return {
        **canonical_v6_decision(
            "paper",
            market_date=DAY,
        ),
        "selection_id": SELECTION_ID,
        "scan_id": "scan-paper",
        "signal_id": SIGNAL_ID,
        "ticker": "NOVA",
        "market_date": DAY,
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": DELIVERED_COHORT,
        "selected_at": f"{DAY}T13:10:00+00:00",
        "input_hash_sha256": "8" * 64,
        "source_lineage_hash_sha256": "9" * 64,
        "delivery_identity": {
            "channel": "telegram",
            "event_key": "alphaops:scan-paper:alpha_morning_watch",
            "delivery_status": "delivered",
        },
        "source_artifact_identity": f"alpha-paper-selection:{DAY}:{SELECTION_ID}",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _direct_signal() -> dict[str, object]:
    return {
        "signal_id": SIGNAL_ID,
        "ticker": "NOVA",
        "market_date": DAY,
        "entry_watch_level": 10.0,
        "target_1": 11.0,
        "invalidation_level": 9.0,
    }


def _canonical_reconciliation_outcome() -> dict[str, object]:
    return canonical_return_outcome(
        market_date=DAY,
        causal_identity=_paper_causal_identity(),
    )


def _paper_causal_identity() -> dict[str, object]:
    return causal_identity_from(
        _direct_selection(),
        kind="alpha_v6_shadow_decision",
    )


def _persist_outcome(store: SQLiteScanStore, *, outcome_status: str) -> None:
    row: dict[str, object] = {
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
                **canonical_return_outcome(
                    market_date=DAY,
                    causal_identity=_paper_causal_identity(),
                ),
                "signal_id": SIGNAL_ID,
                "market_date": DAY,
                "ticker": "NOVA",
            }
        )
    else:
        selection_context = canonical_paper_selection_context(
            store.load_signal_selections(signal_id=SIGNAL_ID)[0],
            delivery=store.load_notification_deliveries(signal_id=SIGNAL_ID)[0],
        )
        selection_causal = causal_identity_from(
            selection_context,
            kind="alpha_paper_selection",
            id_key="selection_id",
            time_key="selected_at",
        )
        row.update(
            {
                **canonical_ineligible_outcome(
                    market_date=DAY,
                    case="not_triggered",
                    causal_identity=selection_causal,
                    replay_binding=replay_binding_from(
                        selection_context,
                        kind="alpha_paper_selection",
                        id_key="selection_id",
                    ),
                ),
                "signal_id": SIGNAL_ID,
                "market_date": DAY,
                "ticker": "NOVA",
            }
        )
    store.persist_signal_outcomes([row], replace=True)


def _seed_v5_reconciliation(
    store: SQLiteScanStore,
    *,
    day: str,
    with_entry_intent: bool,
) -> None:
    if with_entry_intent:
        from intraday_scanner.services.alpha_outcome_capture_service import (
            capture_sourced_alpha_outcomes,
        )
        from tests.test_alpha_outcome_capture_service import (
            _canonical_signal,
            _chart_payload,
            _contiguous_bars,
            _persist_selected_signals,
            _two_source_config,
        )

        _persist_selected_signals(
            store,
            [_canonical_signal(day)],
            authenticated_entry=True,
        )
        rows = _contiguous_bars(
            day=day,
            overrides={"10:01": (12.50, 13.00, 12.40, 12.80)},
        )
        capture_sourced_alpha_outcomes(
            db_path=store.db_path,
            market_date=day,
            requested_at=f"{day}T16:05:00-04:00",
            out_dir=store.db_path.parent / "capture",
            persist=True,
            config=_two_source_config(),
            fetcher=lambda *_args, **_kwargs: _chart_payload(rows),
            fallback_fetcher=lambda *_args, **_kwargs: rows,
        )
        return
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
