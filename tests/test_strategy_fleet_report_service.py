from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from intraday_scanner.paper_ops_root import PAPER_OPS_ROOT_ENV
from intraday_scanner.services.benchmark_service import persist_benchmark_observation
from intraday_scanner.services.strategy_fleet_report_service import (
    ALPHAOPS_HORIZON,
    PAPEROPS_HORIZON,
    build_strategy_fleet_report,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.paper_ops.models import PAPER_EXECUTION_POLICY_VERSION
from intraday_scanner.v2.strategies import build_strategy_catalog

V2_STRATEGY_IDS = {
    "ts_momentum_sma_atr",
    "donchian_breakout_20_10",
    "cross_sectional_relative_strength",
    "pullback_reclaim_uptrend",
    "volatility_contraction_breakout",
    "failed_breakout_reversal_short",
    "bullish_fvg_continuation",
}
ACTIVE_PAPEROPS_STRATEGY_IDS = {
    strategy.strategy_id
    for strategy in build_strategy_catalog()
    if strategy.status
    not in {"baseline", "benchmark", "quarantined", "rejected", "parked"}
}
ADDITIVE_PAPEROPS_STRATEGY_IDS = ACTIVE_PAPEROPS_STRATEGY_IDS - V2_STRATEGY_IDS


class _CalendarTruthStub:
    def __init__(
        self,
        *,
        status: str = "passed",
        math_mismatches: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.math_mismatches = math_mismatches

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate_rows": [],
            "ledger_mismatches": [],
            "math_mismatches": list(self.math_mismatches),
            "missing_rows": [],
            "schema_version": "v2.paper_ops_calendar_truth.v2",
            "status": self.status,
            "warnings": [],
        }


class _SourceTruthStub:
    status = "passed"
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "warnings": []}


@pytest.fixture(autouse=True)
def _stub_calendar_truth_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "intraday_scanner.services.strategy_fleet_report_service.verify_calendar_truth",
        lambda *, output_root: _CalendarTruthStub(),
    )
    monkeypatch.setattr(
        "intraday_scanner.services.strategy_fleet_report_service.verify_source_bar_truth",
        lambda *, output_root, mode: _SourceTruthStub(),
    )


def test_fleet_report_separates_horizons_and_preserves_na_returns(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    paper_root = tmp_path / "paper_ops"
    output = tmp_path / "report"
    _seed_alpha_scorecards(db_path)
    _write_paper_calendar(paper_root)

    result = build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=paper_root,
        out_dir=output,
    )

    assert result["status"] == "partial"
    assert result["sources"]["v2_paper_ops"]["excluded_non_forward_rows"] == 2
    daily = result["daily_rows"]
    alpha = [row for row in daily if row["horizon"] == ALPHAOPS_HORIZON]
    paper = [row for row in daily if row["horizon"] == PAPEROPS_HORIZON]
    assert len(alpha) == 2
    assert {row["strategy_id"] for row in paper} == V2_STRATEGY_IDS
    assert len(paper) == 7
    assert {row["mode"] for row in paper} == {"forward"}
    assert all(row["daily_return_source_value"] != 9.99 for row in paper)

    no_entry = next(row for row in alpha if row["date"] == "2026-07-15")
    assert no_entry["daily_return_source_value"] is None
    assert no_entry["normalized_daily_return_pct"] is None
    assert no_entry["return_observed"] is False
    assert no_entry["return_semantics"].startswith("N/A")

    swing = next(row for row in paper if row["strategy_id"] == "ts_momentum_sma_atr")
    assert swing["daily_return_source_value"] == pytest.approx(0.01)
    assert swing["normalized_daily_return_pct"] == pytest.approx(1.0)
    assert swing["source_return_scale"] == "fraction_of_strategy_equity"
    assert swing["benchmark_return_pct"] == pytest.approx(0.5)
    assert swing["excess_return_vs_benchmark_pct"] == pytest.approx(0.5)
    assert swing["cash_return_pct"] == 0.0
    assert swing["excess_return_vs_cash_pct"] == pytest.approx(1.0)

    missing_swing = next(
        row for row in paper if row["strategy_id"] == "bullish_fvg_continuation"
    )
    assert missing_swing["daily_return_source_value"] is None
    assert missing_swing["normalized_daily_return_pct"] is None
    assert missing_swing["return_observed"] is False

    alpha_summary = next(
        row
        for row in result["strategy_summaries"]
        if row["horizon"] == ALPHAOPS_HORIZON and row["cohort"] == "official_telegram"
    )
    assert alpha_summary["return_observation_count"] == 1
    assert alpha_summary["missing_return_count"] == 1
    assert alpha_summary["normalized_cumulative_return_pct"] == pytest.approx(2.0)
    assert alpha_summary["weighted_realized_return_pct"] == pytest.approx(2.0)
    assert alpha_summary["total_allocated_notional"] == pytest.approx(1_000.0)
    assert alpha_summary["total_realized_net_pnl"] == pytest.approx(20.0)
    assert alpha_summary["hypothetical_compounded_daily_return_pct"] == pytest.approx(2.0)
    assert alpha_summary["benchmark_id"] == "SPY"
    assert alpha_summary["normalized_benchmark_cumulative_return_pct"] == pytest.approx(
        1.0
    )
    assert alpha_summary["normalized_excess_return_vs_benchmark_pct"] == pytest.approx(
        1.0
    )
    assert all(
        row["horizon"] in {ALPHAOPS_HORIZON, PAPEROPS_HORIZON}
        for row in result["strategy_summaries"]
    )
    assert not any("aggregate" in row for row in result["strategy_summaries"])

    json_path = Path(result["artifacts"]["json"])
    first_json = json_path.read_text(encoding="utf-8")
    persisted = json.loads(first_json)
    persisted_no_entry = next(
        row
        for row in persisted["daily_rows"]
        if row["horizon"] == ALPHAOPS_HORIZON and row["date"] == "2026-07-15"
    )
    assert persisted_no_entry["normalized_daily_return_pct"] is None
    assert "N/A" in Path(result["artifacts"]["markdown"]).read_text(encoding="utf-8")

    build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=paper_root,
        out_dir=output,
    )
    assert json_path.read_text(encoding="utf-8") == first_json


def test_fleet_report_excludes_same_id_shadow_version_from_official_rows(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(paper_root, include_shadow=True)

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    paper_rows = [
        row for row in result["daily_rows"] if row["horizon"] == PAPEROPS_HORIZON
    ]
    momentum = [
        row for row in paper_rows if row["strategy_id"] == "ts_momentum_sma_atr"
    ]
    assert len(momentum) == 1
    assert momentum[0]["strategy_version"] == "v1.0"
    assert result["sources"]["v2_paper_ops"]["excluded_unregistered_rows"] == 1


def test_paperops_coverage_marks_pre_inception_strategies_not_yet_registered(
    tmp_path: Path,
) -> None:
    assert ADDITIVE_PAPEROPS_STRATEGY_IDS
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(paper_root, missing_return=False)

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    source = result["sources"]["v2_paper_ops"]
    assert source["status"] == "complete"
    assert source["strategy_registry_inception_status"] == "complete"
    assert source["expected_strategy_ids_by_date"]["2026-07-15"] == sorted(
        V2_STRATEGY_IDS
    )
    assert source["not_yet_registered_strategy_ids_by_date"] == {
        "2026-07-15": sorted(ADDITIVE_PAPEROPS_STRATEGY_IDS)
    }
    assert source["missing_strategy_ids"] == []
    assert source["missing_strategy_ids_by_date"] == {}
    assert {
        strategy_id: source["strategy_coverage_status_by_date"]["2026-07-15"][
            strategy_id
        ]
        for strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS
    } == {
        strategy_id: "not yet registered"
        for strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS
    }


def test_paperops_coverage_requires_strategies_on_and_after_inception(
    tmp_path: Path,
) -> None:
    assert ADDITIVE_PAPEROPS_STRATEGY_IDS
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(
        paper_root,
        calendar_date="2026-07-16",
        missing_return=False,
    )

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    source = result["sources"]["v2_paper_ops"]
    assert source["status"] == "partial"
    assert source["expected_strategy_ids_by_date"]["2026-07-16"] == sorted(
        ACTIVE_PAPEROPS_STRATEGY_IDS
    )
    assert source["not_yet_registered_strategy_ids_by_date"] == {}
    assert source["missing_strategy_ids"] == sorted(ADDITIVE_PAPEROPS_STRATEGY_IDS)
    assert source["missing_strategy_ids_by_date"] == {
        "2026-07-16": sorted(ADDITIVE_PAPEROPS_STRATEGY_IDS)
    }


def test_paperops_after_close_registration_starts_next_session(tmp_path: Path) -> None:
    assert ADDITIVE_PAPEROPS_STRATEGY_IDS
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(
        paper_root,
        calendar_date="2026-07-16",
        additive_registered_at="2026-07-16T20:01:00+00:00",
        missing_return=False,
    )

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    source = result["sources"]["v2_paper_ops"]
    assert source["status"] == "complete"
    assert {
        strategy_id: source["strategy_registry_inception_dates"][strategy_id]
        for strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS
    } == {strategy_id: "2026-07-17" for strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS}
    assert source["not_yet_registered_strategy_ids_by_date"] == {
        "2026-07-16": sorted(ADDITIVE_PAPEROPS_STRATEGY_IDS)
    }
    assert source["missing_strategy_ids_by_date"] == {}


def test_paperops_exact_inception_is_later_of_strategy_and_policy(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(
        paper_root,
        calendar_date="2026-07-16",
        additive_registered_at="2026-07-16T12:00:00+00:00",
        policy_registered_at="2026-07-15T20:01:00+00:00",
        missing_return=False,
    )

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    source = result["sources"]["v2_paper_ops"]
    assert source["strategy_registry_inception_status"] == "complete"
    assert set(source["strategy_registry_inception_dates"].values()) == {
        "2026-07-16"
    }
    assert all(
        key.count("|") == 3
        for key in source["strategy_registry_exact_inception_dates"]
    )


def test_paperops_next_session_activation_policy_prevents_same_day_claim(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(
        paper_root,
        calendar_date="2026-07-16",
        additive_registered_at="2026-07-16T12:00:00+00:00",
        additive_activation_policy="next_market_session_after_registration",
        missing_return=False,
    )

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    source = result["sources"]["v2_paper_ops"]
    assert {
        strategy_id: source["strategy_registry_inception_dates"][strategy_id]
        for strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS
    } == {strategy_id: "2026-07-17" for strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS}
    assert source["not_yet_registered_strategy_ids_by_date"] == {
        "2026-07-16": sorted(ADDITIVE_PAPEROPS_STRATEGY_IDS)
    }


def test_paperops_report_detects_whole_missing_market_session_in_range(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(
        paper_root,
        calendar_date="2026-07-14",
        additive_registered_at="2026-07-17T12:00:00+00:00",
        missing_return=False,
    )
    path = paper_root / "calendar" / "strategy_daily_returns.csv"
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
        fields = list(rows[0])
    forward_rows = [row for row in rows if row["mode"] == "forward"]
    rows.extend(
        {
            **row,
            "date": "2026-07-16",
            "run_id": f"{row['run_id']}-jul16",
        }
        for row in forward_rows
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
        start="2026-07-14",
        end="2026-07-16",
    )

    source = result["sources"]["v2_paper_ops"]
    assert source["status"] == "partial"
    assert source["missing_strategy_ids_by_date"]["2026-07-15"] == sorted(
        V2_STRATEGY_IDS
    )
    assert all(
        key.count("|") == 3
        for key in source["missing_exact_strategy_series_by_date"]["2026-07-15"]
    )


def test_paperops_coverage_fails_closed_when_registry_inception_is_ambiguous(
    tmp_path: Path,
) -> None:
    assert ADDITIVE_PAPEROPS_STRATEGY_IDS
    paper_root = tmp_path / "paper_ops"
    _write_paper_calendar(
        paper_root,
        additive_registered_at=None,
        missing_return=False,
    )

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    source = result["sources"]["v2_paper_ops"]
    assert source["status"] == "partial"
    assert source["strategy_registry_inception_status"] == "invalid"
    assert set(source["strategy_registry_inception_issues"]) == (
        ADDITIVE_PAPEROPS_STRATEGY_IDS
    )
    assert source["not_yet_registered_strategy_ids_by_date"] == {}
    assert source["missing_strategy_ids"] == sorted(ADDITIVE_PAPEROPS_STRATEGY_IDS)
    assert all(
        source["strategy_coverage_status_by_date"]["2026-07-15"][strategy_id]
        == "missing"
        for strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS
    )


def test_alpha_cumulative_return_is_capital_weighted_not_daily_compounded(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    scorecards = [
        _scorecard(
            scorecard_id="weighted-1",
            market_date="2026-07-14",
            closed_count=1,
            return_pct=10.0,
            net_pnl=10.0,
            wins=1,
        ),
        _scorecard(
            scorecard_id="weighted-2",
            market_date="2026-07-15",
            closed_count=1,
            return_pct=-1.0,
            net_pnl=-10.0,
            wins=0,
            losses=1,
        ),
    ]
    trades = [
        _paper_trade("weighted-trade-1", "2026-07-14", notional=100.0, net_pnl=10.0),
        _paper_trade(
            "weighted-trade-2", "2026-07-15", notional=1_000.0, net_pnl=-10.0
        ),
    ]
    store.persist_strategy_reconciliation(
        evaluations=[],
        paper_trades=trades,
        learning_labels=[],
        scorecards=scorecards,
    )
    _persist_delivery(store, "weighted-trade-1")
    _persist_delivery(store, "weighted-trade-2")
    _persist_benchmark(db_path, "2026-07-14", open_price=100.0, close_price=101.0)
    _persist_benchmark(db_path, "2026-07-15", open_price=100.0, close_price=98.0)

    result = build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=tmp_path / "absent-paper",
        out_dir=tmp_path / "report",
    )
    summary = result["strategy_summaries"][0]

    assert summary["total_allocated_notional"] == pytest.approx(1_100.0)
    assert summary["total_realized_net_pnl"] == pytest.approx(0.0)
    assert summary["weighted_realized_return_pct"] == pytest.approx(0.0)
    assert summary["normalized_cumulative_return_pct"] == pytest.approx(0.0)
    assert summary["hypothetical_compounded_daily_return_pct"] == pytest.approx(8.9)
    assert summary["normalized_benchmark_cumulative_return_pct"] == pytest.approx(
        -1.7272727273
    )
    assert summary["normalized_excess_return_vs_benchmark_pct"] == pytest.approx(
        1.7272727273
    )
    assert summary["normalized_excess_return_vs_cash_pct"] == pytest.approx(0.0)
    assert summary["cumulative_return_semantics"].startswith("canonical_sum")


def test_alpha_cumulative_return_is_na_without_canonical_allocation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_strategy_reconciliation(
        evaluations=[],
        paper_trades=[],
        learning_labels=[],
        scorecards=[
            _scorecard(
                scorecard_id="orphan-scorecard",
                market_date="2026-07-14",
                closed_count=1,
                return_pct=2.0,
                net_pnl=20.0,
                wins=1,
            )
        ],
    )

    result = build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=tmp_path / "absent-paper",
        out_dir=tmp_path / "report",
    )
    summary = result["strategy_summaries"][0]

    assert summary["normalized_cumulative_return_pct"] is None
    assert summary["weighted_realized_return_pct"] is None
    assert summary["allocation_evidence_missing_count"] == 1
    assert summary["hypothetical_compounded_daily_return_pct"] == pytest.approx(2.0)
    assert result["sources"]["alphaops_sqlite"]["status"] == "partial"


def test_official_alpha_capital_excludes_non_telegram_delivery(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    store = SQLiteScanStore(db_path)
    store.persist_strategy_reconciliation(
        evaluations=[],
        paper_trades=[
            _paper_trade(
                "slack-only-trade",
                "2026-07-14",
                notional=1_000.0,
                net_pnl=20.0,
            )
        ],
        learning_labels=[],
        scorecards=[
            _scorecard(
                scorecard_id="slack-only-scorecard",
                market_date="2026-07-14",
                closed_count=1,
                return_pct=2.0,
                net_pnl=20.0,
                wins=1,
            )
        ],
    )
    _persist_delivery(store, "slack-only-trade", channel="slack")

    result = build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=tmp_path / "absent-paper",
        out_dir=tmp_path / "report",
    )
    summary = result["strategy_summaries"][0]

    assert summary["cohort"] == "official_telegram"
    assert summary["weighted_realized_return_pct"] is None
    assert summary["allocation_evidence_missing_count"] == 1
    assert result["sources"]["alphaops_sqlite"]["official_delivered_signal_count"] == 0


def test_fleet_report_status_exposes_absent_required_inputs(tmp_path: Path) -> None:
    failed = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=tmp_path / "absent_paper",
        out_dir=tmp_path / "failed_report",
    )

    assert failed["status"] == "failed"
    assert failed["daily_rows"] == []
    assert failed["sources"]["alphaops_sqlite"]["status"] == "missing"
    assert failed["sources"]["v2_paper_ops"]["status"] == "missing"
    assert Path(failed["artifacts"]["json"]).is_file()

    db_path = tmp_path / "alpha.sqlite"
    _seed_alpha_scorecards(db_path)
    partial = build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=tmp_path / "still_absent_paper",
        out_dir=tmp_path / "partial_report",
    )

    assert partial["status"] == "partial"
    assert partial["sources"]["alphaops_sqlite"]["status"] == "complete"
    assert partial["sources"]["v2_paper_ops"]["status"] == "missing"


def test_missing_paperops_benchmark_remains_na_while_cash_policy_is_explicit(
    tmp_path: Path,
) -> None:
    paper_root = tmp_path / "paper"
    _write_paper_calendar(paper_root, include_comparators=False)

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )
    momentum = next(
        row
        for row in result["daily_rows"]
        if row["strategy_id"] == "ts_momentum_sma_atr"
    )
    summary = next(
        row
        for row in result["strategy_summaries"]
        if row["strategy_id"] == "ts_momentum_sma_atr"
    )

    assert momentum["benchmark_id"] is None
    assert momentum["benchmark_return_pct"] is None
    assert momentum["excess_return_vs_benchmark_pct"] is None
    assert momentum["benchmark_comparison_status"].startswith("missing")
    assert summary["normalized_benchmark_cumulative_return_pct"] is None
    assert summary["normalized_excess_return_vs_benchmark_pct"] is None
    assert momentum["cash_baseline_id"] == "cash_no_trade_baseline"
    assert momentum["cash_return_pct"] == 0.0


def test_fleet_report_default_uses_configured_production_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_root = tmp_path / "configured-live-paper"
    monkeypatch.setenv(PAPER_OPS_ROOT_ENV, str(configured_root))

    result = build_strategy_fleet_report(
        db_path=tmp_path / "absent.sqlite",
        out_dir=tmp_path / "report",
    )

    assert result["sources"]["v2_paper_ops"]["path"] == str(
        configured_root / "calendar/strategy_daily_returns.csv"
    )


def test_standalone_fleet_report_recovers_pending_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "alpha.sqlite"
    paper_root = tmp_path / "paper_ops"
    _seed_alpha_scorecards(db_path)
    _write_paper_calendar(paper_root)
    journal_path = paper_root / "state" / "paper_transaction_pending.json"
    journal_contents: dict[str, object] = {"events": [], "state_updates": {}}
    transaction_id = hashlib.sha256(
        json.dumps(
            journal_contents,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": "v2.paper_transaction.v1",
                "transaction_id": transaction_id,
                **journal_contents,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    result = build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    assert not journal_path.exists()
    assert result["sources"]["v2_paper_ops"]["calendar_truth"]["status"] == "passed"


def test_standalone_fleet_report_blocks_failed_calendar_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "alpha.sqlite"
    paper_root = tmp_path / "paper_ops"
    _seed_alpha_scorecards(db_path)
    _write_paper_calendar(paper_root)
    monkeypatch.setattr(
        "intraday_scanner.services.strategy_fleet_report_service.verify_calendar_truth",
        lambda *, output_root: _CalendarTruthStub(
            status="failed",
            math_mismatches=("mutated daily return mismatch",),
        ),
    )

    result = build_strategy_fleet_report(
        db_path=db_path,
        paper_ops_root=paper_root,
        out_dir=tmp_path / "report",
    )

    assert result["status"] == "partial"
    assert result["sources"]["v2_paper_ops"]["status"] == "invalid"
    assert not [
        row for row in result["daily_rows"] if row["source_system"] == "v2_paper_ops"
    ]
    assert any(
        "calendar truth math_mismatches: mutated daily return mismatch" in warning
        for warning in result["warnings"]
    )


def _seed_alpha_scorecards(db_path: Path) -> None:
    store = SQLiteScanStore(db_path)
    scorecards = [
        _scorecard(
            scorecard_id="alpha-2026-07-14",
            market_date="2026-07-14",
            closed_count=1,
            return_pct=2.0,
            net_pnl=20.0,
            wins=1,
        ),
        _scorecard(
            scorecard_id="alpha-2026-07-15",
            market_date="2026-07-15",
            closed_count=0,
            return_pct=None,
            net_pnl=0.0,
            wins=0,
        ),
    ]
    store.persist_strategy_reconciliation(
        evaluations=[],
        paper_trades=[
            _paper_trade(
                "alpha-trade-2026-07-14",
                "2026-07-14",
                notional=1_000.0,
                net_pnl=20.0,
            )
        ],
        learning_labels=[],
        scorecards=scorecards,
    )
    _persist_delivery(store, "alpha-trade-2026-07-14")
    _persist_benchmark(db_path, "2026-07-14", open_price=100.0, close_price=101.0)


def _scorecard(
    *,
    scorecard_id: str,
    market_date: str,
    closed_count: int,
    return_pct: float | None,
    net_pnl: float,
    wins: int,
    losses: int = 0,
) -> dict[str, object]:
    return {
        "scorecard_id": scorecard_id,
        "market_date": market_date,
        "strategy_id": "alphaops_v4",
        "strategy_version": "dawnstrike-alphaops-v4",
        "cohort": "official_telegram",
        "execution_policy_version": "alphaops_intraday_first_touch_v1",
        "selected_count": 1,
        "delivered_count": 1,
        "resolved_count": 1,
        "triggered_count": closed_count,
        "not_triggered_count": 0 if closed_count else 1,
        "filled_count": closed_count,
        "closed_count": closed_count,
        "unresolved_count": 0,
        "wins": wins,
        "losses": losses,
        "flats": 0,
        "activation_rate_pct": float(closed_count * 100),
        "win_rate_pct": 100.0 if closed_count else None,
        "average_net_return_pct": return_pct,
        "net_pnl": net_pnl,
        "return_on_allocated_capital_pct": return_pct,
        "average_r": 1.0 if closed_count else None,
        "expectancy_r": 1.0 if closed_count else None,
        "profit_factor": None,
        "fees": 0.0,
        "slippage_cost": 0.0,
        "reconciliation_status": "complete",
        "created_at": f"{market_date}T21:00:00Z",
    }


def _paper_trade(
    trade_id: str,
    market_date: str,
    *,
    notional: float,
    net_pnl: float,
) -> dict[str, object]:
    return {
        "trade_id": trade_id,
        "selection_id": f"selection-{trade_id}",
        "signal_id": f"signal-{trade_id}",
        "market_date": market_date,
        "ticker": "TEST",
        "strategy_id": "alphaops_v4",
        "strategy_version": "dawnstrike-alphaops-v4",
        "cohort": "official_telegram",
        "direction": "long",
        "decision_time": f"{market_date}T14:00:00Z",
        "entry_time": f"{market_date}T14:01:00Z",
        "entry_fill_price": 10.0,
        "exit_time": f"{market_date}T20:00:00Z",
        "exit_fill_price": 10.0 + (net_pnl / (notional / 10.0)),
        "exit_reason": "eod",
        "quantity": notional / 10.0,
        "notional": notional,
        "net_pnl": net_pnl,
        "net_return_pct": (net_pnl / notional) * 100.0,
        "r_multiple": net_pnl / 10.0,
        "fees": 0.0,
        "slippage_cost": 0.0,
        "source_bar_hash_sha256": "a" * 64,
        "execution_policy_version": "alphaops_intraday_first_touch_v1",
        "created_at": f"{market_date}T21:00:00Z",
    }


def _persist_benchmark(
    db_path: Path,
    market_date: str,
    *,
    open_price: float,
    close_price: float,
) -> None:
    persist_benchmark_observation(
        db_path,
        {
            "benchmark_id": f"SPY:{market_date}:test",
            "symbol": "SPY",
            "market_date": market_date,
            "open_price": open_price,
            "close_price": close_price,
            "source": "test_fixture",
            "source_quality": "sourced_test_fixture",
            "observed_at": f"{market_date}T21:00:00Z",
        },
    )


def _persist_delivery(
    store: SQLiteScanStore,
    trade_id: str,
    *,
    channel: str = "telegram",
) -> None:
    signal_id = f"signal-{trade_id}"
    market_date = "2026-07-15" if trade_id.endswith("2") else "2026-07-14"
    result = store.persist_notification_deliveries(
        [
            {
                "membership_id": f"delivery-{channel}-{trade_id}",
                "selection_id": f"selection-{trade_id}",
                "scan_id": f"scan-{market_date}",
                "signal_id": signal_id,
                "ticker": "TEST",
                "strategy_id": "alphaops_v4",
                "strategy_version": "dawnstrike-alphaops-v4",
                "cohort": "official_telegram",
                "decision": "selected",
                "selected_at": f"{market_date}T14:00:00Z",
                "event_key": f"event-{channel}-{trade_id}",
                "channel": channel,
                "delivery_status": "delivered",
                "attempted_at": f"{market_date}T14:00:01Z",
                "delivered_at": f"{market_date}T14:00:02Z",
                "body_sha256": "b" * 64,
            }
        ]
    )
    assert result["inserted"] == 1


def _write_paper_calendar(
    root: Path,
    *,
    include_comparators: bool = True,
    include_shadow: bool = False,
    calendar_date: str = "2026-07-15",
    additive_registered_at: str | None = "2026-07-16T12:00:00+00:00",
    additive_activation_policy: str | None = None,
    policy_registered_at: str = "2026-07-01T12:00:00+00:00",
    policy_activation_policy: str | None = None,
    missing_return: bool = True,
) -> None:
    calendar = root / "calendar"
    calendar.mkdir(parents=True)
    state = root / "state"
    state.mkdir(parents=True)
    (state / "strategy_registry.json").write_text(
        json.dumps(
            [
                {
                    "strategy_id": strategy_id,
                    "strategy_version": "v1.0",
                    "strategy_status": "experimental",
                    "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
                    "strategy_semantics_fingerprint": "f" * 64,
                }
                for strategy_id in sorted(ACTIVE_PAPEROPS_STRATEGY_IDS)
            ]
        ),
        encoding="utf-8",
    )
    manifest_strategies: dict[str, dict[str, object]] = {}
    for strategy_id in sorted(ACTIVE_PAPEROPS_STRATEGY_IDS):
        registered_at = (
            additive_registered_at
            if strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS
            else "2026-07-01T12:00:00+00:00"
        )
        manifest_row: dict[str, object] = {"fingerprint": "f" * 64}
        if registered_at is not None:
            manifest_row["registered_at"] = registered_at
        if (
            additive_activation_policy is not None
            and strategy_id in ADDITIVE_PAPEROPS_STRATEGY_IDS
        ):
            manifest_row["activation_policy"] = additive_activation_policy
        manifest_strategies[f"{strategy_id}@v1.0"] = manifest_row
    (state / "strategy_semantics_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "v2.strategy_semantics_manifest.v1",
                "strategies": manifest_strategies,
            }
        ),
        encoding="utf-8",
    )
    policy_row: dict[str, object] = {
        "configuration": {},
        "fingerprint": "policy-fingerprint",
        "registered_at": policy_registered_at,
    }
    if policy_activation_policy is not None:
        policy_row["activation_policy"] = policy_activation_policy
    (state / "execution_policy_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "v2.paper_execution_policy_manifest.v1",
                "active_execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
                "policies": {PAPER_EXECUTION_POLICY_VERSION: policy_row},
            }
        ),
        encoding="utf-8",
    )
    fields = [
        "date",
        "mode",
        "strategy_id",
        "strategy_version",
        "execution_policy_version",
        "strategy_semantics_fingerprint",
        "strategy_status",
        "total_pnl",
        "daily_return_pct",
        "cumulative_return_pct",
        "trades_opened",
        "trades_closed",
        "wins",
        "losses",
        "flats",
        "run_id",
    ]
    rows: list[dict[str, object]] = []
    for index, strategy_id in enumerate(sorted(V2_STRATEGY_IDS)):
        daily: object = (
            ""
            if missing_return and strategy_id == "bullish_fvg_continuation"
            else index / 1000
        )
        rows.append(
            {
                "date": calendar_date,
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
                "strategy_semantics_fingerprint": "f" * 64,
                "strategy_status": "experimental",
                "total_pnl": index * 10,
                "daily_return_pct": daily,
                "cumulative_return_pct": index / 100,
                "trades_opened": 1,
                "trades_closed": 1,
                "wins": 1,
                "losses": 0,
                "flats": 0,
                "run_id": f"forward-{index}",
            }
        )
    momentum = next(row for row in rows if row["strategy_id"] == "ts_momentum_sma_atr")
    momentum["daily_return_pct"] = 0.01
    if include_shadow:
        rows.append(
            {
                **momentum,
                "strategy_version": "v2.0-shadow",
                "strategy_status": "shadow",
                "daily_return_pct": 0.99,
                "run_id": "shadow-candidate",
            }
        )
    if include_comparators:
        rows.extend(
            [
                {
                    **momentum,
                    "strategy_id": "benchmark_buy_hold_equal_weight",
                    "strategy_status": "benchmark",
                    "daily_return_pct": 0.005,
                    "cumulative_return_pct": 0.005,
                    "run_id": "forward-benchmark",
                },
                {
                    **momentum,
                    "strategy_id": "cash_no_trade_baseline",
                    "strategy_status": "baseline",
                    "daily_return_pct": 0.0,
                    "cumulative_return_pct": 0.0,
                    "run_id": "forward-cash",
                },
            ]
        )
    rows.extend(
        [
            {
                **momentum,
                "mode": "replay",
                "daily_return_pct": 9.99,
                "run_id": "replay-contamination",
            },
            {
                **momentum,
                "mode": "demo",
                "daily_return_pct": 8.88,
                "run_id": "demo-contamination",
            },
        ]
    )
    with (calendar / "strategy_daily_returns.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
