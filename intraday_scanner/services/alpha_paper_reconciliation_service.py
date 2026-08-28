"""Deterministic AlphaOps paper-trade reconciliation and strategy scorecards.

The live monitor is useful for operator awareness, but five-minute polling is
not precise enough to be the performance ledger.  This service uses the
complete sourced one-minute outcome artifact to reconstruct the exact
paper-only lifecycle after the session.  It never places a broker order.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
    CURRENT_RETURN_TRUTH,
    canonical_paper_enter_intent_context,
    canonical_paper_selection_context,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.path_replay import PathTruthStatus
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_POLICY_VERSION,
    ALPHAOPS_V5_STRATEGY_ID,
    alphaops_strategy_contract,
    is_v5_active,
)
from intraday_scanner.config import ScannerConfig, load_config
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.models import utc_now_iso
from intraday_scanner.services.luna_research_slate_service import (
    AuthenticatedStrategyReceiptResolver,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

LEGACY_ALPHAOPS_STRATEGY_ID = "alphaops_v4"
# Public compatibility name for pre-V5 recovery and watcher imports.
ALPHAOPS_STRATEGY_ID = LEGACY_ALPHAOPS_STRATEGY_ID
ALPHAOPS_STRATEGY_VERSION = "dawnstrike-alphaops-v4"
SELECTED_COHORT = "algorithm_selected"
DELIVERED_COHORT = "official_telegram"
EXECUTION_POLICY_VERSION = "alphaops_intraday_first_touch_v1"
DEFAULT_FEE_BPS = 1.0
_BODY_TICKER = re.compile(r"^\s*\d+\)\s+([A-Z][A-Z0-9.-]*)\s+-", re.MULTILINE)
_BODY_PICK_COUNT = re.compile(
    r"\|\s*(\d+)\s+(?:picks?|names?)\s*\|",
    re.IGNORECASE,
)
_DELIVERED_STATUSES = {
    "delivered",
    "delivered_legacy",
}
_CANONICAL_TRADE_TRUTH_FIELDS = (
    "path_replay_id",
    "path_replay_receipt",
    "return_truth_schema_version",
    "return_truth_hash_sha256",
    "cost_schema_version",
    "cost_receipt_id",
    "cost_receipt_hash_sha256",
    "cost_receipt",
    "observed_cost_model_identity",
    "modeled_cost_model_identity",
    "cost_components",
    "gross_return_pct",
    "after_cost_return_pct",
    "benchmark_symbol",
    "benchmark_return_pct",
    "benchmark_source_bar_hash_sha256",
    "benchmark_independent_reconciliation_status",
    "secondary_benchmark_symbol",
    "secondary_benchmark_return_pct",
    "secondary_benchmark_source_bar_hash_sha256",
    "secondary_benchmark_independent_reconciliation_status",
    "reconciliation_schema_version",
    "reconciliation_receipt_id",
    "reconciliation_receipt_hash_sha256",
    "reconciliation_receipt",
    "independent_reconciliation_status",
    "causal_decision_identity",
    "eligibility_policy_version",
    "evidence_cohort",
)


def _is_official_telegram_delivery(
    row: dict[str, Any],
    *,
    strategy_id: str | None = None,
) -> bool:
    return bool(
        str(row.get("channel") or "").strip().lower() == "telegram"
        and (strategy_id is None or str(row.get("strategy_id") or "") == strategy_id)
        and str(row.get("cohort") or "") == DELIVERED_COHORT
        and str(row.get("delivery_status") or "").strip().lower() in _DELIVERED_STATUSES
    )


def reconcile_alpha_paper_trades(
    *,
    db_path: str | Path = "data/shadow_real.sqlite",
    market_date: str | None = None,
    out_dir: str | Path = "outputs/strategy_reconciliation",
    persist: bool = True,
    notional_per_trade: float = 1_000.0,
    fee_bps: float = DEFAULT_FEE_BPS,
    config: ScannerConfig | None = None,
) -> dict[str, Any]:
    """Reconcile one AlphaOps session from exact selections and sourced outcomes."""

    if notional_per_trade <= 0:
        raise ValueError("notional_per_trade must be positive")
    if fee_bps < 0:
        raise ValueError("fee_bps must be non-negative")
    day = (market_date or date.today().isoformat())[:10]
    strategy_id, strategy_version = alphaops_strategy_contract(f"{day}T12:00:00-04:00")
    execution_policy_version = (
        ALPHAOPS_V5_POLICY_VERSION
        if strategy_id == ALPHAOPS_V5_STRATEGY_ID
        else EXECUTION_POLICY_VERSION
    )
    scanner_config = config or load_config(database_path=Path(db_path))
    store = SQLiteScanStore(db_path, read_only=not persist)
    store.initialize()
    contributor_receipt_verifier = AuthenticatedStrategyReceiptResolver.from_store(
        store,
        market_date=day,
        strategy_id=None,
    )
    recovery = recover_legacy_alpha_delivery_membership(
        store,
        market_date=day,
        persist=persist,
    )
    session_selections = [
        row
        for row in store.load_signal_selections(
            strategy_id=strategy_id,
            limit=50_000,
        )
        if str(row.get("selected_at") or "")[:10] == day
    ]
    selections = [
        row
        for row in session_selections
        if str(row.get("decision") or "").lower() != "no_trade"
        and str(row.get("ticker") or "").upper() != "NO_TRADE"
    ]
    no_trade_selections = [
        row
        for row in session_selections
        if str(row.get("decision") or "").lower() == "no_trade"
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
    ]
    selection_evidence_status = (
        "selected"
        if selections
        else "explicit_no_trade"
        if no_trade_selections
        else "missing_selection_evidence"
    )
    if not session_selections and not persist:
        raise SnapshotValidationError(
            "Exact AlphaOps session selection evidence is absent; reconciliation "
            "is blocked before publishing artifacts."
        )
    if selections and no_trade_selections:
        raise SnapshotValidationError(
            "AlphaOps session selection evidence is contradictory: explicit no-trade "
            "and selected signals coexist."
        )
    deliveries = [
        row
        for row in store.load_notification_deliveries(limit=50_000)
        if str(row.get("selected_at") or row.get("attempted_at") or "")[:10] == day
        and str(row.get("strategy_id") or "") == strategy_id
    ]
    delivery_by_signal = _delivery_by_signal(
        deliveries,
        strategy_id=strategy_id,
    )
    allowed_entry_by_signal = {
        str(row.get("signal_id") or ""): row
        for row in store.load_trade_intents(
            market_date=day,
            action="ENTER_LONG",
            limit=50_000,
        )
        if str(row.get("strategy_id") or "") == strategy_id
        and row.get("official_paper_eligible") is True
    }
    raw_entry_by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in store.load_trade_intent_records(
        market_date=day,
        action="ENTER_LONG",
        limit=50_000,
    ):
        payload = row.get("payload_json")
        signal_id = str(payload.get("signal_id") if isinstance(payload, dict) else "")
        if signal_id:
            raw_entry_by_signal.setdefault(signal_id, []).append(row)
    raw_observations = store.load_price_observation_records(
        market_date=day,
        limit=50_000,
    )
    observation_by_id: dict[str, dict[str, Any]] = {}
    for row in raw_observations:
        columns = row.get("columns")
        observation_id = str(columns.get("observation_id") if isinstance(columns, dict) else "")
        if observation_id:
            observation_by_id[observation_id] = row
    historical = {
        str(row.get("signal_id") or ""): row
        for row in store.load_historical_signals(market_date=day, limit=50_000)
    }
    outcomes = {
        str(row.get("signal_id") or ""): row
        for row in store.load_signal_outcomes(start=day, end=day, limit=50_000)
    }
    reconciled_at = utc_now_iso()
    evaluations: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    learning_labels: list[dict[str, Any]] = []
    for selection in selections:
        signal_id = str(selection.get("signal_id") or "")
        signal = historical.get(signal_id, {})
        outcome = outcomes.get(signal_id)
        delivery = delivery_by_signal.get(signal_id)
        decision_context: dict[str, Any] | None = None
        if delivery is not None:
            try:
                selection_context = canonical_paper_selection_context(
                    selection,
                    delivery=delivery,
                    contributor_receipt_verifier=contributor_receipt_verifier,
                )
                raw_entries = raw_entry_by_signal.get(signal_id, [])
                if len(raw_entries) == 1:
                    intent_payload = raw_entries[0].get("payload_json")
                    source_id = str(
                        intent_payload.get("source_observation_id")
                        if isinstance(intent_payload, dict)
                        else raw_entries[0].get("source_observation_id") or ""
                    )
                    source_record = observation_by_id.get(source_id)
                    if source_record is not None:
                        decision_context = canonical_paper_enter_intent_context(
                            selection_context,
                            intent_record=raw_entries[0],
                            source_observation_record=source_record,
                        )
                elif (
                    outcome is not None
                    and str(outcome.get("outcome_status") or "") == "not_triggered"
                ):
                    decision_context = selection_context
            except ValueError:
                decision_context = None
        evaluation, trade, labels = _reconcile_selection(
            selection=selection,
            signal=signal,
            outcome=outcome,
            delivery=delivery,
            reconciled_at=reconciled_at,
            notional_per_trade=notional_per_trade,
            fee_bps=fee_bps,
            slippage_bps=scanner_config.slippage_bps,
            entry_intent=allowed_entry_by_signal.get(signal_id),
            decision_context=decision_context,
            execution_policy_version=execution_policy_version,
        )
        evaluations.append(evaluation)
        if trade is not None:
            trades.append(trade)
        learning_labels.extend(labels)

    scorecards = _daily_scorecards(
        market_date=day,
        selections=selections,
        session_selections=session_selections,
        deliveries=deliveries,
        evaluations=evaluations,
        trades=trades,
        reconciled_at=reconciled_at,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        execution_policy_version=execution_policy_version,
    )
    persistence: dict[str, Any] = {
        "evaluations": {"inserted": 0, "updated": 0},
        "trades": {"inserted": 0, "updated": 0, "deleted": 0},
        "learning_labels": {"inserted": 0, "updated": 0, "deleted": 0},
        "scorecards": {"inserted": 0, "updated": 0},
    }
    if persist:
        persistence = store.persist_strategy_reconciliation(
            evaluations=evaluations,
            paper_trades=trades,
            learning_labels=learning_labels,
            scorecards=scorecards,
        )

    unresolved = [
        row for row in evaluations if str(row.get("reconciliation_status") or "") == "unresolved"
    ]
    invalid = [
        row for row in evaluations if str(row.get("reconciliation_status") or "") == "invalid"
    ]
    status = "complete" if session_selections and not unresolved and not invalid else "failed"
    output_dir = Path(out_dir) / day
    paths = _write_artifacts(
        output_dir,
        market_date=day,
        status=status,
        recovery=recovery,
        selections=selections,
        session_selections=session_selections,
        deliveries=deliveries,
        evaluations=evaluations,
        trades=trades,
        labels=learning_labels,
        scorecards=scorecards,
        unresolved=unresolved,
        invalid=invalid,
        reconciled_at=reconciled_at,
    )
    return {
        "status": status,
        "market_date": day,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "execution_policy_version": execution_policy_version,
        "selection_count": len(selections),
        "session_selection_count": len(session_selections),
        "no_trade_count": len(no_trade_selections),
        "selection_evidence_status": selection_evidence_status,
        "delivered_count": sum(1 for row in evaluations if row.get("delivered")),
        "triggered_count": sum(1 for row in evaluations if row.get("activated")),
        "closed_trade_count": len(trades),
        "not_triggered_count": sum(
            1 for row in evaluations if row.get("terminal_state") == "not_triggered"
        ),
        "unresolved_count": len(unresolved),
        "invalid_count": len(invalid),
        "research_only": True,
        "broker_execution_enabled": False,
        "recovery": recovery,
        "persistence": persistence,
        "evaluations": evaluations,
        "paper_trades": trades,
        "scorecards": scorecards,
        "paths": paths,
    }


def recover_legacy_alpha_delivery_membership(
    store: SQLiteScanStore,
    *,
    market_date: str,
    persist: bool = True,
) -> dict[str, Any]:
    """Recover old bundled Telegram membership only from its exact saved body."""

    if is_v5_active(f"{market_date}T12:00:00-04:00"):
        return {
            "status": "prospective_v5_legacy_recovery_disabled",
            "recovered": 0,
        }
    existing = store.load_signal_selections(
        strategy_id=LEGACY_ALPHAOPS_STRATEGY_ID,
        limit=50_000,
    )
    existing = [row for row in existing if str(row.get("selected_at") or "")[:10] == market_date]
    if existing:
        return {"status": "not_needed", "recovered": 0}
    notifications = [
        row
        for row in store.load_recent_notifications(limit=10_000)
        if str(row.get("channel") or "").lower() == "telegram"
        and "alpha_morning_watch" in str(row.get("event_key") or "")
        and str(row.get("sent_at") or "")[:10] == market_date
    ]
    if not notifications:
        return {"status": "no_exact_delivery_evidence", "recovered": 0}
    selection_rows: list[dict[str, Any]] = []
    delivery_rows: list[dict[str, Any]] = []
    for notification in notifications:
        body = str(notification.get("body") or "")
        parsed_tickers = _BODY_TICKER.findall(body)
        count_match = _BODY_PICK_COUNT.search(body)
        tickers = tuple(dict.fromkeys(parsed_tickers))
        scan_id = str(notification.get("run_id") or "")
        if (
            not body
            or not tickers
            or not scan_id
            or count_match is None
            or int(count_match.group(1)) != len(parsed_tickers)
            or len(tickers) != len(parsed_tickers)
        ):
            continue
        body_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        signals = store.load_historical_signals(scan_id=scan_id, limit=500)
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for row in signals:
            by_ticker.setdefault(str(row.get("ticker") or "").upper(), []).append(row)
        if any(len(by_ticker.get(ticker, [])) != 1 for ticker in tickers):
            continue
        event_key = str(notification.get("event_key") or "")
        for rank, ticker in enumerate(tickers, start=1):
            signal = by_ticker[ticker][0]
            signal_id = str(signal.get("signal_id") or "")
            selection_id = _stable_id("selection", scan_id, signal_id, DELIVERED_COHORT)
            selected_at = str(signal.get("generated_at") or notification.get("sent_at") or "")
            selection_rows.append(
                {
                    "selection_id": selection_id,
                    "signal_id": signal_id,
                    "scan_id": scan_id,
                    "market_date": market_date,
                    "strategy_id": LEGACY_ALPHAOPS_STRATEGY_ID,
                    "strategy_version": str(
                        signal.get("model_version") or ALPHAOPS_STRATEGY_VERSION
                    ),
                    "cohort": DELIVERED_COHORT,
                    "rank": signal.get("rank") or rank,
                    "decision": "selected",
                    "selected_at": selected_at,
                    "event_key": event_key,
                    "body_sha256": body_sha256,
                    "payload_json": {
                        "legacy_recovered": True,
                        "ticker": ticker,
                        "body_sha256": body_sha256,
                        "reason": "Recovered from exact persisted Telegram body.",
                    },
                }
            )
            delivery_rows.append(
                {
                    "membership_id": _stable_id("delivery", event_key, signal_id),
                    "event_key": event_key,
                    "selection_id": selection_id,
                    "signal_id": signal_id,
                    "scan_id": scan_id,
                    "market_date": market_date,
                    "strategy_id": LEGACY_ALPHAOPS_STRATEGY_ID,
                    "strategy_version": str(
                        signal.get("model_version") or ALPHAOPS_STRATEGY_VERSION
                    ),
                    "cohort": DELIVERED_COHORT,
                    "channel": "telegram",
                    "decision": "selected",
                    "selected_at": selected_at,
                    "delivery_status": "delivered_legacy",
                    "attempted_at": str(notification.get("sent_at") or ""),
                    "delivered_at": str(notification.get("sent_at") or ""),
                    "body_sha256": body_sha256,
                    "payload_json": {
                        "legacy_recovered": True,
                        "exact_body_membership": True,
                    },
                }
            )
    if persist and selection_rows:
        store.persist_signal_selections(selection_rows)
        store.persist_notification_deliveries(delivery_rows)
    return {
        "status": "recovered" if selection_rows else "no_recoverable_membership",
        "recovered": len(selection_rows),
        "signal_ids": [str(row["signal_id"]) for row in selection_rows],
    }


def _reconcile_selection(
    *,
    selection: dict[str, Any],
    signal: dict[str, Any],
    outcome: dict[str, Any] | None,
    delivery: dict[str, Any] | None,
    reconciled_at: str,
    notional_per_trade: float,
    fee_bps: float,
    slippage_bps: float,
    entry_intent: dict[str, Any] | None = None,
    decision_context: dict[str, Any] | None = None,
    execution_policy_version: str = EXECUTION_POLICY_VERSION,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    decision_context = decision_context or selection
    signal_id = str(selection.get("signal_id") or "")
    selection_id = str(selection.get("selection_id") or "")
    ticker = str(selection.get("ticker") or signal.get("ticker") or "").upper()
    market_date = str(selection.get("market_date") or signal.get("market_date") or "")[:10]
    strategy_id = str(selection.get("strategy_id") or LEGACY_ALPHAOPS_STRATEGY_ID)
    delivered = bool(
        delivery
        and _is_official_telegram_delivery(
            delivery,
            strategy_id=strategy_id,
        )
    )
    base = {
        "evaluation_id": _stable_id(
            "evaluation",
            selection_id,
            execution_policy_version,
        ),
        "selection_id": selection_id,
        "signal_id": signal_id,
        "scan_id": selection.get("scan_id") or signal.get("scan_id") or "",
        "market_date": market_date,
        "ticker": ticker,
        "strategy_id": strategy_id,
        "strategy_version": str(selection.get("strategy_version") or ALPHAOPS_STRATEGY_VERSION),
        "cohort": str(selection.get("cohort") or DELIVERED_COHORT),
        "direction": "long",
        "decision_time": signal.get("generated_at") or selection.get("selected_at"),
        "delivery_id": delivery.get("membership_id") if delivery else None,
        "delivery_status": (delivery.get("delivery_status") if delivery else "not_delivered"),
        "delivery_channel": delivery.get("channel") if delivery else None,
        "delivered": delivered,
        "execution_policy_version": execution_policy_version,
        "reconciled_at": reconciled_at,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if strategy_id == ALPHAOPS_V5_STRATEGY_ID and entry_intent is None:
        return (
            {
                **base,
                "observation_status": ("complete" if outcome is not None else "missing"),
                "terminal_state": "research_only_policy_blocked",
                "reconciliation_status": "resolved",
                "activated": None,
                "filled": False,
                "closed": False,
                "trade_return_eligible": False,
                "net_return_pct": None,
                "reason": (
                    "No durable AlphaOps v5 official-paper ENTER_LONG intent "
                    "exists; selection remains research-only."
                ),
            },
            None,
            [],
        )
    if outcome is None:
        return (
            {
                **base,
                "observation_status": "missing",
                "terminal_state": "unresolved_missing_outcome",
                "reconciliation_status": "unresolved",
                "activated": None,
                "trade_return_eligible": False,
                "net_return_pct": None,
                "reason": "No sourced terminal outcome exists for the selected signal.",
            },
            None,
            [],
        )
    outcome_status = str(outcome.get("outcome_status") or "")
    source_complete = bool(outcome.get("source_coverage_complete"))
    source_hash = str(outcome.get("source_bar_hash_sha256") or "")
    evidence = {
        **base,
        "observation_status": "complete" if source_complete else "incomplete",
        "outcome_status": outcome_status,
        "source": outcome.get("outcome_source") or outcome.get("source"),
        "source_url": outcome.get("source_url"),
        "source_bar_hash_sha256": source_hash,
        "source_bar_count": outcome.get("source_bar_count"),
        "source_coverage_complete": source_complete,
        "path_replay_id": outcome.get("path_replay_id"),
        "path_truth_status": outcome.get("path_truth_status"),
        "retrospective_research_eligible": outcome.get("retrospective_research_eligible", True),
        "prospective_promotion_eligible": outcome.get("prospective_promotion_eligible", False),
    }
    path_status = str(outcome.get("path_truth_status") or "")
    if path_status in {
        PathTruthStatus.ENTRY_BAR_AMBIGUOUS.value,
        PathTruthStatus.MISSING_BARS.value,
        PathTruthStatus.KNOWN_HALT_WINDOW.value,
        PathTruthStatus.CORPORATE_ACTION_UNRESOLVED.value,
        PathTruthStatus.SOURCE_CONFLICT.value,
        PathTruthStatus.DATA_INELIGIBLE.value,
    }:
        return (
            {
                **evidence,
                "terminal_state": path_status.lower(),
                "reconciliation_status": "unresolved",
                "activated": None,
                "filled": False,
                "closed": False,
                "trade_return_eligible": False,
                "net_return_pct": None,
                "reason": "Canonical path truth is not eligible for paper reconciliation.",
            },
            None,
            [],
        )
    if outcome_status == "not_triggered" and source_complete:
        if (
            classify_canonical_return_truth(
                outcome,
                decision=decision_context,
            )
            != CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED
        ):
            return (
                {
                    **evidence,
                    "terminal_state": "invalid_canonical_activation_truth",
                    "reconciliation_status": "invalid",
                    "activated": None,
                    "filled": False,
                    "closed": False,
                    "trade_return_eligible": False,
                    "net_return_pct": None,
                    "reason": "Not-triggered evidence is not bound to a canonical decision.",
                },
                None,
                [],
            )
        evaluation = {
            **evidence,
            "terminal_state": "not_triggered",
            "reconciliation_status": "resolved",
            "activated": False,
            "filled": False,
            "closed": False,
            "trade_return_eligible": False,
            "net_return_pct": None,
            "reason": "Complete sourced bars proved the saved trigger was never reached.",
        }
        return evaluation, None, [_activation_label(evaluation, value=False)]
    if outcome_status in {"captured_ineligible_missing_plan", "not_entered_plan_dislocated"}:
        evaluation = {
            **evidence,
            "terminal_state": outcome_status,
            "reconciliation_status": "resolved",
            "activated": True,
            "filled": False,
            "closed": False,
            "trade_return_eligible": False,
            "net_return_pct": None,
            "reason": str(outcome.get("notes") or outcome_status),
        }
        return evaluation, None, [_activation_label(evaluation, value=True)]
    if outcome_status != "complete_sourced" or not source_complete:
        return (
            {
                **evidence,
                "terminal_state": outcome_status or "unresolved_outcome",
                "reconciliation_status": "unresolved",
                "activated": None,
                "filled": False,
                "closed": False,
                "trade_return_eligible": False,
                "net_return_pct": None,
                "reason": "Outcome is not complete sourced execution evidence.",
            },
            None,
            [],
        )
    if (
        classify_canonical_return_truth(
            outcome,
            decision=decision_context,
        )
        != CURRENT_RETURN_TRUTH
    ):
        return (
            {
                **evidence,
                "terminal_state": "invalid_canonical_return_truth",
                "reconciliation_status": "invalid",
                "activated": True,
                "filled": False,
                "closed": False,
                "trade_return_eligible": False,
                "net_return_pct": None,
                "reason": "Return evidence is not authenticated canonical return truth.",
            },
            None,
            [],
        )
    trade = _paper_trade_from_outcome(
        base=base,
        signal=signal,
        outcome=outcome,
        notional_per_trade=notional_per_trade,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        entry_intent=entry_intent,
        execution_policy_version=execution_policy_version,
    )
    if trade is None:
        return (
            {
                **evidence,
                "terminal_state": "invalid_trade_geometry",
                "reconciliation_status": "invalid",
                "activated": True,
                "filled": False,
                "closed": False,
                "trade_return_eligible": False,
                "net_return_pct": None,
                "reason": "Sourced entry/exit evidence could not form a valid paper trade.",
            },
            None,
            [],
        )
    evaluation = {
        **evidence,
        "terminal_state": "filled_and_closed",
        "reconciliation_status": "resolved",
        "activated": True,
        "filled": True,
        "closed": True,
        "trade_return_eligible": True,
        "entry_time": trade["entry_time"],
        "entry_price": trade["entry_fill_price"],
        "exit_time": trade["exit_time"],
        "exit_price": trade["exit_fill_price"],
        "exit_reason": trade["exit_reason"],
        "net_pnl": trade["net_pnl"],
        "net_return_pct": trade["net_return_pct"],
        "r_multiple": trade["r_multiple"],
        "max_favorable_excursion_pct": trade["max_favorable_excursion_pct"],
        "max_adverse_excursion_pct": trade["max_adverse_excursion_pct"],
        "reason": "Paper trade reconstructed from complete sourced one-minute bars.",
    }
    return (
        evaluation,
        trade,
        [
            _activation_label(evaluation, value=True),
            _return_label(evaluation, trade),
        ],
    )


def _paper_trade_from_outcome(
    *,
    base: dict[str, Any],
    signal: dict[str, Any],
    outcome: dict[str, Any],
    notional_per_trade: float,
    fee_bps: float,
    slippage_bps: float,
    entry_intent: dict[str, Any] | None = None,
    execution_policy_version: str = EXECUTION_POLICY_VERSION,
) -> dict[str, Any] | None:
    v5_entry = entry_intent if base.get("strategy_id") == ALPHAOPS_V5_STRATEGY_ID else None
    cost = outcome.get("cost_receipt")
    if not isinstance(cost, dict):
        return None
    components = cost.get("components")
    if not isinstance(components, dict):
        return None
    raw_entry = _number(cost.get("raw_entry_price"))
    raw_exit = _number(cost.get("raw_exit_price"))
    entry_time = str(outcome.get("entry_time") or "")
    exit_time = str(outcome.get("exit_time") or "")
    exit_reason = {
        "TARGET": "target_1",
        "STOP": "invalidation",
        "TIMEOUT": "eod_close",
    }.get(str(outcome.get("path_event") or ""))
    stop = _number(outcome.get("stop_price"))
    if exit_reason is None:
        return None
    if not entry_time or not exit_time or raw_entry is None or raw_exit is None:
        return None
    if raw_entry <= 0 or raw_exit <= 0:
        return None
    if not _strictly_after(exit_time, entry_time):
        return None
    notional = _number(components.get("notional_per_trade"))
    entry_slippage = _number(components.get("entry_slippage_bps"))
    exit_slippage = _number(components.get("exit_slippage_bps"))
    applied_fee_bps = _number(components.get("fee_bps_per_side"))
    commission = _number(components.get("commission_per_share_per_side"))
    if None in {notional, entry_slippage, exit_slippage, applied_fee_bps, commission}:
        return None
    assert notional is not None
    assert entry_slippage is not None
    assert exit_slippage is not None
    assert applied_fee_bps is not None
    assert commission is not None
    entry_fill = raw_entry * (1.0 + entry_slippage / 10_000.0)
    exit_fill = raw_exit * (1.0 - exit_slippage / 10_000.0)
    quantity = notional / entry_fill
    fees = (
        entry_fill * quantity * applied_fee_bps / 10_000.0
        + exit_fill * quantity * applied_fee_bps / 10_000.0
        + quantity * commission * 2.0
    )
    applied_slippage_bps = exit_slippage
    gross_pnl = (raw_exit - raw_entry) * quantity
    fill_pnl = (exit_fill - entry_fill) * quantity
    net_pnl = fill_pnl - fees
    canonical_after_cost = _number(outcome.get("after_cost_return_pct"))
    if (
        canonical_after_cost is None
        or abs((net_pnl / notional) * 100.0 - canonical_after_cost) > 1e-9
    ):
        return None
    risk_amount = (entry_fill - stop) * quantity if stop is not None and stop < entry_fill else None
    return {
        **{field: outcome.get(field) for field in _CANONICAL_TRADE_TRUTH_FIELDS},
        "trade_id": _stable_id(
            "paper_trade",
            base["selection_id"],
            execution_policy_version,
        ),
        "selection_id": base["selection_id"],
        "signal_id": base["signal_id"],
        "scan_id": base["scan_id"],
        "market_date": base["market_date"],
        "ticker": base["ticker"],
        "strategy_id": base["strategy_id"],
        "strategy_version": base["strategy_version"],
        "cohort": base["cohort"],
        "direction": "long",
        "decision_time": base["decision_time"],
        "entry_time": entry_time,
        "raw_entry_price": round(raw_entry, 6),
        "entry_fill_price": round(entry_fill, 6),
        "exit_time": exit_time,
        "raw_exit_price": round(raw_exit, 6),
        "exit_fill_price": round(exit_fill, 6),
        "exit_reason": exit_reason,
        "quantity": round(quantity, 8),
        "notional": round(notional, 4),
        "gross_pnl": round(gross_pnl, 4),
        "gross_return_pct": outcome.get("gross_return_pct"),
        "slippage_cost": round(gross_pnl - fill_pnl, 4),
        "fees": round(fees, 4),
        "net_pnl": round(notional * canonical_after_cost / 100.0, 4),
        "net_return_pct": canonical_after_cost,
        "risk_amount": round(risk_amount, 4) if risk_amount and risk_amount > 0 else None,
        "r_multiple": round(net_pnl / risk_amount, 4) if risk_amount and risk_amount > 0 else None,
        "max_favorable_excursion_pct": _number(outcome.get("mfe_pct")),
        "max_adverse_excursion_pct": _number(outcome.get("mae_pct")),
        "source": outcome.get("outcome_source") or outcome.get("source"),
        "source_url": outcome.get("source_url"),
        "source_bar_hash_sha256": outcome.get("source_bar_hash_sha256"),
        "source_bar_count": outcome.get("source_bar_count"),
        "execution_policy_version": execution_policy_version,
        "account_id": (v5_entry.get("account_id") if v5_entry else None),
        "decision_fingerprint": (v5_entry.get("decision_fingerprint") if v5_entry else None),
        "slippage_bps": applied_slippage_bps,
        "fee_bps": applied_fee_bps,
        "commission_per_share_per_side": (commission),
        "reconstruction_mode": "sourced_eod_one_minute_replay",
        "same_bar_policy": "stop_first_conservative",
        "created_at": base["reconciled_at"],
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _activation_label(evaluation: dict[str, Any], *, value: bool) -> dict[str, Any]:
    return {
        "label_id": _stable_id("learning", evaluation["evaluation_id"], "activation"),
        "evaluation_id": evaluation["evaluation_id"],
        "signal_id": evaluation["signal_id"],
        "market_date": evaluation["market_date"],
        "ticker": evaluation["ticker"],
        "strategy_id": evaluation["strategy_id"],
        "strategy_version": evaluation["strategy_version"],
        "cohort": evaluation["cohort"],
        "label_family": "activation",
        "label_value": 1.0 if value else 0.0,
        "eligible": True,
        "exclusion_reason": "",
        "source_bar_hash_sha256": evaluation.get("source_bar_hash_sha256"),
        "created_at": evaluation["reconciled_at"],
    }


def _return_label(evaluation: dict[str, Any], trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "label_id": _stable_id("learning", evaluation["evaluation_id"], "trade_return"),
        "evaluation_id": evaluation["evaluation_id"],
        "signal_id": evaluation["signal_id"],
        "market_date": evaluation["market_date"],
        "ticker": evaluation["ticker"],
        "strategy_id": evaluation["strategy_id"],
        "strategy_version": evaluation["strategy_version"],
        "cohort": evaluation["cohort"],
        "label_family": "trade_return",
        "label_value": trade["net_return_pct"],
        "r_multiple": trade["r_multiple"],
        "eligible": True,
        "exclusion_reason": "",
        "source_bar_hash_sha256": trade.get("source_bar_hash_sha256"),
        "created_at": evaluation["reconciled_at"],
    }


def _daily_scorecards(
    *,
    market_date: str,
    selections: list[dict[str, Any]],
    session_selections: list[dict[str, Any]],
    deliveries: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    reconciled_at: str,
    strategy_id: str = LEGACY_ALPHAOPS_STRATEGY_ID,
    strategy_version: str = ALPHAOPS_STRATEGY_VERSION,
    execution_policy_version: str = EXECUTION_POLICY_VERSION,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    no_trade_selections = [
        row
        for row in session_selections
        if str(row.get("decision") or "").lower() == "no_trade"
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
    ]
    delivered_selection_ids = {
        str(row.get("selection_id") or "")
        for row in deliveries
        if _is_official_telegram_delivery(row, strategy_id=strategy_id)
    }
    session_status = (
        "selected"
        if selections
        else "explicit_no_trade"
        if no_trade_selections
        else "missing_selection_evidence"
    )
    for cohort, official_only in ((SELECTED_COHORT, False), (DELIVERED_COHORT, True)):
        cohort_evaluations = [
            row for row in evaluations if row.get("delivered") or not official_only
        ]
        signal_ids = {str(row.get("signal_id") or "") for row in cohort_evaluations}
        cohort_trades = [row for row in trades if str(row.get("signal_id") or "") in signal_ids]
        returns = [float(row["net_return_pct"]) for row in cohort_trades]
        pnls = [float(row["net_pnl"]) for row in cohort_trades]
        r_values = [
            float(row["r_multiple"]) for row in cohort_trades if row.get("r_multiple") is not None
        ]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        unresolved = [
            row
            for row in cohort_evaluations
            if row.get("reconciliation_status") in {"unresolved", "invalid"}
        ]
        selected_count = len(selections) if not official_only else len(cohort_evaluations)
        no_trade_count = (
            len(no_trade_selections)
            if not official_only
            else sum(
                1
                for selection in no_trade_selections
                if str(selection.get("selection_id") or "") in delivered_selection_ids
            )
        )
        row = {
            "scorecard_id": _stable_id(
                "scorecard",
                market_date,
                strategy_id,
                strategy_version,
                cohort,
                execution_policy_version,
            ),
            "market_date": market_date,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "cohort": cohort,
            "execution_policy_version": execution_policy_version,
            "selected_count": selected_count,
            "delivered_count": (
                sum(1 for row in cohort_evaluations if row.get("delivered")) + no_trade_count
            ),
            "no_trade_count": no_trade_count,
            "session_status": session_status,
            "resolved_count": len(cohort_evaluations) - len(unresolved),
            "triggered_count": sum(1 for row in cohort_evaluations if row.get("activated")),
            "not_triggered_count": sum(
                1 for row in cohort_evaluations if row.get("terminal_state") == "not_triggered"
            ),
            "filled_count": sum(1 for row in cohort_evaluations if row.get("filled")),
            "closed_count": len(cohort_trades),
            "unresolved_count": len(unresolved),
            "wins": len(wins),
            "losses": len(losses),
            "flats": sum(1 for value in pnls if value == 0),
            "activation_rate_pct": _ratio_pct(
                sum(1 for row in cohort_evaluations if row.get("activated")),
                len([row for row in cohort_evaluations if row.get("activated") is not None]),
            ),
            "win_rate_pct": _ratio_pct(len(wins), len(cohort_trades)),
            "average_net_return_pct": _average(returns),
            "net_pnl": round(sum(pnls), 4),
            "return_on_allocated_capital_pct": (
                round(
                    (
                        sum(pnls)
                        / sum(float(trade.get("notional") or 0.0) for trade in cohort_trades)
                    )
                    * 100.0,
                    4,
                )
                if cohort_trades
                and sum(float(trade.get("notional") or 0.0) for trade in cohort_trades) > 0
                else None
            ),
            "average_r": _average(r_values),
            "expectancy_r": _average(r_values),
            "profit_factor": (
                round(sum(wins) / abs(sum(losses)), 4)
                if wins and losses and sum(losses) != 0
                else None
            ),
            "fees": round(sum(float(row.get("fees") or 0.0) for row in cohort_trades), 4),
            "slippage_cost": round(
                sum(float(row.get("slippage_cost") or 0.0) for row in cohort_trades), 4
            ),
            "trade_return_note": (
                "N/A when no paper position was entered; no-entry is never converted to 0%."
            ),
            "reconciliation_status": (
                "complete"
                if session_status != "missing_selection_evidence" and not unresolved
                else "failed"
            ),
            "created_at": reconciled_at,
            "research_only": True,
        }
        rows.append(row)
    return rows


def _delivery_by_signal(
    rows: Iterable[dict[str, Any]],
    *,
    strategy_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    official_rows = [
        row for row in rows if _is_official_telegram_delivery(row, strategy_id=strategy_id)
    ]
    for row in sorted(
        official_rows,
        key=lambda item: str(item.get("delivered_at") or item.get("attempted_at") or ""),
    ):
        signal_id = str(row.get("signal_id") or "")
        if signal_id:
            output[signal_id] = row
    return output


def _write_artifacts(
    output_dir: Path,
    **payload: Any,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "reconciliation.json"
    evaluations_path = output_dir / "strategy_evaluations.csv"
    trades_path = output_dir / "paper_trades.csv"
    labels_path = output_dir / "learning_labels.csv"
    scorecards_path = output_dir / "daily_strategy_scorecards.csv"
    report_path = output_dir / "strategy_report.md"
    summary_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_csv(evaluations_path, list(payload["evaluations"]))
    _write_csv(trades_path, list(payload["trades"]))
    _write_csv(labels_path, list(payload["labels"]))
    _write_csv(scorecards_path, list(payload["scorecards"]))
    report_path.write_text(_report_markdown(payload), encoding="utf-8")
    return {
        "summary": str(summary_path),
        "evaluations": str(evaluations_path),
        "paper_trades": str(trades_path),
        "learning_labels": str(labels_path),
        "scorecards": str(scorecards_path),
        "report": str(report_path),
    }


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# AlphaOps Paper Reconciliation",
        "",
        f"- Market date: `{payload['market_date']}`",
        f"- Status: `{payload['status']}`",
        f"- Exact selections: `{len(payload['selections'])}`",
        f"- Closed paper trades: `{len(payload['trades'])}`",
        f"- Unresolved: `{len(payload['unresolved'])}`",
        f"- Invalid: `{len(payload['invalid'])}`",
        "- Execution: paper research only; no broker order placement exists.",
        "",
        "## Strategy scorecards",
        "",
    ]
    for row in payload["scorecards"]:
        return_text = (
            f"{float(row['average_net_return_pct']):+.4f}%"
            if row.get("average_net_return_pct") is not None
            else "N/A"
        )
        lines.append(
            f"- `{row['cohort']}`: selected {row['selected_count']}, delivered "
            f"{row['delivered_count']}, triggered {row['triggered_count']}, closed "
            f"{row['closed_count']}, not triggered {row['not_triggered_count']}, "
            f"average closed-trade return {return_text}."
        )
    lines.extend(
        [
            "",
            "A conclusive no-trigger is resolved evidence with trade return N/A. "
            "It is not a loss and is never written as 0%.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    if not fields:
        fields = ["status"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _stable_id(*parts: object) -> str:
    basis = ":".join(str(part) for part in parts)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        number = float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return number


def _strictly_after(later: str, earlier: str) -> bool:
    try:
        later_at = datetime.fromisoformat(later.replace("Z", "+00:00"))
        earlier_at = datetime.fromisoformat(earlier.replace("Z", "+00:00"))
    except ValueError:
        return False
    if later_at.tzinfo is None or earlier_at.tzinfo is None:
        return later > earlier
    return later_at > earlier_at


def _return_pct(price: float | None, entry: float | None) -> float | None:
    if price is None or entry is None or entry <= 0:
        return None
    return round(((price - entry) / entry) * 100.0, 4)


def _ratio_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100.0, 4)


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None
