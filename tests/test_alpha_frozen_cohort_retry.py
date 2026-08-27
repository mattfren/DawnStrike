from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.alpha.v5_policy import ALPHAOPS_V5_STRATEGY_VERSION
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.notifiers import NotificationEvent
from intraday_scanner.notifiers.telegram_formatter import format_alpha_watch
from intraday_scanner.services.alpha_cycle_service import (
    _govern_frozen_official_cohort_retry,
    _persist_notification_delivery_memberships,
    _persist_official_selections,
    _persist_research_radar_selections,
)
from intraday_scanner.services.luna_research_slate_service import (
    build_ranked_research_slate,
)
from intraday_scanner.services.return_attribution_service import (
    record_alpha_historical_signals,
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


def test_official_missing_source_scan_rejects_without_artifacts(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "official-source-missing.sqlite")
    source_less = _signal()
    source_less.pop("scan_id")

    with pytest.raises(SnapshotValidationError, match="FROZEN_COHORT_CONFLICT.*source scan"):
        _persist_official_selections(
            store,
            scan_id="scan-original",
            selected_signals=[source_less],
            decision={"no_trade": False, "decision_tier": "clean_edge"},
            selected_at=SELECTED_AT,
            event=_event("scan-original"),
        )

    assert store.load_signal_selections() == []
    assert store.load_official_strategy_cohort(
        market_date=SELECTED_AT[:10],
        strategy_id="alphaops_v5",
        strategy_version=ALPHAOPS_V5_STRATEGY_VERSION,
        cohort="official_telegram",
    ) is None
    assert store.load_strategy_versions() == []


def test_mutated_manifest_safety_flags_reject_without_replacing_artifacts(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "manifest-safety-conflict.sqlite")
    event = _event("scan-original")
    original_rows, _ = _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[_signal()],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=event,
    )
    original_cohort = store.load_official_strategy_cohort(
        market_date=SELECTED_AT[:10],
        strategy_id="alphaops_v5",
        strategy_version=ALPHAOPS_V5_STRATEGY_VERSION,
        cohort="official_telegram",
    )
    assert original_cohort is not None
    payload = dict(original_cohort["payload_json"])
    manifest = dict(payload["notification_manifest"])
    manifest["broker_execution_enabled"] = True
    payload["notification_manifest"] = manifest
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE official_strategy_cohorts SET payload_json = ? "
            "WHERE official_cohort_id = ?",
            (json.dumps(payload, sort_keys=True), original_cohort["official_cohort_id"]),
        )

    with pytest.raises(SnapshotValidationError, match="FROZEN_COHORT_CONFLICT.*manifest"):
        _govern_frozen_official_cohort_retry(
            store,
            scan_id="scan-retry",
            selected_signals=[_signal()],
            decision={"no_trade": False, "decision_tier": "clean_edge"},
            selected_at="2026-08-26T13:05:00+00:00",
            event=event,
        )

    assert store.load_signal_selections(cohort="official_telegram") == original_rows
    persisted = store.load_official_strategy_cohort(
        market_date=SELECTED_AT[:10],
        strategy_id="alphaops_v5",
        strategy_version=ALPHAOPS_V5_STRATEGY_VERSION,
        cohort="official_telegram",
    )
    assert persisted is not None
    assert persisted["payload_json"]["notification_manifest"]["broker_execution_enabled"] is True


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


def test_duplicate_official_and_radar_signal_has_one_canonical_delivery(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "delivery-overlap.sqlite")
    event = _event("scan-original")
    official, _ = _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[_signal()],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=event,
    )
    radar = dict(official[0])
    radar["selection_id"] = "radar-selection-overlap"
    radar["strategy_id"] = "research_radar"
    radar["strategy_version"] = "dawnstrike-research-radar-v1"
    radar["cohort"] = "research_radar"
    radar["decision"] = "conditional_paper_watch"
    radar["payload_json"] = {**radar, "signal": _signal()}
    store.record_notification(
        event_key=f"{event.event_key}:telegram",
        channel="telegram",
        run_id="scan-original",
        payload={"title": event.title, "body": event.body, "channel_hint": event.channel_hint},
    )

    deliveries = _persist_notification_delivery_memberships(
        store,
        selections=[*official, radar],
        events=[event],
        notify="telegram",
        preexisting_notification_keys=set(),
    )

    assert len(deliveries) == 1
    assert deliveries[0]["cohort"] == "official_telegram"
    assert deliveries[0]["selection_id"] == official[0]["selection_id"]
    assert len(store.load_notification_deliveries(event_key=event.event_key)) == 1


def test_retry_manifest_body_is_stable_across_producer_minute(tmp_path: Path) -> None:
    from intraday_scanner.services.alpha_cycle_service import (
        _load_frozen_official_notification_manifest,
    )

    store = SQLiteScanStore(tmp_path / "stable-body.sqlite")
    first_at = "2026-08-26T13:00:00+00:00"
    retry_at = "2026-08-26T13:05:00+00:00"
    first_body = format_alpha_watch(
        signals=[], edge_label="NONE", generated_at=first_at
    )
    retry_render = format_alpha_watch(
        signals=[], edge_label="NONE", generated_at=retry_at
    )
    assert retry_render != first_body
    _persist_official_selections(
        store,
        scan_id="scan-original",
        selected_signals=[_signal()],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=first_at,
        event=_event("scan-original", body=first_body),
    )

    manifest = _load_frozen_official_notification_manifest(
        store, selected_at=retry_at
    )
    assert manifest is not None
    assert manifest["body"] == first_body


def test_historical_retry_merge_preserves_alert_linkage(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "historical-merge.sqlite")
    original = {
        **_signal(),
        "signal_key": "stable-historical-signal",
        "scan_id": "scan-original",
        "timestamp": SELECTED_AT,
        "alert_sent": True,
    }
    record_alpha_historical_signals(store, [original])
    store.link_historical_signal_notification(
        scan_id="scan-original",
        telegram_event_key="alphaops:scan-original:alpha_morning_watch:telegram",
        was_alerted=True,
        signal_ids=["stable-historical-signal"],
    )
    retry = {
        **original,
        "scan_id": "scan-retry",
        "timestamp": "2026-08-26T13:05:00+00:00",
        "alert_sent": False,
    }
    record_alpha_historical_signals(store, [retry])

    row = next(
        row
        for row in store.load_historical_signals(scan_id="scan-original")
        if row["signal_id"] == "stable-historical-signal"
    )
    assert row["scan_id"] == "scan-original"
    assert row["was_alerted"] is True
    assert row["telegram_event_key"] == (
        "alphaops:scan-original:alpha_morning_watch:telegram"
    )


def test_radar_duplicate_set_rolls_back_without_partial_rows(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "radar-atomic.sqlite")
    source = _signal()
    source["scan_id"] = "scan-original"
    slate = build_ranked_research_slate(
        [source],
        generated_at=SELECTED_AT,
        market_date="2026-08-26",
        scan_id="scan-original",
    )
    frozen = slate["rows"][0]
    event = _event("scan-original")
    with pytest.raises(SnapshotValidationError, match="FROZEN_COHORT_CONFLICT"):
        _persist_research_radar_selections(
            store,
            scan_id="scan-original",
            radar=[frozen, frozen],
            slate=slate,
            selected_at=SELECTED_AT,
            event=event,
        )
    assert store.load_signal_selections(cohort="research_radar") == []
