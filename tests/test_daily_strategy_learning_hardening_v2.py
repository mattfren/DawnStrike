"""Adversarial point-in-time and provenance checks for daily learning."""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import intraday_scanner.cli as cli_module
from intraday_scanner.cli import _hash_strategy_learning_inputs, _no_evidence_candidates, main
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
from intraday_scanner.v2.paper_ops.trade_blotter import (
    ReadOnlyBlotterRows,
    hash_trade_blotter_readonly_inputs,
)
from intraday_scanner.v2.strategies import build_strategy_catalog
from tests._alpha_path_truth import canonical_v6_decision
from tests.test_alpha_strategy_decision_integration import _supported_signal


@pytest.fixture(autouse=True)
def _daily_learning_hmac_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Production imports the persistent key from runtime.env.  Keep these
    # CLI tests deterministic without ever putting a key in an output tree.
    monkeypatch.setenv("DAWNSTRIKE_DAILY_LEARNING_HMAC_KEY", "test-learning-key-" + "x" * 32)


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
    assert coverage["status"] == "INCOMPLETE"
    assert coverage["source_result"] == "NO_EVIDENCE"
    assert coverage["v6_source_status"] == "NO_EVIDENCE"
    assert coverage["missing"] == [
        {
            "strategy_id": "alphaops_v5",
            "strategy_version": "dawnstrike-alphaops-v5.0.0",
            "reason": "no_authenticated_explicit_no_evidence_receipt",
        }
    ]


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


def test_db_acquisition_holds_one_write_blocking_snapshot_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "held-snapshot.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, reconciled_at TEXT, payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?)",
            ("before", "2026-08-20", "2026-08-20T14:00:00+00:00", "{}"),
        )
    args = SimpleNamespace(
        market_date="2026-08-20",
        cutoff="2026-08-20T14:30:00+00:00",
        source_identity="held-snapshot-source",
        source_hash_sha256=None,
        code_sha="test-code",
        out_dir=str(tmp_path / "learning"),
        paper_ops_root=None,
        evidence_file=None,
        db_path=str(database_path),
    )
    writer_result: dict[str, str] = {}
    writer_done = threading.Event()

    def writer() -> None:
        try:
            with sqlite3.connect(database_path, timeout=0.1) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?)",
                    ("during", "2026-08-20", "2026-08-20T14:15:00+00:00", "{}"),
                )
                connection.commit()
            writer_result["status"] = "committed"
        except sqlite3.OperationalError as exc:
            writer_result["status"] = "blocked" if "locked" in str(exc).lower() else str(exc)
        finally:
            writer_done.set()

    original_acquire = cli_module._acquire_strategy_learning_evidence

    def gated_acquire(current_args):
        connection = current_args._learning_db_connection
        assert connection.in_transaction
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        assert writer_done.wait(timeout=5)
        writer_thread.join(timeout=5)
        assert not writer_thread.is_alive()
        return original_acquire(current_args)

    monkeypatch.setattr(cli_module, "_acquire_strategy_learning_evidence", gated_acquire)
    assert cli_module._run_strategy_learning_daily(args) == 1
    assert writer_result == {"status": "blocked"}


def test_missing_database_tables_cannot_mint_authenticated_zero_receipts(
    tmp_path: Path, capsys
) -> None:
    database_path = tmp_path / "missing-lanes.sqlite"
    sqlite3.connect(database_path).close()
    arguments = [
        "strategy-learning-daily",
        "--market-date",
        "2026-08-20",
        "--cutoff",
        "2026-08-20T14:30:00+00:00",
        "--source-identity",
        "missing-lanes-source",
        "--code-sha",
        "test-code",
        "--out-dir",
        str(tmp_path / "learning"),
        "--db-path",
        str(database_path),
    ]
    assert main(arguments) == 1
    result = json.loads(capsys.readouterr().out)
    snapshot = json.loads(
        (
            tmp_path
            / "learning"
            / "2026-08-20"
            / "daily_learning_evidence_snapshot.json"
        ).read_text(encoding="utf-8")
    )
    assert result["status"] == "incomplete"
    assert snapshot["no_evidence_receipts"] == []
    assert all(
        bound["exists"] is False
        for bound in snapshot["source_generation"]["table_bounds"].values()
    )


def test_paper_ops_warningful_empty_materialization_cannot_mint_zero_receipts(
    tmp_path: Path,
) -> None:
    paper_rows = ReadOnlyBlotterRows(
        [],
        input_hash_sha256="a" * 64,
        ledger_hash_sha256="b" * 64,
        warnings=["orphan fill for order missing-order"],
        input_generation={
            "files": [{"path": "ledger/paper_ledger.jsonl", "sha256": "c" * 64, "size": 0}]
        },
    )
    args = SimpleNamespace(market_date="2026-08-20", cutoff="2026-08-20T14:30:00+00:00")
    candidates = _no_evidence_candidates(
        args,
        rows=(),
        decision_receipts=None,
        v6_decisions=None,
        paper_ops_rows=paper_rows,
        source_generation={"mode": "database", "paper_ops": paper_rows.read_only_input_generation},
        component_hashes={"paper_ops_lifecycle_rows": "d" * 64},
    )
    assert candidates == []


@pytest.mark.parametrize("forged_artifact", ["reservation", "acquisition"])
def test_self_hashed_but_unsigned_learning_boundary_is_rejected(
    tmp_path: Path, capsys, forged_artifact: str
) -> None:
    evidence_path = tmp_path / "boundary-evidence.json"
    evidence_path.write_text(
        json.dumps({"default": {"outcomes": [], "misses": [], "proposals": []}}),
        encoding="utf-8",
    )
    out_dir = tmp_path / "learning"
    arguments = [
        "strategy-learning-daily",
        "--market-date",
        "2026-08-20",
        "--cutoff",
        "2026-08-20T14:30:00+00:00",
        "--source-identity",
        "boundary-forgery-source",
        "--code-sha",
        "test-code",
        "--out-dir",
        str(out_dir),
        "--evidence-file",
        str(evidence_path),
    ]
    assert main(arguments) == 1
    capsys.readouterr()
    root = out_dir / "2026-08-20"
    snapshot_path = root / "daily_learning_evidence_snapshot.json"
    if forged_artifact == "reservation":
        target_path = root / "daily_learning_invocation.json"
        payload = json.loads(target_path.read_text(encoding="utf-8"))
        payload["source_identity"] = "forged-source"
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"reservation_sha256", "signature_hmac_sha256"}
        }
        payload["reservation_sha256"] = hashlib.sha256(
            cli_module._canonical_input_bytes(body)
        ).hexdigest()
    else:
        target_path = root / "daily_learning_acquisition_manifest.json"
        payload = json.loads(target_path.read_text(encoding="utf-8"))
        payload["input_hash_sha256"] = "0" * 64
        body = {
            key: value
            for key, value in payload.items()
            if key
            not in {
                "snapshot_sha256",
                "acquisition_manifest_sha256",
                "no_evidence_receipts",
                "signature_hmac_sha256",
            }
        }
        payload["acquisition_manifest_sha256"] = hashlib.sha256(
            cli_module._canonical_input_bytes(body)
        ).hexdigest()
    target_path.write_text(json.dumps(payload), encoding="utf-8")
    args = SimpleNamespace(
        out_dir=str(out_dir),
        market_date="2026-08-20",
        cutoff="2026-08-20T14:30:00+00:00",
        source_identity="boundary-forgery-source",
        source_hash_sha256=None,
        code_sha="test-code",
        evidence_file=str(evidence_path),
        db_path=None,
        paper_ops_root=None,
    )
    with pytest.raises(cli_module.SnapshotValidationError, match="signature"):
        cli_module._read_strategy_learning_evidence_snapshot(snapshot_path, args)


def test_paper_ops_retry_consumes_frozen_cohort_without_rereading_grown_ledger(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    database_path = tmp_path / "paper-retry.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, reconciled_at TEXT, payload_json TEXT)"
        )
    paper_root = tmp_path / "paper-ops"
    paper_root.mkdir()
    (paper_root / "ledger").mkdir()
    (paper_root / "state").mkdir()
    (paper_root / "ledger" / "paper_ledger.jsonl").write_text("", encoding="utf-8")
    (paper_root / "state" / "paper_ops_config.json").write_text("{}", encoding="utf-8")
    (paper_root / "state" / "strategy_registry.json").write_text("{}", encoding="utf-8")
    calls = 0

    def first_materialization(*, output_root, mode):
        nonlocal calls
        assert Path(output_root) == paper_root
        assert mode == "forward"
        calls += 1
        return ReadOnlyBlotterRows(
            [
                {
                    "mode": "forward",
                    "signal_date": "2026-08-20",
                    "strategy_id": "ts_momentum_sma_atr",
                    "strategy_version": "v1.0",
                    "series_role": "champion",
                    "symbol": "NOVA",
                    "lifecycle_status": "blocked",
                    "order_id": "paper-order-1",
                }
            ],
            input_hash_sha256=hash_trade_blotter_readonly_inputs(paper_root),
            ledger_hash_sha256="c" * 64,
            warnings=[],
        )

    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.trade_blotter.load_trade_blotter_readonly",
        first_materialization,
    )
    arguments = [
        "strategy-learning-daily",
        "--market-date",
        "2026-08-20",
        "--cutoff",
        "2026-08-20T14:30:00+00:00",
        "--source-identity",
        "paper-retry-source",
        "--code-sha",
        "test-code",
        "--out-dir",
        str(tmp_path / "learning"),
        "--db-path",
        str(database_path),
        "--paper-ops-root",
        str(paper_root),
    ]
    assert main(arguments) == 1
    capsys.readouterr()
    assert calls == 1

    def forbidden_reread(**_kwargs):
        raise AssertionError("retry reread the grown PaperOps ledger")

    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.trade_blotter.load_trade_blotter_readonly",
        forbidden_reread,
    )
    assert main(arguments) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["idempotent_reused"] is True
    assert calls == 1


def test_snapshot_before_reservation_recovers_with_original_cutoff_and_source(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    database_path = tmp_path / "crash-window.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, reconciled_at TEXT, payload_json TEXT)"
        )
    out_dir = tmp_path / "learning"
    first_arguments = [
        "strategy-learning-daily",
        "--market-date",
        "2026-08-20",
        "--cutoff",
        "2026-08-20T14:30:00+00:00",
        "--source-identity",
        "first-source-cutoff-14:30",
        "--code-sha",
        "test-code",
        "--out-dir",
        str(out_dir),
        "--db-path",
        str(database_path),
    ]
    original_service = cli_module.run_daily_strategy_learning

    def interrupt_after_snapshot(**_kwargs):
        raise RuntimeError("simulated crash after frozen evidence")

    monkeypatch.setattr(cli_module, "run_daily_strategy_learning", interrupt_after_snapshot)
    with pytest.raises(RuntimeError, match="simulated crash"):
        main(first_arguments)
    snapshot_path = (
        out_dir / "2026-08-20" / "daily_learning_evidence_snapshot.json"
    )
    assert snapshot_path.is_file()
    assert (out_dir / "2026-08-20" / "daily_learning_invocation.json").is_file()

    monkeypatch.setattr(cli_module, "run_daily_strategy_learning", original_service)
    retry_arguments = list(first_arguments)
    retry_arguments[retry_arguments.index("--cutoff") + 1] = (
        "2026-08-20T14:31:00+00:00"
    )
    retry_arguments[retry_arguments.index("--source-identity") + 1] = (
        "retry-source-cutoff-14:31"
    )
    assert main(retry_arguments) == 1
    result = json.loads(capsys.readouterr().out)
    reservation = json.loads(
        (out_dir / "2026-08-20" / "daily_learning_invocation.json").read_text(
            encoding="utf-8"
        )
    )
    assert reservation["cutoff"] == "2026-08-20T14:30:00+00:00"
    assert reservation["source_identity"] == "first-source-cutoff-14:30"
    assert result["input_hash_sha256"] == reservation["input_hash_sha256"]


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
    run_daily_strategy_learning(
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

    with pytest.raises(ValueError, match="invocation identity conflict: cutoff"):
        run_daily_strategy_learning(
            market_date="2026-08-20",
            cutoff="2026-08-20T11:00:00+00:00",
            source_identity="retry-source",
            code_sha="test-code",
            out_dir=tmp_path,
            analyzer=RetryMustNotAnalyze(),
            decision_receipts=(),
            v6_decisions=(),
        )


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
            cutoff="2026-08-20T10:00:00+00:00",
            source_identity="direct-retry-source",
            code_sha="test-code",
            out_dir=tmp_path,
            decision_receipts=[second],
        )
    except ValueError as exc:
        assert "invocation identity conflict: input_hash_sha256" in str(exc)
    else:
        raise AssertionError("changed direct evidence reused a frozen artifact")


def test_phase_two_input_binding_is_first_writer_wins_under_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text("{}", encoding="utf-8")
    args = SimpleNamespace(
        out_dir=str(tmp_path / "learning"),
        market_date="2026-08-20",
        cutoff="2026-08-20T14:30:00+00:00",
        source_identity="race-source",
        source_hash_sha256=None,
        code_sha="test-code",
        evidence_file=str(evidence_path),
        db_path=None,
        paper_ops_root=None,
    )
    cli_module._reserve_learning_invocation(args)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    original_unlocked = cli_module._bind_learning_invocation_input_hash_unlocked

    def gated_unlocked(current_args, input_hash_sha256):
        if input_hash_sha256 == "a" * 64:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return original_unlocked(current_args, input_hash_sha256)

    monkeypatch.setattr(
        cli_module,
        "_bind_learning_invocation_input_hash_unlocked",
        gated_unlocked,
    )
    outcomes: dict[str, tuple[str, object]] = {}

    def bind(label: str, input_hash_sha256: str) -> None:
        try:
            outcomes[label] = (
                "ok",
                cli_module._bind_learning_invocation_input_hash(args, input_hash_sha256),
            )
        except Exception as exc:  # pragma: no cover - assertion below reports the race result
            outcomes[label] = ("error", exc)

    first = threading.Thread(target=bind, args=("first", "a" * 64))
    second = threading.Thread(target=bind, args=("second", "b" * 64))
    first.start()
    try:
        assert first_entered.wait(timeout=5)
        second.start()
        assert not second_entered.wait(timeout=0.25)
    finally:
        release_first.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert outcomes["first"][0] == "ok"
    assert outcomes["second"][0] == "error"
    assert isinstance(outcomes["second"][1], cli_module.SnapshotValidationError)
    assert "input_hash_sha256" in str(outcomes["second"][1])
    reservation = json.loads(
        cli_module._strategy_learning_reservation_path(args).read_text(encoding="utf-8")
    )
    assert reservation["input_hash_sha256"] == "a" * 64


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
