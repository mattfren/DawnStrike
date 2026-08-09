from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from intraday_scanner.alpha.path_replay import PathTruthStatus, resolve_path
from intraday_scanner.storage.intraday_evidence_store import IntradayEvidenceStore

UTC = timezone.utc
START = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)


def _bar(minutes: int, *, open: float, high: float, low: float, close: float) -> dict[str, object]:
    return {
        "observed_at": START + timedelta(minutes=minutes),
        "open": open,
        "high": high,
        "low": low,
        "close": close,
    }


def test_resolves_target_first_and_excludes_trigger_bar_extrema() -> None:
    result = resolve_path(
        [
            _bar(0, open=10, high=12, low=8, close=10),
            _bar(1, open=10, high=10.5, low=9.8, close=10.2),
            _bar(2, open=10.2, high=11.1, low=10, close=11),
        ],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert result.target_touched_at == START + timedelta(minutes=2)
    assert result.mfe_price == 11.1
    assert result.mae_price == 9.8
    assert result.entry_bar_excluded is True


def test_resolves_stop_first() -> None:
    result = resolve_path(
        [
            _bar(0, open=10, high=10.6, low=10, close=10.2),
            _bar(1, open=10.2, high=10.3, low=8.9, close=9),
        ],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_STOP_FIRST
    assert result.conservative_policy_result == "stop_first"


def test_same_minute_both_touched_keeps_fact_separate_from_policy() -> None:
    result = resolve_path(
        [
            _bar(0, open=10, high=10.6, low=10, close=10.2),
            _bar(1, open=10.2, high=11.1, low=8.9, close=9.5),
        ],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
    )

    assert result.path_truth_status == PathTruthStatus.SAME_MINUTE_AMBIGUOUS
    assert result.conservative_policy_result == "stop_first"
    assert result.target_touched_at == result.stop_touched_at


def test_entry_bar_ambiguity_has_null_exact_excursion() -> None:
    result = resolve_path(
        [_bar(0, open=10, high=11.2, low=8.8, close=10.5)],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
    )

    assert result.path_truth_status == PathTruthStatus.ENTRY_BAR_AMBIGUOUS
    assert result.mfe_price is None
    assert result.mae_price is None
    assert result.bounds["mfe_upper"] == 11.2


def test_missing_and_no_trigger_states_are_not_conflated() -> None:
    missing = resolve_path([], decision_at=START, trigger=10, target=11, stop=9)
    no_trigger = resolve_path(
        [_bar(0, open=10, high=10.2, low=9.8, close=10)],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
    )

    assert missing.path_truth_status == PathTruthStatus.MISSING_BARS
    assert no_trigger.path_truth_status == PathTruthStatus.NOT_TRIGGERED
    assert no_trigger.mfe_price is None and no_trigger.mae_price is None


def test_halt_corporate_action_and_source_conflict_fail_closed() -> None:
    bars = [
        _bar(0, open=10, high=10.6, low=9.8, close=10),
        _bar(2, open=10, high=11, low=9, close=10),
    ]
    halt = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
        halt_intervals=((START + timedelta(minutes=1), START + timedelta(minutes=2)),),
    )
    action = resolve_path(
        bars, decision_at=START, trigger=10.5, target=11, stop=9, corporate_action_unresolved=True
    )
    conflict = resolve_path(
        bars, decision_at=START, trigger=10.5, target=11, stop=9, source_conflict=True
    )

    assert halt.path_truth_status == PathTruthStatus.KNOWN_HALT_WINDOW
    assert action.path_truth_status == PathTruthStatus.CORPORATE_ACTION_UNRESOLVED
    assert conflict.path_truth_status == PathTruthStatus.SOURCE_CONFLICT


def test_replay_and_excursion_reconciliation_are_append_only(tmp_path: Path) -> None:
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )
    result = resolve_path(
        [
            _bar(0, open=10, high=10.6, low=10, close=10.2),
            _bar(1, open=10.2, high=11.1, low=9.8, close=10.5),
        ],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
    )
    replay = {
        **result.to_dict(),
        "path_replay_id": "replay-1",
        "cohort": "official_outcome_required",
        "selection_id": "selection-1",
        "signal_id": "signal-1",
        "market_date": "2026-11-27",
        "policy_version": "path-replay-v1",
        "artifact_identity": "artifact-1",
        "artifact_hash_sha256": "bars-hash",
        "retrospective_research_eligible": True,
        "prospective_promotion_eligible": False,
    }

    assert store.persist_path_replay(replay) == {"inserted": 1, "row_count": 1}
    assert store.persist_path_replay(replay) == {"inserted": 0, "row_count": 1}
    reconciliation = {
        "reconciliation_id": "reconciliation-1",
        "position_id": "legacy-position-1",
        "path_replay_id": "replay-1",
        "source_bar_hash_sha256": "bars-hash",
        "source_quote_hash_sha256": "quotes-hash",
        "path_truth_status": result.path_truth_status.value,
        "mfe_price": result.mfe_price,
        "mfe_at": result.to_dict()["mfe_at"],
        "mae_price": result.mae_price,
        "mae_at": result.to_dict()["mae_at"],
        "reconciliation_receipt_hash_sha256": "receipt-hash",
    }

    assert store.persist_excursion_reconciliation(reconciliation)["inserted"] == 1
    assert store.persist_excursion_reconciliation(reconciliation)["inserted"] == 0
