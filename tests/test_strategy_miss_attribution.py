from __future__ import annotations

from intraday_scanner.performance.strategy_miss_attribution import (
    AttributionState,
    Eligibility,
    attribute_strategy_misses,
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "record_id": "r-1",
        "market_date": "2026-08-21",
        "cohort": "shadow_challenger",
        "strategy_id": "example_strategy",
        "strategy_version": "v1.0",
        "config_identity": "config-a",
        "record_status": "realized",
        "record_type": "portfolio_observation",
        "return_pct": 0.25,
        "source_hash_sha256": "a" * 64,
        "open_position_count": 0,
    }
    row.update(overrides)
    return row


def test_no_trade_positive_benchmark_is_opportunity_cost_not_a_counterfactual() -> None:
    report = attribute_strategy_misses(
        [
            _row(
                record_id="benchmark",
                strategy_id="benchmark_buy_hold_equal_weight",
                return_pct=1.25,
                source_hash_sha256="b" * 64,
            ),
            _row(
                record_id="miss",
                strategy_id="example_strategy",
                record_status="no_trade",
                return_pct=0.0,
                source_hash_sha256="c" * 64,
            ),
        ]
    )

    miss = next(row for row in report.rows if row.record_id == "miss")
    summary = next(item for item in report.summaries if item.strategy_id == "example_strategy")
    assert miss.state is AttributionState.NO_TRADE
    assert miss.classification == "positive_benchmark_no_trade"
    assert miss.eligibility is Eligibility.UNKNOWN
    assert set(("no_trade", "opportunity_cost")) <= set(miss.categories)
    assert "profitable_miss" not in miss.categories
    assert miss.benchmark_return_pct == 1.25
    assert summary.opportunity_cost_count == 1
    assert summary.opportunity_cost_return_sum_pct == 1.25


def test_missing_benchmark_preserves_unknown_opportunity_cost() -> None:
    report = attribute_strategy_misses(
        [_row(record_status="no_trade", return_pct=0.0, benchmark_return_pct=None)]
    )
    row = report.rows[0]
    summary = report.summaries[0]
    assert row.classification == "no_trade"
    assert row.eligibility is Eligibility.UNKNOWN
    assert "opportunity_cost" not in row.categories
    assert summary.opportunity_cost_count == 0


def test_open_mark_to_market_is_not_a_closed_loss() -> None:
    report = attribute_strategy_misses(
        [_row(return_pct=-2.5, open_position_count=2, unrealized_pnl_cents=-250)]
    )
    row = report.rows[0]
    summary = report.summaries[0]
    assert row.state is AttributionState.OPEN_MTM
    assert row.classification == "open_mtm_loss"
    assert row.eligibility is Eligibility.UNKNOWN
    assert "false_positive" not in row.categories
    assert summary.open_mtm_count == 1
    assert summary.closed_loss_count == 0
    assert summary.open_mtm_return_sum_pct == -2.5


def test_closed_negative_outcome_without_fill_truth_is_provisional() -> None:
    report = attribute_strategy_misses([_row(return_pct=-1.5)])
    row = report.rows[0]
    summary = report.summaries[0]
    assert row.state is AttributionState.CLOSED
    assert row.classification == "closed_provisional"
    assert row.eligibility is Eligibility.INELIGIBLE
    assert set(("closed_provisional", "missing_fill_truth")) <= set(row.categories)
    assert summary.provisional_closed_count == 1
    assert summary.closed_return_sum_pct is None


def test_complete_sourced_status_without_fill_truth_is_provisional() -> None:
    report = attribute_strategy_misses([_row(return_pct=1.0, outcome_status="COMPLETE_SOURCED")])
    row = report.rows[0]
    assert row.classification == "closed_provisional"
    assert row.eligibility is Eligibility.INELIGIBLE
    assert "missing_fill_truth" in row.categories


def test_official_forward_row_without_record_type_or_fill_truth_is_provisional() -> None:
    row = _row(cohort="official_forward", return_pct=1.0)
    row.pop("record_type")
    report = attribute_strategy_misses([row])
    attributed = report.rows[0]
    assert attributed.classification == "closed_provisional"
    assert attributed.eligibility is Eligibility.INELIGIBLE
    assert attributed.fill_truth_status == "missing_committed_fill_truth"


def test_nontrade_and_historical_rows_remain_explicitly_non_return_truth() -> None:
    report = attribute_strategy_misses(
        [
            _row(record_status="no_trade", return_pct=0.0),
            _row(
                record_id="historical",
                cohort="historical_backtest",
                return_pct=2.0,
            ),
        ]
    )
    no_trade = next(row for row in report.rows if row.record_id == "r-1")
    historical = next(row for row in report.rows if row.record_id == "historical")
    assert no_trade.state is AttributionState.NO_TRADE
    assert no_trade.eligibility is Eligibility.UNKNOWN
    assert historical.classification == "closed_provisional"
    assert historical.eligibility is Eligibility.INELIGIBLE


def test_missing_outcome_is_ineligible_and_never_zero() -> None:
    report = attribute_strategy_misses([_row(record_status="missing_outcome", return_pct=None)])
    row = report.rows[0]
    summary = report.summaries[0]
    assert row.state is AttributionState.MISSING_OUTCOME
    assert row.classification == "data_unavailable"
    assert row.return_pct is None
    assert row.eligibility is Eligibility.INELIGIBLE
    assert summary.missing_outcome_count == 1
    assert summary.closed_return_sum_pct is None


def test_conflicting_outcome_is_not_repaired_by_inference() -> None:
    report = attribute_strategy_misses([_row(record_status="no_trade", return_pct=2.0)])
    row = report.rows[0]
    assert row.state is AttributionState.CONFLICTING_OUTCOME
    assert row.classification == "conflicting_outcome"
    assert row.eligibility is Eligibility.INELIGIBLE
    assert "conflicting_outcome" in row.categories


def test_explicit_gate_evidence_survives_as_machine_actionable_categories() -> None:
    report = attribute_strategy_misses(
        [
            _row(
                record_status="no_trade",
                return_pct=0.0,
                risk_veto=True,
                rank_rejected=True,
                capacity_rejected=True,
                entry_not_triggered=True,
                data_unavailable=True,
            )
        ]
    )
    row = report.rows[0]
    summary = report.summaries[0]
    assert set(("risk", "rank", "capacity", "entry_not_triggered", "data_unavailable")) <= set(
        row.categories
    )
    hypothesis_ids = {item["hypothesis_id"] for item in summary.remediation_hypotheses}
    assert {"risk", "rank", "capacity", "entry_not_triggered", "data_quality"} <= hypothesis_ids


def test_cutoff_excludes_future_rows_and_keeps_cohorts_separate() -> None:
    report = attribute_strategy_misses(
        [
            _row(record_id="before", market_date="2026-08-20"),
            _row(record_id="after", market_date="2026-08-22"),
            _row(record_id="replay", cohort="historical_backtest"),
        ],
        date_cutoff="2026-08-21",
    )
    assert report.input_row_count == 3
    assert report.included_row_count == 2
    assert report.excluded_after_cutoff_count == 1
    assert {summary.cohort for summary in report.summaries} == {
        "shadow_challenger",
        "historical_backtest",
    }


def test_identity_and_evidence_hashes_are_preserved_without_inventing_config() -> None:
    report = attribute_strategy_misses(
        [
            _row(
                config_identity="cfg-sha",
                execution_policy_version="policy-v2",
                source_hash_sha256="d" * 64,
                input_hash_sha256="e" * 64,
                source_bar_hash_sha256="f" * 64,
            )
        ]
    )
    row = report.rows[0]
    summary = report.summaries[0]
    assert row.strategy_version == "v1.0"
    assert row.config_identity == "cfg-sha"
    assert row.execution_policy_version == "policy-v2"
    assert row.evidence_hashes == tuple(sorted(("d" * 64, "e" * 64, "f" * 64)))
    assert summary.evidence_hashes == row.evidence_hashes
    assert report.research_only is True
    assert report.promotion_eligible is False
    assert report.policy_changes == ()


def test_unknown_reason_hypothesis_is_emitted_when_no_explanatory_evidence_exists() -> None:
    report = attribute_strategy_misses([_row(record_status="unknown", return_pct=0.5)])
    summary = report.summaries[0]
    assert summary.remediation_hypotheses == (
        {
            "hypothesis_id": "unknown_evidence",
            "trigger_count": 1,
            "action": (
                "Collect explicit gate, rank, risk, capacity, and outcome evidence; "
                "preserve unknowns."
            ),
        },
    )
