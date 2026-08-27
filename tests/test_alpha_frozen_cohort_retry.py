from __future__ import annotations

from pathlib import Path

import pytest

from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.notifiers import NotificationEvent
from intraday_scanner.services.alpha_cycle_service import (
    _govern_frozen_official_cohort_retry,
    _persist_notification_delivery_memberships,
    _persist_official_selections,
    _persist_research_radar_selections,
)
from intraday_scanner.services.luna_research_slate_service import (
    build_ranked_research_slate,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

SELECTED_AT = "2026-08-26T13:00:00+00:00"


def _event(scan_id: str, body: str = "OFFICIAL PAPER CANDIDATES\n1. AAA") -> NotificationEvent:
    return NotificationEvent(
        event_key=f"alphaops:{scan_id}:alpha_morning_watch",
        title="Dawnstrike Alpha Watch",
        body=body,
        channel_hint="alpha_morning_watch",
        payload={"run_id": scan_id, "signals": []},
    )


def _signal(signal_id: str = "signal-stable", ticker: str = "AAA") -> dict[str, object]:
    return {
        "signal_id": signal_id,
        "signal_key": signal_id,
        "scan_id": "scan-source",
        "ticker": ticker,
        "rank": 1,
        "market_date": "2026-08-26",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_dispatch_failure_retry_reuses_exact_frozen_event_and_members(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-retry.sqlite")
    original_event = _event("scan-original")
    signal = _signal()
    original_rows, original_stats = _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[signal],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=original_event,
    )
    assert original_stats["official_cohort_claimed"] is True

    # This is the persisted state left by the cycle's dispatch-exception path.
    failed = _persist_notification_delivery_memberships(
        store,
        selections=original_rows,
        events=[original_event],
        notify="telegram",
        preexisting_notification_keys=set(),
    )
    assert failed[0]["delivery_status"] == "failed"

    retry_event = _event("scan-retry")
    governed = _govern_frozen_official_cohort_retry(
        store,
        scan_id="scan-retry",
        selected_signals=[signal],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at="2026-08-26T13:05:00+00:00",
        event=retry_event,
    )
    assert governed is not None
    assert governed["scan_id"] == "scan-original"
    assert governed["event"].event_key == original_event.event_key
    assert governed["event"].body == original_event.body
    assert governed["selections"] == original_rows
    assert governed["stats"]["official_cohort_reused"] is True
    assert governed["event"].payload["producer_attempt_scan_id"] == "scan-retry"

    notification_key = f"{original_event.event_key}:telegram"
    store.record_notification(
        event_key=notification_key,
        channel="telegram",
        run_id="scan-original",
        payload={
            "title": original_event.title,
            "body": original_event.body,
            "channel_hint": original_event.channel_hint,
        },
    )
    delivered = _persist_notification_delivery_memberships(
        store,
        selections=list(governed["selections"]),
        events=[governed["event"]],
        notify="telegram",
        preexisting_notification_keys=set(),
    )
    assert delivered[0]["delivery_status"] == "delivered"
    assert len(store.load_signal_selections(cohort="official_telegram")) == 1
    assert len(store.load_notification_deliveries(cohort="official_telegram")) == 1


def test_no_trade_retry_reuses_original_sentinel_identity(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-no-trade.sqlite")
    original_event = NotificationEvent(
        event_key="alphaops:source-failure:alpha_no_trade",
        title="Dawnstrike Alpha Check",
        body="No clean edge today.",
        channel_hint="alpha_no_trade",
    )
    original = _signal("no-trade-original", "NO_TRADE")
    _persist_official_selections(
        store,
        scan_id="source-failure",
        selected_signals=[original],
        decision={"no_trade": True, "decision_tier": "no_trade"},
        selected_at=SELECTED_AT,
        event=original_event,
    )

    governed = _govern_frozen_official_cohort_retry(
        store,
        scan_id="source-failure-retry",
        selected_signals=[_signal("no-trade-new-attempt", "NO_TRADE")],
        decision={"no_trade": True, "decision_tier": "no_trade"},
        selected_at="2026-08-26T13:05:00+00:00",
        event=NotificationEvent(
            event_key="alphaops:source-failure-retry:alpha_no_trade",
            title=original_event.title,
            body=original_event.body,
            channel_hint=original_event.channel_hint,
        ),
    )

    assert governed is not None
    assert governed["event"].event_key == original_event.event_key
    assert governed["selections"][0]["signal_id"] == "no-trade-original"


@pytest.mark.parametrize(
    ("selected_signals", "body", "match"),
    [
        ([_signal(signal_id="different")], "OFFICIAL PAPER CANDIDATES\n1. AAA", "members"),
        ([_signal()], "OFFICIAL PAPER CANDIDATES\n1. CHANGED", "rendered body"),
    ],
)
def test_frozen_retry_rejects_membership_or_body_substitution(
    tmp_path: Path,
    selected_signals: list[dict[str, object]],
    body: str,
    match: str,
) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-conflict.sqlite")
    _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[_signal()],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=_event("scan-original"),
    )

    with pytest.raises(SnapshotValidationError, match=f"FROZEN_COHORT_CONFLICT.*{match}"):
        _govern_frozen_official_cohort_retry(
            store,
            scan_id="scan-retry",
            selected_signals=selected_signals,
            decision={"no_trade": False, "decision_tier": "clean_edge"},
            selected_at="2026-08-26T13:05:00+00:00",
            event=_event("scan-retry", body),
        )


@pytest.mark.parametrize(
    ("title", "channel_hint", "match"),
    [
        ("Changed title", "alpha_morning_watch", "immutable notification manifest"),
        ("Dawnstrike Alpha Watch", "changed_channel", "immutable notification manifest"),
    ],
)
def test_frozen_retry_rejects_title_or_channel_substitution(
    tmp_path: Path,
    title: str,
    channel_hint: str,
    match: str,
) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-manifest-conflict.sqlite")
    _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[_signal()],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=_event("scan-original"),
    )

    with pytest.raises(SnapshotValidationError, match=f"FROZEN_COHORT_CONFLICT.*{match}"):
        _govern_frozen_official_cohort_retry(
            store,
            scan_id="scan-retry",
            selected_signals=[_signal()],
            decision={"no_trade": False, "decision_tier": "clean_edge"},
            selected_at="2026-08-26T13:05:00+00:00",
            event=NotificationEvent(
                event_key="alphaops:scan-retry:alpha_morning_watch",
                title=title,
                body="OFFICIAL PAPER CANDIDATES\n1. AAA",
                channel_hint=channel_hint,
            ),
        )


def test_frozen_retry_rejects_source_scan_substitution(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-source-conflict.sqlite")
    original = _signal()
    original["scan_id"] = "frozen-source"
    _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[original],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=_event("scan-original"),
    )

    with pytest.raises(SnapshotValidationError, match="FROZEN_COHORT_CONFLICT.*source scan"):
        _govern_frozen_official_cohort_retry(
            store,
            scan_id="scan-retry",
            selected_signals=[{**_signal(), "scan_id": "replacement-source"}],
            decision={"no_trade": False, "decision_tier": "clean_edge"},
            selected_at="2026-08-26T13:05:00+00:00",
            event=_event("scan-retry"),
        )


def test_frozen_retry_rejects_missing_source_scan_identity(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-source-missing.sqlite")
    original = _signal()
    original["scan_id"] = "frozen-source"
    _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[original],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=_event("scan-original"),
    )
    retry_signal = _signal()
    retry_signal.pop("scan_id")

    with pytest.raises(SnapshotValidationError, match="FROZEN_COHORT_CONFLICT.*source scan"):
        _govern_frozen_official_cohort_retry(
            store,
            scan_id="scan-retry",
            selected_signals=[retry_signal],
            decision={"no_trade": False, "decision_tier": "clean_edge"},
            selected_at="2026-08-26T13:05:00+00:00",
            event=_event("scan-retry"),
        )


def test_radar_retry_conflict_does_not_insert_replacement_rows(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-radar-conflict.sqlite")
    source = _signal()
    source["scan_id"] = "scan-original"
    slate = build_ranked_research_slate(
        [source],
        generated_at=SELECTED_AT,
        market_date="2026-08-26",
        scan_id="scan-original",
    )
    frozen = slate["rows"][0]
    event = _event("scan-original", body="OFFICIAL PAPER CANDIDATES\n1. AAA")
    _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[frozen],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=event,
        slate=slate,
    )
    _persist_research_radar_selections(
        store,
        scan_id="scan-original",
        radar=[frozen],
        slate=slate,
        selected_at=SELECTED_AT,
        event=event,
    )
    before = store.load_signal_selections(cohort="research_radar")

    replacement = dict(frozen)
    replacement["ticker"] = "BBB"
    replacement["research_selection_id"] = "research-selection-replacement"
    replacement["research_row_hash_sha256"] = "0" * 64
    changed_slate = dict(slate)
    changed_slate["rows"] = [replacement]
    with pytest.raises(SnapshotValidationError, match="FROZEN_COHORT_CONFLICT"):
        _persist_research_radar_selections(
            store,
            scan_id="scan-original",
            radar=[replacement],
            slate=changed_slate,
            selected_at=SELECTED_AT,
            event=event,
        )
    assert store.load_signal_selections(cohort="research_radar") == before
