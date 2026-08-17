from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.alpha import path_replay as path_replay_module
from intraday_scanner.alpha.path_replay import (
    PathReplayResult,
    PathTruthStatus,
    canonical_path_contract_valid,
    canonical_path_return_eligible,
    resolve_path,
)
from intraday_scanner.storage.intraday_evidence_store import (
    EvidenceStoreError,
    IntradayEvidenceStore,
)

UTC = timezone.utc
START = datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
FUTURE_EVIDENCE_SCHEMA_VERSION = "dawnstrike.future_evidence_receipt.v1"
ENTRY_RECEIPT_SCHEMA_VERSION = "dawnstrike.path_entry_receipt.v1"


def _paper_replay_binding(
    *,
    selection_id: str = "selection-1",
    scan_id: str = "scan-1",
    signal_id: str = "signal-1",
) -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.path_replay_binding.v1",
        "subject": {"symbol": "NOVA", "market_date": "2026-11-27"},
        "origin": {
            "kind": "alpha_paper_selection",
            "id": selection_id,
            "lineage": {
                "selection_id": selection_id,
                "scan_id": scan_id,
                "signal_id": signal_id,
            },
            "context_hash_sha256": "d" * 64,
        },
    }


def _v6_replay_binding() -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.path_replay_binding.v1",
        "subject": {"symbol": "NOVA", "market_date": "2026-11-27"},
        "origin": {
            "kind": "alpha_v6_shadow_decision",
            "id": "decision-1",
            "lineage": {
                "decision_id": "decision-1",
                "scan_id": "scan-1",
                "source_signal_id": "source-signal-1",
                "shadow_signal_id": "shadow-signal-1",
            },
            "context_hash_sha256": "d" * 64,
        },
    }


def _paper_enter_replay_binding() -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.path_replay_binding.v1",
        "subject": {"symbol": "NOVA", "market_date": "2026-11-27"},
        "origin": {
            "kind": "alpha_paper_enter_intent",
            "id": "intent-1",
            "lineage": {
                "selection_id": "selection-1",
                "scan_id": "scan-1",
                "signal_id": "signal-1",
                "intent_id": "intent-1",
            },
            "context_hash_sha256": "d" * 64,
        },
    }


def _entry_receipt(
    *,
    effective_at: datetime = START,
    observed_at: datetime = START - timedelta(minutes=1),
    completed_at: datetime = START,
    price: float = 10.05,
    replay_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    binding = replay_binding or _paper_enter_replay_binding()
    body: dict[str, object] = {
        "schema_version": ENTRY_RECEIPT_SCHEMA_VERSION,
        "entry_mode": "ALREADY_ENTERED_AT_DECISION",
        "raw_entry_price": price,
        "effective_at": effective_at.isoformat(),
        "source_observation_id": "observation:NOVA:entry-1",
        "source_bar_hash_sha256": "e" * 64,
        "source_observed_at": observed_at.isoformat(),
        "source_bar_completed_at": completed_at.isoformat(),
        "replay_origin": {
            key: copy.deepcopy(binding["origin"][key])
            for key in ("kind", "id", "lineage")
        },
    }
    digest = _sha_json(body)
    return {
        **body,
        "receipt_id": f"path-entry-v1-{digest}",
        "receipt_hash_sha256": digest,
    }


def _bar(minutes: int, *, open: float, high: float, low: float, close: float) -> dict[str, object]:
    return {
        "observed_at": START + timedelta(minutes=minutes),
        "open": open,
        "high": high,
        "low": low,
        "close": close,
    }


def _future_evidence_receipt(
    bars: list[dict[str, object]],
    *,
    symbol: str = "NOVA",
    market_date: str = "2026-11-27",
    raw_artifact_identity: str = "provider-bars:NOVA:2026-11-27",
    coverage_complete: bool = True,
) -> dict[str, object]:
    canonical_bars = [
        {
            "observed_at": row["observed_at"].astimezone(UTC).isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for row in sorted(bars, key=lambda row: row["observed_at"])
    ]
    first_bar_at = canonical_bars[0]["observed_at"]
    last_bar_at = canonical_bars[-1]["observed_at"]
    body: dict[str, object] = {
        "schema_version": FUTURE_EVIDENCE_SCHEMA_VERSION,
        "subject": {"symbol": symbol, "market_date": market_date},
        "raw_artifact_identity": raw_artifact_identity,
        "raw_bar_hash_sha256": _sha_json(canonical_bars),
        "bar_count": len(canonical_bars),
        "first_bar_at": first_bar_at,
        "last_bar_at": last_bar_at,
        "coverage_start": first_bar_at,
        "coverage_end": (
            datetime.fromisoformat(str(last_bar_at)) + timedelta(minutes=1)
        ).isoformat(),
        "coverage_complete": coverage_complete,
    }
    digest = _sha_json(body)
    return {
        **body,
        "receipt_id": f"future-evidence-v1-{digest}",
        "receipt_hash_sha256": digest,
    }


def test_resolves_target_first_and_excludes_trigger_bar_extrema() -> None:
    result = resolve_path(
        [
            _bar(0, open=10, high=10.6, low=10, close=10.5),
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
    assert result.mfe_price is None
    assert result.mae_price is None
    assert result.bounds["mfe_upper"] == 11.1
    assert result.to_dict()["excursion_exact"] is False
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

    assert result.path_truth_status == PathTruthStatus.TARGET_STOP_INTERVAL_CENSORED
    assert result.conservative_policy_result is None
    assert result.to_dict()["path_event"] == "SAME_INTERVAL_CENSORED"
    assert result.target_touched_at == result.stop_touched_at


def test_entry_bar_ambiguity_has_null_exact_excursion() -> None:
    result = resolve_path(
        [_bar(0, open=10, high=11.2, low=8.8, close=10.5)],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
    )

    assert result.path_truth_status == PathTruthStatus.ENTRY_INTERVAL_CENSORED
    assert result.mfe_price is None
    assert result.mae_price is None
    assert result.bounds["mfe_upper"] == 11.2


def test_entry_interval_censoring_is_not_erased_by_later_bars() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.0, high=11.2, low=8.8, close=10.5),
            _bar(1, open=10.5, high=11.1, low=10.2, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert result.path_truth_status == PathTruthStatus.ENTRY_INTERVAL_CENSORED
    assert result.exit_time is None
    assert result.exit_price is None
    assert result.mfe_price is None
    assert result.mae_price is None


def test_same_interval_target_stop_is_factually_censored() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.2, low=8.8, close=9.5),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert result.path_truth_status == PathTruthStatus.TARGET_STOP_INTERVAL_CENSORED
    assert result.to_dict()["path_event"] == "SAME_INTERVAL_CENSORED"
    assert result.target_touched_at == result.stop_touched_at


def test_complete_ordered_events_resolve_same_interval_ambiguity() -> None:
    interval = START + timedelta(minutes=1)
    events = (
        {"observed_at": START, "event_type": "TRADE", "price": 10.6},
        {"observed_at": interval + timedelta(seconds=5), "event_type": "TRADE", "price": 10.6},
        {"observed_at": interval + timedelta(seconds=15), "event_type": "TRADE", "price": 11.0},
        {"observed_at": interval + timedelta(seconds=40), "event_type": "TRADE", "price": 8.9},
    )
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.2, low=8.8, close=9.5),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trades-fixture",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=2),
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert result.exit_time == interval + timedelta(seconds=15)
    assert result.exit_price == 11.0
    assert result.to_dict()["path_event"] == "TARGET"


def test_activated_no_touch_path_is_right_censored_at_verified_close() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=10.9, low=10.3, close=10.7),
            _bar(2, open=10.7, high=10.8, low=10.4, close=10.75),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=3),
    )

    assert result.path_truth_status == PathTruthStatus.RIGHT_CENSORED_SESSION_CLOSE
    assert result.exit_time == START + timedelta(minutes=3)
    assert result.exit_price == 10.75
    assert result.to_dict()["path_event"] == "TIMEOUT"
    assert result.to_dict()["excursion_exact"] is False
    assert result.mfe_price is None
    assert result.mae_price is None
    assert result.bounds == {"mfe_upper": 10.9, "mae_lower": 10.2}


def test_missing_interval_is_not_bridged_to_a_later_touch() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(2, open=10.7, high=11.2, low=10.4, close=11.1),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=3),
    )

    assert result.path_truth_status == PathTruthStatus.MISSING_INTERVAL_CENSORED
    assert result.exit_time is None
    assert result.to_dict()["path_event"] == "LIQUIDITY_FAILURE"
    assert result.to_dict()["sequence_complete_through_exit"] is False


def test_halt_gap_remains_censored_after_resume() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(3, open=10.7, high=11.2, low=10.4, close=11.1),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=4),
        halt_intervals=(
            (START + timedelta(minutes=1), START + timedelta(minutes=3)),
        ),
    )

    assert result.path_truth_status == PathTruthStatus.HALT_CENSORED
    assert result.exit_time is None
    assert result.to_dict()["path_event"] == "HALT"


def test_gap_through_stop_uses_worse_executable_open() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=8.5, high=9.1, low=8.2, close=8.8),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_STOP_FIRST
    assert result.exit_price == 8.5
    assert result.to_dict()["path_event"] == "STOP"


def test_excursions_stop_at_canonical_exit_and_require_complete_sequence() -> None:
    interval = START + timedelta(minutes=1)
    events = (
        {"observed_at": START, "event_type": "TRADE", "price": 10.6},
        {"observed_at": interval + timedelta(seconds=5), "event_type": "TRADE", "price": 10.6},
        {"observed_at": interval + timedelta(seconds=10), "event_type": "TRADE", "price": 10.4},
        {"observed_at": interval + timedelta(seconds=20), "event_type": "TRADE", "price": 11.0},
    )
    complete = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.2, low=10.4, close=11.1),
            _bar(2, open=11.1, high=50.0, low=1.0, close=25.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trades-fixture",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=2),
    )
    interval_only = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.2, low=10.4, close=11.1),
            _bar(2, open=11.1, high=50.0, low=1.0, close=25.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert complete.to_dict()["excursion_exact"] is True
    assert complete.mfe_price == 11.0
    assert complete.mae_price == 10.4
    assert interval_only.to_dict()["excursion_exact"] is False
    assert interval_only.mfe_price is None
    assert interval_only.mae_price is None
    assert interval_only.bounds["mfe_upper"] == 11.2
    assert interval_only.bounds["mae_lower"] == 10.2


def test_open_known_order_resolves_single_touch_and_open_gap_before_later_stop() -> None:
    entry_target = resolve_path(
        [_bar(0, open=10.6, high=11.2, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )
    next_open_target = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=11.2, high=11.3, low=8.8, close=9.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert entry_target.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert entry_target.to_dict()["path_event"] == "TARGET"
    assert next_open_target.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert next_open_target.exit_time == START + timedelta(minutes=1)
    assert next_open_target.to_dict()["event_time_precision"] == "EXACT"


def test_complete_ordered_quotes_resolve_trigger_interval_but_missing_identity_does_not() -> None:
    events = (
        {
            "observed_at": START + timedelta(seconds=5),
            "event_type": "QUOTE",
            "bid": 10.4,
            "ask": 10.45,
        },
        {
            "observed_at": START + timedelta(seconds=15),
            "event_type": "QUOTE",
            "bid": 10.5,
            "ask": 10.55,
        },
        {
            "observed_at": START + timedelta(seconds=30),
            "event_type": "QUOTE",
            "bid": 11.0,
            "ask": 11.05,
        },
    )
    kwargs = {
        "decision_at": START,
        "trigger": 10.5,
        "target": 11.0,
        "stop": 9.0,
        "ordered_events": events,
        "ordered_evidence_complete": True,
    }
    resolved = resolve_path(
        [_bar(0, open=10.4, high=11.2, low=10.2, close=11.0)],
        **kwargs,
        ordered_evidence_identity="quotes-fixture",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )
    unbound = resolve_path(
        [_bar(0, open=10.4, high=11.2, low=10.2, close=11.0)],
        **kwargs,
    )

    assert resolved.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert resolved.entry_price == 10.55
    assert resolved.exit_time == START + timedelta(seconds=30)
    assert resolved.exit_price == 11.0
    assert resolved.to_dict()["event_time_precision"] == "EXACT"
    assert resolved.mae_price == 10.5
    assert resolved.mfe_price == 11.0
    assert unbound.path_truth_status == PathTruthStatus.SOURCE_CONFLICT


def test_complete_ordered_trigger_interval_can_resolve_stop_first() -> None:
    events = (
        {
            "observed_at": START + timedelta(seconds=5),
            "event_type": "QUOTE",
            "bid": 10.45,
            "ask": 10.55,
        },
        {
            "observed_at": START + timedelta(seconds=15),
            "event_type": "QUOTE",
            "bid": 8.9,
            "ask": 9.0,
        },
        {
            "observed_at": START + timedelta(seconds=30),
            "event_type": "QUOTE",
            "bid": 11.1,
            "ask": 11.2,
        },
    )
    result = resolve_path(
        [_bar(0, open=10.4, high=11.2, low=8.8, close=9.5)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trigger-stop-first",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.entry_price == 10.55
    assert result.path_truth_status == PathTruthStatus.RESOLVED_STOP_FIRST
    assert result.to_dict()["path_event"] == "STOP"
    assert result.exit_time == START + timedelta(seconds=15)
    assert result.exit_price == 8.9


def test_ordered_stop_first_and_partial_evidence_remain_factually_distinct() -> None:
    interval = START + timedelta(minutes=1)
    stop_first = (
        {"observed_at": interval + timedelta(seconds=5), "event_type": "TRADE", "price": 10.5},
        {"observed_at": interval + timedelta(seconds=10), "event_type": "TRADE", "price": 8.9},
        {"observed_at": interval + timedelta(seconds=40), "event_type": "TRADE", "price": 11.1},
    )
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.2, low=8.8, close=9.5),
    ]
    resolved = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=stop_first,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trades-stop-first",
        ordered_evidence_hash_sha256=_ordered_hash(stop_first),
        ordered_evidence_start=interval,
        ordered_evidence_end=interval + timedelta(minutes=1),
    )
    partial = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=stop_first[:1],
        ordered_evidence_complete=True,
        ordered_evidence_identity="partial",
        ordered_evidence_hash_sha256=_ordered_hash(stop_first[:1]),
        ordered_evidence_start=interval,
        ordered_evidence_end=interval + timedelta(minutes=1),
    )

    assert resolved.path_truth_status == PathTruthStatus.RESOLVED_STOP_FIRST
    assert resolved.to_dict()["path_event"] == "STOP"
    assert partial.path_truth_status == PathTruthStatus.SOURCE_CONFLICT


def test_both_touch_interval_requires_ordered_evidence_of_both_barriers() -> None:
    interval = START + timedelta(minutes=1)
    stop_only = (
        {
            "observed_at": interval + timedelta(seconds=10),
            "event_type": "TRADE",
            "price": 8.9,
        },
    )
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.2, low=8.8, close=9.5),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=stop_only,
        ordered_evidence_complete=True,
        ordered_evidence_identity="incomplete-stop-only",
        ordered_evidence_hash_sha256=_ordered_hash(stop_only),
        ordered_evidence_start=interval,
        ordered_evidence_end=interval + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert result.to_dict()["path_event"] == "SOURCE_CONFLICT"


def test_simultaneous_conflicting_ordered_events_are_rejected_order_invariantly() -> None:
    interval = START + timedelta(minutes=1)
    observed_at = START + timedelta(minutes=1, seconds=10)
    target = {"observed_at": observed_at, "event_type": "TRADE", "price": 11.1}
    stop = {"observed_at": observed_at, "event_type": "TRADE", "price": 8.9}
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.2, low=8.8, close=9.5),
    ]
    results = []
    for events in ((target, stop), (stop, target)):
        results.append(
            resolve_path(
                bars,
                decision_at=START,
                trigger=10.5,
                target=11.0,
                stop=9.0,
                ordered_events=events,
                ordered_evidence_complete=True,
                ordered_evidence_identity="simultaneous-conflict",
                ordered_evidence_hash_sha256=_ordered_hash(events),
                ordered_evidence_start=interval,
                ordered_evidence_end=interval + timedelta(minutes=1),
            )
        )

    assert {result.path_replay_id for result in results} == {results[0].path_replay_id}
    assert all(
        result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
        for result in results
    )


def test_ordered_event_type_and_aware_offset_normalize_to_one_identity() -> None:
    interval = START + timedelta(minutes=1)
    utc_event = (
        {
            "observed_at": START + timedelta(minutes=1, seconds=10),
            "event_type": "TRADE",
            "price": 11.0,
        },
    )
    offset = timezone(timedelta(hours=-5))
    offset_event = (
        {
            "observed_at": utc_event[0]["observed_at"].astimezone(offset),
            "event_type": "trade",
            "price": 11.0,
        },
    )
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    results = [
        resolve_path(
            bars,
            decision_at=START,
            trigger=10.5,
            target=11.0,
            stop=9.0,
            ordered_events=events,
            ordered_evidence_complete=True,
            ordered_evidence_identity="same-feed",
            ordered_evidence_hash_sha256=_ordered_hash(events),
            ordered_evidence_start=interval,
            ordered_evidence_end=interval + timedelta(minutes=1),
        )
        for events in (utc_event, offset_event)
    ]

    assert results[0].path_replay_id == results[1].path_replay_id
    assert results[0].exit_time == results[1].exit_time


@pytest.mark.parametrize(
    "malformed",
    (
        {"event_type": "QUOTE", "bid": 10.6, "ask": 10.5},
        {"event_type": "QUOTE", "bid": 0.0, "ask": 10.5},
        {"event_type": "QUOTE", "bid": 10.5, "ask": -1.0},
        {"event_type": "TRADE", "price": 0.0},
    ),
)
def test_malformed_ordered_market_events_cannot_resolve_ambiguity(
    malformed: dict[str, object],
) -> None:
    interval = START + timedelta(minutes=1)
    events = (
        {
            **malformed,
            "observed_at": START + timedelta(minutes=1, seconds=10),
        },
    )
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.2, low=8.8, close=9.5),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="malformed",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=interval,
        ordered_evidence_end=interval + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT


def test_interval_only_solo_touches_publish_bounds_not_fabricated_exact_times() -> None:
    target = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.2, low=10.3, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )
    entry_stop = resolve_path(
        [_bar(0, open=10.6, high=10.8, low=8.8, close=9.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert target.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert target.to_dict()["event_time_precision"] == "INTERVAL"
    assert target.to_dict()["event_interval_start"] == (START + timedelta(minutes=1)).isoformat()
    assert target.to_dict()["event_interval_end"] == (START + timedelta(minutes=2)).isoformat()
    assert entry_stop.path_truth_status == PathTruthStatus.RESOLVED_STOP_FIRST
    assert entry_stop.to_dict()["path_event"] == "STOP"
    assert entry_stop.to_dict()["event_time_precision"] == "INTERVAL"


@pytest.mark.parametrize(
    "broken_field",
    ("open", "high", "low", "close"),
)
def test_nonfinite_ohlc_blocks_the_sequence_as_source_conflict(broken_field: str) -> None:
    broken = _bar(1, open=10.6, high=10.9, low=10.3, close=10.7)
    broken[broken_field] = float("nan")
    result = resolve_path(
        [_bar(0, open=10.6, high=10.8, low=10.2, close=10.6), broken],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert result.to_dict()["path_event"] == "SOURCE_CONFLICT"
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert result.to_dict()["sequence_complete_through_exit"] is False


def test_session_close_boundary_wins_over_bars_and_events_at_or_after_close() -> None:
    events = (
        {
            "observed_at": START + timedelta(minutes=1),
            "event_type": "TRADE",
            "price": 11.1,
        },
    )
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.7),
            _bar(1, open=10.7, high=11.2, low=10.5, close=11.1),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        ordered_events=events,
        ordered_evidence_complete=False,
        ordered_evidence_identity="after-close",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.RIGHT_CENSORED_SESSION_CLOSE
    assert result.to_dict()["path_event"] == "TIMEOUT"
    assert result.exit_time == START + timedelta(minutes=1)
    assert result.exit_price == 10.7


def test_ordered_event_outside_ohlc_is_source_conflict() -> None:
    event = (
        {
            "observed_at": START + timedelta(minutes=1, seconds=10),
            "event_type": "TRADE",
            "price": 11.1,
        },
    )
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.7),
            _bar(1, open=10.7, high=10.9, low=10.5, close=10.8),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        ordered_events=event,
        ordered_evidence_complete=True,
        ordered_evidence_identity="ohlc-conflict",
        ordered_evidence_hash_sha256=_ordered_hash(event),
        ordered_evidence_start=START + timedelta(minutes=1),
        ordered_evidence_end=START + timedelta(minutes=2),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert result.to_dict()["path_event"] == "SOURCE_CONFLICT"


def test_same_quote_can_trigger_at_ask_and_stop_at_executable_bid() -> None:
    events = (
        {
            "observed_at": START + timedelta(seconds=10),
            "event_type": "QUOTE",
            "bid": 8.9,
            "ask": 10.55,
        },
    )
    result = resolve_path(
        [_bar(0, open=10.4, high=10.6, low=8.8, close=9.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="same-quote-entry-stop",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.entry_price == 10.55
    assert result.path_truth_status == PathTruthStatus.RESOLVED_STOP_FIRST
    assert result.exit_time == START + timedelta(seconds=10)
    assert result.exit_price == 8.9


def test_exact_ordered_excursion_excludes_pre_entry_interval_prices() -> None:
    events = (
        {
            "observed_at": START + timedelta(seconds=5),
            "event_type": "QUOTE",
            "bid": 1.0,
            "ask": 1.05,
        },
        {
            "observed_at": START + timedelta(seconds=15),
            "event_type": "QUOTE",
            "bid": 10.5,
            "ask": 10.55,
        },
        {
            "observed_at": START + timedelta(seconds=30),
            "event_type": "QUOTE",
            "bid": 11.0,
            "ask": 11.05,
        },
    )
    result = resolve_path(
        [_bar(0, open=10.4, high=11.2, low=1.0, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="pre-entry-extreme",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert result.mae_price == 10.5
    assert result.mae_price != 1.0
    assert result.mfe_price == 11.0


def test_bounded_gap_stop_includes_worse_exit_in_excursion_bounds() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.0, high=10.6, low=10.0, close=10.5),
            _bar(1, open=8.5, high=9.0, low=8.2, close=8.8),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_STOP_FIRST
    assert result.to_dict()["excursion_exact"] is False
    assert result.bounds["mae_lower"] <= 8.5
    assert result.bounds["mfe_upper"] >= 10.5


@pytest.mark.parametrize(
    ("trigger", "target", "stop"),
    (
        (float("nan"), 11.0, 9.0),
        (10.5, float("nan"), 9.0),
        (10.5, 11.0, float("inf")),
        (0.0, 11.0, 9.0),
    ),
)
def test_nonfinite_or_nonpositive_levels_fail_closed(
    trigger: float,
    target: float,
    stop: float,
) -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=10.8, low=10.2, close=10.7)],
        decision_at=START,
        trigger=trigger,
        target=target,
        stop=stop,
        session_close=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_return_eligible(result.to_dict()) is False


def test_halt_overlapping_trigger_interval_censors_activation() -> None:
    result = resolve_path(
        [_bar(0, open=10.4, high=10.6, low=10.2, close=10.5)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        halt_intervals=((START, START + timedelta(minutes=1)),),
        session_close=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.HALT_CENSORED
    assert result.to_dict()["path_event"] == "HALT"


def test_replay_identity_is_order_invariant_and_binds_source_and_truth_flags() -> None:
    bars = (
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    )
    kwargs = {
        "decision_at": START,
        "trigger": 10.5,
        "target": 11.0,
        "stop": 9.0,
        "source_artifact_identity": "bars:NOVA:2026-11-27",
        "source_artifact_hash_sha256": "a" * 64,
        "source_coverage_complete": True,
    }
    canonical = resolve_path(bars, **kwargs)
    reversed_input = resolve_path(tuple(reversed(bars)), **kwargs)
    different_source = resolve_path(
        bars,
        **{**kwargs, "source_artifact_hash_sha256": "b" * 64},
    )
    conflict = resolve_path(bars, **kwargs, source_conflict=True)
    corporate_action = resolve_path(bars, **kwargs, corporate_action_unresolved=True)

    assert canonical.path_replay_id == reversed_input.path_replay_id
    assert canonical.path_replay_id != different_source.path_replay_id
    assert canonical.path_replay_id != conflict.path_replay_id
    assert canonical.path_replay_id != corporate_action.path_replay_id
    assert canonical.path_replay_id.startswith("path-v2-")
    assert _is_sha(canonical.to_dict()["path_replay_policy_hash_sha256"])


def test_structural_resolution_without_complete_source_is_not_return_eligible() -> None:
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    structural = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )
    legacy_sourced = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity="bars:NOVA:2026-11-27",
        source_artifact_hash_sha256="a" * 64,
        source_coverage_complete=True,
    )
    sourced = _sourced_target_result()

    assert structural.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert canonical_path_return_eligible(structural.to_dict()) is False
    assert canonical_path_contract_valid(legacy_sourced.to_dict()) is True
    assert canonical_path_return_eligible(legacy_sourced.to_dict()) is False
    assert sourced.to_dict()["path_replay_schema_version"] == "dawnstrike.path_truth.v2"
    assert sourced.to_dict()["eligibility_policy_version"] == (
        "dawnstrike.alphaops-v6-eligibility.v2"
    )
    assert canonical_path_return_eligible(sourced.to_dict()) is True
    incomplete_hash = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        source_artifact_identity="bars:NOVA:2026-11-27",
        source_artifact_hash_sha256="not-a-sha",
        source_coverage_complete=True,
    )
    assert canonical_path_return_eligible(incomplete_hash.to_dict()) is False


def test_timestamp_inputs_fail_closed_or_normalize_to_one_utc_identity() -> None:
    naive_bar = _bar(0, open=10.6, high=10.8, low=10.2, close=10.7)
    naive_bar["observed_at"] = START.replace(tzinfo=None)
    malformed_bar = {
        **_bar(0, open=10.6, high=10.8, low=10.2, close=10.7),
        "observed_at": "not-a-timestamp",
    }
    naive = resolve_path(
        [naive_bar],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )
    malformed = resolve_path(
        [malformed_bar],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )
    naive_decision = resolve_path(
        [_bar(0, open=10.6, high=10.8, low=10.2, close=10.7)],
        decision_at=START.replace(tzinfo=None),
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )

    assert naive.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert malformed.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert naive_decision.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(naive_decision.to_dict()) is True

    offset = timezone(timedelta(hours=-5))
    offset_bar = _bar(0, open=10.6, high=10.8, low=10.2, close=10.7)
    offset_bar["observed_at"] = START.astimezone(offset)
    utc_result = resolve_path(
        [_bar(0, open=10.6, high=10.8, low=10.2, close=10.7)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )
    offset_result = resolve_path(
        [offset_bar],
        decision_at=START.astimezone(offset),
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=(START + timedelta(minutes=1)).astimezone(offset),
    )

    assert utc_result.path_replay_id == offset_result.path_replay_id


def test_path_receipt_self_hash_and_coherence_reject_forged_outputs() -> None:
    result = _sourced_target_result()
    receipt = result.to_dict()

    assert _is_sha(receipt["replay_receipt_hash_sha256"])
    assert canonical_path_return_eligible(receipt) is True
    assert canonical_path_return_eligible({**receipt, "exit_price": 9_999.0}) is False
    assert (
        canonical_path_return_eligible(
            {
                **receipt,
                "path_truth_status": "RESOLVED_STOP_FIRST",
                "path_event": "STOP",
                "exit_price": 1.0,
            }
        )
        is False
    )


def test_missing_and_no_trigger_states_are_not_conflated() -> None:
    missing = resolve_path([], decision_at=START, trigger=10, target=11, stop=9)
    no_trigger = resolve_path(
        [_bar(0, open=10, high=10.2, low=9.8, close=10)],
        decision_at=START,
        trigger=10.5,
        target=11,
        stop=9,
        session_close=START + timedelta(minutes=1),
    )

    assert missing.path_truth_status == PathTruthStatus.MISSING_BARS
    assert no_trigger.path_truth_status == PathTruthStatus.NOT_TRIGGERED
    assert no_trigger.mfe_price is None and no_trigger.mae_price is None
    assert canonical_path_contract_valid(missing.to_dict()) is True
    assert canonical_path_contract_valid(no_trigger.to_dict()) is True


def test_pretrigger_missing_intervals_halts_and_late_start_never_bridge() -> None:
    missing_before_trigger = resolve_path(
        [
            _bar(0, open=10.0, high=10.2, low=9.8, close=10.1),
            _bar(2, open=10.1, high=10.6, low=10.0, close=10.5),
            _bar(3, open=10.5, high=11.1, low=10.4, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=4),
    )
    halt_before_trigger = resolve_path(
        [
            _bar(0, open=10.0, high=10.2, low=9.8, close=10.1),
            _bar(2, open=10.1, high=10.6, low=10.0, close=10.5),
            _bar(3, open=10.5, high=11.1, low=10.4, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=4),
        halt_intervals=((START + timedelta(minutes=1), START + timedelta(minutes=2)),),
    )
    first_bar_late = resolve_path(
        [
            _bar(1, open=10.0, high=10.6, low=9.8, close=10.5),
            _bar(2, open=10.5, high=11.1, low=10.4, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=3),
    )

    assert missing_before_trigger.path_truth_status == PathTruthStatus.MISSING_INTERVAL_CENSORED
    assert missing_before_trigger.to_dict()["path_event"] == "LIQUIDITY_FAILURE"
    assert halt_before_trigger.path_truth_status == PathTruthStatus.HALT_CENSORED
    assert halt_before_trigger.to_dict()["path_event"] == "HALT"
    assert first_bar_late.path_truth_status == PathTruthStatus.MISSING_INTERVAL_CENSORED


def test_no_trigger_requires_complete_cadence_through_verified_close() -> None:
    sparse = resolve_path(
        [
            _bar(0, open=10.0, high=10.2, low=9.8, close=10.1),
            _bar(2, open=10.1, high=10.3, low=9.9, close=10.2),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=3),
    )
    unbounded = resolve_path(
        [_bar(0, open=10.0, high=10.2, low=9.8, close=10.1)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert sparse.path_truth_status == PathTruthStatus.MISSING_INTERVAL_CENSORED
    assert unbounded.path_truth_status == PathTruthStatus.MISSING_INTERVAL_CENSORED


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

    assert halt.path_truth_status == PathTruthStatus.HALT_CENSORED
    assert action.path_truth_status == PathTruthStatus.CORPORATE_ACTION_UNRESOLVED
    assert conflict.path_truth_status == PathTruthStatus.SOURCE_CONFLICT


def test_legacy_path_status_remains_distinct_and_never_becomes_canonical() -> None:
    assert PathTruthStatus.ENTRY_BAR_AMBIGUOUS.value == "ENTRY_BAR_AMBIGUOUS"
    assert (
        PathTruthStatus.ENTRY_BAR_AMBIGUOUS
        is not PathTruthStatus.ENTRY_INTERVAL_CENSORED
    )


def test_replay_and_excursion_reconciliation_are_append_only(tmp_path: Path) -> None:
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )
    result = _sourced_target_result()
    replay = {
        **_store_envelope(result),
        "retrospective_research_eligible": True,
    }

    assert store.persist_path_replay(replay) == {"inserted": 1, "row_count": 1}
    assert store.persist_path_replay(replay) == {"inserted": 0, "row_count": 1}
    reconciliation = {
        "reconciliation_id": "reconciliation-1",
        "position_id": "legacy-position-1",
        "path_replay_id": result.path_replay_id,
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


def test_path_replay_store_rejects_same_id_with_different_payload(tmp_path: Path) -> None:
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )
    result = _sourced_target_result()
    replay = _store_envelope(result)

    assert store.persist_path_replay(replay) == {"inserted": 1, "row_count": 1}
    with pytest.raises(EvidenceStoreError, match="immutable path replay conflict"):
        store.persist_path_replay({**replay, "cohort": "different-envelope"})


def test_contract_reruns_manifest_and_rejects_rehashed_output_forgery() -> None:
    result = _sourced_target_result()
    receipt = result.to_dict()
    forged = {
        **receipt,
        "exit_price": 12.0,
        "bounds": {"mfe_upper": 12.0, "mae_lower": 10.2},
    }
    forged["replay_receipt_hash_sha256"] = _receipt_hash(forged)

    assert canonical_path_contract_valid(receipt) is True
    assert canonical_path_return_eligible(receipt) is True
    assert canonical_path_contract_valid(forged) is False
    assert canonical_path_return_eligible(forged) is False
    assert _is_sha(receipt["replay_truth_hash_sha256"])
    assert receipt["path_replay_id"].startswith("path-v2-")


def test_path_store_rejects_invalid_first_write_before_insert(tmp_path: Path) -> None:
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity="bars:NOVA:2026-11-27",
        source_artifact_hash_sha256="a" * 64,
        source_coverage_complete=True,
    )
    receipt = result.to_dict()
    forged = {**receipt, "exit_price": 12.0}
    forged["replay_receipt_hash_sha256"] = _receipt_hash(forged)
    envelope = {
        **forged,
        "cohort": "official_outcome_required",
        "selection_id": "selection-1",
        "signal_id": "signal-1",
        "market_date": "2026-11-27",
        "artifact_identity": "bars:NOVA:2026-11-27",
        "artifact_hash_sha256": "a" * 64,
        "retrospective_research_eligible": False,
        "prospective_promotion_eligible": False,
    }

    with pytest.raises(EvidenceStoreError, match="canonical path replay"):
        store.persist_path_replay(envelope)

    assert not (tmp_path / "replay.sqlite").exists()


def test_partial_session_close_interval_cannot_resolve_post_close_ohlc() -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.2, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(seconds=30),
    )

    assert result.path_truth_status == PathTruthStatus.MISSING_INTERVAL_CENSORED
    assert result.to_dict()["path_event"] == "LIQUIDITY_FAILURE"
    assert result.exit_time is None
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    ("bars", "halts", "session_close", "expected"),
    (
        (
            (
                _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
                _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
            ),
            ((START + timedelta(minutes=1), START + timedelta(minutes=2)),),
            START + timedelta(minutes=2),
            PathTruthStatus.HALT_CENSORED,
        ),
        (
            (_bar(0, open=10.6, high=10.8, low=10.2, close=10.6),),
            ((START + timedelta(minutes=1), START + timedelta(minutes=3)),),
            START + timedelta(minutes=3),
            PathTruthStatus.HALT_CENSORED,
        ),
        (
            (),
            ((START, START + timedelta(minutes=2)),),
            START + timedelta(minutes=2),
            PathTruthStatus.HALT_CENSORED,
        ),
        (
            (_bar(0, open=10.6, high=10.8, low=10.2, close=10.6),),
            ((START + timedelta(minutes=1), START + timedelta(minutes=2)),),
            START + timedelta(minutes=3),
            PathTruthStatus.MISSING_INTERVAL_CENSORED,
        ),
    ),
)
def test_current_tail_and_no_bar_halts_require_complete_sourced_coverage(
    bars: tuple[dict[str, object], ...],
    halts: tuple[tuple[datetime, datetime], ...],
    session_close: datetime,
    expected: PathTruthStatus,
) -> None:
    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=session_close,
        halt_intervals=halts,
    )

    assert result.path_truth_status == expected
    assert result.to_dict()["path_event"] == (
        "HALT" if expected is PathTruthStatus.HALT_CENSORED else "LIQUIDITY_FAILURE"
    )
    assert result.exit_time is None
    assert canonical_path_contract_valid(result.to_dict()) is True


def test_exact_later_exit_excursion_uses_only_ordered_post_entry_points() -> None:
    events = (
        {
            "observed_at": START + timedelta(seconds=5),
            "event_type": "QUOTE",
            "bid": 1.0,
            "ask": 1.05,
        },
        {
            "observed_at": START + timedelta(seconds=15),
            "event_type": "QUOTE",
            "bid": 10.5,
            "ask": 10.55,
        },
        {
            "observed_at": START + timedelta(minutes=1, seconds=15),
            "event_type": "QUOTE",
            "bid": 11.0,
            "ask": 11.05,
        },
    )
    result = resolve_path(
        [
            _bar(0, open=10.4, high=10.7, low=1.0, close=10.6),
            _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="ordered-feed:NOVA",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=2),
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert result.to_dict()["excursion_exact"] is True
    assert result.mae_price == 10.5
    assert result.mfe_price == 11.0
    assert result.mae_at == START + timedelta(seconds=15)
    assert canonical_path_contract_valid(result.to_dict()) is True


def test_complete_ordered_feed_must_reproduce_ohlc_activation_and_barriers() -> None:
    events = (
        {
            "observed_at": START + timedelta(seconds=10),
            "event_type": "TRADE",
            "price": 10.4,
        },
    )
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="ordered-feed:NOVA",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert result.to_dict()["path_event"] == "SOURCE_CONFLICT"
    assert canonical_path_contract_valid(result.to_dict()) is True


def test_open_entry_with_both_barriers_is_same_interval_not_entry_censored() -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=8.9, close=10.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert result.path_truth_status == PathTruthStatus.TARGET_STOP_INTERVAL_CENSORED
    assert result.to_dict()["path_event"] == "SAME_INTERVAL_CENSORED"
    assert canonical_path_contract_valid(result.to_dict()) is True


def test_replay_manifest_identity_ignores_out_of_scope_input_rows() -> None:
    core = (_bar(0, open=10.6, high=10.8, low=10.2, close=10.7),)
    scoped = resolve_path(
        core,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )
    extras = resolve_path(
        (
            _bar(-1, open=99.0, high=100.0, low=98.0, close=99.0),
            *core,
            _bar(1, open=1.0, high=100.0, low=0.5, close=50.0),
        ),
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )

    assert scoped.to_dict()["replay_input_manifest"] == extras.to_dict()[
        "replay_input_manifest"
    ]
    assert scoped.path_replay_id == extras.path_replay_id


def test_complete_quote_feed_uses_executable_sides_outside_trade_ohlc() -> None:
    events = (
        {
            "observed_at": START + timedelta(seconds=5),
            "event_type": "QUOTE",
            "bid": 10.4,
            "ask": 10.7,
        },
        {
            "observed_at": START + timedelta(seconds=20),
            "event_type": "QUOTE",
            "bid": 11.0,
            "ask": 11.1,
        },
    )
    result = resolve_path(
        [_bar(0, open=10.4, high=10.6, low=10.4, close=10.5)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="nbbo:NOVA",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert result.entry_price == 10.7
    assert result.exit_price == 11.0
    assert canonical_path_contract_valid(result.to_dict()) is True


def test_no_trigger_tail_and_partial_close_full_halts_remain_halt_truth() -> None:
    trailing = resolve_path(
        [_bar(0, open=10.0, high=10.2, low=9.8, close=10.1)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=3),
        halt_intervals=(
            (START + timedelta(minutes=1), START + timedelta(minutes=3)),
        ),
    )
    partial_close = resolve_path(
        [],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(seconds=30),
        halt_intervals=((START, START + timedelta(seconds=30)),),
    )

    for result in (trailing, partial_close):
        assert result.path_truth_status == PathTruthStatus.HALT_CENSORED
        assert result.to_dict()["path_event"] == "HALT"
        assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    ("trigger", "bar_mutation"),
    (
        (True, {}),
        (10.5, {"high": "11.2"}),
        (10.5, {"open": True, "high": True}),
    ),
)
def test_core_numeric_fields_reject_bool_and_string_coercion(
    trigger: object,
    bar_mutation: dict[str, object],
) -> None:
    bar = {
        **_bar(0, open=10.6, high=11.2, low=10.2, close=11.0),
        **bar_mutation,
    }
    result = resolve_path(
        [bar],
        decision_at=START,
        trigger=trigger,  # type: ignore[arg-type]
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )

    assert result.path_truth_status != PathTruthStatus.RESOLVED_TARGET_FIRST
    assert canonical_path_return_eligible(result.to_dict()) is False


@pytest.mark.parametrize("coercion", (1, "true", [1], -1))
def test_path_store_rejects_coercion_only_eligibility_before_initialize(
    tmp_path: Path,
    coercion: object,
) -> None:
    result = _sourced_target_result()
    envelope = {
        **_store_envelope(result),
        "prospective_promotion_eligible": coercion,
    }
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="eligibility.*boolean"):
        store.persist_path_replay(envelope)
    assert not (tmp_path / "replay.sqlite").exists()


@pytest.mark.parametrize(
    "mutation",
    (
        {"cohort": ""},
        {"selection_id": ""},
        {"market_date": "not-a-date"},
        {"artifact_identity": ""},
        {"artifact_hash_sha256": "not-a-sha"},
        {"retrospective_research_eligible": None},
    ),
)
def test_path_store_rejects_malformed_envelope_before_initialize(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    envelope = {**_store_envelope(_sourced_target_result()), **mutation}
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="canonical path replay"):
        store.persist_path_replay(envelope)
    assert not (tmp_path / "replay.sqlite").exists()


@pytest.mark.parametrize(
    "field",
    (
        "cohort",
        "selection_id",
        "market_date",
        "artifact_identity",
        "artifact_hash_sha256",
        "retrospective_research_eligible",
        "prospective_promotion_eligible",
    ),
)
def test_path_store_rejects_absent_envelope_key_before_initialize(
    tmp_path: Path,
    field: str,
) -> None:
    envelope = _store_envelope(_sourced_target_result())
    envelope.pop(field)
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="canonical path replay"):
        store.persist_path_replay(envelope)
    assert not (tmp_path / "replay.sqlite").exists()


@pytest.mark.parametrize(
    ("source_identity", "source_hash"),
    (
        (None, None),
        ("bars:NOVA:2026-11-27", None),
        ("bars:NOVA:2026-11-27", "not-a-sha"),
    ),
)
def test_path_store_requires_complete_source_envelope_for_censored_receipts(
    tmp_path: Path,
    source_identity: str | None,
    source_hash: str | None,
) -> None:
    result = resolve_path(
        [_bar(0, open=10.0, high=11.2, low=8.8, close=10.5)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        source_artifact_identity=source_identity,
        source_artifact_hash_sha256=source_hash,
        source_coverage_complete=True,
        replay_binding=_paper_replay_binding(),
    )
    envelope = {
        **_store_envelope(result),
        "artifact_identity": source_identity or "",
        "artifact_hash_sha256": source_hash or "",
    }
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="source"):
        store.persist_path_replay(envelope)
    assert not (tmp_path / "replay.sqlite").exists()


@pytest.mark.parametrize("interval_seconds", (True, 30.0, 1e20, 1e300))
def test_manifest_decoder_rejects_noncanonical_or_overflowing_cadence(
    interval_seconds: object,
) -> None:
    receipt = _sourced_target_result().to_dict()
    forged = copy.deepcopy(receipt)
    forged["replay_input_manifest"]["bar_interval_seconds"] = interval_seconds
    forged["replay_input_hash_sha256"] = _sha_json(forged["replay_input_manifest"])
    forged["replay_receipt_hash_sha256"] = _receipt_hash(forged)

    assert canonical_path_contract_valid(forged) is False


def test_manifest_validator_never_raises_for_cyclic_or_nonserializable_input() -> None:
    receipt = _sourced_target_result().to_dict()
    cyclic = copy.deepcopy(receipt)
    cyclic_manifest: dict[str, object] = {}
    cyclic_manifest["self"] = cyclic_manifest
    cyclic["replay_input_manifest"] = cyclic_manifest
    nonserializable = copy.deepcopy(receipt)
    nonserializable["replay_input_manifest"]["bars"] = [object()]

    assert canonical_path_contract_valid(cyclic) is False
    assert canonical_path_contract_valid(nonserializable) is False


def test_contract_requires_every_explicit_null_key_and_receipt_body_hash() -> None:
    receipt = _sourced_target_result().to_dict()
    null_keys = [key for key, value in receipt.items() if value is None]

    assert null_keys
    for key in null_keys:
        forged = {name: value for name, value in receipt.items() if name != key}
        assert canonical_path_contract_valid(forged) is False, key
        assert canonical_path_return_eligible(forged) is False, key


@pytest.mark.parametrize(
    ("field", "alias_value"),
    (
        ("exit_price", 11),
        ("entry_bar_excluded", 0),
        ("post_entry_bar_count", True),
    ),
)
def test_contract_rejects_python_equality_type_aliases(
    field: str,
    alias_value: object,
) -> None:
    receipt = _sourced_target_result().to_dict()
    forged = {**receipt, field: alias_value}

    assert forged[field] == receipt[field]
    assert type(forged[field]) is not type(receipt[field])
    assert canonical_path_contract_valid(forged) is False


def test_path_store_rejects_missing_null_contract_key_before_initialize(
    tmp_path: Path,
) -> None:
    envelope = _store_envelope(_sourced_target_result())
    envelope.pop("stop_touched_at")
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="canonical path replay"):
        store.persist_path_replay(envelope)
    assert not (tmp_path / "replay.sqlite").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_conflict", 1),
        ("source_conflict", "false"),
        ("corporate_action_unresolved", [1]),
        ("source_coverage_complete", "true"),
        ("ordered_evidence_complete", 1),
        ("ordered_evidence_complete", "false"),
    ),
)
def test_public_boundary_rejects_nonboolean_truth_flags_with_valid_receipt(
    field: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "decision_at": START,
        "trigger": 10.5,
        "target": 11.0,
        "stop": 9.0,
        "session_close": START + timedelta(minutes=2),
        field: value,
    }

    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
        ],
        **kwargs,  # type: ignore[arg-type]
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


@pytest.mark.parametrize("trigger", (True, "10.5", "bad", object()))
def test_public_boundary_invalid_levels_return_self_valid_ineligible_receipt(
    trigger: object,
) -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=trigger,  # type: ignore[arg-type]
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    "bars",
    (
        None,
        1,
        "bars",
        {"observed_at": START},
        iter((_bar(0, open=10.6, high=11.1, low=10.2, close=11.0),)),
    ),
)
def test_public_boundary_rejects_nonmaterialized_bar_containers_without_raising(
    bars: object,
) -> None:
    result = resolve_path(
        bars,  # type: ignore[arg-type]
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("decision_at", "2026-11-27T14:30:00+00:00"),
        ("decision_at", datetime(2026, 11, 27, 14, 30)),
        ("session_close", datetime(2026, 11, 27, 14, 31)),
        ("session_close", 1),
        ("bar_interval", 60),
        ("bar_interval", "60"),
        ("halt_intervals", None),
        ("halt_intervals", ((START, "bad"),)),
    ),
)
def test_public_boundary_rejects_malformed_temporal_inputs_without_raising(
    field: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "decision_at": START,
        "trigger": 10.5,
        "target": 11.0,
        "stop": 9.0,
        "session_close": START + timedelta(minutes=1),
        field: value,
    }
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        **kwargs,  # type: ignore[arg-type]
    )

    expected = (
        PathTruthStatus.SOURCE_CONFLICT
        if field == "halt_intervals" and isinstance(value, tuple)
        else PathTruthStatus.DATA_INELIGIBLE
    )
    assert result.path_truth_status == expected
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    "ordered_events",
    (
        None,
        1,
        "events",
        {"price": 10.5},
        iter(({"observed_at": START, "event_type": "TRADE", "price": 10.6},)),
    ),
)
def test_unclaimed_ordered_inputs_are_cleared_without_inspection(
    ordered_events: object,
) -> None:
    baseline = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    )
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        ordered_events=ordered_events,  # type: ignore[arg-type]
        ordered_evidence_complete=False,
    )

    assert result.path_replay_id == baseline.path_replay_id
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    "ordered_events",
    (
        None,
        1,
        "events",
        {"observed_at": START, "event_type": "TRADE", "price": 10.6},
        iter(({"observed_at": START, "event_type": "TRADE", "price": 10.6},)),
    ),
)
def test_claimed_ordered_inputs_require_a_materialized_container(
    ordered_events: object,
) -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        ordered_events=ordered_events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="ordered-feed",
        ordered_evidence_hash_sha256="a" * 64,
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    "halt_intervals",
    (
        1,
        "halts",
        {"start": START, "end": START + timedelta(minutes=1)},
        ((START,),),
        ((START, START + timedelta(minutes=1), START + timedelta(minutes=2)),),
    ),
)
def test_public_boundary_rejects_malformed_halt_containers_without_raising(
    halt_intervals: object,
) -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        halt_intervals=halt_intervals,
    )

    assert result.path_truth_status in {
        PathTruthStatus.DATA_INELIGIBLE,
        PathTruthStatus.SOURCE_CONFLICT,
    }
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("ordered_evidence_identity", 123),
        ("ordered_evidence_identity", ""),
        ("ordered_evidence_hash_sha256", True),
        ("ordered_evidence_hash_sha256", "A" * 64),
        ("ordered_evidence_start", datetime(2026, 11, 27, 14, 30)),
        ("ordered_evidence_end", "2026-11-27T14:31:00+00:00"),
    ),
)
def test_claimed_ordered_metadata_requires_exact_types(
    field: str,
    value: object,
) -> None:
    events = (
        {"observed_at": START, "event_type": "TRADE", "price": 10.6},
    )
    kwargs: dict[str, object] = {
        "decision_at": START,
        "trigger": 10.5,
        "target": 11.0,
        "stop": 9.0,
        "session_close": START + timedelta(minutes=1),
        "ordered_events": events,
        "ordered_evidence_complete": True,
        "ordered_evidence_identity": "ordered-feed",
        "ordered_evidence_hash_sha256": _ordered_hash(events),
        "ordered_evidence_start": START,
        "ordered_evidence_end": START + timedelta(minutes=1),
        field: value,
    }
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        **kwargs,  # type: ignore[arg-type]
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    "event",
    (
        {"observed_at": START, "event_type": "TRADE", "price": True},
        {"observed_at": START, "event_type": "TRADE", "price": "10.6"},
        {"observed_at": START, "event_type": "QUOTE", "bid": True, "ask": 10.7},
        {"observed_at": START, "event_type": "QUOTE", "bid": 10.5, "ask": "10.7"},
    ),
)
def test_claimed_ordered_numeric_type_violations_return_self_valid_receipt(
    event: dict[str, object],
) -> None:
    events = (event,)
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="ordered-feed",
        ordered_evidence_hash_sha256=_ordered_hash(events),
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_artifact_identity", 123),
        ("source_artifact_identity", True),
        ("source_artifact_identity", ["source"]),
        ("source_artifact_hash_sha256", 123),
        ("source_artifact_hash_sha256", ["a" * 64]),
    ),
)
def test_public_boundary_rejects_coercion_only_source_identity(
    field: str,
    value: object,
) -> None:
    kwargs: dict[str, object] = {
        "decision_at": START,
        "trigger": 10.5,
        "target": 11.0,
        "stop": 9.0,
        "source_artifact_identity": "bars:NOVA:2026-11-27",
        "source_artifact_hash_sha256": "a" * 64,
        "source_coverage_complete": True,
        field: value,
    }
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        **kwargs,  # type: ignore[arg-type]
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("signal_id", 123),
        ("signal_id", ""),
        ("signal_id", object()),
        ("created_at", 123),
        ("created_at", "nonsense"),
        ("created_at", "2026-11-27T14:30:00"),
        ("created_at", []),
    ),
)
def test_path_store_rejects_invalid_optional_envelope_before_initialize(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    envelope = {**_store_envelope(_sourced_target_result()), field: value}
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="canonical path replay"):
        store.persist_path_replay(envelope)
    assert list(tmp_path.iterdir()) == []


def test_path_store_rejects_cyclic_optional_envelope_before_initialize(
    tmp_path: Path,
) -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    envelope = {**_store_envelope(_sourced_target_result()), "signal_id": cyclic}
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="canonical path replay"):
        store.persist_path_replay(envelope)
    assert list(tmp_path.iterdir()) == []


def test_manifest_round_trip_rejects_every_noncanonical_identity_alias() -> None:
    receipt = _sourced_target_result().to_dict()
    base = receipt["replay_input_manifest"]
    assert isinstance(base, dict)
    close = START + timedelta(minutes=2)
    offset = timezone(timedelta(hours=-5))
    mutations: list[dict[str, object]] = []

    invalid_close = copy.deepcopy(base)
    invalid_close["session_close"] = "not-a-time"
    mutations.append(invalid_close)
    naive_close = copy.deepcopy(base)
    naive_close["session_close"] = close.replace(tzinfo=None).isoformat()
    mutations.append(naive_close)
    offset_decision = copy.deepcopy(base)
    offset_decision["decision_at"] = START.astimezone(offset).isoformat()
    mutations.append(offset_decision)
    integer_alias = copy.deepcopy(base)
    integer_alias["bars"][1]["close"] = 11  # type: ignore[index]
    mutations.append(integer_alias)
    extra_key = copy.deepcopy(base)
    extra_key["bars"][0]["attacker"] = True  # type: ignore[index]
    mutations.append(extra_key)
    reversed_bars = copy.deepcopy(base)
    reversed_bars["bars"] = list(reversed(reversed_bars["bars"]))  # type: ignore[arg-type]
    mutations.append(reversed_bars)
    post_close = copy.deepcopy(base)
    post_close["bars"].append(  # type: ignore[union-attr]
        {
            "observed_at": close.isoformat(),
            "open": 99.0,
            "high": 100.0,
            "low": 98.0,
            "close": 99.0,
        }
    )
    mutations.append(post_close)
    offset_bar = copy.deepcopy(base)
    offset_bar["bars"][0]["observed_at"] = START.astimezone(offset).isoformat()  # type: ignore[index]
    mutations.append(offset_bar)
    future_halt = copy.deepcopy(base)
    future_halt["halt_intervals"].append(  # type: ignore[union-attr]
        [
            (close + timedelta(minutes=1)).isoformat(),
            (close + timedelta(minutes=2)).isoformat(),
        ]
    )
    mutations.append(future_halt)
    unclaimed_ordered = copy.deepcopy(base)
    unclaimed_ordered.update(
        {
            "ordered_feed_identity": "ignored-feed",
            "ordered_feed_hash_sha256": "a" * 64,
            "ordered_coverage_start": START.isoformat(),
            "ordered_coverage_end": close.isoformat(),
            "ordered_events": [{"attacker": True}],
        }
    )
    mutations.append(unclaimed_ordered)

    for manifest in mutations:
        with pytest.raises(ValueError, match="canonical normal form"):
            path_replay_module._resolve_manifest(manifest)  # type: ignore[attr-defined]


def test_input_contract_markers_cannot_be_removed_or_desynchronized() -> None:
    receipt = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=True,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
    ).to_dict()
    manifest = receipt["replay_input_manifest"]
    assert isinstance(manifest, dict)
    assert manifest["input_contract_markers"]
    assert manifest["input_contract_violations"]

    missing_violations = copy.deepcopy(manifest)
    missing_violations["input_contract_violations"] = []
    with pytest.raises(ValueError, match="shape|normal form|markers"):
        path_replay_module._resolve_manifest(missing_violations)  # type: ignore[attr-defined]
    missing_markers = copy.deepcopy(manifest)
    missing_markers["input_contract_markers"] = []
    with pytest.raises(ValueError, match="shape|normal form|markers"):
        path_replay_module._resolve_manifest(missing_markers)  # type: ignore[attr-defined]
    missing_both = copy.deepcopy(manifest)
    missing_both["input_contract_markers"] = []
    missing_both["input_contract_violations"] = []
    with pytest.raises(ValueError, match="shape|normal form|markers"):
        path_replay_module._resolve_manifest(missing_both)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mutation",
    (
        {"bar_interval": timedelta(seconds=30)},
        {"source_conflict": 0},
        {"corporate_action_unresolved": "false"},
        {"ordered_evidence_complete": 1},
    ),
)
def test_paired_marker_list_deletion_cannot_erase_typed_invalid_sentinel(
    mutation: dict[str, object],
) -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        source_artifact_identity="bars:NOVA:2026-11-27",
        source_artifact_hash_sha256="a" * 64,
        source_coverage_complete=True,
        **mutation,
    )
    manifest = copy.deepcopy(result.to_dict()["replay_input_manifest"])
    assert isinstance(manifest, dict)
    assert manifest["input_contract_markers"]
    manifest["input_contract_markers"] = []
    manifest["input_contract_violations"] = []

    with pytest.raises(ValueError, match="shape|normal form|markers"):
        path_replay_module._resolve_manifest(manifest)  # type: ignore[attr-defined]


@pytest.mark.parametrize("field", ("code", "category"))
def test_contract_rejects_arbitrary_marker_code_or_category_mutation(
    field: str,
) -> None:
    manifest = copy.deepcopy(
        resolve_path(
            [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
            decision_at=START,
            trigger=True,
            target=11.0,
            stop=9.0,
        ).to_dict()["replay_input_manifest"]
    )
    assert isinstance(manifest, dict)
    markers = manifest["input_contract_markers"]
    assert isinstance(markers, list) and isinstance(markers[0], dict)
    markers[0][field] = "attacker"

    with pytest.raises(ValueError, match="shape|normal form|markers"):
        path_replay_module._resolve_manifest(manifest)  # type: ignore[attr-defined]


def test_huge_complete_ordered_numeric_returns_self_valid_conflict_receipt() -> None:
    events = (
        {
            "observed_at": START,
            "event_type": "TRADE",
            "price": 10**10_000,
        },
    )
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        ordered_events=events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trades",
        ordered_evidence_hash_sha256="a" * 64,
        ordered_evidence_start=START,
        ordered_evidence_end=START + timedelta(minutes=1),
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


def test_return_eligibility_requires_a_canonical_bounded_session_close() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        source_artifact_identity="bars:NOVA:2026-11-27",
        source_artifact_hash_sha256="a" * 64,
        source_coverage_complete=True,
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


def test_out_of_scope_rows_are_dropped_before_payload_validation() -> None:
    close = START + timedelta(minutes=2)
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=10.9, low=10.4, close=10.7),
    ]
    baseline = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.5,
        stop=9.0,
        session_close=close,
    )
    bar_extra = resolve_path(
        [
            *bars,
            {
                "observed_at": close,
                "open": "bad",
                "high": True,
                "low": object(),
                "close": "bad",
            },
        ],
        decision_at=START,
        trigger=10.5,
        target=11.5,
        stop=9.0,
        session_close=close,
    )
    base_events = (
        {"observed_at": START, "event_type": "TRADE", "price": 10.6},
        {
            "observed_at": START + timedelta(minutes=1),
            "event_type": "TRADE",
            "price": 10.7,
        },
    )
    extra_events = (
        *base_events,
        {
            "observed_at": close,
            "event_type": "TRADE",
            "price": "bad",
        },
    )
    ordered_baseline = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.5,
        stop=9.0,
        session_close=close,
        ordered_events=base_events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trades",
        ordered_evidence_hash_sha256=_ordered_hash(base_events),
        ordered_evidence_start=START,
        ordered_evidence_end=close,
    )
    ordered_extra = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.5,
        stop=9.0,
        session_close=close,
        ordered_events=extra_events,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trades",
        ordered_evidence_hash_sha256=_ordered_hash(extra_events),
        ordered_evidence_start=START,
        ordered_evidence_end=close,
    )

    assert bar_extra.path_replay_id == baseline.path_replay_id
    assert ordered_extra.path_replay_id == ordered_baseline.path_replay_id
    assert canonical_path_contract_valid(bar_extra.to_dict()) is True
    assert canonical_path_contract_valid(ordered_extra.to_dict()) is True


@pytest.mark.parametrize(
    ("source_conflict", "corporate_action", "expected"),
    (
        (True, False, PathTruthStatus.SOURCE_CONFLICT),
        (False, True, PathTruthStatus.CORPORATE_ACTION_UNRESOLVED),
        (True, True, PathTruthStatus.SOURCE_CONFLICT),
    ),
)
def test_known_source_truth_has_priority_over_generic_parameter_violations(
    source_conflict: bool,
    corporate_action: bool,
    expected: PathTruthStatus,
) -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger="bad",
        target=11.0,
        stop=9.0,
        source_conflict=source_conflict,
        corporate_action_unresolved=corporate_action,
    )

    assert result.path_truth_status == expected
    assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize("payload", (None, [], "receipt", 1))
def test_contract_validator_is_total_for_non_mapping_payloads(payload: object) -> None:
    assert canonical_path_contract_valid(payload) is False
    assert canonical_path_return_eligible(payload) is False


def test_extreme_boundary_inputs_return_self_valid_ineligible_receipts() -> None:
    huge_level = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10**10_000,
        target=11.0,
        stop=9.0,
    )
    extreme_datetime = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        trigger=10.5,
        target=11.0,
        stop=9.0,
    )

    for result in (huge_level, extreme_datetime):
        assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
        assert canonical_path_contract_valid(result.to_dict()) is True


@pytest.mark.parametrize(
    "extreme",
    (
        datetime.max.replace(tzinfo=UTC),
        (datetime.max - timedelta(seconds=30)).replace(tzinfo=UTC),
    ),
)
def test_extreme_aware_utc_timestamps_never_escape_interval_arithmetic(
    extreme: datetime,
) -> None:
    decision_result = resolve_path(
        [
            {
                "observed_at": extreme,
                "open": 10.6,
                "high": 11.1,
                "low": 10.2,
                "close": 11.0,
            }
        ],
        decision_at=extreme,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=None,
    )
    bar_result = resolve_path(
        [
            {
                "observed_at": extreme,
                "open": 10.6,
                "high": 11.1,
                "low": 10.2,
                "close": 11.0,
            }
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=None,
    )

    for result in (decision_result, bar_result):
        assert result.path_truth_status in {
            PathTruthStatus.DATA_INELIGIBLE,
            PathTruthStatus.SOURCE_CONFLICT,
        }
        assert canonical_path_contract_valid(result.to_dict()) is True
        assert canonical_path_return_eligible(result.to_dict()) is False


def test_replay_binding_is_hash_authenticated_and_origin_specific() -> None:
    first_binding = _paper_replay_binding(
        selection_id="selection-1",
        scan_id="scan-1",
        signal_id="signal-1",
    )
    second_binding = _paper_replay_binding(
        selection_id="selection-2",
        scan_id="scan-2",
        signal_id="signal-2",
    )
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    evidence = _future_evidence_receipt(bars)
    kwargs: dict[str, object] = {
        "decision_at": START,
        "trigger": 10.5,
        "target": 11.0,
        "stop": 9.0,
        "session_close": START + timedelta(minutes=2),
        "source_artifact_identity": evidence["receipt_id"],
        "source_artifact_hash_sha256": evidence["receipt_hash_sha256"],
        "source_coverage_complete": True,
        "future_evidence_receipt": evidence,
    }
    first = resolve_path(bars, replay_binding=first_binding, **kwargs)
    same = resolve_path(bars, replay_binding=first_binding, **kwargs)
    second = resolve_path(bars, replay_binding=second_binding, **kwargs)
    unbound = resolve_path(bars, **kwargs)

    assert first.to_dict()["replay_input_manifest"]["replay_binding"] == first_binding
    assert first.to_dict()["replay_input_manifest"]["future_evidence_receipt"] == evidence
    assert first.path_replay_id == same.path_replay_id
    assert first.path_replay_id != second.path_replay_id
    assert first.path_replay_id != unbound.path_replay_id
    assert unbound.to_dict()["replay_input_manifest"]["replay_binding"] is None
    for result in (first, second, unbound):
        assert canonical_path_contract_valid(result.to_dict()) is True


def test_future_evidence_receipt_binds_subject_raw_bars_and_top_source() -> None:
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    evidence = _future_evidence_receipt(bars)
    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=_paper_replay_binding(),
        future_evidence_receipt=evidence,
    )
    payload = result.to_dict()

    assert payload["replay_input_manifest"]["future_evidence_receipt"] == evidence
    assert payload["source_artifact_identity"] == evidence["receipt_id"]
    assert payload["source_artifact_hash_sha256"] == evidence["receipt_hash_sha256"]
    assert canonical_path_contract_valid(payload) is True
    assert canonical_path_return_eligible(payload) is True


@pytest.mark.parametrize(
    ("path", "value"),
    (
        (("schema_version",), "attacker.v1"),
        (("subject", "symbol"), "ATTK"),
        (("subject", "market_date"), "2026-11-28"),
        (("raw_artifact_identity",), ""),
        (("raw_bar_hash_sha256",), "b" * 64),
        (("bar_count",), 999),
        (("bar_count",), True),
        (("first_bar_at",), "2026-11-27T14:31:00+00:00"),
        (("last_bar_at",), "2026-11-27T14:30:00+00:00"),
        (("coverage_start",), "2026-11-27T14:29:00+00:00"),
        (("coverage_end",), "2026-11-27T14:31:00+00:00"),
        (("coverage_complete",), 1),
        (("receipt_id",), "future-evidence-v1-" + "f" * 64),
        (("receipt_hash_sha256",), "f" * 64),
    ),
)
def test_future_evidence_receipt_rejects_exact_field_tampering(
    path: tuple[str, ...],
    value: object,
) -> None:
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    evidence = _future_evidence_receipt(bars)
    mutated = copy.deepcopy(evidence)
    cursor = mutated
    for key in path[:-1]:
        child = cursor[key]
        assert isinstance(child, dict)
        cursor = child
    cursor[path[-1]] = value
    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=_paper_replay_binding(),
        future_evidence_receipt=mutated,
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


@pytest.mark.parametrize("mutation", ("missing", "extra"))
def test_future_evidence_receipt_requires_exact_keys(mutation: str) -> None:
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    evidence = _future_evidence_receipt(bars)
    if mutation == "missing":
        evidence.pop("first_bar_at")
    else:
        evidence["extra"] = True
    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity="future-evidence-v1-" + "a" * 64,
        source_artifact_hash_sha256="a" * 64,
        source_coverage_complete=True,
        replay_binding=_paper_replay_binding(),
        future_evidence_receipt=evidence,
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


def test_co_mutated_subject_with_retained_trusted_receipt_digest_is_rejected() -> None:
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    trusted = _future_evidence_receipt(bars)
    attacker = _future_evidence_receipt(
        bars,
        symbol="ATTK",
        raw_artifact_identity="provider-bars:NOVA:2026-11-27",
    )
    binding = copy.deepcopy(_paper_replay_binding())
    binding["subject"] = {"symbol": "ATTK", "market_date": "2026-11-27"}
    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity=trusted["receipt_id"],
        source_artifact_hash_sha256=trusted["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=attacker,
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


@pytest.mark.parametrize(
    "replay_binding",
    (
        {},
        {"schema_version": "attacker"},
        {
            **_paper_replay_binding(),
            "subject": {"symbol": "nova", "market_date": "20261127"},
        },
        {
            **_paper_replay_binding(),
            "origin": {
                **_paper_replay_binding()["origin"],
                "context_hash_sha256": "not-a-hash",
            },
        },
    ),
)
def test_malformed_replay_binding_returns_self_valid_ineligible_receipt(
    replay_binding: object,
) -> None:
    result = resolve_path(
        [_bar(0, open=10.6, high=11.1, low=10.2, close=11.0)],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        replay_binding=replay_binding,
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("selection_id", "selection-attacker"),
        ("signal_id", "signal-attacker"),
        ("market_date", "2026-11-28"),
    ),
)
def test_path_store_cross_checks_paper_envelope_against_replay_binding_before_init(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    envelope = {**_store_envelope(_sourced_target_result()), field: value}
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite",
        evidence_root=tmp_path / "evidence",
    )

    with pytest.raises(EvidenceStoreError, match="replay binding"):
        store.persist_path_replay(envelope)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("binding", (None, _v6_replay_binding()))
def test_path_store_requires_paper_selection_binding_before_initialize(
    tmp_path: Path,
    binding: dict[str, object] | None,
) -> None:
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    evidence = _future_evidence_receipt(bars)
    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=evidence,
    )
    envelope = _store_envelope(result)
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite",
        evidence_root=tmp_path / "evidence",
    )

    with pytest.raises(EvidenceStoreError, match="paper selection replay binding"):
        store.persist_path_replay(envelope)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ("open", "high", "low", "close"))
@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_nonfinite_in_scope_ohlc_is_explicit_sentinel_backed_source_conflict(
    field: str,
    value: float,
) -> None:
    broken = _bar(1, open=10.6, high=10.9, low=10.3, close=10.7)
    broken[field] = value
    result = resolve_path(
        [_bar(0, open=10.6, high=10.8, low=10.2, close=10.6), broken],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity="bars:NOVA:2026-11-27",
        source_artifact_hash_sha256="a" * 64,
        source_coverage_complete=True,
    )
    manifest = result.to_dict()["replay_input_manifest"]
    assert isinstance(manifest, dict)

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert manifest["input_contract_markers"]
    assert manifest["input_contract_violations"]
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


def test_nonfinite_bar_after_earlier_exit_but_before_close_still_blocks_truth() -> None:
    result = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
            _bar(2, open=11.0, high=float("nan"), low=10.8, close=10.9),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=3),
        source_artifact_identity="bars:NOVA:2026-11-27",
        source_artifact_hash_sha256="a" * 64,
        source_coverage_complete=True,
    )

    assert result.path_truth_status == PathTruthStatus.SOURCE_CONFLICT
    assert canonical_path_contract_valid(result.to_dict()) is True
    assert canonical_path_return_eligible(result.to_dict()) is False


def test_nonfinite_post_close_bar_is_causally_scoped_out() -> None:
    baseline = _sourced_target_result()
    baseline_payload = baseline.to_dict()
    evidence = baseline_payload["replay_input_manifest"]["future_evidence_receipt"]
    extended = resolve_path(
        [
            _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
            _bar(2, open=float("nan"), high=float("inf"), low=0.0, close=0.0),
        ],
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity=baseline.source_artifact_identity,
        source_artifact_hash_sha256=baseline.source_artifact_hash_sha256,
        source_coverage_complete=True,
        replay_binding=_paper_replay_binding(),
        future_evidence_receipt=evidence,
    )

    assert extended.path_replay_id == baseline.path_replay_id
    assert extended.to_dict() == baseline.to_dict()


@pytest.mark.parametrize("market_date", ("20261127", "2026-W48-5", "2026-11-28"))
def test_path_store_rejects_noncanonical_or_unbound_market_date_before_initialize(
    tmp_path: Path,
    market_date: str,
) -> None:
    envelope = {**_store_envelope(_sourced_target_result()), "market_date": market_date}
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="market_date"):
        store.persist_path_replay(envelope)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("cohort", " official_outcome_required"),
        ("cohort", "official_outcome_required "),
        ("selection_id", " selection-1"),
        ("selection_id", "selection-1 "),
        ("signal_id", " signal-1"),
        ("signal_id", "signal-1 "),
    ),
)
def test_path_store_rejects_whitespace_aliased_ids_before_initialize(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    envelope = {**_store_envelope(_sourced_target_result()), field: value}
    store = IntradayEvidenceStore(
        tmp_path / "replay.sqlite", evidence_root=tmp_path / "evidence"
    )

    with pytest.raises(EvidenceStoreError, match="canonical path replay"):
        store.persist_path_replay(envelope)
    assert list(tmp_path.iterdir()) == []


def test_seeded_entry_starts_at_authenticated_decision_boundary() -> None:
    binding = _paper_enter_replay_binding()
    bars = [
        _bar(0, open=10.1, high=10.2, low=10.0, close=10.1),
        _bar(1, open=10.1, high=11.1, low=10.0, close=11.0),
    ]
    evidence = _future_evidence_receipt(bars)
    entry = _entry_receipt(replay_binding=binding)

    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.0,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=evidence,
        entry_mode="ALREADY_ENTERED_AT_DECISION",
        entry_receipt=entry,
    )

    payload = result.to_dict()
    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert result.entry_time == START
    assert result.entry_price == 10.05
    assert result.exit_time == START + timedelta(minutes=1)
    assert payload["replay_input_manifest"]["entry_mode"] == (
        "ALREADY_ENTERED_AT_DECISION"
    )
    assert payload["replay_input_manifest"]["entry_receipt"] == entry
    assert canonical_path_contract_valid(payload)
    assert canonical_path_return_eligible(payload)


def test_seeded_entry_excludes_all_predecision_terminal_and_excursion_evidence() -> None:
    binding = _paper_enter_replay_binding()
    scoped_bars = [
        _bar(0, open=10.1, high=10.2, low=10.0, close=10.1),
        _bar(1, open=10.1, high=11.1, low=10.0, close=11.0),
    ]
    predecision = _bar(-1, open=10.05, high=50.0, low=1.0, close=25.0)
    evidence = _future_evidence_receipt(scoped_bars)
    kwargs = {
        "decision_at": START,
        "trigger": 10.0,
        "target": 11.0,
        "stop": 9.0,
        "session_close": START + timedelta(minutes=2),
        "source_artifact_identity": evidence["receipt_id"],
        "source_artifact_hash_sha256": evidence["receipt_hash_sha256"],
        "source_coverage_complete": True,
        "replay_binding": binding,
        "future_evidence_receipt": evidence,
        "entry_mode": "ALREADY_ENTERED_AT_DECISION",
        "entry_receipt": _entry_receipt(replay_binding=binding),
    }

    baseline = resolve_path(scoped_bars, **kwargs)
    attacked = resolve_path([predecision, *scoped_bars], **kwargs)

    assert attacked.to_dict() == baseline.to_dict()
    assert attacked.bounds["mfe_upper"] < 50.0
    assert attacked.bounds["mae_lower"] > 1.0


def test_seeded_partial_entry_interval_censors_before_later_target() -> None:
    effective_at = START + timedelta(seconds=30)
    replay_at = START + timedelta(minutes=1)
    binding = _paper_enter_replay_binding()
    binding["subject"] = {
        "symbol": "NOVA",
        "market_date": "2026-11-27",
    }
    bars = [
        {
            **_bar(1, open=10.1, high=10.2, low=10.0, close=10.1),
            "observed_at": replay_at,
        },
        {
            **_bar(2, open=10.1, high=11.1, low=10.0, close=11.0),
            "observed_at": replay_at + timedelta(minutes=1),
        },
    ]
    evidence = _future_evidence_receipt(bars)

    result = resolve_path(
        bars,
        decision_at=replay_at,
        trigger=10.0,
        target=11.0,
        stop=9.0,
        session_close=replay_at + timedelta(minutes=2),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=evidence,
        entry_mode="ALREADY_ENTERED_AT_DECISION",
        entry_receipt=_entry_receipt(
            effective_at=effective_at,
            completed_at=effective_at,
            replay_binding=binding,
        ),
    )

    assert result.path_truth_status == PathTruthStatus.MISSING_INTERVAL_CENSORED
    assert result.to_dict()["path_event"] == "LIQUIDITY_FAILURE"
    assert result.entry_time == effective_at
    assert result.exit_time is None
    assert result.event_interval_start == effective_at
    assert result.event_interval_end == replay_at
    assert canonical_path_contract_valid(result.to_dict())
    assert not canonical_path_return_eligible(result.to_dict())


def test_seeded_partial_entry_interval_can_continue_with_exact_ordered_remainder() -> None:
    effective_at = START + timedelta(seconds=30)
    replay_at = START + timedelta(minutes=1)
    binding = _paper_enter_replay_binding()
    bars = [
        {
            **_bar(1, open=10.1, high=10.2, low=10.0, close=10.1),
            "observed_at": replay_at,
        },
        {
            **_bar(2, open=10.1, high=11.1, low=10.0, close=11.0),
            "observed_at": replay_at + timedelta(minutes=1),
        },
    ]
    evidence = _future_evidence_receipt(bars)
    ordered = (
        {
            "observed_at": effective_at,
            "event_type": "TRADE",
            "price": 10.05,
        },
        {
            "observed_at": effective_at + timedelta(seconds=15),
            "event_type": "TRADE",
            "price": 10.1,
        },
    )

    result = resolve_path(
        bars,
        decision_at=replay_at,
        trigger=10.0,
        target=11.0,
        stop=9.0,
        session_close=replay_at + timedelta(minutes=2),
        ordered_events=ordered,
        ordered_evidence_complete=True,
        ordered_evidence_identity="trades:NOVA:entry-remainder",
        ordered_evidence_hash_sha256=_ordered_hash(ordered),
        ordered_evidence_start=effective_at,
        ordered_evidence_end=replay_at,
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=evidence,
        entry_mode="ALREADY_ENTERED_AT_DECISION",
        entry_receipt=_entry_receipt(
            effective_at=effective_at,
            completed_at=effective_at,
            replay_binding=binding,
        ),
    )

    assert result.path_truth_status == PathTruthStatus.RESOLVED_TARGET_FIRST
    assert result.entry_time == effective_at
    assert result.exit_time == replay_at + timedelta(minutes=1)
    assert canonical_path_contract_valid(result.to_dict())
    assert canonical_path_return_eligible(result.to_dict())


def test_seeded_partial_entry_interval_maps_sourced_halt_to_halt_censor() -> None:
    effective_at = START + timedelta(seconds=30)
    replay_at = START + timedelta(minutes=1)
    binding = _paper_enter_replay_binding()
    bars = [
        {
            **_bar(1, open=10.1, high=10.2, low=10.0, close=10.1),
            "observed_at": replay_at,
        }
    ]
    evidence = _future_evidence_receipt(bars)
    result = resolve_path(
        bars,
        decision_at=replay_at,
        trigger=10.0,
        target=11.0,
        stop=9.0,
        halt_intervals=((effective_at, replay_at),),
        session_close=replay_at + timedelta(minutes=1),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=evidence,
        entry_mode="ALREADY_ENTERED_AT_DECISION",
        entry_receipt=_entry_receipt(
            effective_at=effective_at,
            completed_at=effective_at,
            replay_binding=binding,
        ),
    )

    assert result.path_truth_status == PathTruthStatus.HALT_CENSORED
    assert result.to_dict()["path_event"] == "HALT"
    assert result.event_interval_start == effective_at
    assert result.event_interval_end == replay_at
    assert canonical_path_contract_valid(result.to_dict())


@pytest.mark.parametrize(
    "mutation",
    (
        "missing",
        "mode",
        "price",
        "effective_before",
        "effective_after",
        "observation_id",
        "source_hash",
        "observed_after_completed",
        "completed_after_decision",
        "origin",
        "receipt_id",
        "receipt_hash",
        "extra_key",
    ),
)
def test_seeded_entry_receipt_one_fact_mutations_fail_closed(mutation: str) -> None:
    binding = _paper_enter_replay_binding()
    bars = [_bar(0, open=10.1, high=10.2, low=10.0, close=10.1)]
    evidence = _future_evidence_receipt(bars)
    entry = _entry_receipt(replay_binding=binding)
    if mutation == "missing":
        entry.pop("source_observation_id")
    elif mutation == "mode":
        entry["entry_mode"] = "DISCOVER_TRIGGER"
    elif mutation == "price":
        entry["raw_entry_price"] = 999.0
    elif mutation == "effective_before":
        entry["effective_at"] = (START - timedelta(minutes=1)).isoformat()
    elif mutation == "effective_after":
        entry["effective_at"] = (START + timedelta(seconds=1)).isoformat()
    elif mutation == "observation_id":
        entry["source_observation_id"] = "forged-observation"
    elif mutation == "source_hash":
        entry["source_bar_hash_sha256"] = "a" * 64
    elif mutation == "observed_after_completed":
        entry["source_observed_at"] = (START + timedelta(seconds=1)).isoformat()
    elif mutation == "completed_after_decision":
        entry["source_bar_completed_at"] = (START + timedelta(seconds=1)).isoformat()
    elif mutation == "origin":
        entry["replay_origin"] = copy.deepcopy(entry["replay_origin"])
        entry["replay_origin"]["id"] = "other-intent"
    elif mutation == "receipt_id":
        entry["receipt_id"] = f"path-entry-v1-{'a' * 64}"
    elif mutation == "receipt_hash":
        entry["receipt_hash_sha256"] = "a" * 64
    elif mutation == "extra_key":
        entry["attacker"] = True
    else:  # pragma: no cover - parameter exhaustiveness
        raise AssertionError(mutation)

    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.0,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=1),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=evidence,
        entry_mode="ALREADY_ENTERED_AT_DECISION",
        entry_receipt=entry,
    )

    assert result.path_truth_status == PathTruthStatus.DATA_INELIGIBLE
    assert canonical_path_contract_valid(result.to_dict())
    assert not canonical_path_return_eligible(result.to_dict())


@pytest.mark.parametrize(
    ("bars", "halts", "close_minutes", "expected_status", "expected_event"),
    (
        (
            [_bar(0, open=10.1, high=11.1, low=8.9, close=10.0)],
            (),
            1,
            PathTruthStatus.TARGET_STOP_INTERVAL_CENSORED,
            "SAME_INTERVAL_CENSORED",
        ),
        (
            [
                _bar(0, open=10.1, high=10.2, low=10.0, close=10.1),
                _bar(2, open=10.1, high=11.1, low=10.0, close=11.0),
            ],
            (),
            3,
            PathTruthStatus.MISSING_INTERVAL_CENSORED,
            "LIQUIDITY_FAILURE",
        ),
        (
            [
                _bar(0, open=10.1, high=10.2, low=10.0, close=10.1),
                _bar(2, open=10.1, high=11.1, low=10.0, close=11.0),
            ],
            ((START + timedelta(minutes=1), START + timedelta(minutes=2)),),
            3,
            PathTruthStatus.HALT_CENSORED,
            "HALT",
        ),
        (
            [
                _bar(0, open=10.1, high=10.2, low=10.0, close=10.1),
                _bar(1, open=10.1, high=10.3, low=10.0, close=10.2),
            ],
            (),
            2,
            PathTruthStatus.RIGHT_CENSORED_SESSION_CLOSE,
            "TIMEOUT",
        ),
    ),
)
def test_seeded_entry_reuses_canonical_lifecycle_semantics(
    bars: list[dict[str, object]],
    halts: tuple[tuple[datetime, datetime], ...],
    close_minutes: int,
    expected_status: PathTruthStatus,
    expected_event: str,
) -> None:
    binding = _paper_enter_replay_binding()
    evidence = _future_evidence_receipt(bars)
    result = resolve_path(
        bars,
        decision_at=START,
        trigger=10.0,
        target=11.0,
        stop=9.0,
        halt_intervals=halts,
        session_close=START + timedelta(minutes=close_minutes),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=binding,
        future_evidence_receipt=evidence,
        entry_mode="ALREADY_ENTERED_AT_DECISION",
        entry_receipt=_entry_receipt(replay_binding=binding),
    )

    assert result.path_truth_status == expected_status
    assert result.to_dict()["path_event"] == expected_event
    assert result.entry_time == START
    assert result.entry_price == 10.05


def _ordered_hash(events: tuple[dict[str, object], ...]) -> str:
    canonical = [
        {
            key: (
                value.astimezone(UTC).isoformat()
                if isinstance(value, datetime) and value.tzinfo is not None
                else str(value).upper()
                if key == "event_type"
                else value
            )
            for key, value in sorted(event.items())
        }
        for event in events
    ]
    canonical.sort(key=lambda row: (str(row.get("observed_at")), json.dumps(row, sort_keys=True)))
    encoded = json.dumps(canonical, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _sourced_target_result() -> PathReplayResult:
    bars = [
        _bar(0, open=10.6, high=10.8, low=10.2, close=10.6),
        _bar(1, open=10.6, high=11.1, low=10.4, close=11.0),
    ]
    evidence = _future_evidence_receipt(bars)
    return resolve_path(
        bars,
        decision_at=START,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        session_close=START + timedelta(minutes=2),
        source_artifact_identity=evidence["receipt_id"],
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=_paper_replay_binding(),
        future_evidence_receipt=evidence,
    )


def _store_envelope(result: PathReplayResult) -> dict[str, object]:
    return {
        **result.to_dict(),
        "cohort": "official_outcome_required",
        "selection_id": "selection-1",
        "signal_id": "signal-1",
        "market_date": "2026-11-27",
        "artifact_identity": result.source_artifact_identity,
        "artifact_hash_sha256": result.source_artifact_hash_sha256,
        "retrospective_research_eligible": False,
        "prospective_promotion_eligible": False,
    }


def _sha_json(payload: object) -> str:
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt_hash(payload: dict[str, object]) -> str:
    body = {
        key: value
        for key, value in payload.items()
        if key != "replay_receipt_hash_sha256"
    }
    encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _is_sha(value: object) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text)
