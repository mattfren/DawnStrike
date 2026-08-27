"""Adversarial point-in-time and provenance checks for daily learning."""

import json
from pathlib import Path

from intraday_scanner.cli import main
from intraday_scanner.performance.strategy_miss_attribution import (
    attribute_strategy_misses,
    load_alpha_v6_decisions_readonly,
)
from intraday_scanner.services.daily_strategy_learning_service import (
    DailyLearningContext,
    _normalize_analysis,
    run_daily_strategy_learning,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.strategies import build_strategy_catalog


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
