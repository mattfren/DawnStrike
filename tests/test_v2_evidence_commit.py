from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.evidence_commit import (
    commit,
    init,
    propose,
    rebuild_state,
    reconcile,
    reject,
    report,
    review,
)

RUN_DATE = date(2026, 1, 5)
ORDER_ID = "order:forward:2026-01-05:fixture_strategy:v1:TST:2026-01-02T21:00:00+00:00:long"
PICK_ID = "forward:2026-01-05:fixture_strategy:v1:TST:2026-01-02T21:00:00+00:00:long"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            json.dump(row, handle, sort_keys=True)
            handle.write("\n")


def _seed_fixture(
    *,
    synthetic: bool = False,
    include_pending: bool = True,
    provider_backed: bool = False,
    canonical_hash: str = "canonical-provider-hash",
    canonical_duplicate_timestamp_count: int = 0,
) -> None:
    data_type = (
        "synthetic_demo"
        if synthetic
        else "broker_or_vendor_intraday"
        if provider_backed
        else "real_local_intraday"
    )
    provider = (
        "local_demo_intraday_fixture"
        if synthetic
        else "autodata:alpaca_market_data"
        if provider_backed
        else "broker_export_csv"
    )
    source_file_sha256 = (
        "raw-provider-hash"
        if provider_backed
        else "raw-fixture-hash"
        if not synthetic
        else ""
    )
    reconciliation_status = (
        "not_applicable_demo"
        if synthetic
        else "provider_with_public_fallback_comparison"
        if provider_backed
        else "reconciled"
    )
    decision = {
        "data_granularity": "intraday",
        "data_snapshot_id": "filltruth_intraday_fixture",
        "direction": "long",
        "execution_model": "intraday_bar_sequence",
        "fee": 0.01,
        "fill_certainty": "intraday_sequence_supported",
        "fill_id": "filltruth:fixture",
        "fill_price": 10.01,
        "fill_time": "2026-01-05T14:30:00+00:00",
        "order_id": ORDER_ID,
        "quantity": 10,
        "resolution_status": "filled",
        "run_date": RUN_DATE.isoformat(),
        "slippage": 0.1,
        "source_provider": provider,
        "stop": 9.5,
        "strategy_id": "fixture_strategy",
        "symbol": "TST",
        "target": 11.0,
        "warnings": [],
    }
    _write_json(
        Path("data/v2_fill_truth/reports/pending_resolution_latest.json"),
        {
            "decisions": [decision],
            "pending_orders_inspected": 1 if include_pending else 0,
            "run_date": RUN_DATE.isoformat(),
            "status": "passed",
        },
    )
    _write_json(
        Path("data/v2_fill_truth/manifests/filltruth_resolve_2026-01-05_fixture.json"),
        {"run_id": "filltruth_resolve_fixture", "schema_version": "fixture"},
    )
    _write_json(
        Path("data/v2_fill_truth/manifests/latest_intraday_import.json"),
        {
            "data_type": data_type,
            "daily_reconciliation_status": reconciliation_status,
            "filltruth_commit_eligible": (
                not synthetic
                and (not provider_backed or bool(canonical_hash))
                and canonical_duplicate_timestamp_count == 0
            ),
            "accepted_row_count": 2 if not synthetic else 0,
            "row_count": 2 if not synthetic else 0,
            "session_completeness": "complete_session",
            "normalized_artifact_hash": "abc123",
            "snapshot_id": "filltruth_intraday_fixture",
            "source_file_sha256": source_file_sha256,
            "source_label": data_type if synthetic else data_type,
            "source_provider": provider,
            **(
                {
                    "canonical_provider_id": "alpaca_market_data",
                    "comparison_provider_ids": ["yahoo_chart_public_fallback"],
                    "canonical_dataset_hash": canonical_hash,
                    "canonical_duplicate_timestamp_count": canonical_duplicate_timestamp_count,
                    "provider_reconciliation_status": reconciliation_status,
                }
                if provider_backed
                else {}
            ),
        },
    )
    pending = [
        {
            "direction": "long",
            "entry": 10.0,
            "mode": "forward",
            "order_id": ORDER_ID,
            "order_status": "pending",
            "pick_id": PICK_ID,
            "quantity": 10,
            "run_id": "paper_ops:forward:2026-01-05:fixture",
            "signal_time": "2026-01-02T21:00:00+00:00",
            "stop": 9.5,
            "strategy_id": "fixture_strategy",
            "strategy_version": "v1",
            "symbol": "TST",
            "target": 11.0,
        }
    ]
    pending_path = Path("data/v2_paper_ops/state/pending_orders.json")
    _write_json(pending_path, pending if include_pending else [])
    _write_json(Path("data/v2_paper_ops/state/open_positions.json"), [])
    _write_json(Path("data/v2_paper_ops/state/replay_pending_orders.json"), [])
    _write_json(Path("data/v2_paper_ops/state/strategy_registry.json"), [
        {
            "strategy_id": "fixture_strategy",
            "strategy_status": "experimental",
            "strategy_version": "v1",
        }
    ])
    _write_json(
        Path("data/v2_paper_ops/state/paper_ops_config.json"),
        {"starting_equity": 100000.0},
    )
    _append_jsonl(
        Path("data/v2_paper_ops/ledger/paper_ledger.jsonl"),
        [
            {
                "event_id": "pick-fixture",
                "event_type": "paper_pick_decision",
                "mode": "forward",
                "payload": {
                    "pick_id": PICK_ID,
                    "strategy_status": "experimental",
                    "strategy_version": "v1",
                },
                "run_id": "paper_ops:forward:2026-01-05:fixture",
                "strategy_id": "fixture_strategy",
                "symbol": "TST",
                "trade_date": RUN_DATE.isoformat(),
            },
            {
                "event_id": "order-fixture",
                "event_type": "paper_order_created",
                "mode": "forward",
                "payload": pending[0],
                "run_id": "paper_ops:forward:2026-01-05:fixture",
                "strategy_id": "fixture_strategy",
                "symbol": "TST",
                "trade_date": RUN_DATE.isoformat(),
            },
        ],
    )
    _write_json(
        Path("data/v2_forward_evidence/frozen_picks/2026-01-05_picks.json"),
        {"pick_set_hash": "frozen-hash", "rows": [{"pick_id": PICK_ID}]},
    )
    _write_json(
        Path("data/v2_forward_evidence/pick_hashes/2026-01-05_hash.json"),
        {"pick_set_hash": "frozen-hash"},
    )


def test_synthetic_filltruth_proposal_is_blocked_and_rejectable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(synthetic=True)
    init()

    proposal = propose(run_date=RUN_DATE)
    review_payload = review(run_date=RUN_DATE)
    commit_payload = commit(run_date=RUN_DATE)
    rejection = reject(run_date=RUN_DATE, reason="synthetic source")
    reconciliation = reconcile(run_date=RUN_DATE)

    assert proposal["blocked"] == 1
    assert review_payload["blocking_reasons"] == [
        "demo or synthetic FillTruth source cannot commit into true forward state"
    ]
    assert commit_payload["committed_count"] == 0
    assert rejection["rejected_count"] == 1
    assert reconciliation["pending_divergence_status"] == "resolved_by_policy_block_or_rejection"


def test_rejection_survives_regenerated_filltruth_artifact(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(synthetic=True)
    init()

    propose(run_date=RUN_DATE)
    first = json.loads(
        Path("data/v2_evidence_commit/proposals/latest_proposals.json").read_text()
    )["proposals"][0]
    reject(run_date=RUN_DATE, reason="synthetic source")

    resolution_path = Path("data/v2_fill_truth/reports/pending_resolution_latest.json")
    resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    resolution["regenerated_at"] = "2026-01-05T23:00:00+00:00"
    _write_json(resolution_path, resolution)

    propose(run_date=RUN_DATE)
    second = json.loads(
        Path("data/v2_evidence_commit/proposals/latest_proposals.json").read_text()
    )["proposals"][0]
    reconciliation = reconcile(run_date=RUN_DATE)
    summary = report()

    assert second["source_filltruth_artifact_hash"] != first["source_filltruth_artifact_hash"]
    assert second["proposal_id"] == first["proposal_id"]
    assert reconciliation["proposals_rejected"] == 1
    assert summary["rejected"] == 1


def test_real_intraday_readiness_blocks_zero_row_real_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(synthetic=False)
    meta_path = Path("data/v2_fill_truth/manifests/latest_intraday_import.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update(
        {
            "accepted_row_count": 0,
            "daily_reconciliation_status": "invalid_intraday",
            "filltruth_commit_eligible": False,
            "row_count": 0,
        }
    )
    _write_json(meta_path, meta)
    init()

    summary = report()
    readiness = json.loads(
        Path("data/v2_evidence_commit/reports/real_intraday_readiness.json").read_text(
            encoding="utf-8"
        )
    )

    assert readiness["status"] == "blocked_needs_real_intraday"
    assert readiness["accepted_row_count"] == 0
    assert summary["real_intraday_readiness_status"] == "blocked_needs_real_intraday"


def test_real_local_intraday_proposal_commits_append_only_and_rebuilds_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(synthetic=False)
    init()
    propose(run_date=RUN_DATE)

    before = Path("data/v2_paper_ops/ledger/paper_ledger.jsonl").read_text(encoding="utf-8")
    commit_payload = commit(run_date=RUN_DATE)
    after = Path("data/v2_paper_ops/ledger/paper_ledger.jsonl").read_text(encoding="utf-8")
    rebuild_state(run_date=RUN_DATE)
    reconciliation = reconcile(run_date=RUN_DATE)

    assert after.startswith(before)
    assert commit_payload["committed_count"] == 3
    assert json.loads(Path("data/v2_paper_ops/state/pending_orders.json").read_text()) == []
    open_positions = json.loads(Path("data/v2_paper_ops/state/open_positions.json").read_text())
    assert len(open_positions) == 1
    assert reconciliation["pending_divergence_status"] == "resolved_by_commit"


def test_provider_backed_intraday_proposal_commits_with_canonical_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(provider_backed=True)
    init()

    proposal = propose(run_date=RUN_DATE, require_provider_intraday=True)
    row = json.loads(
        Path("data/v2_evidence_commit/proposals/latest_proposals.json").read_text(
            encoding="utf-8"
        )
    )["proposals"][0]
    commit_payload = commit(run_date=RUN_DATE, require_provider_intraday=True)

    assert proposal["eligible"] == 1
    assert row["source_kind"] == "broker_or_vendor_intraday"
    assert row["canonical_provider_id"] == "alpaca_market_data"
    assert row["comparison_provider_ids"] == ["yahoo_chart_public_fallback"]
    assert row["canonical_dataset_hash"] == "canonical-provider-hash"
    assert row["canonical_duplicate_timestamp_count"] == 0
    assert row["provider_reconciliation_status"] == "provider_with_public_fallback_comparison"
    assert commit_payload["committed_count"] == 3


def test_provider_backed_commit_blocks_missing_canonical_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(provider_backed=True, canonical_hash="")
    init()

    proposal = propose(run_date=RUN_DATE, require_provider_intraday=True)
    commit_payload = commit(run_date=RUN_DATE, require_provider_intraday=True)
    row = json.loads(
        Path("data/v2_evidence_commit/proposals/latest_proposals.json").read_text(
            encoding="utf-8"
        )
    )["proposals"][0]

    assert proposal["blocked"] == 1
    assert row["commit_eligibility"] == "blocked_invalid_data"
    assert "canonical provider dataset hash is missing" in row["blocking_reasons"]
    assert commit_payload["committed_count"] == 0


def test_provider_backed_commit_blocks_duplicate_canonical_timestamps(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(provider_backed=True, canonical_duplicate_timestamp_count=1)
    init()

    proposal = propose(run_date=RUN_DATE, require_provider_intraday=True)
    commit_payload = commit(run_date=RUN_DATE, require_provider_intraday=True)
    row = json.loads(
        Path("data/v2_evidence_commit/proposals/latest_proposals.json").read_text(
            encoding="utf-8"
        )
    )["proposals"][0]

    assert proposal["blocked"] == 1
    assert row["commit_eligibility"] == "blocked_invalid_data"
    assert (
        "canonical provider dataset contains duplicate symbol/timestamp rows"
        in row["blocking_reasons"]
    )
    assert commit_payload["committed_count"] == 0


def test_duplicate_fill_event_blocks_second_proposal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(synthetic=False)
    init()
    propose(run_date=RUN_DATE)
    commit(run_date=RUN_DATE)

    second = propose(run_date=RUN_DATE)
    proposals = json.loads(
        Path("data/v2_evidence_commit/proposals/latest_proposals.json").read_text()
    )

    assert second["blocked"] == 1
    assert proposals["proposals"][0]["commit_eligibility"] == "blocked_duplicate_event"


def test_missing_pending_order_blocks_orphan_fill(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(synthetic=False, include_pending=False)
    init()

    propose(run_date=RUN_DATE)
    proposals = json.loads(
        Path("data/v2_evidence_commit/proposals/latest_proposals.json").read_text()
    )

    assert proposals["proposals"][0]["commit_eligibility"] == "blocked_missing_order"


def test_report_and_command_center_pages_expose_commitbridge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_fixture(synthetic=True)
    init()
    propose(run_date=RUN_DATE)
    commit(run_date=RUN_DATE)
    reject(run_date=RUN_DATE, reason="synthetic source")
    rebuild_state(run_date=RUN_DATE)
    reconcile(run_date=RUN_DATE)
    report()

    center = build_command_center()

    assert center.status == "passed"
    for page in (
        "evidence_commit.html",
        "commit_proposals.html",
        "pending_divergence.html",
        "real_intraday_readiness.html",
    ):
        text = Path("data/v2_command_center", page).read_text(encoding="utf-8")
        assert "research-only; no live execution." in text.lower()
        assert "<script" not in text.lower()


def test_evidence_commit_modules_avoid_live_execution_and_database_imports() -> None:
    forbidden_import_roots = {
        "app",
        "httpx",
        "requests",
        "socket",
        "sqlite3",
        "streamlit",
        "urllib",
    }
    forbidden_import_prefixes = {"intraday_scanner.integrations", "intraday_scanner.storage"}
    forbidden_calls = {"connect", "execute", "executemany", "submit" + "_order"}

    for path in Path("intraday_scanner/v2/evidence_commit").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_import_roots, path
                    assert not any(
                        alias.name.startswith(prefix) for prefix in forbidden_import_prefixes
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_import_roots, path
                assert not any(
                    node.module.startswith(prefix) for prefix in forbidden_import_prefixes
                )
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path
