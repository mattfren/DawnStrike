"""Stable identity reconciliation for the Scenario paper lifecycle."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from intraday_scanner.scenario.contracts import (
    SCENARIO_FORWARD_COHORT,
    SCENARIO_STRATEGY_ID,
    utc_now_iso,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def refresh_scenario_lifecycle_links(
    store: SQLiteScanStore,
    *,
    signal_ids: set[str] | None = None,
    updated_at: str | None = None,
) -> dict[str, int]:
    """Refresh Scenario links from durable records without changing established IDs."""

    links = [
        row
        for row in store.load_scenario_signal_links(limit=50_000)
        if str(row.get("cohort") or "") == SCENARIO_FORWARD_COHORT
        and str(row.get("strategy_id") or "") == SCENARIO_STRATEGY_ID
        and (
            signal_ids is None
            or str(row.get("signal_id") or "") in signal_ids
        )
    ]
    if not links:
        return {"refreshed": 0, "row_count": 0}

    wanted = {str(row.get("signal_id") or "") for row in links}
    intents_by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for intent in store.load_trade_intents(limit=50_000):
        signal_id = str(intent.get("signal_id") or "")
        if signal_id in wanted:
            intents_by_signal[signal_id].append(intent)
    positions_by_signal = {
        str(position.get("signal_id") or ""): position
        for position in store.load_paper_positions(limit=50_000)
        if str(position.get("signal_id") or "") in wanted
    }
    fills_by_position: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in store.load_paper_trade_fills(limit=50_000):
        position_id = str(fill.get("position_id") or "")
        if position_id:
            fills_by_position[position_id].append(fill)
    outcome_signals = {
        str(outcome.get("signal_id") or "")
        for outcome in store.load_signal_outcomes(limit=50_000)
        if str(outcome.get("signal_id") or "") in wanted
    }

    now = updated_at or utc_now_iso()
    refreshed: list[dict[str, Any]] = []
    for link in links:
        signal_id = str(link.get("signal_id") or "")
        intents = sorted(
            intents_by_signal.get(signal_id, []),
            key=lambda row: (str(row.get("decision_time") or ""), str(row.get("intent_id") or "")),
        )
        position = positions_by_signal.get(signal_id, {})
        position_id = str(position.get("position_id") or link.get("position_id") or "")
        fills = sorted(
            fills_by_position.get(position_id, []),
            key=lambda row: (str(row.get("fill_time") or ""), str(row.get("fill_id") or "")),
        )
        entry_intent_id = str(position.get("entry_intent_id") or "") or _intent_id(
            intents, "ENTER_LONG"
        )
        exit_intent_id = str(position.get("exit_intent_id") or "") or _intent_id(
            intents, "EXIT_LONG", reverse=True
        )
        entry_fill_id = _fill_id(fills, "BUY")
        exit_fill_id = _fill_id(fills, "SELL", reverse=True)
        refreshed.append(
            {
                **link,
                "paper_intent_id": entry_intent_id,
                "entry_intent_id": entry_intent_id,
                "exit_intent_id": exit_intent_id,
                "position_id": position_id,
                "entry_fill_id": entry_fill_id,
                "exit_fill_id": exit_fill_id,
                # A watcher paper trade is represented by its durable position row.
                "paper_trade_id": position_id,
                # signal_outcomes is keyed by signal_id; that key is its durable identity.
                "outcome_id": signal_id if signal_id in outcome_signals else "",
                "updated_at": now,
                "lifecycle_identity_contract": {
                    "paper_intent_table": "trade_intents",
                    "paper_trade_table": "paper_positions",
                    "fill_table": "paper_trade_fills",
                    "outcome_table": "signal_outcomes",
                },
            }
        )
    store.upsert_scenario_signal_links(refreshed)
    return {"refreshed": len(refreshed), "row_count": len(links)}


def _intent_id(
    rows: list[dict[str, Any]], action: str, *, reverse: bool = False
) -> str:
    iterable = reversed(rows) if reverse else rows
    return next(
        (str(row.get("intent_id") or "") for row in iterable if row.get("action") == action),
        "",
    )


def _fill_id(rows: list[dict[str, Any]], side: str, *, reverse: bool = False) -> str:
    iterable = reversed(rows) if reverse else rows
    return next(
        (str(row.get("fill_id") or "") for row in iterable if row.get("side") == side),
        "",
    )
