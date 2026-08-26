"""Execution-grade watch service for Dawnstrike trade intents.

This module creates auditable paper trade decisions. It does not place broker
orders; live execution is intentionally locked until paper performance, risk
limits, and credentials are approved separately.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.episode_identity import (
    EpisodeIdentityError,
    build_episode_identity,
    deduplicate_episode_candidates,
)
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_STRATEGY_ID,
    DEFAULT_V5_POLICY,
    alphaops_strategy_contract,
    evaluate_v5_official_paper,
    is_v5_active,
)
from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent
from intraday_scanner.notifiers.console import ConsoleNotifier
from intraday_scanner.notifiers.service import build_notifiers, dispatch_events
from intraday_scanner.scenario.contracts import (
    SCENARIO_FORWARD_COHORT,
    SCENARIO_POLICY_VERSION,
    SCENARIO_STRATEGY_ID,
)
from intraday_scanner.scenario.lifecycle import refresh_scenario_lifecycle_links
from intraday_scanner.scenario.point_in_time import subsequent_entry_evidence_violations
from intraday_scanner.services.alpha_paper_reconciliation_service import (
    ALPHAOPS_STRATEGY_ID,
    recover_legacy_alpha_delivery_membership,
)
from intraday_scanner.services.price_observation_service import (
    EASTERN,
    UTC,
    collect_price_observations,
    parse_requested_at,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

MODE_OBSERVE = "observe_only"
MODE_PAPER = "paper_execute"
MODE_LIVE = "live_execute"
ALLOWED_MODES = {MODE_OBSERVE, MODE_PAPER, MODE_LIVE}

ACTION_ENTER = "ENTER_LONG"
ACTION_EXIT = "EXIT_LONG"
ACTION_STAND_DOWN = "STAND_DOWN"

STATE_WATCHING = "WATCHING"
STATE_ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
STATE_PAPER_OPEN = "PAPER_OPEN"
STATE_EXIT_TRIGGERED = "EXIT_TRIGGERED"
STATE_CLOSED = "CLOSED"
STATE_STAND_DOWN = "STAND_DOWN"
STATE_STALE_DATA = "STALE_DATA"


@dataclass(frozen=True)
class WatcherSettings:
    mode: str = MODE_PAPER
    notional_per_trade: float = 1000.0
    simulated_equity: float = 100_000.0
    max_open_positions: int = 3
    max_daily_entries: int = 10
    min_reward_risk: float = 1.5
    notify_blocked: bool = False


def run_trade_watcher(
    *,
    db_path: str | Path = "data/shadow_real.sqlite",
    mode: str = MODE_PAPER,
    source: str = "auto",
    tickers: list[str] | None = None,
    market_date: str | None = None,
    requested_at: str | None = None,
    minute_bars: str | Path | None = None,
    max_age_seconds: int = 360,
    notify: str = "console",
    dry_run: bool = False,
    notional_per_trade: float = 1000.0,
    simulated_equity: float = 100_000.0,
    max_open_positions: int = 3,
    max_daily_entries: int = 10,
    min_reward_risk: float = 1.5,
    notify_blocked: bool = False,
    include_scenarios: bool = False,
    config: ScannerConfig | None = None,
) -> dict[str, Any]:
    """Run one watch cycle and optionally create paper trade fills."""

    normalized_mode = mode.strip().lower()
    if normalized_mode not in ALLOWED_MODES:
        raise SnapshotValidationError(
            "trade watcher mode must be one of: observe_only, paper_execute, live_execute."
        )
    if normalized_mode == MODE_LIVE:
        raise SnapshotValidationError(
            "live_execute is locked. Run paper_execute until the paper ledger proves "
            "risk, fill, and notification behavior."
        )
    if notional_per_trade <= 0:
        raise SnapshotValidationError("notional_per_trade must be positive.")
    if simulated_equity <= 0:
        raise SnapshotValidationError("simulated_equity must be positive.")
    if max_open_positions < 1:
        raise SnapshotValidationError("max_open_positions must be at least 1.")
    if max_daily_entries < 1:
        raise SnapshotValidationError("max_daily_entries must be at least 1.")
    if min_reward_risk <= 0:
        raise SnapshotValidationError("min_reward_risk must be positive.")

    scanner_config = config or load_config(database_path=Path(db_path))
    settings = WatcherSettings(
        mode=normalized_mode,
        notional_per_trade=notional_per_trade,
        simulated_equity=simulated_equity,
        max_open_positions=max_open_positions,
        max_daily_entries=max_daily_entries,
        min_reward_risk=min_reward_risk,
        notify_blocked=notify_blocked,
    )
    store = SQLiteScanStore(db_path)
    store.initialize()
    resolved_at = parse_requested_at(requested_at, market_date=market_date)
    resolved_day = market_date or resolved_at.astimezone(EASTERN).date().isoformat()
    session_signals = _watch_signals(
        store,
        market_date=resolved_day,
        include_scenarios=include_scenarios,
    )
    existing_intents = store.load_trade_intents(limit=50_000)
    existing_intent_ids = {str(row.get("intent_id") or "") for row in existing_intents}
    positions = store.load_paper_positions(limit=50_000)
    existing_episode_ids = {
        str(value)
        for row in existing_intents + [dict(item) for item in positions]
        for value in (
            row.get("episode_id"),
            (row.get("payload_json") or {}).get("episode_id")
            if isinstance(row.get("payload_json"), dict)
            else None,
        )
        if str(value or "").strip()
    }
    existing_symbol_lifecycles = _existing_symbol_lifecycles(existing_intents, positions)
    open_position_rows = [dict(row) for row in positions if row.get("status") == "OPEN"]
    (
        repair_intents,
        repair_positions,
        repair_fills,
        repair_events,
        repaired_signal_ids,
    ) = _canonical_eod_repairs(
        store,
        open_positions=open_position_rows,
        current_market_date=resolved_day,
        config=scanner_config,
        enabled=normalized_mode == MODE_PAPER,
    )
    remaining_open_rows = [
        row
        for row in open_position_rows
        if str(row.get("signal_id") or "") not in repaired_signal_ids
    ]
    carry_signals = _signals_for_open_positions(store, remaining_open_rows)
    signals_by_id = {_signal_id(row): row for row in carry_signals}
    signals_by_id.update({_signal_id(row): row for row in session_signals})
    signals = list(signals_by_id.values())
    episode_diagnostics = next(
        (
            dict(signal.get("episode_dedup_counts") or {})
            for signal in signals
            if signal.get("episode_dedup_counts")
        ),
        {
            "status": "LEGACY_IDENTITY_UNAVAILABLE",
            "raw_pair_count": 0,
            "unique_symbol_count": 0,
            "unique_episode_count": 0,
            "duplicate_collapse_count": 0,
        },
    )
    target_tickers = tickers or [str(row.get("ticker") or "") for row in signals]
    price_result = collect_price_observations(
        db_path=db_path,
        source=source,
        tickers=target_tickers,
        market_date=resolved_day,
        requested_at=resolved_at.isoformat(),
        minute_bars=minute_bars,
        max_age_seconds=max_age_seconds,
        persist=True,
        config=scanner_config,
    )
    observations = list(price_result.get("observations") or [])
    prior_entry_signal_ids = {
        str(row.get("signal_id") or "")
        for row in existing_intents
        if row.get("action") == ACTION_ENTER
    }
    open_positions = {str(row.get("signal_id") or ""): dict(row) for row in remaining_open_rows}
    all_position_signal_ids = {str(row.get("signal_id") or "") for row in positions}
    daily_entry_count = sum(
        1
        for row in store.load_paper_trade_fills(market_date=resolved_day, limit=5000)
        if row.get("side") == "BUY"
    )
    open_count = len(open_positions)
    observations_by_signal, observations_by_ticker = _latest_observations(observations)

    new_intents: list[dict[str, Any]] = [
        row for row in repair_intents if str(row.get("intent_id") or "") not in existing_intent_ids
    ]
    existing_intent_ids.update(str(row.get("intent_id") or "") for row in new_intents)
    paper_positions: list[dict[str, Any]] = list(repair_positions)
    paper_fills: list[dict[str, Any]] = list(repair_fills)
    signal_events: list[dict[str, Any]] = list(repair_events)
    notification_events_by_key: dict[str, NotificationEvent] = {}
    for outbox_intent in existing_intents + repair_intents:
        if _should_notify(outbox_intent, settings):
            event = _notification_event(_notification_ready_intent(outbox_intent))
            notification_events_by_key[event.event_key] = event
    states: list[dict[str, Any]] = []

    created_at = _utc_now()
    for signal in signals:
        signal_id = _signal_id(signal)
        ticker = str(signal.get("ticker") or "").upper()
        observation = observations_by_signal.get(signal_id) or observations_by_ticker.get(ticker)
        open_position = open_positions.get(signal_id)
        existing_symbol_notional = sum(
            float(row.get("notional") or 0.0)
            for row in open_positions.values()
            if str(row.get("ticker") or "").upper() == ticker
        )
        prior_entry = signal_id in prior_entry_signal_ids or signal_id in all_position_signal_ids
        decision = _decision_for_signal(
            signal=signal,
            observation=observation,
            open_position=open_position,
            prior_entry=prior_entry,
            settings=settings,
            open_count=open_count,
            daily_entry_count=daily_entry_count,
            existing_symbol_notional=existing_symbol_notional,
            scanner_config=scanner_config,
        )
        states.append(_state_row(signal, observation, decision, open_position))
        intent = decision.get("intent")
        if not intent:
            continue
        episode_id = str(intent.get("episode_id") or signal.get("episode_id") or "").strip()
        if (
            episode_id
            and intent.get("action") == ACTION_ENTER
            and episode_id in existing_episode_ids
        ):
            states[-1]["reason"] = "duplicate_episode_existing_lifecycle"
            states[-1]["episode_id"] = episode_id
            continue
        if (
            intent.get("action") == ACTION_ENTER
            and ticker in existing_symbol_lifecycles
        ):
            states[-1]["reason"] = "duplicate_symbol_existing_open_or_pending_lifecycle"
            states[-1]["episode_id"] = episode_id or None
            continue
        intent["created_at"] = created_at
        intent["notification_event_key"] = f"trade_intent:{intent['intent_id']}"
        intent["payload_json"] = dict(intent)
        if intent["intent_id"] in existing_intent_ids:
            continue
        new_intents.append(intent)
        existing_intent_ids.add(intent["intent_id"])
        if episode_id:
            existing_episode_ids.add(episode_id)
        if intent.get("action") == ACTION_ENTER:
            existing_symbol_lifecycles.add(ticker)
        if _should_notify(intent, settings):
            event = _notification_event(intent)
            notification_events_by_key[event.event_key] = event
        if normalized_mode == MODE_PAPER and intent["action"] == ACTION_ENTER:
            position, fill = _open_paper_position(intent, scanner_config)
            open_positions[signal_id] = position
            all_position_signal_ids.add(signal_id)
            daily_entry_count += 1
            open_count += 1
            paper_positions.append(position)
            paper_fills.append(fill)
            signal_events.append(_signal_event(intent, "ENTRY_SIGNAL"))
        elif normalized_mode == MODE_PAPER and intent["action"] == ACTION_EXIT and open_position:
            position, fill = _close_paper_position(open_position, intent, scanner_config)
            open_positions.pop(signal_id, None)
            open_count = max(0, open_count - 1)
            paper_positions.append(position)
            paper_fills.append(fill)
            signal_events.append(
                _signal_event(
                    intent,
                    "INVALIDATED" if "invalidation" in intent["reason"].lower() else "EXIT_SIGNAL",
                )
            )

    lifecycle_stats = store.persist_trade_watcher_lifecycle(
        intents=new_intents,
        paper_positions=paper_positions,
        paper_fills=paper_fills,
        signal_events=signal_events,
    )
    scenario_link_stats = (
        refresh_scenario_lifecycle_links(
            store,
            signal_ids={
                _signal_id(signal)
                for signal in signals
                if str(signal.get("strategy_id") or "") == SCENARIO_STRATEGY_ID
            },
            updated_at=created_at,
        )
        if include_scenarios
        else {"refreshed": 0, "row_count": 0}
    )
    intent_stats = lifecycle_stats["intents"]
    position_stats = lifecycle_stats["paper_positions"]
    fill_stats = lifecycle_stats["paper_fills"]
    event_stats = lifecycle_stats["signal_events"]
    notification_stats = _dispatch_notifications(
        list(notification_events_by_key.values()),
        notify=notify,
        db_path=db_path,
        dry_run=dry_run,
    )
    return {
        "status": "ok" if signals or repair_intents else "no_signals",
        "mode": normalized_mode,
        "market_date": resolved_day,
        "requested_at": resolved_at.isoformat(),
        "price_observation": _price_summary(price_result),
        "signal_count": len(signals),
        "state_count": len(states),
        "intent_stats": intent_stats,
        "paper_position_stats": position_stats,
        "paper_fill_stats": fill_stats,
        "signal_event_stats": event_stats,
        "scenario_link_stats": scenario_link_stats,
        "notification_stats": notification_stats,
        "notification_outbox": {
            "candidate_count": len(notification_events_by_key),
            "contract": "trade_intent_is_durable_outbox_notification_is_receipt",
            "retry_safe": True,
        },
        "episode_diagnostics": episode_diagnostics,
        "prior_open_position_count": len(open_position_rows),
        "carried_open_position_count": len(remaining_open_rows),
        "canonical_eod_repair_count": len(repair_positions),
        "states": states,
        "intents": new_intents,
        "paper_positions": paper_positions,
        "paper_fills": paper_fills,
        "live_execution_enabled": False,
    }


def _watch_signals(
    store: SQLiteScanStore,
    *,
    market_date: str,
    include_scenarios: bool = False,
) -> list[dict[str, Any]]:
    recover_legacy_alpha_delivery_membership(
        store,
        market_date=market_date,
        persist=True,
    )
    session_selections = [
        row
        for row in store.load_signal_selections(
            cohort="official_telegram",
            limit=50_000,
        )
        if str(row.get("selected_at") or "")[:10] == market_date
    ]
    expected_strategy_id, expected_strategy_version = alphaops_strategy_contract(
        f"{market_date}T12:00:00-04:00"
    )
    all_selections = [
        row
        for row in session_selections
        if str(row.get("strategy_id") or "") == expected_strategy_id
        and str(row.get("strategy_version") or "") == expected_strategy_version
    ]
    if session_selections and not all_selections:
        observed = sorted(
            {
                (
                    str(row.get("strategy_id") or ""),
                    str(row.get("strategy_version") or ""),
                )
                for row in session_selections
            }
        )
        raise SnapshotValidationError(
            "AlphaOps session selection uses the wrong prospective strategy "
            f"contract; expected {expected_strategy_id}:{expected_strategy_version}, "
            f"observed {observed}."
        )
    _validate_exact_session_selections(all_selections, market_date=market_date)
    scenario_selections = []
    if include_scenarios:
        scenario_selections = [
            row
            for row in store.load_signal_selections(cohort=SCENARIO_FORWARD_COHORT, limit=50_000)
            if str(row.get("selected_at") or "")[:10] == market_date
        ]
        _validate_scenario_selections(scenario_selections, market_date=market_date)
    scenario_open_positions = []
    if include_scenarios:
        scenario_open_positions = [
            row
            for row in store.load_paper_positions(limit=50_000)
            if str(row.get("status") or "") == "OPEN"
            and str(row.get("strategy_id") or "") == SCENARIO_STRATEGY_ID
            and str(row.get("strategy_version") or "") == SCENARIO_POLICY_VERSION
            and str(row.get("cohort") or "") == SCENARIO_FORWARD_COHORT
        ]
    if not all_selections and not scenario_selections and not scenario_open_positions:
        raise SnapshotValidationError(
            "Exact AlphaOps session selection evidence is absent; paper watcher "
            "refuses ranked, legacy, or partial fallback rows."
        )
    selections = [
        row
        for row in all_selections
        if str(row.get("decision") or "").lower() != "no_trade"
        and str(row.get("ticker") or "").upper() != "NO_TRADE"
    ]
    no_trade_selections = [
        row
        for row in all_selections
        if str(row.get("decision") or "").lower() == "no_trade"
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
    ]
    if selections and no_trade_selections:
        raise SnapshotValidationError(
            "AlphaOps session selection evidence is contradictory: explicit no-trade "
            "and selected signals coexist."
        )
    historical = [
        row
        for row in store.load_historical_signals(market_date=market_date, limit=100)
        if _is_watchable_signal(row)
    ]
    historical_by_id = {str(row.get("signal_id") or ""): row for row in historical}
    missing_signal_ids = {
        str(selection.get("signal_id") or "")
        for selection in selections
        if str(selection.get("signal_id") or "") not in historical_by_id
    }
    if missing_signal_ids:
        raise SnapshotValidationError(
            "Exact AlphaOps session selection is only partially persisted; missing "
            "watchable historical signals: " + ", ".join(sorted(missing_signal_ids))
        )
    selected_rows = selections + scenario_selections
    missing_signal_ids = {
        str(selection.get("signal_id") or "")
        for selection in selected_rows
        if str(selection.get("signal_id") or "") not in historical_by_id
    }
    if missing_signal_ids:
        raise SnapshotValidationError(
            "Selected paper signals are only partially persisted; "
            "missing watchable historical signals: "
            + ", ".join(sorted(missing_signal_ids))
        )
    for selection in selected_rows:
        historical_row = historical_by_id[str(selection.get("signal_id") or "")]
        _validate_selection_historical_scan_binding(
            selection,
            historical_row,
            market_date=market_date,
        )
    # Apply the frozen episode boundary immediately before watcher intent
    # creation. Legacy selections without an episode contract remain compatible;
    # opted-in rows are deduplicated and conflicting directions fail closed.
    candidate_rows = [
        {
            **historical_by_id[str(selection.get("signal_id") or "")],
            **_episode_identity_payload(
                historical_by_id[str(selection.get("signal_id") or "")],
                selection,
            ),
            **selection,
            # The immutable selection envelope stores the original Alpha
            # signal under ``payload_json.signal`` while historical rows keep
            # it under ``raw_payload_json``.  Present both sources to the
            # identity validator so a frozen plan cannot be downgraded to a
            # legacy row merely because of storage wrapping.
            "payload_json": _episode_identity_payload(
                historical_by_id[str(selection.get("signal_id") or "")],
                selection,
            ),
        }
        for selection in selected_rows
    ]
    identity_marked = [row for row in candidate_rows if _identity_fields_present(row)]
    if identity_marked:
        if len(identity_marked) != len(candidate_rows):
            raise SnapshotValidationError(
                "Selected paper signals mix episode identity fields with legacy rows; "
                "intent creation is blocked."
            )
        try:
            deduped = deduplicate_episode_candidates(candidate_rows)
        except (EpisodeIdentityError, ValueError) as exc:
            raise SnapshotValidationError(f"Episode identity validation failed: {exc}") from exc
        if deduped["blocked"]:
            raise SnapshotValidationError(
                "Conflicting or incomplete episode candidates are blocked before "
                "PaperOps intent creation."
            )
        selected_rows = list(deduped["selected"])
        for row in selected_rows:
            row["episode_dedup_counts"] = dict(deduped["counts"])
            row["episode_dedup_counts"]["status"] = "FROZEN_IDENTITY_ACTIVE"
    return [
        {
            **historical_by_id[str(selection.get("signal_id") or "")],
            **_episode_identity_payload(
                historical_by_id[str(selection.get("signal_id") or "")],
                selection,
            ),
            "selection_id": selection.get("selection_id"),
            "strategy_id": selection.get("strategy_id"),
            "strategy_version": selection.get("strategy_version"),
            "cohort": selection.get("cohort"),
            "decision": selection.get("decision"),
            "selected_at": selection.get("selected_at"),
            "selection_payload_json": selection.get("payload_json") or {},
            "episode_id": selection.get("episode_id"),
            "matched_strategy_ids": selection.get("matched_strategy_ids") or [],
            "primary_strategy_id": selection.get("primary_strategy_id") or "",
            "matched_episode_ids": selection.get("matched_episode_ids") or [],
            "alternative_episode_ids": selection.get("alternative_episode_ids") or [],
            "alternative_strategy_ids": selection.get("alternative_strategy_ids") or [],
            "session_id": selection.get("session_id"),
            "direction": selection.get("direction"),
            "entry_window": selection.get("entry_window"),
            "frozen_plan_hash": selection.get("frozen_plan_hash"),
            "plan_freeze_status": selection.get("plan_freeze_status"),
            "episode_dedup_counts": selection.get("episode_dedup_counts") or {},
        }
        for selection in selected_rows
    ]


def _validate_selection_historical_scan_binding(
    selection: dict[str, Any],
    historical: dict[str, Any],
    *,
    market_date: str,
) -> None:
    """Reject cross-scan joins unless an exact frozen slate authorizes reuse."""

    selection_scan_id = str(selection.get("scan_id") or "")
    historical_scan_id = str(historical.get("scan_id") or "")
    if selection_scan_id and selection_scan_id == historical_scan_id:
        return
    payload = selection.get("payload_json")
    if not isinstance(payload, dict):
        raise SnapshotValidationError(
            "Selection/historical scan identity mismatch has no governed frozen-slate lineage."
        )
    lineage = payload.get("frozen_slate_lineage")
    slate = payload.get("frozen_ranked_research_slate")
    frozen_signal = payload.get("signal")
    if not isinstance(lineage, dict) or not isinstance(slate, dict) or not isinstance(
        frozen_signal, dict
    ):
        raise SnapshotValidationError(
            "Selection/historical scan identity mismatch has incomplete frozen-slate lineage."
        )
    try:
        from intraday_scanner.services.luna_research_slate_service import (
            validate_ranked_research_slate,
        )

        validate_ranked_research_slate(slate, market_date=market_date)
    except (TypeError, ValueError) as exc:
        raise SnapshotValidationError(
            "Selection/historical cross-scan frozen slate failed integrity checks."
        ) from exc
    frozen_scan_id = str(slate.get("scan_id") or "")
    if (
        str(selection.get("cohort") or "") != "research_radar"
        or str(lineage.get("schema_version") or "")
        != "dawnstrike.luna.frozen_slate_selection_lineage.v1"
        or str(lineage.get("slate_id") or "") != str(slate.get("slate_id") or "")
        or str(lineage.get("slate_content_hash_sha256") or "")
        != str(slate.get("content_hash_sha256") or "")
        or str(lineage.get("frozen_source_scan_id") or "") != frozen_scan_id
        or str(lineage.get("current_scan_id") or "") != selection_scan_id
        or str(lineage.get("reuse_status") or "") != "GOVERNED_DAILY_FREEZE_REUSE"
        or frozen_scan_id != historical_scan_id
        or str(selection.get("source_scan_id") or payload.get("source_scan_id") or "")
        != historical_scan_id
        or str(
            selection.get("scan_lineage_status")
            or payload.get("scan_lineage_status")
            or ""
        )
        != "GOVERNED_DAILY_FREEZE_REUSE"
    ):
        raise SnapshotValidationError(
            "Selection/historical cross-scan lineage does not bind the exact frozen slate."
        )
    selection_id = str(frozen_signal.get("research_selection_id") or "")
    matching_rows = [
        row
        for row in slate.get("rows") or []
        if str(row.get("research_selection_id") or "") == selection_id
    ]
    signal_id = str(selection.get("signal_id") or "")
    frozen_signal_id = str(
        frozen_signal.get("signal_id") or frozen_signal.get("signal_key") or ""
    )
    ticker = str(selection.get("ticker") or "").upper()
    if (
        not selection_id
        or len(matching_rows) != 1
        or json.dumps(matching_rows[0], sort_keys=True, separators=(",", ":"))
        != json.dumps(frozen_signal, sort_keys=True, separators=(",", ":"))
        or not signal_id
        or signal_id != frozen_signal_id
        or signal_id != str(historical.get("signal_id") or "")
        or ticker != str(frozen_signal.get("ticker") or "").upper()
        or ticker != str(historical.get("ticker") or "").upper()
    ):
        raise SnapshotValidationError(
            "Selection/historical cross-scan lineage does not bind the selected signal."
        )


def _existing_symbol_lifecycles(
    intents: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> set[str]:
    """Return symbols with an existing open or unresolved entry lifecycle."""

    closed_entry_intent_ids = {
        str(row.get("entry_intent_id") or "").strip()
        for row in positions
        if str(row.get("status") or "").upper() == "CLOSED"
        and str(row.get("entry_intent_id") or "").strip()
    }
    symbols = {
        str(row.get("ticker") or "").upper()
        for row in positions
        if str(row.get("status") or "").upper() in {"OPEN", "PENDING"}
        and str(row.get("ticker") or "").strip()
    }
    terminal = {
        STATE_CLOSED,
        STATE_STAND_DOWN,
        "CANCELLED",
        "REJECTED",
        "BLOCKED",
        "FAILED",
    }
    for row in intents:
        if str(row.get("action") or "").upper() != ACTION_ENTER:
            continue
        lifecycle = str(row.get("lifecycle_state") or "").upper()
        if lifecycle in terminal:
            continue
        ticker = str(row.get("ticker") or "").upper().strip()
        # A durable ENTER intent can retain a pre-close lifecycle state after
        # its position has been reconciled and closed.  The closed position is
        # authoritative only when its exact entry intent is linked; a missing
        # or different link must keep the symbol locked conservatively.
        intent_id = str(row.get("intent_id") or "").strip()
        if ticker and intent_id not in closed_entry_intent_ids:
            symbols.add(ticker)
    return symbols


def _has_episode_identity(row: dict[str, Any]) -> bool:
    try:
        build_episode_identity(row)
    except (EpisodeIdentityError, TypeError, ValueError):
        return False
    return True


_EPISODE_IDENTITY_MARKERS = (
    "episode_id",
    "session_id",
    "market_session",
    "session",
    "direction",
    "trade_direction",
    "entry_window",
    "entry_window_id",
    "entry_window_key",
    "decision_window",
    "entry_window_start",
    "entry_window_end",
    "entry_start",
    "entry_end",
    "frozen_plan",
    "plan",
    "alphaops_market_structure_plan",
    "frozen_plan_hash",
    "plan_hash",
    "plan_hash_sha256",
    "strategy_plan_hash",
    "plan_freeze_status",
    "freeze_status",
    "provenance_status",
    "plan_provenance_status",
    "plan_levels_frozen",
    "plan_construction_status",
)


def _identity_fields_present(row: dict[str, Any]) -> bool:
    """Detect partial modern identity before deciding legacy compatibility."""

    payload = row.get("payload_json")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    payload = payload if isinstance(payload, dict) else {}
    return any(
        row.get(field) not in (None, "") or payload.get(field) not in (None, "")
        for field in _EPISODE_IDENTITY_MARKERS
    )


def _episode_identity_payload(
    historical: dict[str, Any],
    selection: dict[str, Any],
) -> dict[str, Any]:
    """Expose persisted signal identity without changing its source envelope."""

    payload: dict[str, Any] = {}
    raw = historical.get("raw_payload_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    if isinstance(raw, dict):
        payload.update(raw)
    selected_payload = selection.get("payload_json")
    if isinstance(selected_payload, str):
        try:
            selected_payload = json.loads(selected_payload)
        except (TypeError, ValueError):
            selected_payload = None
    if isinstance(selected_payload, dict):
        payload.update(selected_payload)
        nested_signal = selected_payload.get("signal")
        if isinstance(nested_signal, dict):
            payload.update(nested_signal)
    nested_signal = payload.get("signal")
    if isinstance(nested_signal, dict):
        payload.update(nested_signal)
    return payload


def _validate_exact_session_selections(
    rows: list[dict[str, Any]],
    *,
    market_date: str,
) -> None:
    required = (
        "selection_id",
        "signal_id",
        "ticker",
        "strategy_id",
        "strategy_version",
        "cohort",
        "decision",
        "selected_at",
        "event_key",
        "body_sha256",
    )
    for row in rows:
        missing = [name for name in required if not str(row.get(name) or "").strip()]
        if missing or str(row.get("selected_at") or "")[:10] != market_date:
            detail = ", ".join(missing) if missing else "wrong selected_at date"
            raise SnapshotValidationError(
                "AlphaOps session selection evidence is partially persisted: " + detail
            )


def _validate_scenario_selections(rows: list[dict[str, Any]], *, market_date: str) -> None:
    """Validate scenario members without weakening AlphaOps' frozen cohort contract."""

    _validate_exact_session_selections(rows, market_date=market_date)
    for row in rows:
        if (
            str(row.get("strategy_id") or "") != SCENARIO_STRATEGY_ID
            or str(row.get("strategy_version") or "") != SCENARIO_POLICY_VERSION
            or str(row.get("cohort") or "") != SCENARIO_FORWARD_COHORT
            or str(row.get("decision") or "").lower() != "paper_entry"
        ):
            raise SnapshotValidationError(
                "Scenario selection violates the bounded paper-lifecycle contract."
            )


def _signals_for_open_positions(
    store: SQLiteScanStore,
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rehydrate every open paper position, including prior-session positions."""

    if not positions:
        return []
    wanted = {str(row.get("signal_id") or "") for row in positions}
    historical_by_id = {
        str(row.get("signal_id") or ""): row
        for row in store.load_historical_signals(limit=50_000)
        if str(row.get("signal_id") or "") in wanted
    }
    output: list[dict[str, Any]] = []
    for position in positions:
        signal_id = str(position.get("signal_id") or "")
        historical = historical_by_id.get(signal_id, {})
        # The position snapshot is itself durable lifecycle truth.  If the source
        # signal row was lost, its saved stop/target are enough to carry or close
        # safely without inventing a new entry.
        output.append(
            {
                **historical,
                "signal_id": signal_id,
                "market_date": str(position.get("market_date") or "")[:10],
                "ticker": str(position.get("ticker") or "").upper(),
                "entry_watch_level": position.get("entry_price"),
                "invalidation_level": position.get("stop_price"),
                "target_1": position.get("target_price"),
                "selection_id": position.get("selection_id"),
                "strategy_id": position.get("strategy_id") or ALPHAOPS_STRATEGY_ID,
                "strategy_version": position.get("strategy_version"),
                "cohort": position.get("cohort") or "official_telegram",
                "carry_forward_open_position": True,
            }
        )
    return output


def _canonical_eod_repairs(
    store: SQLiteScanStore,
    *,
    open_positions: list[dict[str, Any]],
    current_market_date: str,
    config: ScannerConfig,
    enabled: bool,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    set[str],
]:
    """Close stale watcher positions from canonical sourced EOD reconciliation."""

    if not enabled or not open_positions:
        return [], [], [], [], set()
    canonical_trades = store.load_strategy_paper_trades(
        strategy_id=ALPHAOPS_STRATEGY_ID,
        limit=50_000,
    )
    trade_by_selection = {
        str(row.get("selection_id") or ""): row
        for row in canonical_trades
        if str(row.get("selection_id") or "")
    }
    trade_by_signal_day = {
        (str(row.get("signal_id") or ""), str(row.get("market_date") or "")[:10]): row
        for row in canonical_trades
    }
    intents: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    repaired_signal_ids: set[str] = set()
    for position in open_positions:
        entry_day = str(position.get("market_date") or "")[:10]
        if not entry_day or entry_day > current_market_date:
            continue
        selection_id = str(position.get("selection_id") or "")
        signal_id = str(position.get("signal_id") or "")
        trade = trade_by_selection.get(selection_id) or trade_by_signal_day.get(
            (signal_id, entry_day)
        )
        if trade is None or str(trade.get("exit_time") or "") == "":
            continue
        trade_id = str(trade.get("trade_id") or "")
        position_id = str(position.get("position_id") or "")
        exit_price = _number(trade.get("exit_fill_price"))
        exit_time = str(trade.get("exit_time") or "")
        if not trade_id or not position_id or exit_price is None or exit_price <= 0:
            continue
        intent_id = (
            "ti_"
            + hashlib.sha256(f"canonical_eod_repair:{trade_id}:{position_id}".encode()).hexdigest()[
                :24
            ]
        )
        intent = {
            "intent_id": intent_id,
            "signal_id": signal_id,
            "market_date": entry_day,
            "ticker": str(position.get("ticker") or "").upper(),
            "mode": MODE_PAPER,
            "lifecycle_state": STATE_EXIT_TRIGGERED,
            "action": ACTION_EXIT,
            "decision_time": exit_time,
            "decision_price": exit_price,
            "trigger_price": position.get("entry_price"),
            "stop_price": position.get("stop_price"),
            "target_price": position.get("target_price"),
            "quantity": position.get("quantity"),
            "notional": position.get("notional"),
            "reason": (
                "Canonical sourced EOD reconciliation repair from "
                f"{trade.get('exit_reason') or 'resolved exit'}."
            ),
            "blocked_reason": "",
            "source_observation_id": "",
            "selection_id": selection_id or trade.get("selection_id"),
            "strategy_id": trade.get("strategy_id") or ALPHAOPS_STRATEGY_ID,
            "strategy_version": trade.get("strategy_version"),
            "cohort": trade.get("cohort") or position.get("cohort"),
            "source_reconciliation_trade_id": trade_id,
            "source_bar_hash_sha256": trade.get("source_bar_hash_sha256"),
            "created_at": _utc_now(),
            "notification_event_key": f"trade_intent:{intent_id}",
            "research_only": True,
            "broker_execution_enabled": False,
        }
        intent["payload_json"] = dict(intent)
        quantity = float(position.get("quantity") or 0.0)
        entry_price = float(position.get("entry_price") or 0.0)
        realized_pnl = round((exit_price - entry_price) * quantity, 4)
        realized_return = (
            round(((exit_price - entry_price) / entry_price) * 100.0, 4)
            if entry_price > 0
            else None
        )
        closed = {
            **position,
            "status": "CLOSED",
            "exit_intent_id": intent_id,
            "closed_at": exit_time,
            "exit_price": exit_price,
            "realized_pnl": realized_pnl,
            "realized_return_pct": realized_return,
            "updated_at": exit_time,
            "source_reconciliation_trade_id": trade_id,
            "canonical_net_pnl": trade.get("net_pnl"),
            "canonical_net_return_pct": trade.get("net_return_pct"),
            "canonical_fees": trade.get("fees"),
            "canonical_slippage_cost": trade.get("slippage_cost"),
        }
        closed["payload_json"] = dict(closed)
        fill = _fill(
            intent,
            position_id=position_id,
            side="SELL",
            fill_price=exit_price,
            quantity=quantity,
            config=config,
        )
        fill.update(
            {
                "source_reconciliation_trade_id": trade_id,
                "source_bar_hash_sha256": trade.get("source_bar_hash_sha256"),
                "canonical_eod_repair": True,
            }
        )
        fill["payload_json"] = dict(fill)
        intents.append(intent)
        positions.append(closed)
        fills.append(fill)
        events.append(_signal_event(intent, "CANONICAL_EOD_REPAIR"))
        repaired_signal_ids.add(signal_id)
    return intents, positions, fills, events, repaired_signal_ids


def _notification_ready_intent(intent: dict[str, Any]) -> dict[str, Any]:
    ready = dict(intent)
    intent_id = str(ready.get("intent_id") or "")
    ready["notification_event_key"] = str(
        ready.get("notification_event_key") or f"trade_intent:{intent_id}"
    )
    return ready


def _is_watchable_signal(row: dict[str, Any]) -> bool:
    ticker = str(row.get("ticker") or "").upper()
    if not ticker or ticker == "NO_TRADE":
        return False
    if row.get("no_trade_reason"):
        return False
    if "can_alert" in row and not row.get("can_alert"):
        return False
    return True


def _alpha_signal_to_watch(row: dict[str, Any], *, market_date: str) -> dict[str, Any]:
    ticker = str(row.get("ticker") or "").upper()
    scan_id = str(row.get("scan_id") or "alpha")
    rank = int(float(row.get("rank") or 0))
    generated = str(row.get("timestamp") or row.get("as_of_timestamp") or "")
    return {
        **dict(row),
        "signal_id": str(
            row.get("signal_id") or row.get("signal_key") or f"{scan_id}:{rank}:{ticker}"
        ),
        "market_date": str(row.get("market_date") or generated[:10] or market_date)[:10],
        "generated_at": generated,
        "entry_watch_level": _first_number(
            row,
            "entry_watch_level",
            "entry_trigger",
            "breakout_trigger",
            "premarket_price",
        ),
        "invalidation_level": _first_number(row, "invalidation_level", "invalidation", "exit_line"),
        "target_1": _first_number(row, "target_1", "first_target", "target"),
    }


def _latest_observations(
    observations: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    usable = [dict(row) for row in observations if _truthy(row.get("is_usable"))]
    usable.sort(key=lambda row: str(row.get("observed_at") or row.get("requested_at") or ""))
    by_signal: dict[str, dict[str, Any]] = {}
    by_ticker: dict[str, dict[str, Any]] = {}
    for row in usable:
        signal_id = str(row.get("signal_id") or "")
        ticker = str(row.get("ticker") or "").upper()
        if signal_id:
            by_signal[signal_id] = row
        if ticker:
            by_ticker[ticker] = row
    return by_signal, by_ticker


def _decision_for_signal(
    *,
    signal: dict[str, Any],
    observation: dict[str, Any] | None,
    open_position: dict[str, Any] | None,
    prior_entry: bool,
    settings: WatcherSettings,
    open_count: int,
    daily_entry_count: int,
    existing_symbol_notional: float,
    scanner_config: ScannerConfig,
) -> dict[str, Any]:
    if not observation:
        return {"state": STATE_STALE_DATA, "reason": "No usable current price observation."}
    price = _number(observation.get("price"))
    if price is None or price <= 0:
        return {"state": STATE_STALE_DATA, "reason": "Current price is missing or invalid."}
    if open_position:
        return _exit_decision(signal, observation, open_position, scanner_config)
    if prior_entry:
        return {"state": STATE_CLOSED, "reason": "Signal already has a paper entry record."}
    if str(signal.get("strategy_id") or "") == SCENARIO_STRATEGY_ID:
        violations = subsequent_entry_evidence_violations(
            decision_at=str(signal.get("generated_at") or signal.get("selected_at") or ""),
            requested_at=str(observation.get("requested_at") or ""),
            observed_at=str(observation.get("observed_at") or ""),
            bar_completed_at=str(observation.get("bar_completed_at") or ""),
            is_complete=observation.get("is_complete"),
            source_bar_hash_sha256=str(observation.get("source_bar_hash_sha256") or ""),
        )
        if violations:
            return {
                "state": STATE_STALE_DATA,
                "reason": "Scenario entry evidence failed point-in-time guard: "
                + ", ".join(violations),
            }
    return _entry_decision(
        signal,
        observation,
        settings=settings,
        open_count=open_count,
        daily_entry_count=daily_entry_count,
        existing_symbol_notional=existing_symbol_notional,
    )


def _entry_decision(
    signal: dict[str, Any],
    observation: dict[str, Any],
    *,
    settings: WatcherSettings,
    open_count: int,
    daily_entry_count: int,
    existing_symbol_notional: float,
) -> dict[str, Any]:
    price = float(_number(observation.get("price")) or 0.0)
    trigger = _level(
        signal,
        "entry_watch_level",
        "entry_trigger",
        "breakout_trigger",
        "premarket_price",
    )
    stop = _level(signal, "invalidation_level", "invalidation", "exit_line")
    target = _level(signal, "target_1", "first_target", "target")
    decision_time = str(observation.get("requested_at") or observation.get("observed_at") or "")
    if str(signal.get("strategy_id") or "") == ALPHAOPS_V5_STRATEGY_ID and is_v5_active(
        decision_time
    ):
        v5 = evaluate_v5_official_paper(
            signal,
            observation,
            simulated_equity=settings.simulated_equity,
            existing_symbol_notional=existing_symbol_notional,
            decision_time=decision_time,
            policy=DEFAULT_V5_POLICY,
        )
        trace = v5.to_dict()
        if not v5.eligible_for_official_paper:
            first_reason = v5.reasons[0] if v5.reasons else "v5_policy_blocked"
            return _stand_down(
                signal,
                observation,
                "AlphaOps v5 official-paper policy blocked this research setup: "
                + "; ".join(v5.reasons),
                first_reason,
                settings=settings,
                trigger=trigger,
                stop=stop,
                target=target,
                decision_trace=trace,
            )
        if trigger is None or stop is None or target is None:
            raise SnapshotValidationError(
                "AlphaOps v5 policy allowed an entry without complete levels."
            )
        if price < trigger:
            return {
                "state": STATE_WATCHING,
                "reason": f"Price {price:.4f} has not reached trigger {trigger:.4f}.",
                "decision_trace": trace,
            }
        if open_count >= settings.max_open_positions:
            return _stand_down(
                signal,
                observation,
                "Maximum open paper positions reached.",
                "max_open_positions",
                settings=settings,
                trigger=trigger,
                stop=stop,
                target=target,
                decision_trace=trace,
            )
        if daily_entry_count >= settings.max_daily_entries:
            return _stand_down(
                signal,
                observation,
                "Maximum daily paper entries reached.",
                "max_daily_entries",
                settings=settings,
                trigger=trigger,
                stop=stop,
                target=target,
                decision_trace=trace,
            )
        shares = int(v5.sizing["shares"])
        proposed_notional = float(v5.sizing["proposed_notional"])
        proposed_risk = float(v5.sizing["proposed_risk"])
        after_cost_r = float(v5.computed["actual_after_cost_reward_risk"])
        intent = _intent(
            signal,
            observation,
            action=ACTION_ENTER,
            lifecycle_state=STATE_ENTRY_TRIGGERED,
            reason=(
                f"AlphaOps v5 official-paper candidate passed at {after_cost_r:.2f}R "
                f"after modeled costs; {shares} shares risk-sized from simulated equity."
            ),
            mode=settings.mode,
            trigger=trigger,
            stop=stop,
            target=target,
            quantity=float(shares),
            notional=proposed_notional,
            risk_amount=proposed_risk,
            decision_trace=trace,
        )
        return {
            "state": STATE_ENTRY_TRIGGERED,
            "reason": intent["reason"],
            "intent": intent,
            "decision_trace": trace,
        }
    if trigger is None or stop is None or target is None:
        return _stand_down(
            signal,
            observation,
            "Missing trigger, stop, or target level.",
            "missing_levels",
            settings=settings,
        )
    if price < trigger:
        return {
            "state": STATE_WATCHING,
            "reason": f"Price {price:.4f} has not reached trigger {trigger:.4f}.",
        }
    if price <= stop:
        return _stand_down(
            signal,
            observation,
            "Price is already at or below invalidation.",
            "already_invalidated",
            settings=settings,
            trigger=trigger,
            stop=stop,
            target=target,
        )
    if price >= target:
        return _stand_down(
            signal,
            observation,
            "Price is already at or beyond target; entry is late.",
            "already_extended",
            settings=settings,
            trigger=trigger,
            stop=stop,
            target=target,
        )
    reward = target - price
    risk = price - stop
    reward_risk = reward / risk if risk > 0 else 0.0
    if reward_risk + 1e-9 < settings.min_reward_risk:
        return _stand_down(
            signal,
            observation,
            f"Reward/risk {reward_risk:.2f} is below threshold {settings.min_reward_risk:.2f}.",
            "reward_risk_too_low",
            settings=settings,
            trigger=trigger,
            stop=stop,
            target=target,
        )
    if open_count >= settings.max_open_positions:
        return _stand_down(
            signal,
            observation,
            "Maximum open paper positions reached.",
            "max_open_positions",
            settings=settings,
            trigger=trigger,
            stop=stop,
            target=target,
        )
    if daily_entry_count >= settings.max_daily_entries:
        return _stand_down(
            signal,
            observation,
            "Maximum daily paper entries reached.",
            "max_daily_entries",
            settings=settings,
            trigger=trigger,
            stop=stop,
            target=target,
        )
    intent = _intent(
        signal,
        observation,
        action=ACTION_ENTER,
        lifecycle_state=STATE_ENTRY_TRIGGERED,
        reason=(
            f"Price confirmed above trigger {trigger:.4f}; target {target:.4f}, "
            f"stop {stop:.4f}, reward/risk {reward_risk:.2f}."
        ),
        mode=settings.mode,
        trigger=trigger,
        stop=stop,
        target=target,
        notional=settings.notional_per_trade,
        risk_amount=settings.notional_per_trade * ((price - stop) / price),
    )
    return {"state": STATE_ENTRY_TRIGGERED, "reason": intent["reason"], "intent": intent}


def _exit_decision(
    signal: dict[str, Any],
    observation: dict[str, Any],
    open_position: dict[str, Any],
    scanner_config: ScannerConfig,
) -> dict[str, Any]:
    price = float(_number(observation.get("price")) or 0.0)
    stop = _number(open_position.get("stop_price")) or _level(
        signal,
        "invalidation_level",
        "exit_line",
    )
    target = _number(open_position.get("target_price")) or _level(
        signal,
        "target_1",
        "first_target",
    )
    decision_time = str(observation.get("observed_at") or observation.get("requested_at") or "")
    if stop is not None and price <= stop:
        reason = f"Price hit invalidation {stop:.4f}."
    elif target is not None and price >= target:
        reason = f"Price hit target {target:.4f}."
    elif _is_eod(decision_time, scanner_config.close_exit_time):
        reason = f"End-of-day flatten rule at {scanner_config.close_exit_time}."
    else:
        return {"state": STATE_PAPER_OPEN, "reason": "Paper position remains open."}
    intent = _intent(
        signal,
        observation,
        action=ACTION_EXIT,
        lifecycle_state=STATE_EXIT_TRIGGERED,
        reason=reason,
        mode=MODE_PAPER,
        trigger=_number(open_position.get("entry_price")),
        stop=stop,
        target=target,
        quantity=_number(open_position.get("quantity")),
        notional=_number(open_position.get("notional")),
    )
    return {"state": STATE_EXIT_TRIGGERED, "reason": reason, "intent": intent}


def _stand_down(
    signal: dict[str, Any],
    observation: dict[str, Any],
    reason: str,
    blocked_reason: str,
    *,
    settings: WatcherSettings,
    trigger: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    decision_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intent = _intent(
        signal,
        observation,
        action=ACTION_STAND_DOWN,
        lifecycle_state=STATE_STAND_DOWN,
        reason=reason,
        mode=settings.mode,
        trigger=trigger,
        stop=stop,
        target=target,
        blocked_reason=blocked_reason,
        stable_block=True,
        decision_trace=decision_trace,
    )
    return {
        "state": STATE_STAND_DOWN,
        "reason": reason,
        "intent": intent,
        "decision_trace": decision_trace,
    }


def _intent(
    signal: dict[str, Any],
    observation: dict[str, Any],
    *,
    action: str,
    lifecycle_state: str,
    reason: str,
    mode: str,
    trigger: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    quantity: float | None = None,
    notional: float | None = None,
    risk_amount: float | None = None,
    blocked_reason: str = "",
    stable_block: bool = False,
    decision_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    signal_id = _signal_id(signal)
    ticker = str(signal.get("ticker") or "").upper()
    market_date = str(signal.get("market_date") or observation.get("market_date") or "")[:10]
    trace = dict(decision_trace or {})
    computed = trace.get("computed")
    trace_decision_time = (
        str(computed.get("decision_time") or "")
        if isinstance(computed, dict)
        else ""
    )
    requested_at = str(observation.get("requested_at") or "")
    decision_time = (
        trace_decision_time
        or str(observation.get("observed_at") or requested_at)
    )
    if trace_decision_time and trace_decision_time != requested_at:
        raise SnapshotValidationError(
            "AlphaOps v5 decision trace does not match its requested evidence time."
        )
    price = _number(observation.get("price"))
    intent_id = _intent_id(
        mode=mode,
        market_date=market_date,
        signal_id=signal_id,
        ticker=ticker,
        action=action,
        decision_time=decision_time,
        price=price,
        blocked_reason=blocked_reason,
        stable_block=stable_block,
        episode_id=str(signal.get("episode_id") or ""),
    )
    raw_signal_payload = signal.get("raw_payload_json")
    if not isinstance(raw_signal_payload, dict):
        raw_signal_payload = {}
    return {
        "intent_id": intent_id,
        "signal_id": signal_id,
        "market_date": market_date,
        "ticker": ticker,
        "mode": mode,
        "lifecycle_state": lifecycle_state,
        "action": action,
        "decision_time": decision_time,
        "decision_price": price,
        "trigger_price": trigger,
        "stop_price": stop,
        "target_price": target,
        "quantity": quantity,
        "notional": notional,
        "risk_amount": round(risk_amount, 4) if risk_amount is not None else None,
        "reason": reason,
        "blocked_reason": blocked_reason,
        "source_observation_id": str(observation.get("observation_id") or ""),
        "source_bar_hash_sha256": str(observation.get("source_bar_hash_sha256") or ""),
        "source_observed_at": str(observation.get("observed_at") or ""),
        "source_bar_completed_at": str(observation.get("bar_completed_at") or ""),
        "selection_id": str(signal.get("selection_id") or ""),
        "episode_id": str(signal.get("episode_id") or ""),
        "matched_strategy_ids": list(signal.get("matched_strategy_ids") or []),
        "primary_strategy_id": str(signal.get("primary_strategy_id") or ""),
        "episode_dedup_counts": dict(signal.get("episode_dedup_counts") or {}),
        "strategy_id": str(signal.get("strategy_id") or ALPHAOPS_STRATEGY_ID),
        "strategy_version": str(
            signal.get("strategy_version")
            or signal.get("model_version")
            or "dawnstrike-alphaops-v4"
        ),
        "cohort": str(signal.get("cohort") or "algorithm_selected"),
        "account_id": str(trace.get("account_id") or ""),
        "execution_policy_version": str(trace.get("policy_version") or ""),
        "cost_model_version": str(
            trace.get("cost_model_version")
            or raw_signal_payload.get("cost_model_version")
            or ""
        ),
        "decision_fingerprint": str(trace.get("decision_fingerprint") or ""),
        "official_paper_eligible": trace.get("eligible_for_official_paper"),
        "decision_trace": trace,
    }


def _intent_id(
    *,
    mode: str,
    market_date: str,
    signal_id: str,
    ticker: str,
    action: str,
    decision_time: str,
    price: float | None,
    blocked_reason: str,
    stable_block: bool,
    episode_id: str = "",
) -> str:
    identity = episode_id or signal_id
    basis = (
        f"{mode}:{market_date}:{identity}:{ticker}:{action}:{blocked_reason}"
        if stable_block
        else f"{mode}:{market_date}:{identity}:{ticker}:{action}:{decision_time}:{price}"
    )
    return "ti_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


def _open_paper_position(
    intent: dict[str, Any],
    config: ScannerConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    decision_price = float(intent.get("decision_price") or 0.0)
    trace = dict(intent.get("decision_trace") or {})
    computed = dict(trace.get("computed") or {})
    expected_entry = _number(computed.get("expected_entry_price"))
    fill_price = round(
        expected_entry
        if expected_entry is not None
        else decision_price * (1 + config.slippage_bps / 10000.0),
        6,
    )
    notional = float(intent.get("notional") or 0.0)
    requested_quantity = _number(intent.get("quantity"))
    quantity = (
        float(requested_quantity)
        if requested_quantity is not None and requested_quantity > 0
        else round(notional / fill_price, 6)
        if fill_price > 0
        else 0.0
    )
    position_id = (
        "pp_"
        + hashlib.sha256(
            f"{intent.get('episode_id') or intent['signal_id']}:{intent['market_date']}".encode()
        ).hexdigest()[:24]
    )
    now = str(intent.get("decision_time") or "")
    position = {
        "position_id": position_id,
        "signal_id": intent["signal_id"],
        "market_date": intent["market_date"],
        "ticker": intent["ticker"],
        "status": "OPEN",
        "quantity": quantity,
        "entry_intent_id": intent["intent_id"],
        "exit_intent_id": "",
        "opened_at": now,
        "closed_at": "",
        "entry_price": fill_price,
        "exit_price": None,
        "stop_price": intent.get("stop_price"),
        "target_price": intent.get("target_price"),
        "notional": round(fill_price * quantity, 4),
        "realized_pnl": None,
        "realized_return_pct": None,
        "max_favorable_excursion": None,
        "max_adverse_excursion": None,
        "updated_at": now,
        "selection_id": intent.get("selection_id"),
        "episode_id": intent.get("episode_id"),
        "strategy_id": intent.get("strategy_id"),
        "strategy_version": intent.get("strategy_version"),
        "cohort": intent.get("cohort"),
        "account_id": intent.get("account_id"),
        "execution_policy_version": intent.get("execution_policy_version"),
        "cost_model_version": intent.get("cost_model_version"),
        "decision_fingerprint": intent.get("decision_fingerprint"),
        "official_paper_eligible": intent.get("official_paper_eligible"),
    }
    position["payload_json"] = dict(position)
    fill = _fill(
        intent,
        position_id=position_id,
        side="BUY",
        fill_price=fill_price,
        quantity=quantity,
        config=config,
    )
    return position, fill


def _close_paper_position(
    position: dict[str, Any],
    intent: dict[str, Any],
    config: ScannerConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    decision_price = float(intent.get("decision_price") or 0.0)
    fill_price = round(decision_price * (1 - config.slippage_bps / 10000.0), 6)
    quantity = float(position.get("quantity") or 0.0)
    entry = float(position.get("entry_price") or 0.0)
    pnl = round((fill_price - entry) * quantity, 4)
    return_pct = round(((fill_price - entry) / entry) * 100, 4) if entry > 0 else None
    updated = {
        **dict(position),
        "status": "CLOSED",
        "exit_intent_id": intent["intent_id"],
        "closed_at": intent.get("decision_time"),
        "exit_price": fill_price,
        "realized_pnl": pnl,
        "realized_return_pct": return_pct,
        "updated_at": intent.get("decision_time"),
    }
    updated["payload_json"] = dict(updated)
    fill = _fill(
        intent,
        position_id=str(position.get("position_id") or ""),
        side="SELL",
        fill_price=fill_price,
        quantity=quantity,
        config=config,
    )
    return updated, fill


def _fill(
    intent: dict[str, Any],
    *,
    position_id: str,
    side: str,
    fill_price: float,
    quantity: float,
    config: ScannerConfig,
) -> dict[str, Any]:
    fill_id = (
        "pf_"
        + hashlib.sha256(f"{intent['intent_id']}:{side}:{position_id}".encode()).hexdigest()[:24]
    )
    fill = {
        "fill_id": fill_id,
        "position_id": position_id,
        "intent_id": intent["intent_id"],
        "signal_id": intent["signal_id"],
        "market_date": intent["market_date"],
        "ticker": intent["ticker"],
        "side": side,
        "fill_time": intent["decision_time"],
        "fill_price": fill_price,
        "quantity": quantity,
        "gross_notional": round(abs(fill_price * quantity), 4),
        "slippage_bps": config.slippage_bps,
        "selection_id": intent.get("selection_id"),
        "strategy_id": intent.get("strategy_id"),
        "strategy_version": intent.get("strategy_version"),
        "cohort": intent.get("cohort"),
        "account_id": intent.get("account_id"),
        "execution_policy_version": intent.get("execution_policy_version"),
        "cost_model_version": intent.get("cost_model_version"),
        "decision_fingerprint": intent.get("decision_fingerprint"),
    }
    fill["payload_json"] = dict(fill)
    return fill


def _signal_event(intent: dict[str, Any], event_type: str) -> dict[str, Any]:
    event_id = (
        "se_" + hashlib.sha256(f"{intent['intent_id']}:{event_type}".encode()).hexdigest()[:24]
    )
    return {
        "event_id": event_id,
        "signal_id": intent["signal_id"],
        "event_type": event_type,
        "event_timestamp": intent["decision_time"],
        "event_price": intent.get("decision_price"),
        "source": "trade_watcher",
        "notes": intent.get("reason") or "",
        "payload_json": {
            "intent_id": intent["intent_id"],
            "action": intent["action"],
            "mode": intent["mode"],
            "reason": intent.get("reason") or "",
            "selection_id": intent.get("selection_id"),
            "strategy_id": intent.get("strategy_id"),
            "strategy_version": intent.get("strategy_version"),
            "cohort": intent.get("cohort"),
            "source_reconciliation_trade_id": intent.get("source_reconciliation_trade_id"),
            "source_bar_hash_sha256": intent.get("source_bar_hash_sha256"),
            "research_only": True,
            "broker_execution_enabled": False,
        },
    }


def _notification_event(intent: dict[str, Any]) -> NotificationEvent:
    message = format_trade_intent_message(intent)
    title = f"Dawnstrike {intent['action'].replace('_', ' ').title()}: {intent['ticker']}"
    return NotificationEvent(
        event_key=str(intent["notification_event_key"]),
        title=title,
        body=message,
        channel_hint="trade_intent",
        ticker=str(intent.get("ticker") or ""),
        payload={
            **dict(intent),
            "telegram_compact_message": message,
        },
    )


def format_trade_intent_message(intent: dict[str, Any]) -> str:
    action = str(intent.get("action") or "")
    mode = str(intent.get("mode") or "").upper()
    ticker = str(intent.get("ticker") or "n/a")
    if action == ACTION_ENTER:
        lead = "PAPER INTENT ONLY - ENTRY SIGNAL"
    elif action == ACTION_EXIT:
        lead = "PAPER INTENT ONLY - EXIT SIGNAL"
    else:
        lead = "PAPER INTENT ONLY - STAND DOWN"
    lines = [
        f"Dawnstrike {lead} ({mode})",
        f"{ticker} | {action.replace('_', ' ')}",
        f"Time: {intent.get('decision_time') or 'n/a'}",
        f"Price: {_fmt_price(intent.get('decision_price'))}",
        f"Trigger: {_fmt_price(intent.get('trigger_price'))}",
        f"Stop: {_fmt_price(intent.get('stop_price'))}",
        f"Target: {_fmt_price(intent.get('target_price'))}",
        f"Notional: {_fmt_money(intent.get('notional'))}",
        f"Reason: {intent.get('reason') or 'n/a'}",
        f"Intent: {intent.get('intent_id')}",
        "Research/watchlist only. No broker order was placed.",
    ]
    return "\n".join(lines)


def _dispatch_notifications(
    events: list[NotificationEvent],
    *,
    notify: str,
    db_path: str | Path,
    dry_run: bool,
) -> dict[str, int]:
    if not events:
        return {"sent": 0, "skipped": 0}
    channels = [channel.strip().lower() for channel in notify.split(",") if channel.strip()]
    if not channels:
        channels = ["console"]
    config = load_config(database_path=Path(db_path), notifier_channels=",".join(channels))
    notifiers: list[BaseNotifier]
    if (
        dry_run
        and "telegram" in channels
        and not (config.telegram_bot_token and config.telegram_chat_id)
    ):
        notifiers = [ConsoleNotifier()]
    else:
        notifiers = build_notifiers(config)
    return dispatch_events(events, notifiers, SQLiteScanStore(db_path), dry_run=dry_run)


def _state_row(
    signal: dict[str, Any],
    observation: dict[str, Any] | None,
    decision: dict[str, Any],
    open_position: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "ticker": str(signal.get("ticker") or "").upper(),
        "signal_id": _signal_id(signal),
        "episode_id": str(signal.get("episode_id") or ""),
        "matched_strategy_ids": list(signal.get("matched_strategy_ids") or []),
        "primary_strategy_id": str(signal.get("primary_strategy_id") or ""),
        "episode_dedup_counts": dict(signal.get("episode_dedup_counts") or {}),
        "rank": signal.get("rank"),
        "state": decision.get("state"),
        "reason": decision.get("reason"),
        "current_price": _number(dict(observation or {}).get("price")),
        "observed_at": dict(observation or {}).get("observed_at"),
        "entry_watch_level": _level(
            signal,
            "entry_watch_level",
            "entry_trigger",
            "breakout_trigger",
        ),
        "invalidation_level": _level(signal, "invalidation_level", "invalidation", "exit_line"),
        "target_1": _level(signal, "target_1", "first_target", "target"),
        "open_position_id": dict(open_position or {}).get("position_id", ""),
        "official_paper_eligible": dict(decision.get("decision_trace") or {}).get(
            "eligible_for_official_paper"
        ),
        "decision_fingerprint": dict(decision.get("decision_trace") or {}).get(
            "decision_fingerprint", ""
        ),
        "feasibility_score": dict(decision.get("decision_trace") or {}).get("feasibility_score"),
    }


def _price_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "source": result.get("source"),
        "requested_at": result.get("requested_at"),
        "market_date": result.get("market_date"),
        "target_count": result.get("target_count"),
        "usable_count": result.get("usable_count"),
        "rejected_count": result.get("rejected_count"),
        "no_lookahead": result.get("no_lookahead", True),
    }


def _should_notify(intent: dict[str, Any], settings: WatcherSettings) -> bool:
    return intent.get("action") in {ACTION_ENTER, ACTION_EXIT} or settings.notify_blocked


def _signal_id(signal: dict[str, Any]) -> str:
    ticker = str(signal.get("ticker") or "").upper()
    return str(
        signal.get("signal_id")
        or signal.get("signal_key")
        or f"{signal.get('scan_id') or 'signal'}:{signal.get('rank') or 0}:{ticker}"
    )


def _level(signal: dict[str, Any], *names: str) -> float | None:
    value = _first_number(signal, *names)
    if value is not None:
        return value
    payload = dict(signal.get("raw_payload_json") or {})
    return _first_number(payload, *names)


def _first_number(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = _number(row.get(name))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(str(value).replace("$", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _is_eod(value: str, close_exit_time: str) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    local = parsed.astimezone(EASTERN)
    try:
        hour, minute = [int(part) for part in close_exit_time.split(":", 1)]
    except ValueError:
        return False
    return (local.hour, local.minute) >= (hour, minute)


def _fmt_price(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"${number:.4f}".rstrip("0").rstrip(".")


def _fmt_money(value: Any) -> str:
    number = _number(value)
    return "n/a" if number is None else f"${number:.2f}"
