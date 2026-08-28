from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION
from intraday_scanner.alpha.canonical_return_truth import (
    canonical_paper_selection_context,
)
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
)
from intraday_scanner.services import premarket_enrichment_service as premarket
from intraday_scanner.services.alpha_cycle_service import (
    ALPHAOPS_OFFICIAL_COHORT,
    ALPHAOPS_STRATEGY_ID,
    _existing_notification_keys,
    _link_notification_events,
    _official_selection_notification_event,
    _persist_notification_delivery_memberships,
    _persist_official_selections,
    recover_legacy_alpha_notification_memberships,
)
from intraday_scanner.services.luna_research_slate_service import (
    build_ranked_research_slate,
)
from intraday_scanner.services.trade_watcher_service import _watch_signals
from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, get_schema_version
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

SCAN_ID = "scan-exact-membership"
SELECTED_AT = "2026-07-15T13:10:00+00:00"


def test_notification_delivery_retries_unsent_but_preserves_sent(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "notification-delivery.sqlite")
    event_key = "dawnstrike:test:telegram"

    assert store.record_notification_delivery(
        event_key=event_key,
        channel="telegram:not_configured",
        payload={"sent": False},
    )
    assert store.record_notification_delivery(
        event_key=event_key,
        channel="telegram:sent",
        payload={"sent": True},
    )
    assert not store.record_notification_delivery(
        event_key=event_key,
        channel="telegram:not_configured",
        payload={"sent": False},
    )

    notification = store.load_notification(event_key)
    assert notification is not None
    assert notification["channel"] == "telegram:sent"
    assert notification["sent"] is True


def test_exact_selected_membership_excludes_blocked_and_survives_dedupe(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "alpha.sqlite")
    selected = _signal("SOBR", rank=1, can_alert=True)
    blocked = _signal("ELVA", rank=2, can_alert=False)
    store.persist_historical_signals(
        [_historical_signal(selected), _historical_signal(blocked)]
    )

    event = _official_selection_notification_event(
        SCAN_ID,
        "alpha_morning_watch",
        "Dawnstrike Alpha Watch",
        "1) SOBR - Opportunity 72.0\nResearch only.",
        selected_signals=[selected],
    )

    assert event.payload is not None
    assert event.payload["signals"] == [selected]
    assert all(row["ticker"] != "ELVA" for row in event.payload["signals"])

    selections, stats = _persist_official_selections(
        store,
        scan_id=SCAN_ID,
        selected_signals=[selected],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=event,
    )
    persisted = store.load_signal_selections(scan_id=SCAN_ID)

    assert stats["inserted"] == 1
    assert [row["signal_id"] for row in persisted] == [selected["signal_key"]]
    assert persisted[0]["strategy_id"] == ALPHAOPS_STRATEGY_ID
    assert persisted[0]["strategy_version"] == ALPHA_MODEL_VERSION
    assert persisted[0]["cohort"] == ALPHAOPS_OFFICIAL_COHORT
    assert persisted[0]["decision"] == "clean_edge"
    assert persisted[0]["selected_at"] == SELECTED_AT
    assert persisted[0]["event_key"] == event.event_key
    assert persisted[0]["body_sha256"]

    notification_key = f"{event.event_key}:telegram"
    assert store.record_notification(
        event_key=notification_key,
        channel="telegram",
        run_id=SCAN_ID,
        payload={
            "title": event.title,
            "body": event.body,
            "channel_hint": event.channel_hint,
            "payload": event.payload,
        },
    )
    deliveries = _persist_notification_delivery_memberships(
        store,
        selections=selections,
        events=[event],
        notify="telegram",
        preexisting_notification_keys=set(),
    )
    link = _link_notification_events(
        store,
        scan_id=SCAN_ID,
        events=[event],
        notify="telegram",
        dry_run=False,
        signal_ids=[selected["signal_key"]],
        notification_deliveries=deliveries,
    )

    historical = {
        row["signal_id"]: row for row in store.load_historical_signals(scan_id=SCAN_ID)
    }
    assert link["was_alerted"] is True
    assert historical[selected["signal_key"]]["was_alerted"] is True
    assert historical[selected["signal_key"]]["telegram_event_key"] == notification_key
    assert historical[blocked["signal_key"]]["was_alerted"] is False
    assert historical[blocked["signal_key"]]["telegram_event_key"] == ""
    telegram_events = [
        row for row in store.load_signal_events(limit=20) if row["event_type"] == "TELEGRAM_SENT"
    ]
    assert [row["signal_id"] for row in telegram_events] == [selected["signal_key"]]

    preexisting = _existing_notification_keys(store, events=[event], notify="telegram")
    deduped = _persist_notification_delivery_memberships(
        store,
        selections=selections,
        events=[event],
        notify="telegram",
        preexisting_notification_keys=preexisting,
    )

    assert len(deduped) == 1
    assert deduped[0]["delivery_status"] == "delivered"
    assert deduped[0]["payload_json"]["attempt_status"] == "deduplicated"
    assert deduped[0]["payload_json"]["deduplicated"] is True
    assert len(store.load_notification_deliveries(scan_id=SCAN_ID)) == 1


def test_cross_scan_frozen_official_selection_remains_watchable_and_canonical(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "frozen-official-retry.sqlite")
    source_scan_id = "scan-frozen-source"
    retry_scan_id = "scan-frozen-retry"
    source_signal = {
        **_signal("SOBR", rank=1, can_alert=True),
        "scan_id": source_scan_id,
        "signal_key": f"{source_scan_id}:1:SOBR",
        "market_date": "2026-07-15",
        "timestamp": SELECTED_AT,
        "strategy_contributors": [
            {
                "strategy_id": "alphaops_v5",
                "receipt_id": "receipt-alpha",
                "receipt_hash_sha256": "a" * 64,
            },
            {
                "strategy_id": "gap_up_continuation",
                "receipt_id": "receipt-gap",
                "receipt_hash_sha256": "b" * 64,
                "strategy_adapter": "morning_strategy_adapter_v3",
                "prior_session_lineage": {
                    "source_signal_id": "gap-prior",
                    "prior_session_date": "2026-07-14",
                },
            },
        ],
        "strategy_decision_receipts": [
            {"receipt_id": "receipt-alpha"},
            {"receipt_id": "receipt-gap"},
        ],
        "canonical_primary_strategy_id": "alphaops_v5",
    }
    source_signal.update(_authenticated_observation("SOBR", SELECTED_AT))
    slate = build_ranked_research_slate(
        [source_signal],
        generated_at=SELECTED_AT,
        market_date="2026-07-15",
        scan_id=source_scan_id,
        require_safety=True,
    )
    frozen_signal = slate["rows"][0]
    historical = {
        **_historical_signal(source_signal),
        "signal_id": source_signal["signal_key"],
        "scan_id": source_scan_id,
        "raw_payload_json": frozen_signal,
    }
    store.persist_historical_signals([historical])
    body = "\n".join(
        [
            "OFFICIAL PAPER CANDIDATES",
            "1) SOBR — Alpha 72 | frozen retry",
            "",
            "RESEARCH WATCHLIST",
            "- None",
        ]
    )
    event = _official_selection_notification_event(
        retry_scan_id,
        "alpha_morning_watch",
        "Dawnstrike Alpha Watch",
        body,
        selected_signals=[frozen_signal],
    )
    selections, _ = _persist_official_selections(
        store,
        scan_id=retry_scan_id,
        selected_signals=[frozen_signal],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=event,
        slate=slate,
    )
    notification_key = f"{event.event_key}:telegram"
    assert store.record_notification(
        event_key=notification_key,
        channel="telegram",
        run_id=retry_scan_id,
        payload={
            "title": event.title,
            "body": event.body,
            "channel_hint": event.channel_hint,
            "payload": event.payload,
        },
    )
    deliveries = _persist_notification_delivery_memberships(
        store,
        selections=selections,
        events=[event],
        notify="telegram",
        preexisting_notification_keys=set(),
    )
    persisted = store.load_signal_selections(scan_id=retry_scan_id)[0]

    context = canonical_paper_selection_context(
        persisted,
        delivery=deliveries[0],
    )
    watched = _watch_signals(store, market_date="2026-07-15")

    assert context["signal_id"] == source_signal["signal_key"]
    assert context["authoritative_signal"] == frozen_signal
    assert [
        item["receipt_id"] for item in context["authoritative_signal"]["strategy_contributors"]
    ] == ["receipt-alpha", "receipt-gap"]
    assert watched[0]["signal_id"] == source_signal["signal_key"]
    assert watched[0]["selection_id"] == persisted["selection_id"]
    assert [
        item["receipt_id"] for item in watched[0]["strategy_contributors"]
    ] == ["receipt-alpha", "receipt-gap"]
    assert persisted["payload_json"]["source_scan_id"] == source_scan_id
    assert (
        persisted["payload_json"]["scan_lineage_status"]
        == "GOVERNED_DAILY_FREEZE_REUSE"
    )


@pytest.mark.parametrize("schema_version", ["v2_unsafe", "v1"])
def test_canonical_paper_context_rejects_nonproduction_cross_scan_slates(
    tmp_path: Path, schema_version: str
) -> None:
    source_scan_id = "scan-frozen-source"
    retry_scan_id = "scan-frozen-retry"
    source_signal = {
        **_signal("SOBR", rank=1, can_alert=True),
        "scan_id": source_scan_id,
        "signal_key": f"{source_scan_id}:1:SOBR",
        "market_date": "2026-07-15",
        "timestamp": SELECTED_AT,
    }
    source_signal.update(_authenticated_observation("SOBR", SELECTED_AT))
    slate = build_ranked_research_slate(
        [source_signal],
        generated_at=SELECTED_AT,
        market_date="2026-07-15",
        scan_id=source_scan_id,
        require_safety=False,
    )
    if schema_version == "v1":
        slate = dict(slate)
        slate["schema_version"] = "dawnstrike.luna.ranked_research_slate.v1"
        slate.pop("require_safety", None)
        slate["content_hash_sha256"] = _slate_hash(slate)
        slate["slate_id"] = "luna-slate-" + slate["content_hash_sha256"][:24]
    store = SQLiteScanStore(tmp_path / f"hostile-{schema_version}.sqlite")
    historical = {
        **_historical_signal(source_signal),
        "signal_id": source_signal["signal_key"],
        "scan_id": source_scan_id,
        "raw_payload_json": slate["rows"][0],
    }
    store.persist_historical_signals([historical])
    event = _official_selection_notification_event(
        retry_scan_id,
        "alpha_morning_watch",
        "Dawnstrike Alpha Watch",
        "OFFICIAL PAPER CANDIDATES\n1) SOBR - frozen retry",
        selected_signals=[slate["rows"][0]],
    )
    selections, _ = _persist_official_selections(
        store,
        scan_id=retry_scan_id,
        selected_signals=[slate["rows"][0]],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at=SELECTED_AT,
        event=event,
        slate=slate,
    )
    selection = store.load_signal_selections(scan_id=retry_scan_id)[0]
    notification_key = f"{event.event_key}:telegram"
    assert store.record_notification(
        event_key=notification_key,
        channel="telegram",
        run_id=retry_scan_id,
        payload={
            "title": event.title,
            "body": event.body,
            "channel_hint": event.channel_hint,
            "payload": event.payload,
        },
    )
    deliveries = _persist_notification_delivery_memberships(
        store,
        selections=selections,
        events=[event],
        notify="telegram",
        preexisting_notification_keys=set(),
    )
    with pytest.raises(ValueError, match="frozen-slate lineage"):
        canonical_paper_selection_context(selection, delivery=deliveries[0])


def test_selection_identity_migration_is_additive_and_idempotent(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "migration.sqlite")
    store.initialize()
    store.initialize()

    with sqlite3.connect(store.db_path) as connection:
        assert get_schema_version(connection) == CURRENT_SCHEMA_VERSION

    assert store.load_strategy_versions() == []
    assert store.load_signal_selections() == []
    assert store.load_notification_deliveries() == []


def test_selection_identity_switches_to_v5_only_after_activation(
    tmp_path: Path,
) -> None:
    store = SQLiteScanStore(tmp_path / "prospective.sqlite")
    signal = _signal("NOVA", rank=1, can_alert=True)
    event = _official_selection_notification_event(
        SCAN_ID,
        "alpha_morning_watch",
        "Dawnstrike Alpha Watch",
        "1) NOVA - Opportunity 82.0\nResearch only.",
        selected_signals=[signal],
    )

    rows, stats = _persist_official_selections(
        store,
        scan_id=SCAN_ID,
        selected_signals=[signal],
        decision={"no_trade": False, "decision_tier": "clean_edge"},
        selected_at="2026-07-31T12:10:00+00:00",
        event=event,
    )

    assert rows[0]["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    assert rows[0]["strategy_version"] == ALPHAOPS_V5_STRATEGY_VERSION
    assert rows[0]["payload_json"]["signal"] == signal
    assert stats["strategy_id"] == ALPHAOPS_V5_STRATEGY_ID
    assert stats["strategy_version"] == ALPHAOPS_V5_STRATEGY_VERSION


def test_legacy_recovery_trusts_only_an_unambiguous_rendered_body(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    store = SQLiteScanStore(db_path)
    selected = _signal("SOBR", rank=1, can_alert=True)
    blocked = _signal("ELVA", rank=2, can_alert=False)
    store.persist_historical_signals(
        [_historical_signal(selected), _historical_signal(blocked)]
    )
    body = "\n".join(
        [
            "🚀 Dawnstrike Alpha Watch",
            "⏱ 8:10 AM CT | 1 name | High",
            "Manual watch only. Not a buy signal.",
            "",
            "1) SOBR - Opportunity 72.0 (High)",
            "🧪 Research only. No orders placed.",
        ]
    )
    event_key = f"alphaops:{SCAN_ID}:alpha_morning_watch:telegram"
    store.record_notification(
        event_key=event_key,
        channel="telegram",
        run_id=SCAN_ID,
        payload={
            "title": "Dawnstrike Alpha Watch",
            "body": body,
            "channel_hint": "alpha_morning_watch",
            # Legacy structured payloads could contain blocked ranked rows.
            "payload": {"signals": [selected, blocked]},
        },
    )

    result = recover_legacy_alpha_notification_memberships(db_path=db_path)

    assert result["notifications_recovered"] == 1
    assert result["memberships_recovered"] == 1
    assert [
        row["signal_id"] for row in store.load_signal_selections(scan_id=SCAN_ID)
    ] == [selected["signal_key"]]
    memberships = store.load_notification_deliveries(scan_id=SCAN_ID)
    assert [row["signal_id"] for row in memberships] == [selected["signal_key"]]
    assert memberships[0]["payload_json"]["legacy_recovery"] == "exact_rendered_body"
    historical = {
        row["signal_id"]: row for row in store.load_historical_signals(scan_id=SCAN_ID)
    }
    assert historical[selected["signal_key"]]["was_alerted"] is True
    assert historical[blocked["signal_key"]]["was_alerted"] is False


def _signal(ticker: str, *, rank: int, can_alert: bool) -> dict[str, object]:
    signal_id = f"{SCAN_ID}:{rank}:{ticker}"
    return {
        "signal_key": signal_id,
        "scan_id": SCAN_ID,
        "ticker": ticker,
        "rank": rank,
        "can_alert": can_alert,
        "alpha_score": 72.0 if can_alert else 60.0,
        "model_version": ALPHA_MODEL_VERSION,
        "timestamp": SELECTED_AT,
    }


def _historical_signal(signal: dict[str, object]) -> dict[str, object]:
    return {
        "signal_id": signal["signal_key"],
        "scan_id": SCAN_ID,
        "alpha_signal_id": signal["signal_key"],
        "generated_at": SELECTED_AT,
        "market_date": "2026-07-15",
        "ticker": signal["ticker"],
        "rank": signal["rank"],
        "model_version": ALPHA_MODEL_VERSION,
        "signal_label": "WATCH" if signal["can_alert"] else "BLOCKED",
        "was_alerted": False,
        "raw_payload_json": signal,
    }


def _authenticated_observation(ticker: str, generated_at: str) -> dict[str, object]:
    generated = datetime.fromisoformat(generated_at)
    observation = premarket.observation_from_alpaca_bars(
        ticker,
        [
            {
                "ticker": ticker,
                "timestamp": (generated - timedelta(minutes=2)).isoformat(),
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "volume": 1_000,
            }
        ],
        previous_close=9.5,
        requested_at=generated,
        max_age_seconds=600,
        feed="iex",
    )
    observation_hash, observation_payload = premarket._canonical_observation_payload(
        observation
    )
    return {
        "source_count": 1,
        "source_quality_status": "VERIFIED",
        "freshness_status": "FRESH",
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "input_status": "VERIFIED",
        "evidence_status": "VERIFIED",
        "enrichment_observation_sha256": observation_hash,
        "enrichment_observation_payload_json": observation_payload,
        "enrichment_max_age_seconds": 600,
    }


def _slate_hash(slate: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in slate.items()
                if key not in {"content_hash_sha256", "slate_id"}
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
