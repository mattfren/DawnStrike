from __future__ import annotations

from collections.abc import Iterable
from dataclasses import FrozenInstanceError
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.v2.data import MarketBar
from intraday_scanner.v2.mover_pattern_lab.candidate_study import (
    CandidateSplitAssignment,
    CandidateStudyAssumptions,
    CandidateUniverseDenominator,
    study_all_candidates,
)
from intraday_scanner.v2.mover_pattern_lab.contracts import ProspectiveMoverSnapshot

ET = ZoneInfo("America/New_York")
MARKET_DATE = "2026-07-16"
CUTOFF = datetime(2026, 7, 16, 9, 45, tzinfo=ET)


def _snapshot(
    symbol: str,
    *,
    price: float = 100.0,
    previous_close: float = 95.0,
    cutoff: datetime = CUTOFF,
) -> ProspectiveMoverSnapshot:
    return ProspectiveMoverSnapshot.from_mapping(
        {
            "snapshot_id": f"snapshot-{symbol}-{cutoff.isoformat()}",
            "market_date": cutoff.date().isoformat(),
            "symbol": symbol,
            "observed_at": cutoff.isoformat(),
            "feature_cutoff_at": cutoff.isoformat(),
            "universe_selected_at": (cutoff - timedelta(minutes=30)).isoformat(),
            "universe_source_ref": f"source://universe/{symbol}",
            "universe_selection_method": "live_intraday_scan",
            "context_observed_at": (cutoff - timedelta(minutes=1)).isoformat(),
            "price": price,
            "previous_close": previous_close,
            "session_open": price - 1.0,
            "opening_range_high": price,
            "opening_range_low": price - 2.0,
            "opening_range_complete": True,
            "running_vwap": price - 0.5,
            "cumulative_volume": 1_000_000,
            "cumulative_dollar_volume": 100_000_000,
            "same_clock_rvol": 3.0,
            "spread_pct": 0.1,
            "split_adjusted": True,
            "reverse_split_days": 500,
            "recent_offering_days": 500,
            "halt_state": "clear",
            "source_conflict": False,
            "catalyst_verified": False,
            "source_refs": [f"source://universe/{symbol}"],
            "raw_payload": {"bar_interval_minutes": 5},
        }
    )


def _bars(
    symbol: str,
    *,
    cutoff: datetime = CUTOFF,
    close_time: time = time(16, 0),
    start_price: float = 100.0,
    step: float = 0.1,
    omit: Iterable[datetime] = (),
) -> list[MarketBar]:
    omitted = set(omit)
    bars: list[MarketBar] = []
    timestamp = cutoff + timedelta(minutes=5)
    index = 0
    while timestamp.time() <= close_time:
        if timestamp not in omitted:
            open_price = start_price + index * step
            close_price = open_price + step
            bars.append(
                MarketBar(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=open_price,
                    high=max(open_price, close_price) + 0.2,
                    low=min(open_price, close_price) - 0.2,
                    close=close_price,
                    volume=10_000 + index,
                )
            )
        timestamp += timedelta(minutes=5)
        index += 1
    return bars


def _denominator(
    snapshots: Iterable[ProspectiveMoverSnapshot],
    *,
    complete: bool = True,
    extra_symbols: Iterable[str] = (),
) -> CandidateUniverseDenominator:
    rows = tuple(snapshots)
    return CandidateUniverseDenominator.create(
        market_date=rows[0].market_date,
        feature_cutoff_at=rows[0].feature_cutoff_at,
        expected_symbols=tuple(row.symbol for row in rows) + tuple(extra_symbols),
        source_ref="sha256:complete-universe",
        expected_symbols_complete=complete,
    )


def _split(
    snapshots: Iterable[ProspectiveMoverSnapshot],
    split: str = "discovery",
) -> CandidateSplitAssignment:
    return CandidateSplitAssignment.create(
        {row.snapshot_id: split for row in snapshots},
        source_ref="sha256:frozen-split",
    )


def _study(
    snapshots: list[ProspectiveMoverSnapshot],
    bars: list[MarketBar],
    *,
    denominator: CandidateUniverseDenominator | None = None,
    eod: list[dict[str, object]] | None = None,
):
    return study_all_candidates(
        snapshots=snapshots,
        bars=bars,
        universe_denominators=[denominator or _denominator(snapshots)],
        split_assignment=_split(snapshots),
        assumptions=CandidateStudyAssumptions(
            bar_interval_minutes=5,
            slippage_bps=10,
            fee_bps=1,
        ),
        bars_source_ref="sha256:source-bars",
        descriptive_eod_movers=eod or [],
    )


def test_labels_every_candidate_with_exact_next_bar_entry_and_costs() -> None:
    snapshot = _snapshot("AAA")
    bars = _bars("AAA")

    result = _study([snapshot], bars)
    outcome = result.outcomes[0]

    assert outcome.status == "complete"
    assert outcome.entry_bar_close_at == datetime(2026, 7, 16, 9, 50, tzinfo=ET)
    assert outcome.entry_at == CUTOFF
    assert outcome.entry_reference == 100.0
    assert outcome.entry_fill == 100.1
    assert outcome.gross_return_5m_pct is not None
    assert outcome.gross_return_15m_pct is not None
    assert outcome.gross_return_30m_pct is not None
    assert outcome.gross_return_60m_pct is not None
    assert outcome.after_cost_close_return_pct is not None
    assert outcome.after_cost_close_return_pct < outcome.gross_close_return_pct
    close_reference = bars[-1].close
    exit_fill = close_reference * (1 - 10 / 10_000)
    fees = outcome.entry_fill * (1 / 10_000) + exit_fill * (1 / 10_000)
    expected_after_cost = (exit_fill - outcome.entry_fill - fees) / outcome.entry_fill * 100
    assert outcome.after_cost_close_return_pct == pytest.approx(expected_after_cost)
    assert outcome.mfe_pct is not None
    assert outcome.mae_pct is not None
    assert outcome.candidate_return_rank == 1
    assert outcome.candidate_return_population == 1
    assert outcome.outcome_id and outcome.label_id and result.study_id
    assert result.to_dict()["broker_execution_enabled"] is False
    with pytest.raises(FrozenInstanceError):
        outcome.status = "changed"  # type: ignore[misc]


def test_ids_and_outputs_are_content_stable_under_input_order() -> None:
    snapshots = [_snapshot("AAA"), _snapshot("BBB", price=105, previous_close=100)]
    bars = _bars("AAA") + _bars("BBB", start_price=105, step=-0.02)
    denominator = _denominator(snapshots)
    first = _study(snapshots, bars, denominator=denominator)
    second = _study(list(reversed(snapshots)), list(reversed(bars)), denominator=denominator)

    assert first.study_id == second.study_id
    assert [row.outcome_id for row in first.outcomes] == [
        row.outcome_id for row in second.outcomes
    ]
    assert [row.label_id for row in first.outcomes] == [
        row.label_id for row in second.outcomes
    ]


def test_candidate_study_rejects_mixed_forward_and_replay_evidence() -> None:
    replay = _snapshot("AAA")
    forward_row = _snapshot("BBB", price=105, previous_close=100).to_dict()
    forward_ref = "sha256:" + "a" * 64 + ":C:/evidence/universe.json"
    receipt_ref = "sha256:" + "b" * 64 + ":C:/evidence/receipt.json"
    forward_row.update(
        {
            "evidence_mode": "forward_observation",
            "source_captured_at": forward_row["feature_cutoff_at"],
            "system_received_at": forward_row["feature_cutoff_at"],
            "forward_receipt_ref": receipt_ref,
            "universe_source_ref": forward_ref,
            "source_refs": [forward_ref, receipt_ref],
        }
    )
    forward = ProspectiveMoverSnapshot.from_mapping(forward_row)
    with pytest.raises(ValueError, match="exactly one evidence_mode"):
        study_all_candidates(
            snapshots=[replay, forward],
            bars=_bars("AAA") + _bars("BBB", start_price=105),
            universe_denominators=[_denominator([replay, forward])],
            split_assignment=_split([replay, forward]),
            assumptions=CandidateStudyAssumptions(5, 10, 1),
            bars_source_ref="sha256:bars",
        )


def test_accepts_validated_snapshot_and_denominator_mappings() -> None:
    snapshot = _snapshot("AAA")
    denominator = _denominator([snapshot])
    assignment_source = {snapshot.snapshot_id: "discovery"}
    assignment = CandidateSplitAssignment.create(
        assignment_source,
        source_ref="sha256:immutable-split",
    )
    assignment_source[snapshot.snapshot_id] = "locked_test"

    result = study_all_candidates(
        snapshots=[snapshot.to_dict()],
        bars={"AAA": _bars("AAA")},
        universe_denominators=[denominator.to_dict()],
        split_assignment=assignment,
        assumptions=CandidateStudyAssumptions(5, 10, 1),
        bars_source_ref="sha256:bars",
    )

    assert result.outcomes[0].split == "discovery"
    assert result.coverage[0].snapshot_coverage_complete is True


def test_denominator_mapping_rejects_tampered_content_id() -> None:
    snapshot = _snapshot("AAA")
    row = _denominator([snapshot]).to_dict()
    row["denominator_id"] = "tampered"

    with pytest.raises(ValueError, match="supplied denominator_id"):
        CandidateUniverseDenominator.from_mapping(row)


def test_study_rejects_extra_unrelated_universe_denominator() -> None:
    snapshot = _snapshot("AAA")
    extra_cutoff = CUTOFF + timedelta(minutes=5)
    extra = CandidateUniverseDenominator.create(
        market_date=MARKET_DATE,
        feature_cutoff_at=extra_cutoff,
        expected_symbols=("AAA",),
        source_ref="sha256:extra-universe",
        expected_symbols_complete=True,
    )

    with pytest.raises(ValueError, match="exactly match supplied candidate cohorts"):
        study_all_candidates(
            snapshots=[snapshot],
            bars=_bars("AAA"),
            universe_denominators=[_denominator([snapshot]), extra],
            split_assignment=_split([snapshot]),
            assumptions=CandidateStudyAssumptions(5, 10, 1),
            bars_source_ref="sha256:bars",
        )


def test_same_day_candidate_cohort_cannot_cross_frozen_splits() -> None:
    snapshots = [_snapshot("AAA"), _snapshot("BBB")]
    assignment = CandidateSplitAssignment.create(
        {
            snapshots[0].snapshot_id: "discovery",
            snapshots[1].snapshot_id: "validation",
        },
        source_ref="sha256:frozen-split",
    )

    with pytest.raises(ValueError, match="same-day candidate cohorts"):
        study_all_candidates(
            snapshots=snapshots,
            bars=_bars("AAA") + _bars("BBB"),
            universe_denominators=[_denominator(snapshots)],
            split_assignment=assignment,
            assumptions=CandidateStudyAssumptions(5, 10, 1),
            bars_source_ref="sha256:bars",
        )


def test_incomplete_grid_keeps_missing_outcomes_null_not_zero() -> None:
    snapshot = _snapshot("AAA")
    missing = datetime(2026, 7, 16, 10, 0, tzinfo=ET)
    outcome = _study([snapshot], _bars("AAA", omit=[missing])).outcomes[0]

    assert outcome.status == "pending_incomplete_session_grid"
    assert outcome.missing_expected_bar_at == missing
    assert outcome.gross_return_5m_pct is not None
    assert outcome.gross_return_15m_pct is None
    assert outcome.gross_close_return_pct is None
    assert outcome.after_cost_close_return_pct is None
    assert outcome.mfe_pct is None
    assert outcome.mae_pct is None
    assert outcome.candidate_return_rank is None


def test_candidate_rank_requires_complete_population_truth() -> None:
    snapshots = [_snapshot("AAA"), _snapshot("BBB")]
    missing = datetime(2026, 7, 16, 10, 0, tzinfo=ET)
    mixed = _study(
        snapshots,
        _bars("AAA") + _bars("BBB", omit=[missing]),
    )

    assert any(row.status == "complete" for row in mixed.outcomes)
    assert any(row.status.startswith("pending_") for row in mixed.outcomes)
    assert all(row.candidate_return_rank is None for row in mixed.outcomes)
    assert all(row.candidate_return_population is None for row in mixed.outcomes)

    snapshot = _snapshot("CCC")
    incomplete_denominator = _denominator([snapshot], complete=False)
    incomplete_population = _study(
        [snapshot],
        _bars("CCC"),
        denominator=incomplete_denominator,
    )
    assert incomplete_population.outcomes[0].status == "complete"
    assert incomplete_population.outcomes[0].candidate_return_rank is None
    assert incomplete_population.outcomes[0].candidate_return_population is None


def test_coverage_uses_caller_denominator_and_ranks_only_complete_members() -> None:
    snapshots = [_snapshot("AAA"), _snapshot("BBB", price=105, previous_close=100)]
    bars = _bars("AAA", step=0.2) + _bars("BBB", start_price=105, step=-0.02)
    denominator = _denominator(snapshots, extra_symbols=["CCC"])
    result = _study(snapshots, bars, denominator=denominator)
    coverage = result.coverage[0]

    assert coverage.expected_count == 3
    assert coverage.observed_count == 2
    assert coverage.missing_symbols == ("CCC",)
    assert coverage.snapshot_coverage_pct == pytest.approx(66.66666667)
    assert coverage.snapshot_coverage_complete is False
    assert all(row.candidate_return_rank is None for row in result.outcomes)
    assert all(row.candidate_return_population is None for row in result.outcomes)


def test_verified_complete_eod_list_enables_winner_control_comparison() -> None:
    snapshots = [_snapshot("AAA"), _snapshot("BBB", price=105, previous_close=100)]
    bars = _bars("AAA", step=0.2) + _bars("BBB", start_price=105, step=-0.02)
    eod = [
        {
            "market_date": MARKET_DATE,
            "symbol": "AAA",
            "rank": 1,
            "dataset_role": "descriptive_eod_movers",
            "source_snapshot_kind": "realized_eod_gainers",
            "source_complete": True,
            "source_coverage_complete": True,
            "list_coverage_complete": True,
            "expected_row_count": 1,
            "source_ref": "sha256:eod-list",
            "source_artifact_ref": "sha256:eod-list",
            "eod_label_eligible": True,
            "prospective_signal_eligible": False,
            "ingestion_channel": "local_operator_csv",
            "corporate_action_status": "verified_clear",
            "corporate_action_source_ref": "sha256:corporate-action-evidence",
            "extracted_at": "2026-07-16T16:05:00-04:00",
            "system_received_at": "2026-07-16T16:06:00-04:00",
        }
    ]

    result = _study(snapshots, bars, eod=eod)
    by_symbol = {row.symbol: row for row in result.outcomes}
    comparison = result.mover_control_comparisons[0]

    assert by_symbol["AAA"].eod_mover_matched is True
    assert by_symbol["AAA"].eod_mover_rank == 1
    assert by_symbol["BBB"].eod_mover_matched is False
    assert comparison.applicable is True
    assert comparison.matched_candidate_count == 1
    assert comparison.nonmatched_control_count == 1
    assert comparison.matched_minus_control_pct is not None


@pytest.mark.parametrize(
    ("override", "expected_match"),
    [
        ({"dataset_role": "prospective_mover_snapshots"}, None),
        ({"source_snapshot_kind": "premarket_gainers"}, None),
        ({"source_complete": False}, None),
        ({"list_coverage_complete": False}, True),
    ],
)
def test_eod_nonmatch_stays_unknown_without_proven_complete_list(
    override: dict[str, object], expected_match: bool | None
) -> None:
    snapshots = [_snapshot("AAA"), _snapshot("BBB", price=105, previous_close=100)]
    row: dict[str, object] = {
        "market_date": MARKET_DATE,
        "symbol": "AAA",
        "rank": 1,
        "dataset_role": "descriptive_eod_movers",
        "source_snapshot_kind": "realized_eod_gainers",
        "source_complete": True,
        "source_coverage_complete": True,
        "list_coverage_complete": True,
        "expected_row_count": 1,
        "source_ref": "sha256:eod-list",
        "source_artifact_ref": "sha256:eod-list",
        "eod_label_eligible": True,
        "prospective_signal_eligible": False,
        "ingestion_channel": "local_operator_csv",
        "corporate_action_status": "verified_clear",
        "corporate_action_source_ref": "sha256:corporate-action-evidence",
        "extracted_at": "2026-07-16T16:05:00-04:00",
        "system_received_at": "2026-07-16T16:06:00-04:00",
    }
    row.update(override)
    result = _study(
        snapshots,
        _bars("AAA") + _bars("BBB", start_price=105),
        eod=[row],
    )
    by_symbol = {outcome.symbol: outcome for outcome in result.outcomes}

    assert by_symbol["AAA"].eod_mover_matched is expected_match
    assert by_symbol["BBB"].eod_mover_matched is None
    assert result.mover_control_comparisons[0].applicable is False


def test_discovery_correlations_use_all_candidates_and_frozen_assignment() -> None:
    snapshots = [
        _snapshot("AAA", price=100, previous_close=99),
        _snapshot("BBB", price=105, previous_close=100),
        _snapshot("CCC", price=110, previous_close=100),
    ]
    bars = (
        _bars("AAA", start_price=100, step=0.01)
        + _bars("BBB", start_price=105, step=0.1)
        + _bars("CCC", start_price=110, step=0.2)
    )
    result = _study(snapshots, bars)
    gap = next(row for row in result.discovery_correlations if row.feature == "gap_pct")

    assert gap.population == "all_candidate_snapshots"
    assert gap.split == "discovery"
    assert gap.sample_count == 3
    assert gap.status == "calculated"
    assert gap.spearman_rho == pytest.approx(1.0)

    incomplete_assignment = CandidateSplitAssignment.create(
        {snapshots[0].snapshot_id: "discovery"}, source_ref="sha256:bad-split"
    )
    with pytest.raises(ValueError, match="exactly cover"):
        study_all_candidates(
            snapshots=snapshots,
            bars=bars,
            universe_denominators=[_denominator(snapshots)],
            split_assignment=incomplete_assignment,
            assumptions=CandidateStudyAssumptions(5, 10, 1),
            bars_source_ref="sha256:bars",
        )


def test_naive_bar_timestamp_is_rejected_instead_of_assumed_utc() -> None:
    snapshot = _snapshot("AAA")
    bad = MarketBar(
        symbol="AAA",
        timestamp=datetime(2026, 7, 16, 9, 50),
        open=100,
        high=101,
        low=99,
        close=100.5,
        volume=1000,
    )
    with pytest.raises(ValueError, match="timezone"):
        _study([snapshot], [bad])


def test_uses_published_early_close_not_hard_coded_1600() -> None:
    cutoff = datetime(2026, 11, 27, 9, 45, tzinfo=ET)
    snapshot = _snapshot("AAA", cutoff=cutoff)
    outcome = _study(
        [snapshot],
        _bars("AAA", cutoff=cutoff, close_time=time(13, 0)),
    ).outcomes[0]

    assert outcome.status == "complete"
    assert outcome.official_close_at == datetime(2026, 11, 27, 13, 0, tzinfo=ET)


def test_assumptions_must_support_exact_fixed_horizons() -> None:
    with pytest.raises(ValueError, match="divide every fixed"):
        CandidateStudyAssumptions(
            bar_interval_minutes=3,
            slippage_bps=10,
            fee_bps=1,
        )
