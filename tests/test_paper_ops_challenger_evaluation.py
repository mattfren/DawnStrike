from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops import engine as paper_engine
from intraday_scanner.v2.paper_ops.challenger_evaluation import (
    ChallengerEvaluationConfig,
    evaluate_paperops_challengers,
)
from intraday_scanner.v2.paper_ops.shadow_runner import (
    REGISTRATION_EVENT_SCHEMA,
    SHADOW_MANIFEST_SCHEMA,
    _freeze_registration,
    _sha256,
)
from intraday_scanner.v2.paper_ops.storage import (
    append_jsonl_unique,
    read_json,
    read_jsonl,
    write_csv,
    write_json,
)
from intraday_scanner.v2.paper_ops.trade_blotter import (
    build_trade_blotter,
    verify_trade_blotter,
)
from intraday_scanner.v2.strategies import build_strategy_catalog


def _write_complete_forward_manifest(
    root: Path,
    *,
    session_date: str,
    run_id: str,
    snapshot: str,
) -> None:
    payload: dict[str, object] = {
        "schema_version": "v2.paper_ops_manifest.v3",
        "run_id": run_id,
        "mode": "forward",
        "run_date": session_date,
        "data_snapshot_id": snapshot,
        "output_artifacts": [],
        "warnings": [],
        "execution_policy_version": POLICY,
        "execution_policy_fingerprint": "fixture-policy-fingerprint",
        "universe_id": "fixture-universe",
        "universe_symbols": ["AAA"],
        "data_snapshot_content_hash": "fixture-content-hash",
        "data_snapshot_manifest_payload_hash": "fixture-manifest-hash",
        "data_snapshot_normalized_hash": "fixture-normalized-hash",
        "data_snapshot_normalized_path": "normalized/fixture.csv",
        "data_truth_root_relative": "../v2_data_truth",
    }
    payload["manifest_payload_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    write_json(root / "manifests" / f"forward_{session_date}.json", payload)

STRATEGY_IDS = (
    "ts_momentum_sma_atr",
    "donchian_breakout_20_10",
    "cross_sectional_relative_strength",
    "pullback_reclaim_uptrend",
    "volatility_contraction_breakout",
    "failed_breakout_reversal_short",
    "bullish_fvg_continuation",
)


class _SourceTruthStub:
    status = "passed"
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "warnings": []}


@pytest.fixture(autouse=True)
def _stub_source_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.challenger_evaluation.verify_source_bar_truth",
        lambda *, output_root, mode: _SourceTruthStub(),
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.trade_blotter.verify_source_bar_truth",
        lambda *, output_root, mode=None: _SourceTruthStub(),
    )
POLICY = "paperops_daily_next_open_risk_v2"
CALENDAR_FIELDS = (
    "date",
    "mode",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "data_snapshot_id",
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "trades_opened",
    "trades_closed",
    "pending_orders",
    "open_positions",
    "wins",
    "losses",
    "flats",
    "average_r",
    "expectancy_r",
    "exposure_pct",
    "fees_paid",
    "slippage_estimate",
    "warnings",
    "run_id",
    "execution_policy_version",
    "strategy_semantics_fingerprint",
)


def test_loop_emits_seven_fail_closed_proposals_without_inventing_challengers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    _write_registry(root, STRATEGY_IDS)
    write_json(
        root / "state" / "paper_ops_config.json",
        {
            "universe_symbols": ["AAA"],
            "starting_equity": 100_000.0,
            "fee_bps": 0.0,
            "slippage_bps": 0.0,
        },
    )

    first = evaluate_paperops_challengers(output_root=root)
    second = evaluate_paperops_challengers(output_root=root)

    assert first["evaluation_id"] == second["evaluation_id"]
    assert first["champion_count"] == 7
    assert first["registered_challenger_count"] == 0
    assert len(first["proposals"]) == 7
    assert {row["strategy_id"] for row in first["proposals"]} == set(STRATEGY_IDS)
    assert {row["evaluation_status"] for row in first["proposals"]} == {
        "no_registered_challenger"
    }
    assert all(row["promotion_allowed"] is False for row in first["proposals"])
    assert all(row["automatic_promotion_enabled"] is False for row in first["proposals"])
    assert len(read_jsonl(root / "reports" / "challenger_evaluation_history.jsonl")) == 1
    assert Path(first["artifacts"]["json"]).is_file()


def test_frozen_challenger_uses_only_exact_completed_forward_after_cost_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(100)
    _seed_candidate_evidence(root, dates)
    config = _test_config()

    first = evaluate_paperops_challengers(output_root=root, config=config)
    proposal = first["proposals"][0]

    assert proposal["evaluation_status"] == "evidence_passed_operator_process_missing"
    assert proposal["promotion_allowed"] is False
    assert proposal["operator_review_eligible"] is False
    assert proposal["candidate_metrics"]["eligible_session_count"] == 100
    assert proposal["candidate_metrics"]["closed_trade_count"] == 100
    assert proposal["candidate_metrics"]["coverage_pct"] == pytest.approx(100.0)
    assert proposal["candidate_metrics"]["net_after_cost_cumulative_return_pct"] == (
        pytest.approx(((1.002**100) - 1.0) * 100.0)
    )
    assert proposal["champion_metrics"]["net_after_cost_cumulative_return_pct"] == (
        pytest.approx(((1.001**100) - 1.0) * 100.0)
    )
    assert proposal["comparison"]["walk_forward_fold_count"] >= 3
    assert proposal["comparison"]["walk_forward_positive_excess_ratio"] == 1.0
    assert proposal["comparison"]["holdout_candidate_excess_pct"] > 0
    assert proposal["comparison"]["benchmark"]["coverage_pct"] == 100.0
    assert proposal["comparison"]["cash"]["coverage_pct"] == 100.0
    assert any("wrong-policy" in warning for warning in first["warnings"])

    repeated = evaluate_paperops_challengers(output_root=root, config=config)
    assert repeated["evaluation_id"] == first["evaluation_id"]
    assert len(read_jsonl(root / "reports" / "challenger_evaluation_history.jsonl")) == 1

    operator_audit = root / "governance" / "operator_process_audit.md"
    operator_audit.parent.mkdir(parents=True, exist_ok=True)
    operator_audit.write_text("independently audited manual-only process\n", encoding="utf-8")
    write_json(
        root / "state" / "audited_operator_promotion_process.json",
        {
            "schema_version": "v2.paper_ops_operator_promotion_process.v1",
            "process_id": "manual-paper-strategy-review-v1",
            "status": "active",
            "review_mode": "manual_only",
            "automatic_promotion_allowed": False,
            "approved_by": "independent-review-board",
            "approved_at": "2026-01-01T00:00:00Z",
            "audit_artifact_path": "governance/operator_process_audit.md",
            "audit_artifact_sha256": hashlib.sha256(
                operator_audit.read_bytes()
            ).hexdigest(),
        },
    )
    for name in (
        "reconciliation_latest.json",
        "calendar_truth_latest.json",
        "ledger_rebuild_latest.json",
        "source_bar_truth_forward_latest.json",
    ):
        write_json(root / "reconciliation" / name, {"status": "passed"})
    reviewed = evaluate_paperops_challengers(output_root=root, config=config)
    reviewed_proposal = reviewed["proposals"][0]

    assert reviewed_proposal["evaluation_status"] == "eligible_for_audited_manual_review"
    assert reviewed_proposal["operator_review_eligible"] is True
    assert reviewed_proposal["promotion_allowed"] is False
    assert reviewed_proposal["automatic_promotion_enabled"] is False
    assert len(read_jsonl(root / "reports" / "challenger_evaluation_history.jsonl")) == 2


def test_same_day_registration_passes_operationally_without_candidate_evidence(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    session_date = "2026-01-02"
    challenger_id = _seed_same_day_registration_without_candidate_artifacts(
        root,
        session_date,
    )

    result = evaluate_paperops_challengers(output_root=root)
    proposal = result["proposals"][0]

    assert result["status"] == "passed", result["operational_blockers"]
    assert result["operational_blockers"] == []
    assert result["completed_forward_session_count"] == 1
    assert result["registered_challenger_count"] == 1
    assert proposal["challenger_id"] == challenger_id
    assert proposal["evaluation_status"] == "insufficient_evidence"
    assert proposal["champion_metrics"]["expected_completed_session_count"] == 0
    assert proposal["champion_metrics"]["eligible_session_count"] == 0
    assert proposal["candidate_metrics"]["expected_completed_session_count"] == 0
    assert proposal["candidate_metrics"]["eligible_session_count"] == 0
    assert proposal["candidate_metrics"]["closed_trade_count"] == 0
    assert proposal["candidate_metrics"]["coverage_pct"] is None
    assert proposal["comparison"]["aligned_session_count"] == 0
    assert proposal["promotion_allowed"] is False
    assert proposal["automatic_promotion_enabled"] is False
    assert proposal["operator_review_eligible"] is False
    assert not list(root.glob("manifests/shadow_*.json"))
    assert not list(root.glob("exports/shadow_strategy_decisions_*.json"))
    assert not list((root / "state").glob("shadow/**/*.json"))


def test_missing_cost_and_decision_truth_are_excluded_with_exact_reasons(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(6)
    _seed_candidate_evidence(
        root,
        dates,
        candidate_missing_entry_fee=dates[2],
        candidate_missing_decision=dates[3],
    )

    result = evaluate_paperops_challengers(output_root=root, config=_test_config())
    proposal = result["proposals"][0]
    candidate_exclusions = proposal["excluded_dates"]["candidate"]

    assert result["status"] == "failed"
    assert proposal["evaluation_status"] == "insufficient_evidence"
    assert proposal["candidate_metrics"]["eligible_session_count"] == 0
    assert proposal["candidate_metrics"]["closed_trade_count"] == 0
    assert any(
        "entry_fee is missing" in reason for reason in candidate_exclusions[dates[2]]
    )
    assert any(
        "decision_artifact_sha256 mismatch" in reason
        for reason in candidate_exclusions[dates[3]]
    )
    assert any(
        "candidate needs 60 more sessions" in reason
        for reason in proposal["evidence_blockers"]
    )
    assert proposal["promotion_allowed"] is False


def test_sourced_no_setup_sessions_count_as_coverage_but_not_as_trades(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(3)
    _seed_candidate_evidence(root, dates, no_trades=True)
    config = ChallengerEvaluationConfig(
        min_forward_sessions=60,
        min_closed_trades=100,
        min_coverage_pct=100.0,
        holdout_fraction=0.30,
        min_holdout_sessions=1,
        min_walk_forward_folds=1,
        min_sessions_per_fold=1,
        min_positive_walk_forward_ratio=0.0,
        max_drawdown_pct=-8.0,
    )

    result = evaluate_paperops_challengers(output_root=root, config=config)
    proposal = result["proposals"][0]

    assert proposal["candidate_metrics"]["eligible_session_count"] == 3
    assert proposal["candidate_metrics"]["coverage_pct"] == 100.0
    assert proposal["candidate_metrics"]["closed_trade_count"] == 0
    assert proposal["candidate_metrics"]["win_rate_pct"] is None
    assert proposal["evaluation_status"] == "insufficient_evidence"
    assert "candidate needs 100 more closed trades" in proposal["evidence_blockers"]


def test_wrong_policy_decisions_cannot_satisfy_exact_candidate_coverage(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(6)
    _seed_candidate_evidence(root, dates)
    decision_path = (
        root
        / "exports"
        / f"shadow_strategy_decisions_forward_{dates[2]}_{STRATEGY_IDS[0]}_shadow_v2.json"
    )
    decisions = read_json(decision_path, [])
    assert isinstance(decisions, list)
    for row in decisions:
        if isinstance(row, dict) and row.get("strategy_version") == "v2.0":
            row["execution_policy_version"] = "wrong-policy"
    write_json(decision_path, decisions)

    result = evaluate_paperops_challengers(output_root=root, config=_test_config())
    proposal = result["proposals"][0]

    assert result["status"] == "failed"
    assert proposal["candidate_metrics"]["eligible_session_count"] <= 5
    assert dates[2] in proposal["excluded_dates"]["candidate"]
    assert any(
        "decision_artifact_sha256 mismatch" in reason
        for reason in result["operational_blockers"]
    )


def test_shared_daily_champion_decisions_allow_other_exact_active_series(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(6)
    _seed_candidate_evidence(root, dates)
    second_id = STRATEGY_IDS[1]
    _write_registry(root, (STRATEGY_IDS[0], second_id))
    second_semantics = _registered_semantics(root, second_id)
    for session_date in dates:
        path = root / "exports" / f"strategy_decisions_forward_{session_date}.json"
        decisions = read_json(path, [])
        assert isinstance(decisions, list)
        run_id = str(decisions[0]["run_id"])
        decisions.append(
            _decision(
                second_id,
                "v1.0",
                session_date,
                run_id,
                no_trades=True,
                semantics=second_semantics,
            )
        )
        write_json(path, decisions)
    _refresh_evaluation_truth(root)

    result = evaluate_paperops_challengers(output_root=root, config=_test_config())
    proposal = next(
        row for row in result["proposals"] if row["challenger_id"] is not None
    )

    assert result["status"] == "passed", result["operational_blockers"]
    assert proposal["champion_metrics"]["eligible_session_count"] == 6
    assert proposal["candidate_metrics"]["eligible_session_count"] == 6
    assert not any(
        "cross-series contamination" in reason
        for reason in result["operational_blockers"]
    )


def test_shared_champion_decisions_reject_conflicting_registered_series_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(6)
    _seed_candidate_evidence(root, dates)
    second_id = STRATEGY_IDS[1]
    _write_registry(root, (STRATEGY_IDS[0], second_id))
    decisions_path = (
        root / "exports" / f"strategy_decisions_forward_{dates[0]}.json"
    )
    decisions = read_json(decisions_path, [])
    assert isinstance(decisions, list)
    decisions.append(
        _decision(
            second_id,
            "v9.0",
            dates[0],
            str(decisions[0]["run_id"]),
            no_trades=True,
            semantics=_registered_semantics(root, second_id),
        )
    )
    write_json(decisions_path, decisions)
    _refresh_evaluation_truth(root)

    result = evaluate_paperops_challengers(output_root=root, config=_test_config())

    assert result["status"] == "failed"
    assert any(
        "unmatched or cross-series contamination" in reason
        for reason in result["operational_blockers"]
    )


def test_shared_champion_decisions_reject_shadow_lineage_on_champion_identity(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(6)
    _seed_candidate_evidence(root, dates)
    decisions_path = (
        root / "exports" / f"strategy_decisions_forward_{dates[0]}.json"
    )
    decisions = read_json(decisions_path, [])
    assert isinstance(decisions, list)
    assert isinstance(decisions[0], dict)
    decisions[0]["challenger_id"] = "injected_shadow"
    decisions[0]["logic_artifact_sha256"] = "f" * 64
    write_json(decisions_path, decisions)
    _refresh_evaluation_truth(root)

    result = evaluate_paperops_challengers(output_root=root, config=_test_config())

    assert result["status"] == "failed"
    assert any(
        "unmatched or cross-series contamination" in reason
        for reason in result["operational_blockers"]
    )


def test_champion_evidence_dates_begin_at_exact_registry_inception(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(2)
    _seed_candidate_evidence(root, dates)
    future_id = STRATEGY_IDS[1]
    _write_registry(root, (STRATEGY_IDS[0], future_id))
    semantics_manifest = read_json(
        root / "state" / "strategy_semantics_manifest.json",
        {},
    )
    assert isinstance(semantics_manifest, dict)
    strategies = semantics_manifest["strategies"]
    assert isinstance(strategies, dict)
    future_entry = strategies[f"{future_id}@v1.0"]
    assert isinstance(future_entry, dict)
    future_entry["activation_policy"] = "next_market_session_after_registration"
    future_entry["registered_at"] = "2026-01-02T21:00:00+00:00"
    future_entry["coverage_inception_date"] = "2026-01-05"
    write_json(
        root / "state" / "strategy_semantics_manifest.json",
        semantics_manifest,
    )
    _refresh_evaluation_truth(root)

    result = evaluate_paperops_challengers(output_root=root)
    future = next(
        row for row in result["proposals"] if row["strategy_id"] == future_id
    )

    assert result["status"] == "passed", result["operational_blockers"]
    assert future["champion_metrics"]["expected_completed_session_count"] == 0
    assert future["champion_metrics"]["eligible_session_count"] == 0
    assert future["excluded_dates"]["champion"] == {}
    assert not any(
        "cross-series contamination" in reason
        for reason in result["operational_blockers"]
    )


def test_shared_champion_decisions_reject_pre_inception_series(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper"
    dates = _dates(2)
    _seed_candidate_evidence(root, dates)
    future_id = STRATEGY_IDS[1]
    _write_registry(root, (STRATEGY_IDS[0], future_id))
    semantics_manifest = read_json(
        root / "state" / "strategy_semantics_manifest.json",
        {},
    )
    assert isinstance(semantics_manifest, dict)
    strategies = semantics_manifest["strategies"]
    assert isinstance(strategies, dict)
    future_entry = strategies[f"{future_id}@v1.0"]
    assert isinstance(future_entry, dict)
    future_entry["activation_policy"] = "next_market_session_after_registration"
    future_entry["registered_at"] = "2026-01-02T21:00:00+00:00"
    future_entry["coverage_inception_date"] = "2026-01-05"
    write_json(
        root / "state" / "strategy_semantics_manifest.json",
        semantics_manifest,
    )
    decisions_path = (
        root / "exports" / f"strategy_decisions_forward_{dates[0]}.json"
    )
    decisions = read_json(decisions_path, [])
    assert isinstance(decisions, list)
    decisions.append(
        _decision(
            future_id,
            "v1.0",
            dates[0],
            str(decisions[0]["run_id"]),
            no_trades=True,
            semantics=str(future_entry["fingerprint"]),
        )
    )
    write_json(decisions_path, decisions)
    _refresh_evaluation_truth(root)

    result = evaluate_paperops_challengers(output_root=root)

    assert result["status"] == "failed"
    assert any(
        "unmatched or cross-series contamination" in reason
        for reason in result["operational_blockers"]
    )


def _test_config() -> ChallengerEvaluationConfig:
    return ChallengerEvaluationConfig(
        min_forward_sessions=60,
        min_closed_trades=100,
        min_coverage_pct=100.0,
        holdout_fraction=0.30,
        min_holdout_sessions=2,
        min_walk_forward_folds=2,
        min_sessions_per_fold=2,
        min_positive_walk_forward_ratio=1.0,
        max_drawdown_pct=-8.0,
        max_drawdown_worsening_pct=0.0,
        max_win_rate_decline_pct_points=0.0,
    )


def _dates(count: int) -> list[str]:
    start = date(2026, 1, 2)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def _write_registry(root: Path, strategy_ids: tuple[str, ...]) -> None:
    paper_engine.PaperOpsPaths.create(root)
    catalog = {strategy.strategy_id: strategy for strategy in build_strategy_catalog()}
    fingerprints = {
        strategy_id: paper_engine._strategy_semantics_fingerprint(catalog[strategy_id])
        for strategy_id in strategy_ids
    }
    write_json(
        root / "state" / "strategy_registry.json",
        [
            {
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "strategy_status": "experimental",
                "execution_policy_version": POLICY,
                "strategy_semantics_fingerprint": fingerprints[strategy_id],
            }
            for strategy_id in strategy_ids
        ],
    )
    write_json(
        root / "state" / "strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": {
                f"{strategy_id}@v1.0": {
                    "activation_policy": "first_eligible_session",
                    "coverage_inception_date": "2026-01-02",
                    "fingerprint": fingerprints[strategy_id],
                    "registered_at": "2026-01-01T12:00:00+00:00",
                }
                for strategy_id in strategy_ids
            },
        },
    )
    write_json(
        root / "state" / "execution_policy_manifest.json",
        {
            "active_execution_policy_version": POLICY,
            "policies": {
                POLICY: {
                    "activation_policy": "first_eligible_session",
                    "coverage_inception_date": "2026-01-02",
                    "fingerprint": "fixture-policy-fingerprint",
                    "registered_at": "2026-01-01T12:00:00+00:00",
                }
            },
            "schema_version": "v2.paper_execution_policy_manifest.v1",
        },
    )


def _registered_semantics(root: Path, strategy_id: str) -> str:
    payload = read_json(root / "state" / "strategy_semantics_manifest.json", {})
    assert isinstance(payload, dict)
    strategies = payload["strategies"]
    assert isinstance(strategies, dict)
    entry = strategies[f"{strategy_id}@v1.0"]
    assert isinstance(entry, dict)
    return str(entry["fingerprint"])


def _refresh_evaluation_truth(root: Path) -> None:
    build_trade_blotter(output_root=root)
    verify_trade_blotter(output_root=root)
    for name in (
        "reconciliation_latest.json",
        "calendar_truth_latest.json",
        "ledger_rebuild_latest.json",
        "source_bar_truth_forward_latest.json",
    ):
        write_json(root / "reconciliation" / name, {"status": "passed"})


def _seed_candidate_evidence(
    root: Path,
    dates: list[str],
    *,
    candidate_missing_entry_fee: str | None = None,
    candidate_missing_decision: str | None = None,
    no_trades: bool = False,
) -> None:
    strategy_id = STRATEGY_IDS[0]
    _write_registry(root, (strategy_id,))
    write_json(
        root / "state" / "paper_ops_config.json",
        {
            "universe_symbols": ["AAA"],
            "starting_equity": 100_000.0,
            "fee_bps": 0.0,
            "slippage_bps": 0.0,
        },
    )
    challenger_id = f"{strategy_id}_shadow_v2"
    registration = _freeze_registration(
        {
            "schema_version": SHADOW_MANIFEST_SCHEMA,
            "challenger_id": challenger_id,
            "strategy_id": strategy_id,
            "champion_strategy_version": "v1.0",
            "candidate_strategy_version": "v2.0",
            "execution_policy_version": POLICY,
            "status": "shadow",
            "frozen_at": "2026-01-01T00:00:00Z",
            "hypothesis": "Frozen fixture candidate improves net after-cost expectancy.",
            "implementation": {
                "kind": "parent_signal_filter_v1",
                "parameters": {
                    "trend_sma_period": 1,
                    "atr_period": 1,
                    "max_atr_pct": 1.0,
                    "min_parent_score": 0.0,
                },
            },
        },
        root,
        registered_at="2026-01-01T00:00:00Z",
    )
    write_json(
        root / "state" / "strategy_challenger_registry.json",
        {
            "schema_version": "v2.paper_ops_challenger_registry.v1",
            "challengers": [registration],
        },
    )
    append_jsonl_unique(
        root / "state" / "shadow_registration_ledger.jsonl",
        [
            {
                "schema_version": REGISTRATION_EVENT_SCHEMA,
                "event_type": "shadow_challenger_registered",
                "registration_event_id": registration["registration_id"],
                "registered_at": registration["registered_at"],
                "registration": registration,
            }
        ],
        "registration_event_id",
    )
    rows: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    champion_equity = 100_000.0
    candidate_equity = 100_000.0
    benchmark_equity = 100_000.0
    for session_date in dates:
        snapshot = f"datatruth_sourced_{session_date.replace('-', '')}_abcdef"
        run_id = f"paper_ops:forward:{session_date}:{snapshot}"
        _write_complete_forward_manifest(
            root,
            session_date=session_date,
            run_id=run_id,
            snapshot=snapshot,
        )
        champion_before = champion_equity
        candidate_before = candidate_equity
        champion_return = 0.0 if no_trades else 0.001
        candidate_return = 0.0 if no_trades else 0.002
        champion_equity *= 1.0 + champion_return
        candidate_equity *= 1.0 + candidate_return
        benchmark_equity *= 1.0002
        rows.extend(
            [
                _calendar_row(
                    session_date,
                    run_id,
                    snapshot,
                    strategy_id,
                    "v1.0",
                    POLICY,
                    champion_return,
                    champion_before,
                    champion_equity,
                    trades=0 if no_trades else 1,
                    semantics=str(
                        registration["champion_strategy_semantics_fingerprint"]
                    ),
                ),
                _calendar_row(
                    session_date,
                    run_id,
                    snapshot,
                    strategy_id,
                    "v2.0",
                    POLICY,
                    candidate_return,
                    candidate_before,
                    candidate_equity,
                    trades=0 if no_trades else 1,
                    semantics=str(
                        registration["candidate_strategy_semantics_fingerprint"]
                    ),
                ),
                _calendar_row(
                    session_date,
                    run_id,
                    snapshot,
                    "benchmark_buy_hold_equal_weight",
                    "v1.0",
                    "equal_weight_close_to_close_v1",
                    0.0002,
                    benchmark_equity / 1.0002,
                    benchmark_equity,
                    trades=0,
                    status="benchmark",
                    semantics="benchmark-semantics-v1",
                ),
                _calendar_row(
                    session_date,
                    run_id,
                    snapshot,
                    "cash_no_trade_baseline",
                    "v1.0",
                    "cash_zero_interest_v1",
                    0.0,
                    100_000.0,
                    100_000.0,
                    trades=0,
                    status="baseline",
                    semantics="cash-semantics-v1",
                ),
                {
                    **_calendar_row(
                        session_date,
                        run_id,
                        snapshot,
                        strategy_id,
                        "v2.0",
                        "wrong-policy",
                        9.0,
                        100_000.0,
                        1_000_000.0,
                        trades=0,
                    ),
                },
                {
                    **_calendar_row(
                        session_date,
                        run_id,
                        snapshot,
                        strategy_id,
                        "v1.0",
                        POLICY,
                        8.0,
                        100_000.0,
                        900_000.0,
                        trades=0,
                    ),
                    "mode": "replay",
                },
                {
                    **_calendar_row(
                        session_date,
                        run_id,
                        snapshot,
                        strategy_id,
                        "v1.0",
                        POLICY,
                        7.0,
                        100_000.0,
                        800_000.0,
                        trades=0,
                    ),
                    "mode": "demo",
                },
            ]
        )
        write_json(
            root / "reports" / "daily" / f"forward_{session_date}.json",
            {
                "date": session_date,
                "mode": "forward",
                "run_id": run_id,
                "data_snapshot_id": snapshot,
                "provider_status": "passed",
                "stats": {"phase": "close", "closes": 0 if no_trades else 2},
            },
        )
        write_json(
            root / "exports" / f"preflight_forward_{session_date}.json",
            {
                "status": "passed",
                "mode": "forward",
                "run_date": session_date,
                "latest_completed_date": session_date,
                "run_id": run_id,
                "data_snapshot_id": snapshot,
                "universe_status": "complete",
                "symbols": ["AAA"],
            },
        )
        champion_decision = _decision(
            strategy_id,
            "v1.0",
            session_date,
            run_id,
            no_trades=no_trades,
            semantics=str(
                registration["champion_strategy_semantics_fingerprint"]
            ),
        )
        candidate_decision = {
            **_decision(
                strategy_id,
                "v2.0",
                session_date,
                run_id,
                no_trades=no_trades,
                semantics=str(
                    registration["candidate_strategy_semantics_fingerprint"]
                ),
            ),
            "challenger_id": challenger_id,
            "logic_artifact_sha256": registration["logic_artifact_sha256"],
            "strategy_semantics_fingerprint": registration[
                "candidate_strategy_semantics_fingerprint"
            ],
        }
        champion_decisions = [champion_decision]
        candidate_decisions = [candidate_decision]
        candidate_artifact = (
            [] if candidate_missing_decision == session_date else candidate_decisions
        )
        write_json(
            root / "exports" / f"strategy_decisions_forward_{session_date}.json",
            champion_decisions,
        )
        write_json(
            root
            / "exports"
            / f"shadow_strategy_decisions_forward_{session_date}_{challenger_id}.json",
            candidate_artifact,
        )
        champion_run_events = _trade_events(
            champion_decision,
            session_date=session_date,
            run_id=run_id,
            net_pnl=champion_equity - champion_before,
            no_trades=no_trades,
        )
        candidate_run_events = _trade_events(
            candidate_decision,
            session_date=session_date,
            run_id=run_id,
            net_pnl=candidate_equity - candidate_before,
            no_trades=no_trades,
            omit_entry_fee=candidate_missing_entry_fee == session_date,
        )
        events.extend(champion_run_events)
        events.extend(candidate_run_events)
        write_json(
            root / "manifests" / f"shadow_forward_{session_date}_{challenger_id}.json",
            {
                "schema_version": "v2.paper_ops_shadow_run.v1",
                "status": "completed",
                "date": session_date,
                "mode": "forward",
                "run_id": run_id,
                "data_snapshot_id": snapshot,
                "challenger_id": challenger_id,
                "strategy_id": strategy_id,
                "strategy_version": "v2.0",
                "execution_policy_version": POLICY,
                "logic_artifact_sha256": registration["logic_artifact_sha256"],
                "strategy_semantics_fingerprint": registration[
                    "candidate_strategy_semantics_fingerprint"
                ],
                "decision_coverage": len(candidate_decisions),
                "decision_coverage_status": "complete",
                "decision_artifact_sha256": _sha256(candidate_decisions),
                "decision_symbols_sha256": _sha256(["AAA"]),
                "transaction_event_count": len(candidate_run_events),
                "transaction_event_ids_sha256": _sha256(
                    sorted(str(row["event_id"]) for row in candidate_run_events)
                ),
                "transaction_events_sha256": _sha256(candidate_run_events),
                "orders_created": 0 if no_trades else 1,
                "orders_blocked": 0,
                "fills": 0 if no_trades else 1,
                "closes": 0 if no_trades else 1,
                "pending_orders": 0,
                "open_positions": 0,
                "calendar_warnings": [],
                "research_only": True,
                "automatic_promotion_enabled": False,
                "broker_execution_allowed": False,
            },
        )
    write_csv(root / "calendar" / "strategy_daily_returns.csv", rows, CALENDAR_FIELDS)
    if events:
        append_jsonl_unique(root / "ledger" / "paper_ledger.jsonl", events, "event_id")
    else:
        (root / "ledger").mkdir(parents=True, exist_ok=True)
        (root / "ledger" / "paper_ledger.jsonl").write_text("", encoding="utf-8")
    build_trade_blotter(output_root=root)
    verify_trade_blotter(output_root=root)
    for name in (
        "reconciliation_latest.json",
        "calendar_truth_latest.json",
        "ledger_rebuild_latest.json",
        "source_bar_truth_forward_latest.json",
    ):
        write_json(root / "reconciliation" / name, {"status": "passed"})


def _calendar_row(
    session_date: str,
    run_id: str,
    snapshot: str,
    strategy_id: str,
    version: str,
    policy: str,
    daily_return: float,
    before: float,
    after: float,
    *,
    trades: int,
    status: str = "experimental",
    semantics: str = "unknown",
) -> dict[str, object]:
    pnl = after - before
    return {
        "date": session_date,
        "mode": "forward",
        "strategy_id": strategy_id,
        "strategy_version": version,
        "strategy_status": status,
        "data_snapshot_id": snapshot,
        "starting_equity": 100_000.0,
        "ending_equity": after,
        "realized_pnl": pnl if trades else 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": pnl,
        "daily_return_pct": daily_return,
        "cumulative_return_pct": (after - 100_000.0) / 100_000.0,
        "drawdown_pct": 0.0,
        "trades_opened": trades,
        "trades_closed": trades,
        "pending_orders": 0,
        "open_positions": 0,
        "wins": trades,
        "losses": 0,
        "flats": 0,
        "average_r": 1.0 if trades else 0.0,
        "expectancy_r": 1.0 if trades else 0.0,
        "exposure_pct": 0.0,
        "fees_paid": 0.0,
        "slippage_estimate": 0.0,
        "warnings": "",
        "run_id": run_id,
        "execution_policy_version": policy,
        "strategy_semantics_fingerprint": semantics,
    }


def _seed_same_day_registration_without_candidate_artifacts(
    root: Path,
    session_date: str,
) -> str:
    strategy_id = STRATEGY_IDS[0]
    challenger_id = f"{strategy_id}_same_day_shadow_v2"
    _write_registry(root, (strategy_id,))
    write_json(
        root / "state" / "paper_ops_config.json",
        {
            "universe_symbols": ["AAA"],
            "starting_equity": 100_000.0,
            "fee_bps": 0.0,
            "slippage_bps": 0.0,
        },
    )
    registration = _freeze_registration(
        {
            "schema_version": SHADOW_MANIFEST_SCHEMA,
            "challenger_id": challenger_id,
            "strategy_id": strategy_id,
            "champion_strategy_version": "v1.0",
            "candidate_strategy_version": "v2.0",
            "execution_policy_version": POLICY,
            "status": "shadow",
            "frozen_at": f"{session_date}T00:00:00Z",
            "hypothesis": "Same-day registration must create no eligible evidence.",
            "implementation": {
                "kind": "parent_signal_filter_v1",
                "parameters": {
                    "trend_sma_period": 1,
                    "atr_period": 1,
                    "max_atr_pct": 1.0,
                    "min_parent_score": 0.0,
                },
            },
        },
        root,
        registered_at=f"{session_date}T01:00:00Z",
    )
    write_json(
        root / "state" / "strategy_challenger_registry.json",
        {
            "schema_version": "v2.paper_ops_challenger_registry.v1",
            "challengers": [registration],
        },
    )
    append_jsonl_unique(
        root / "state" / "shadow_registration_ledger.jsonl",
        [
            {
                "schema_version": REGISTRATION_EVENT_SCHEMA,
                "event_type": "shadow_challenger_registered",
                "registration_event_id": registration["registration_id"],
                "registered_at": registration["registered_at"],
                "registration": registration,
            }
        ],
        "registration_event_id",
    )

    snapshot = f"datatruth_sourced_{session_date.replace('-', '')}_same_day"
    run_id = f"paper_ops:forward:{session_date}:{snapshot}"
    _write_complete_forward_manifest(
        root,
        session_date=session_date,
        run_id=run_id,
        snapshot=snapshot,
    )
    write_csv(
        root / "calendar" / "strategy_daily_returns.csv",
        [
            _calendar_row(
                session_date,
                run_id,
                snapshot,
                strategy_id,
                "v1.0",
                POLICY,
                0.0,
                100_000.0,
                100_000.0,
                trades=0,
                semantics=str(registration["champion_strategy_semantics_fingerprint"]),
            ),
            _calendar_row(
                session_date,
                run_id,
                snapshot,
                "benchmark_buy_hold_equal_weight",
                "v1.0",
                "equal_weight_close_to_close_v1",
                0.0,
                100_000.0,
                100_000.0,
                trades=0,
                status="benchmark",
                semantics="benchmark-semantics-v1",
            ),
            _calendar_row(
                session_date,
                run_id,
                snapshot,
                "cash_no_trade_baseline",
                "v1.0",
                "cash_zero_interest_v1",
                0.0,
                100_000.0,
                100_000.0,
                trades=0,
                status="baseline",
                semantics="cash-semantics-v1",
            ),
        ],
        CALENDAR_FIELDS,
    )
    write_json(
        root / "reports" / "daily" / f"forward_{session_date}.json",
        {
            "date": session_date,
            "mode": "forward",
            "run_id": run_id,
            "data_snapshot_id": snapshot,
            "provider_status": "passed",
            "stats": {"phase": "close", "closes": 0},
        },
    )
    write_json(
        root / "exports" / f"preflight_forward_{session_date}.json",
        {
            "status": "passed",
            "mode": "forward",
            "run_date": session_date,
            "latest_completed_date": session_date,
            "run_id": run_id,
            "data_snapshot_id": snapshot,
            "universe_status": "complete",
            "symbols": ["AAA"],
        },
    )
    decision = _decision(
        strategy_id,
        "v1.0",
        session_date,
        run_id,
        no_trades=True,
        semantics=str(registration["champion_strategy_semantics_fingerprint"]),
    )
    write_json(
        root / "exports" / f"strategy_decisions_forward_{session_date}.json",
        [decision],
    )
    ledger_path = root / "ledger" / "paper_ledger.jsonl"
    append_jsonl_unique(
        ledger_path,
        [
            {
                "event_id": f"no-setup:{decision['decision_id']}",
                "event_type": "paper_no_setup_decision",
                "mode": "forward",
                "payload": decision,
                "run_id": run_id,
                "strategy_id": strategy_id,
                "symbol": "AAA",
                "trade_date": session_date,
            }
        ],
        "event_id",
    )
    build_trade_blotter(output_root=root)
    verify_trade_blotter(output_root=root)
    for name in (
        "reconciliation_latest.json",
        "calendar_truth_latest.json",
        "ledger_rebuild_latest.json",
        "source_bar_truth_forward_latest.json",
    ):
        write_json(root / "reconciliation" / name, {"status": "passed"})
    return challenger_id


def _decision(
    strategy_id: str,
    version: str,
    session_date: str,
    run_id: str,
    *,
    no_trades: bool,
    semantics: str,
) -> dict[str, object]:
    decision_id = f"decision:{session_date}:{strategy_id}:{version}:AAA"
    return {
        "decision_id": decision_id,
        "pick_id": decision_id,
        "decision_status": "no_setup" if no_trades else "accepted",
        "decision": "no_setup" if no_trades else "accepted",
        "mode": "forward",
        "trade_date": session_date,
        "run_id": run_id,
        "strategy_id": strategy_id,
        "strategy_version": version,
        "execution_policy_version": POLICY,
        "strategy_semantics_fingerprint": semantics,
        "symbol": "AAA",
        "direction": "long",
        "signal_time": f"{session_date}T14:30:00+00:00",
        "entry_reference": 100.0,
        "stop": 0.0,
        "target": 1_000.0,
        "trade_return_eligible": not no_trades,
        "trade_return_pct": None,
    }


def _trade_events(
    decision: dict[str, object],
    *,
    session_date: str,
    run_id: str,
    net_pnl: float,
    no_trades: bool,
    omit_entry_fee: bool = False,
) -> list[dict[str, object]]:
    strategy_id = str(decision["strategy_id"])
    version = str(decision["strategy_version"])
    pick_id = str(decision["pick_id"])
    suffix = f"{session_date}:{strategy_id}:{version}"
    base = {
        "mode": "forward",
        "trade_date": session_date,
        "run_id": run_id,
        "strategy_id": strategy_id,
        "strategy_version": version,
        "execution_policy_version": POLICY,
        "strategy_semantics_fingerprint": decision["strategy_semantics_fingerprint"],
        "symbol": "AAA",
        "direction": "long",
    }
    if decision.get("challenger_id"):
        base["challenger_id"] = decision["challenger_id"]
    decision_event_type = (
        "paper_no_setup_decision" if no_trades else "paper_pick_decision"
    )
    events = [
        {
            "event_id": f"decision:{suffix}",
            "event_type": decision_event_type,
            "mode": "forward",
            "trade_date": session_date,
            "run_id": run_id,
            "strategy_id": strategy_id,
            "symbol": "AAA",
            "payload": dict(decision),
        }
    ]
    if no_trades:
        return events
    order_id = f"order:{suffix}"
    fill_id = f"fill:{suffix}"
    position_id = f"position:{suffix}"
    close_id = f"close:{suffix}"
    entry_price = 100.0
    close_price = entry_price + net_pnl
    order = {
        **base,
        "order_id": order_id,
        "pick_id": pick_id,
        "signal_time": f"{session_date}T14:30:00+00:00",
        "entry": entry_price,
        "stop": 0.0,
        "target": 1_000.0,
        "quantity": 1,
        "expected_fill_rule": "fixture_same_session",
        "earliest_fill_date": session_date,
    }
    fill = {
        **base,
        "fill_id": fill_id,
        "order_id": order_id,
        "fill_time": f"{session_date}T14:31:00+00:00",
        "fill_price": entry_price,
        "quantity": 1,
        "fee": 0.0,
        "slippage": 0.0,
    }
    position = {
        **base,
        "position_id": position_id,
        "order_id": order_id,
        "entry_time": f"{session_date}T14:31:00+00:00",
        "entry_price": entry_price,
        "quantity": 1,
        "stop": 0.0,
        "target": 1_000.0,
        "entry_fee": 0.0,
        "last_mark_price": close_price,
        "unrealized_pnl": net_pnl,
    }
    close: dict[str, object] = {
        **base,
        "close_id": close_id,
        "position_id": position_id,
        "close_time": f"{session_date}T20:00:00+00:00",
        "close_price": close_price,
        "close_reason": "eod_flat",
        "gross_pnl": net_pnl,
        "net_pnl": net_pnl,
        "fee": 0.0,
        "slippage": 0.0,
        "r_multiple": net_pnl / 100.0,
    }
    if not omit_entry_fee:
        close["entry_fee"] = 0.0
    events.extend(
        [
            {
                "event_id": f"order:{suffix}",
                "event_type": "paper_order_created",
                "mode": "forward",
                "trade_date": session_date,
                "run_id": run_id,
                "strategy_id": strategy_id,
                "symbol": "AAA",
                "payload": order,
            },
            {
                "event_id": f"fill:{suffix}",
                "event_type": "paper_fill",
                "mode": "forward",
                "trade_date": session_date,
                "run_id": run_id,
                "strategy_id": strategy_id,
                "symbol": "AAA",
                "payload": fill,
            },
            {
                "event_id": f"open:{suffix}",
                "event_type": "paper_position_opened",
                "mode": "forward",
                "trade_date": session_date,
                "run_id": run_id,
                "strategy_id": strategy_id,
                "symbol": "AAA",
                "payload": position,
            },
            {
                "event_id": f"close:{suffix}",
                "event_type": "paper_position_closed",
                "mode": "forward",
                "trade_date": session_date,
                "run_id": run_id,
                "strategy_id": strategy_id,
                "symbol": "AAA",
                "payload": close,
            },
        ]
    )
    return events
