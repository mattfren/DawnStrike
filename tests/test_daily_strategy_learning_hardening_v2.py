"""Adversarial point-in-time and provenance checks for daily learning."""

import json
import sqlite3
from pathlib import Path

from intraday_scanner.cli import _hash_strategy_learning_inputs, main
from intraday_scanner.config import load_config
from intraday_scanner.performance.strategy_miss_attribution import (
    attribute_strategy_misses,
    load_alpha_v6_decisions_readonly,
    load_portfolio_performance_rows_readonly,
    load_strategy_decision_receipts_readonly,
)
from intraday_scanner.services.alpha_cycle_service import _apply_strategy_decision_receipts
from intraday_scanner.services.daily_strategy_learning_service import (
    DailyLearningContext,
    _normalize_analysis,
    run_daily_strategy_learning,
)
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.strategies import build_strategy_catalog
from tests._alpha_path_truth import canonical_v6_decision
from tests.test_alpha_strategy_decision_integration import _supported_signal


def _context() -> DailyLearningContext:
    return DailyLearningContext(
        market_date="2026-08-20",
        cutoff="2026-08-20T14:30:00+00:00",
        source_identity="adversarial-v2",
        code_sha="test-code",
        source_hash_sha256="a" * 64,
    )


def test_all_populated_terminal_aliases_are_ordered() -> None:
    strategy = build_strategy_catalog()[0]
    evidence, _ = _normalize_analysis(
        strategy,
        _context(),
        {
            "outcomes": [
                {
                    "market_date": "2026-08-20",
                    "status": "RESOLVED",
                    "terminal_event_at": "2026-08-20T14:00:00+00:00",
                    "closed_at": "2026-08-20T15:00:00+00:00",
                    "return_pct": 1.0,
                },
                {
                    "market_date": "2026-08-20",
                    "status": "RESOLVED",
                    "terminal_event_at": "2026-08-20T14:00:00+00:00",
                    "closed_at": "not-a-time",
                    "return_pct": 1.0,
                },
            ],
            "misses": [],
        },
    )
    assert evidence["outcomes"] == []
    assert evidence["counts"]["future_evidence_excluded"] == 1
    assert evidence["counts"]["terminal_timestamp_quarantined"] == 1


def test_proposals_without_orderable_date_or_future_time_are_quarantined() -> None:
    strategy = build_strategy_catalog()[0]
    evidence, proposals = _normalize_analysis(
        strategy,
        _context(),
        {
            "outcomes": [],
            "misses": [],
            "proposals": [
                {"proposal_at": "2026-08-20T15:00:00+00:00"},
                {"market_date": "2026-08-20"},
            ],
        },
    )
    assert proposals == []
    assert evidence["counts"]["proposals_quarantined"] == 2


def test_malformed_market_date_is_quarantined_before_historical_date_ordering() -> None:
    strategy = build_strategy_catalog()[0]
    evidence, _ = _normalize_analysis(
        strategy,
        _context(),
        {
            "outcomes": [
                {
                    "market_date": "2026-08-19-not-an-iso-date",
                    "status": "RESOLVED",
                    "terminal_event_at": "2026-08-19T14:00:00+00:00",
                    "return_pct": 1.0,
                }
            ],
            "misses": [],
        },
    )
    assert evidence["outcomes"] == []
    assert evidence["counts"]["terminal_timestamp_quarantined"] == 1


def test_same_day_miss_without_orderable_timestamp_is_quarantined() -> None:
    strategy = build_strategy_catalog()[0]
    evidence, _ = _normalize_analysis(
        strategy,
        _context(),
        {"outcomes": [], "misses": [{"market_date": "2026-08-20", "status": "MISSING"}]},
    )
    assert evidence["misses"] == []
    assert evidence["counts"]["evidence_timestamp_quarantined"] == 1


def test_portfolio_reconciled_at_after_cutoff_is_not_loaded(tmp_path: Path) -> None:
    database_path = tmp_path / "performance.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, reconciled_at TEXT, payload_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?)",
            [
                ("before", "2026-08-20", "2026-08-20T14:00:00+00:00", "{}"),
                ("after", "2026-08-20", "2026-08-20T15:00:00+00:00", "{}"),
            ],
        )
    rows = load_portfolio_performance_rows_readonly(
        database_path, date_cutoff="2026-08-20T14:30:00+00:00"
    )
    assert [row["record_id"] for row in rows] == ["before"]


def test_rejected_evidence_diagnostics_are_part_of_input_identity() -> None:
    first = {"receipt_id": "forged-a", "receipt_hash_sha256": "0" * 64}
    second = {"receipt_id": "forged-b", "receipt_hash_sha256": "0" * 64}
    first_hash = _hash_strategy_learning_inputs(decision_receipts=(first,))
    second_hash = _hash_strategy_learning_inputs(decision_receipts=(second,))
    assert first_hash != second_hash


def test_plain_or_forged_receipts_cannot_certify_daily_learning(tmp_path: Path) -> None:
    result = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T14:30:00+00:00",
        source_identity="forged-evidence",
        code_sha="test-code",
        out_dir=tmp_path,
        decision_receipts=[
            {
                "receipt_id": "sdr-forged",
                "receipt_hash_sha256": "0" * 64,
                "strategy_id": "alphaops_v5",
                "strategy_version": "dawnstrike-alphaops-v5.0.0",
                "market_date": "2026-08-20",
                "decision_at": "2026-08-20T10:00:00+00:00",
                "research_only": True,
                "broker_execution_enabled": False,
            }
        ],
    )
    artifact = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    learning = artifact["decision_receipt_learning"]
    assert result["status"] == "incomplete"
    assert learning["valid_receipt_count"] == 0
    assert learning["invalid_receipt_count"] == 1
    assert learning["expected_strategy_coverage"]["status"] == "INCOMPLETE"


def test_cli_evidence_file_routes_forged_receipts_through_central_ingress(
    tmp_path: Path, capsys
) -> None:
    evidence_path = tmp_path / "forged-evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "default": {"outcomes": [], "misses": [], "proposals": []},
                "decision_receipts": [
                    {
                        "receipt_id": "cli-forged",
                        "receipt_hash_sha256": "0" * 64,
                        "strategy_id": "alphaops_v5",
                        "strategy_version": "dawnstrike-alphaops-v5.0.0",
                        "market_date": "2026-08-20",
                        "decision_at": "2026-08-20T15:00:00+00:00",
                        "research_only": True,
                        "broker_execution_enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    status = main(
        [
            "strategy-learning-daily",
            "--market-date",
            "2026-08-20",
            "--cutoff",
            "2026-08-20T14:30:00+00:00",
            "--source-identity",
            "cli-forged-source",
            "--code-sha",
            "test-code",
            "--out-dir",
            str(tmp_path / "cli-output"),
            "--evidence-file",
            str(evidence_path),
        ]
    )
    assert status == 1
    result = json.loads(capsys.readouterr().out)
    artifact = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    learning = artifact["decision_receipt_learning"]
    assert learning["valid_receipt_count"] == 0
    assert learning["invalid_receipt_count"] == 1
    assert learning["invalid_receipt_reasons"]
    assert learning["expected_strategy_coverage"]["status"] == "INCOMPLETE"


def test_v6_batch_invalid_rows_are_excluded_and_diagnosed(tmp_path: Path) -> None:
    decision = {
        "decision_id": "invalid-v6",
        "scan_id": "scan-invalid-v6",
        "source_signal_id": "source-invalid-v6",
        "shadow_signal_id": "shadow-invalid-v6",
        "market_date": "2026-08-20",
        "decision_at": "2026-08-20T12:00:00+00:00",
        "ticker": "NOVA",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "model_version": "model-v6",
        "action": "SHADOW_TRACK",
        "setup_key": "breakout",
        "regime_key": "SELECTIVE",
        "safety_vetoes": [],
        "input_hash_sha256": "6" * 64,
        "source_lineage_hash_sha256": "7" * 64,
    }
    database_path = tmp_path / "v6.sqlite"
    SQLiteScanStore(database_path).persist_alpha_v6_decisions([decision])
    batch = load_alpha_v6_decisions_readonly(
        database_path,
        market_date="2026-08-20",
        date_cutoff="9999-12-31T23:59:59+00:00",
    )
    assert batch is not None
    assert len(batch) == 0
    assert batch.invalid_count > 0
    assert batch.invalid_reasons


def test_valid_persisted_v5_is_accepted_and_created_at_cutoff_is_enforced(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("DAWNSTRIKE_CODE_SHA", "a" * 40)
    config = load_config(
        strategy_evidence_enabled=True,
        strategy_evidence_shadow_only=True,
        strategy_evidence_max_candidates=1,
        alert_score_threshold=0,
    )
    signal = _supported_signal()
    signal["market_date"] = "2026-08-20"
    database_path = tmp_path / "v5.sqlite"
    store = SQLiteScanStore(database_path)
    _apply_strategy_decision_receipts(
        [signal],
        store=store,
        config=config,
        decision_at="2026-08-20T12:00:00+00:00",
        source_summary={"source_identity": "v5-fixture"},
    )
    accepted = load_strategy_decision_receipts_readonly(
        database_path,
        market_date="2026-08-20",
        date_cutoff="9999-12-31T23:59:59+00:00",
    )
    assert accepted is not None
    assert len(accepted) == 1
    assert accepted.invalid_count == 0
    with sqlite3.connect(database_path) as connection:
        # Replace the append-only fixture row with a deliberately forged
        # persisted availability timestamp to exercise the readonly loader.
        connection.execute("DROP TRIGGER strategy_decision_receipts_no_update")
        connection.execute(
            "UPDATE strategy_decision_receipts SET created_at = ?",
            ("9999-12-31T23:59:59+00:00",),
        )
    rejected = load_strategy_decision_receipts_readonly(
        database_path,
        market_date="2026-08-20",
        date_cutoff="2026-08-20T14:30:00+00:00",
    )
    assert rejected is not None
    assert len(rejected) == 0
    assert rejected.invalid_reasons == {"persisted_created_at_after_cutoff": 1}


def test_checked_empty_v5_and_v6_report_no_evidence_without_certifying_run(
    tmp_path: Path,
) -> None:
    result = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T14:30:00+00:00",
        source_identity="checked-empty-source",
        code_sha="test-code",
        out_dir=tmp_path,
        decision_receipts=(),
        v6_decisions=(),
    )
    artifact = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    learning = artifact["decision_receipt_learning"]
    coverage = learning["expected_strategy_coverage"]
    assert result["status"] == "incomplete"
    assert coverage["status"] == "COMPLETE"
    assert coverage["source_result"] == "NO_EVIDENCE"
    assert coverage["v6_source_status"] == "NO_EVIDENCE"


def test_valid_v6_is_accepted_only_through_stored_at_governed_lane(tmp_path: Path) -> None:
    decision = canonical_v6_decision("valid-v6", market_date="2026-08-20")
    decision.update(
        {
            "execution_assumptions": {"policy": "research_only"},
            "uncertainty": {"status": "UNSCORED"},
            "universe_membership": {
                "universe_id": "fixture-universe",
                "source_lineage_hash_sha256": "8" * 64,
            },
        }
    )
    database_path = tmp_path / "v6-valid.sqlite"
    SQLiteScanStore(database_path).persist_alpha_v6_decisions([decision])
    accepted = load_alpha_v6_decisions_readonly(
        database_path,
        market_date="2026-08-20",
        date_cutoff="9999-12-31T23:59:59+00:00",
    )
    assert accepted is not None
    assert len(accepted) == 1
    result = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="9999-12-31T23:59:59+00:00",
        source_identity="v6-source",
        code_sha="test-code",
        out_dir=tmp_path / "learning",
        decision_receipts=(),
        v6_decisions=accepted,
    )
    artifact = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    learning = artifact["decision_receipt_learning"]
    assert learning["v6_source_status"] == "PROVIDED"
    assert learning["v6_decision_count"] == 1

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE alpha_v6_decisions SET stored_at = ?",
            ("9999-12-31T23:59:59+00:00",),
        )
    rejected = load_alpha_v6_decisions_readonly(
        database_path,
        market_date="2026-08-20",
        date_cutoff="2026-08-20T14:30:00+00:00",
    )
    assert rejected is not None
    assert len(rejected) == 0
    assert rejected.invalid_reasons == {"stored_at_after_cutoff": 1}


def test_db_retry_uses_frozen_cutoff_when_later_row_is_added(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "retry.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, reconciled_at TEXT, payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?)",
            ("before", "2026-08-20", "2026-08-20T14:00:00+00:00", "{}"),
        )
    arguments = [
        "strategy-learning-daily",
        "--market-date",
        "2026-08-20",
        "--cutoff",
        "2026-08-20T14:30:00+00:00",
        "--source-identity",
        "retry-db-source",
        "--code-sha",
        "test-code",
        "--out-dir",
        str(tmp_path / "learning"),
        "--db-path",
        str(database_path),
    ]
    assert main(arguments) == 1
    capsys.readouterr()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?)",
            ("later", "2026-08-20", "2026-08-20T15:00:00+00:00", "{}"),
        )
    assert main(arguments) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["idempotent_reused"] is True


def test_migration_032_adds_v6_stored_at_to_preexisting_schema_31_db(tmp_path: Path) -> None:
    database_path = tmp_path / "pre-032.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_version VALUES (31, '2026-08-20T00:00:00+00:00')"
        )
        connection.execute(
            "CREATE TABLE alpha_v6_decisions ("
            "decision_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)"
        )
        assert run_migrations(connection) == 31
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(alpha_v6_decisions)")
        }
    assert "stored_at" in columns


def test_retry_reuses_first_cutoff_reservation(tmp_path: Path) -> None:
    first = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T10:00:00+00:00",
        source_identity="retry-source",
        code_sha="test-code",
        out_dir=tmp_path,
        decision_receipts=(),
        v6_decisions=(),
    )

    class RetryMustNotAnalyze:
        def analyze(self, strategy, context):
            raise AssertionError("retry reused no frozen cutoff")

    second = run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T11:00:00+00:00",
        source_identity="retry-source",
        code_sha="test-code",
        out_dir=tmp_path,
        analyzer=RetryMustNotAnalyze(),
        decision_receipts=(),
        v6_decisions=(),
    )
    assert second["run_id"] == first["run_id"]
    assert second["idempotent_reused"] is True


def test_direct_retry_with_changed_receipt_evidence_cannot_reuse_artifact(tmp_path: Path) -> None:
    first = {"receipt_id": "first-forged", "receipt_hash_sha256": "0" * 64}
    run_daily_strategy_learning(
        market_date="2026-08-20",
        cutoff="2026-08-20T10:00:00+00:00",
        source_identity="direct-retry-source",
        code_sha="test-code",
        out_dir=tmp_path,
        decision_receipts=[first],
    )
    second = {"receipt_id": "second-forged", "receipt_hash_sha256": "0" * 64}
    try:
        run_daily_strategy_learning(
            market_date="2026-08-20",
            cutoff="2026-08-20T11:00:00+00:00",
            source_identity="direct-retry-source",
            code_sha="test-code",
            out_dir=tmp_path,
            decision_receipts=[second],
        )
    except ValueError as exc:
        assert "invocation identity conflict: input_hash_sha256" in str(exc)
    else:
        raise AssertionError("changed direct evidence reused a frozen artifact")


def test_aggregate_record_type_self_label_does_not_exempt_fill_truth() -> None:
    report = attribute_strategy_misses(
        [
            {
                "record_id": "forged-aggregate",
                "market_date": "2026-08-20",
                "cohort": "official_forward_paper",
                "strategy_id": "ts_momentum_sma_atr",
                "strategy_version": "v1.0",
                "record_status": "realized",
                "record_type": "portfolio_aggregate",
                "return_pct": 2.0,
                "close_time": "2026-08-20T14:00:00+00:00",
            }
        ]
    )
    row = report.rows[0]
    assert row.eligibility.value == "ineligible"
    assert row.classification == "closed_provisional"
