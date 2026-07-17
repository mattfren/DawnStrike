"""Exact AlphaOps Telegram-cohort and paper-only EOD reconciliation.

This module is intentionally narrower than the general PaperOps engine.  Its
only eligible universe is the compact AlphaOps Telegram body that was actually
delivered.  It never places an order, calls a broker, or treats absent market
truth as a zero return.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.errors import StorageError
from intraday_scanner.market_calendar import MARKET_TIMEZONE, market_session
from intraday_scanner.models import utc_now_iso
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

ALPHAOPS_STRATEGY_ID = "alphaops_intraday"
ALPHAOPS_STRATEGY_VERSION = "dawnstrike-alphaops-v4"
ALPHAOPS_COHORT = "official_telegram"
ALPHAOPS_EXECUTION_POLICY = "alphaops_intraday_first_touch_v1"
ALPHAOPS_BAR_SEMANTICS = "bar_close"
ALPHAOPS_BAR_TIMEFRAME = "1min"
DELIVERED_STATUS = "delivered"

_PRICE_QUANT = Decimal("0.000001")
_MONEY_QUANT = Decimal("0.000001")
_PCT_QUANT = Decimal("0.000001")


@dataclass(frozen=True)
class SourceBars:
    bars_by_symbol: dict[str, tuple[dict[str, Any], ...]]
    raw_sha256: str
    upstream_raw_sha256: str
    normalized_sha256: str
    source_path: str
    source_paths: tuple[str, ...]
    selected_artifact_bytes: bytes
    expected_bar_count_per_symbol: int
    session_open_et: str
    session_close_et: str


def freeze_alpha_telegram_cohort(
    store: SQLiteScanStore,
    *,
    scan_id: str,
    selected_at: str,
    event_key: str,
    body: str,
    rendered_rows: Sequence[Mapping[str, Any]],
    no_trade_row: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Freeze the exact rows rendered in one AlphaOps Telegram message.

    ``rendered_rows`` must already come from the formatter's shared selection
    function.  When there are no rendered picks, an explicit historical
    ``NO_TRADE`` identity is required; empty evidence is never inferred to be a
    no-trade decision.
    """

    scan_id = scan_id.strip()
    event_key = event_key.strip()
    if not scan_id or not event_key or not body:
        raise StorageError("AlphaOps cohort requires scan, event, and body truth")
    selected = _parse_aware(selected_at, "selected_at")
    body_sha256 = _sha256_bytes(body.encode("utf-8"))
    rows = [dict(row) for row in rendered_rows]
    if len(rows) > 3:
        raise StorageError("AlphaOps official Telegram cohort cannot exceed three picks")
    if not rows:
        if no_trade_row is None:
            raise StorageError("An empty AlphaOps message requires an explicit NO_TRADE row")
        sentinel = dict(no_trade_row)
        if str(sentinel.get("ticker") or "").upper() != "NO_TRADE":
            raise StorageError("AlphaOps no-trade identity must use ticker NO_TRADE")
        rows = [sentinel]

    definition = {
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": ALPHAOPS_COHORT,
        "execution_policy_version": ALPHAOPS_EXECUTION_POLICY,
        "research_only": True,
        "broker_execution_enabled": False,
        "max_rendered_picks": 3,
    }
    store.persist_strategy_versions(
        [
            {
                **definition,
                "registered_at": selected.isoformat(),
                "definition": definition,
            }
        ]
    )

    selections: list[dict[str, Any]] = []
    for index, signal in enumerate(rows, start=1):
        ticker = str(signal.get("ticker") or "").upper().strip()
        signal_id = str(
            signal.get("signal_id")
            or signal.get("signal_key")
            or (
                f"no_trade:{scan_id}:{selected.astimezone(MARKET_TIMEZONE).date()}"
                if ticker == "NO_TRADE"
                else ""
            )
        ).strip()
        if not ticker or not signal_id:
            raise StorageError("Every rendered AlphaOps row requires ticker and signal identity")
        decision = "no_trade" if ticker == "NO_TRADE" else "selected"
        rank = 0 if decision == "no_trade" else _int(signal.get("rank"), index)
        signal_snapshot_sha256 = _canonical_sha256(signal)
        identity = _identity(
            "alpha-selection",
            ALPHAOPS_STRATEGY_ID,
            ALPHAOPS_STRATEGY_VERSION,
            ALPHAOPS_COHORT,
            scan_id,
            signal_id,
        )
        selections.append(
            {
                "selection_id": identity,
                "scan_id": scan_id,
                "signal_id": signal_id,
                "ticker": ticker,
                "rank": rank,
                "strategy_id": ALPHAOPS_STRATEGY_ID,
                "strategy_version": ALPHAOPS_STRATEGY_VERSION,
                "cohort": ALPHAOPS_COHORT,
                "decision": decision,
                "selected_at": selected.isoformat(),
                "event_key": event_key,
                "body_sha256": body_sha256,
                "market_date": selected.astimezone(MARKET_TIMEZONE).date().isoformat(),
                "signal_snapshot": signal,
                "signal_snapshot_sha256": signal_snapshot_sha256,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        )

    market_date = selected.astimezone(MARKET_TIMEZONE).date().isoformat()
    membership_sha256 = _membership_sha256(selections)
    store.persist_official_signal_cohort(
        {
            "official_cohort_id": _identity(
                "alpha-official-cohort",
                market_date,
                ALPHAOPS_STRATEGY_ID,
                ALPHAOPS_STRATEGY_VERSION,
                ALPHAOPS_COHORT,
            ),
            "market_date": market_date,
            "strategy_id": ALPHAOPS_STRATEGY_ID,
            "strategy_version": ALPHAOPS_STRATEGY_VERSION,
            "cohort": ALPHAOPS_COHORT,
            "scan_id": scan_id,
            "event_key": event_key,
            "body_sha256": body_sha256,
            "membership_sha256": membership_sha256,
            "claimed_at": selected.isoformat(),
            "research_only": True,
            "broker_execution_enabled": False,
        },
        selections,
    )
    persisted = store.load_signal_selections(
        scan_id=scan_id,
        strategy_id=ALPHAOPS_STRATEGY_ID,
        cohort=ALPHAOPS_COHORT,
        limit=20,
    )
    if _selection_contract(persisted) != _selection_contract(selections):
        raise StorageError("AlphaOps exact Telegram cohort did not persist completely")
    return selections


def classify_alpha_telegram_delivery(
    store: SQLiteScanStore,
    *,
    event_key: str,
    body: str,
    dry_run: bool,
    notification_stats: Mapping[str, Any] | None,
) -> str:
    """Classify one dispatch without upgrading dry-runs or failures."""

    return str(
        alpha_telegram_delivery_proof(
            store,
            event_key=event_key,
            body=body,
            dry_run=dry_run,
            notification_stats=notification_stats,
        )["status"]
    )


def alpha_telegram_delivery_proof(
    store: SQLiteScanStore,
    *,
    event_key: str,
    body: str,
    dry_run: bool,
    notification_stats: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return verified final Telegram transport evidence for one Alpha message."""

    if dry_run:
        return {"status": "dry_run", "transport_receipt": None}
    notification = store.load_notification(f"{event_key}:telegram")
    if notification is None:
        return {"status": "failed", "transport_receipt": None}
    if bool(notification.get("dry_run")):
        return {"status": "failed", "transport_receipt": None}
    stats = dict(notification_stats or {})
    deliveries = [
        dict(row)
        for row in stats.get("deliveries") or []
        if isinstance(row, Mapping)
        and str(row.get("channel") or "") == "telegram"
        and str(row.get("event_key") or "") == event_key
    ]
    if len(deliveries) != 1:
        return {"status": "failed", "transport_receipt": None}
    delivery = deliveries[0]
    if str(delivery.get("status") or "") not in {"delivered", "duplicate_suppressed"}:
        return {"status": "failed", "transport_receipt": None}
    receipt = delivery.get("transport_receipt")
    if not isinstance(receipt, Mapping):
        return {"status": "failed", "transport_receipt": None}
    verified = _validated_transport_receipt(receipt, expected_body=body)
    persisted_receipt = notification.get("transport_receipt")
    if not isinstance(persisted_receipt, Mapping):
        return {"status": "failed", "transport_receipt": None}
    if _canonical_sha256(dict(persisted_receipt)) != _canonical_sha256(verified):
        raise StorageError("Persisted Telegram receipt conflicts with dispatch evidence")
    return {"status": DELIVERED_STATUS, "transport_receipt": verified}


def persist_alpha_telegram_delivery(
    store: SQLiteScanStore,
    *,
    selections: Sequence[Mapping[str, Any]],
    delivery_status: str,
    transport_receipt: Mapping[str, Any] | None = None,
    attempted_at: str | None = None,
) -> dict[str, int]:
    """Persist one exact delivery membership per rendered signal."""

    if delivery_status not in {DELIVERED_STATUS, "dry_run", "failed"}:
        raise StorageError(f"Unsupported AlphaOps delivery status: {delivery_status}")
    verified_receipt: dict[str, Any] | None = None
    if delivery_status == DELIVERED_STATUS:
        if not selections:
            raise StorageError("Delivered AlphaOps membership cannot be empty")
        if not isinstance(transport_receipt, Mapping):
            raise StorageError("Delivered AlphaOps membership requires a Telegram receipt")
        expected_hashes = {str(row.get("body_sha256") or "") for row in selections}
        if len(expected_hashes) != 1:
            raise StorageError("Delivered AlphaOps selections have conflicting body hashes")
        verified_receipt = _validated_transport_receipt(
            transport_receipt,
            expected_sha256=next(iter(expected_hashes)),
        )
    attempt = _parse_aware(attempted_at or utc_now_iso(), "attempted_at")
    rows: list[dict[str, Any]] = []
    for raw in selections:
        selection = dict(raw)
        membership_id = _identity(
            "alpha-membership",
            str(selection.get("event_key") or ""),
            "telegram",
            str(selection.get("signal_id") or ""),
        )
        rows.append(
            {
                **selection,
                "membership_id": membership_id,
                "selection_id": str(selection.get("selection_id") or ""),
                "channel": "telegram",
                "delivery_status": delivery_status,
                "attempted_at": attempt.isoformat(),
                "delivered_at": attempt.isoformat() if delivery_status == DELIVERED_STATUS else "",
                "delivery_is_real": delivery_status == DELIVERED_STATUS,
                "notification_dry_run": delivery_status == "dry_run",
                "transport_receipt": verified_receipt,
                "transmitted_bytes_sha256": (
                    verified_receipt.get("transmitted_bytes_sha256")
                    if verified_receipt
                    else None
                ),
                "telegram_message_id": (
                    verified_receipt.get("message_id") if verified_receipt else None
                ),
            }
        )
    stats = store.persist_notification_deliveries(rows)
    if delivery_status == DELIVERED_STATUS:
        for persisted_selection in selections:
            matches = store.load_notification_deliveries(
                event_key=str(persisted_selection.get("event_key") or ""),
                signal_id=str(persisted_selection.get("signal_id") or ""),
                channel="telegram",
                cohort=ALPHAOPS_COHORT,
                limit=5,
            )
            if len(matches) != 1 or matches[0].get("delivery_status") != DELIVERED_STATUS:
                raise StorageError("Real AlphaOps Telegram membership was not durably recorded")
    return stats


def alpha_paper_reconcile(
    *,
    db_path: str | Path,
    market_date: str,
    bars_csv: str | Path | None,
    out_dir: str | Path = "outputs/strategy_reconciliation",
    persist: bool = False,
    slippage_bps: float = 5.0,
    fee_bps: float = 0.0,
    notional_per_trade: float = 1000.0,
    bar_timestamp_semantics: str = ALPHAOPS_BAR_SEMANTICS,
    reconciled_at: str | None = None,
) -> dict[str, Any]:
    """Rebuild the authoritative paper lifecycle for one delivered cohort.

    Exit semantics are returned in ``exit_code``: 0 complete, 2 blocked or
    incomplete, and 1 is reserved by the CLI for operational exceptions.
    """

    try:
        run_date = date.fromisoformat(market_date)
    except ValueError:
        return _blocked_result(market_date, ["market_date must use YYYY-MM-DD"])
    session = market_session(run_date)
    if not session.is_trading_day:
        return _blocked_result(market_date, ["requested date is not a trading session"])
    if bar_timestamp_semantics != ALPHAOPS_BAR_SEMANTICS:
        return _blocked_result(
            market_date,
            ["AlphaOps EOD requires bar_timestamp_semantics=bar_close"],
        )
    if slippage_bps < 0 or fee_bps < 0 or notional_per_trade <= 0:
        return _blocked_result(
            market_date,
            ["slippage/fee bps must be non-negative and notional must be positive"],
        )

    store = SQLiteScanStore(db_path)
    store.initialize()
    selections, cohort_blockers = _load_exact_delivered_cohort(store, run_date)
    if cohort_blockers:
        return _write_reconciliation_result(
            out_dir,
            _blocked_result(market_date, cohort_blockers),
        )

    no_trade = len(selections) == 1 and selections[0].get("decision") == "no_trade"
    source: SourceBars | None = None
    if not no_trade:
        expected_symbols = {
            str(row.get("ticker") or "").upper()
            for row in selections
            if row.get("decision") == "selected"
        }
        if bars_csv is None:
            return _write_reconciliation_result(
                out_dir,
                _blocked_result(
                    market_date,
                    ["complete sourced one-minute RTH bars are required"],
                ),
            )
        try:
            source = _resolve_complete_rth_bars(
                Path(bars_csv),
                run_date,
                session.close_time_et,
                expected_symbols=expected_symbols,
            )
        except (OSError, StorageError, ValueError) as exc:
            return _write_reconciliation_result(
                out_dir,
                _blocked_result(market_date, [str(exc)]),
            )

    retained_source = ""
    if source is not None:
        try:
            retained_source = _retain_source_artifact(
                Path(out_dir),
                source,
            )
        except StorageError as exc:
            return _write_reconciliation_result(
                out_dir,
                _blocked_result(market_date, [str(exc)]),
            )
        source = SourceBars(
            bars_by_symbol=source.bars_by_symbol,
            raw_sha256=source.raw_sha256,
            upstream_raw_sha256=source.upstream_raw_sha256,
            normalized_sha256=source.normalized_sha256,
            source_path=retained_source,
            source_paths=source.source_paths,
            selected_artifact_bytes=source.selected_artifact_bytes,
            expected_bar_count_per_symbol=source.expected_bar_count_per_symbol,
            session_open_et=source.session_open_et,
            session_close_et=source.session_close_et,
        )

    now = _parse_aware(reconciled_at or utc_now_iso(), "reconciled_at").isoformat()
    if no_trade:
        evaluations: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        labels: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
    else:
        assert source is not None
        evaluations = []
        trades = []
        labels = []
        outcomes = []
        events = []
        for selection in selections:
            evaluation, trade, row_labels, outcome, event = _evaluate_selection(
                selection=selection,
                bars=source.bars_by_symbol[str(selection["ticker"])],
                source=source,
                market_date=market_date,
                reconciled_at=now,
                slippage_bps=slippage_bps,
                fee_bps=fee_bps,
                notional_per_trade=notional_per_trade,
            )
            evaluations.append(evaluation)
            labels.extend(row_labels)
            if trade is not None:
                trades.append(trade)
            if outcome is not None:
                outcomes.append(outcome)
            if event is not None:
                events.append(event)

    scorecard = _build_scorecard(
        market_date=market_date,
        selections=selections,
        evaluations=evaluations,
        trades=trades,
        source=source,
        reconciled_at=now,
        no_trade=no_trade,
    )
    complete = scorecard["reconciliation_status"] == "complete"
    persistence: dict[str, Any] = {}
    if persist:
        persistence["strategy_reconciliation"] = store.persist_strategy_reconciliation(
            evaluations=evaluations,
            paper_trades=trades,
            learning_labels=labels,
            scorecards=[scorecard],
            immutable=True,
        )
        persistence["signal_outcomes"] = store.persist_signal_outcomes_with_events(
            outcomes,
            events,
            immutable=True,
        )

    result: dict[str, Any] = {
        "status": "complete" if complete else "incomplete",
        "exit_code": 0 if complete else 2,
        "market_date": market_date,
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": ALPHAOPS_COHORT,
        "execution_policy_version": ALPHAOPS_EXECUTION_POLICY,
        "selection_count": len(selections),
        "evaluation_count": len(evaluations),
        "trade_count": len(trades),
        "label_count": len(labels),
        "outcome_count": len(outcomes),
        "no_trade": no_trade,
        "source_bar_raw_sha256": source.raw_sha256 if source else None,
        "source_bar_upstream_raw_sha256": source.upstream_raw_sha256 if source else None,
        "source_bar_normalized_sha256": source.normalized_sha256 if source else None,
        "retained_source_path": retained_source or None,
        "scorecard": scorecard,
        "evaluations": evaluations,
        "paper_trades": trades,
        "learning_labels": labels,
        "persistence": persistence,
        "research_only": True,
        "broker_execution_enabled": False,
        "blocked_reasons": [],
    }
    return _write_reconciliation_result(out_dir, result)


def alpha_reconciliation_gate(
    store: SQLiteScanStore,
    *,
    market_date: str,
) -> tuple[bool, str]:
    """Return whether one exact AlphaOps day is safe to consume for learning."""

    rows = store.load_daily_strategy_scorecards(
        start=market_date,
        end=market_date,
        strategy_id=ALPHAOPS_STRATEGY_ID,
        cohort=ALPHAOPS_COHORT,
        limit=10,
    )
    exact = [
        row
        for row in rows
        if str(row.get("strategy_version") or "") == ALPHAOPS_STRATEGY_VERSION
        and str(row.get("execution_policy_version") or "") == ALPHAOPS_EXECUTION_POLICY
    ]
    if len(exact) != 1:
        return False, "exact AlphaOps reconciliation scorecard is missing or ambiguous"
    if str(exact[0].get("reconciliation_status") or "") != "complete":
        return False, "AlphaOps reconciliation is incomplete"
    return True, "complete"


def load_alpha_official_delivered_cohort(
    store: SQLiteScanStore,
    *,
    market_date: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Load one date's exact, receipt-proven official AlphaOps cohort."""

    return _load_exact_delivered_cohort(store, market_date)


def _load_exact_delivered_cohort(
    store: SQLiteScanStore,
    market_date: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    blockers: list[str] = []
    all_selections = store.load_signal_selections(cohort=ALPHAOPS_COHORT, limit=50_000)
    dated: list[dict[str, Any]] = []
    for row in all_selections:
        try:
            row_date = _parse_aware(str(row.get("selected_at") or ""), "selected_at").astimezone(
                MARKET_TIMEZONE
            ).date()
        except (StorageError, ValueError) as exc:
            blockers.append(str(exc))
            continue
        if row_date == market_date:
            dated.append(row)
    if not dated:
        return [], ["no frozen official Telegram cohort exists for the requested date"]
    official = store.load_official_strategy_cohort(
        market_date=market_date.isoformat(),
        strategy_id=ALPHAOPS_STRATEGY_ID,
        strategy_version=ALPHAOPS_STRATEGY_VERSION,
        cohort=ALPHAOPS_COHORT,
    )
    if official is None:
        return [], ["date-level official AlphaOps cohort lock is absent"]
    foreign = [
        row
        for row in dated
        if row.get("strategy_id") != ALPHAOPS_STRATEGY_ID
        or row.get("strategy_version") != ALPHAOPS_STRATEGY_VERSION
    ]
    if foreign:
        blockers.append("official Telegram date contains conflicting strategy lineage")
    selections = [
        row
        for row in dated
        if row.get("strategy_id") == ALPHAOPS_STRATEGY_ID
        and row.get("strategy_version") == ALPHAOPS_STRATEGY_VERSION
        and row.get("scan_id") == official.get("scan_id")
        and row.get("event_key") == official.get("event_key")
        and row.get("body_sha256") == official.get("body_sha256")
    ]
    if len(selections) != len(dated):
        blockers.append("market date contains selections outside the official cohort lock")
    if selections and _membership_sha256(selections) != official.get("membership_sha256"):
        blockers.append("official AlphaOps membership SHA-256 fails recomputation")
    if len({str(row.get("scan_id") or "") for row in selections}) != 1:
        blockers.append("official Telegram date must resolve to exactly one morning scan")
    if len({str(row.get("event_key") or "") for row in selections}) != 1:
        blockers.append("official Telegram cohort must share exactly one message event")
    if len({str(row.get("body_sha256") or "") for row in selections}) != 1:
        blockers.append("official Telegram cohort contains conflicting body hashes")

    delivery_rows = store.load_notification_deliveries(
        channel="telegram",
        cohort=ALPHAOPS_COHORT,
        limit=50_000,
    )
    if selections:
        event_key = str(selections[0].get("event_key") or "")
        scan_id = str(selections[0].get("scan_id") or "")
        relevant_deliveries = [
            row
            for row in delivery_rows
            if row.get("event_key") == event_key
            and row.get("scan_id") == scan_id
            and row.get("strategy_id") == ALPHAOPS_STRATEGY_ID
            and row.get("strategy_version") == ALPHAOPS_STRATEGY_VERSION
        ]
        expected_membership = {
            (str(row.get("selection_id") or ""), str(row.get("signal_id") or ""))
            for row in selections
        }
        actual_membership = {
            (str(row.get("selection_id") or ""), str(row.get("signal_id") or ""))
            for row in relevant_deliveries
        }
        if actual_membership != expected_membership:
            blockers.append(
                "Telegram delivery membership set does not exactly match frozen selections"
            )
    by_signal: dict[str, list[dict[str, Any]]] = {}
    for delivery in delivery_rows:
        by_signal.setdefault(str(delivery.get("signal_id") or ""), []).append(delivery)
    for selection in selections:
        snapshot = _signal_snapshot(selection)
        payload = selection.get("payload_json")
        expected_snapshot_sha = (
            str(payload.get("signal_snapshot_sha256") or "")
            if isinstance(payload, Mapping)
            else str(selection.get("signal_snapshot_sha256") or "")
        )
        if not expected_snapshot_sha or _canonical_sha256(snapshot) != expected_snapshot_sha:
            blockers.append(f"{selection.get('ticker')}: frozen signal snapshot hash fails")
        matches = [
            row
            for row in by_signal.get(str(selection.get("signal_id") or ""), [])
            if row.get("selection_id") == selection.get("selection_id")
            and row.get("event_key") == selection.get("event_key")
        ]
        if len(matches) != 1:
            blockers.append(
                f"{selection.get('ticker')}: exact Telegram delivery membership is missing"
            )
            continue
        delivery = matches[0]
        if delivery.get("delivery_status") != DELIVERED_STATUS:
            blockers.append(
                f"{selection.get('ticker')}: Telegram membership is not a proven real delivery"
            )
        if delivery.get("body_sha256") != selection.get("body_sha256"):
            blockers.append(f"{selection.get('ticker')}: delivery body hash conflicts")
        receipt = delivery.get("payload_json")
        receipt = receipt.get("transport_receipt") if isinstance(receipt, Mapping) else None
        try:
            if not isinstance(receipt, Mapping):
                raise StorageError("Telegram transport receipt is absent")
            _validated_transport_receipt(
                receipt,
                expected_sha256=str(selection.get("body_sha256") or ""),
            )
        except StorageError as exc:
            blockers.append(f"{selection.get('ticker')}: {exc}")

    if selections:
        event_key = str(selections[0].get("event_key") or "")
        notification = store.load_notification(f"{event_key}:telegram")
        if notification is None or notification.get("dry_run"):
            blockers.append("real Telegram notification record is absent")
        else:
            receipt = notification.get("transport_receipt")
            try:
                if not isinstance(receipt, Mapping):
                    raise StorageError("persisted Telegram transport receipt is absent")
                _validated_transport_receipt(
                    receipt,
                    expected_sha256=str(selections[0].get("body_sha256") or ""),
                )
            except StorageError as exc:
                blockers.append(str(exc))

    no_trade = [row for row in selections if row.get("decision") == "no_trade"]
    selected = [row for row in selections if row.get("decision") == "selected"]
    if no_trade and (len(no_trade) != 1 or selected):
        blockers.append("NO_TRADE cannot coexist with delivered picks")
    if not no_trade and not selected:
        blockers.append("delivered cohort has neither picks nor an explicit NO_TRADE identity")
    if len(selected) > 3:
        blockers.append("delivered AlphaOps cohort exceeds Telegram's three-pick limit")
    tickers = [str(row.get("ticker") or "") for row in selected]
    if len(tickers) != len(set(tickers)):
        blockers.append("delivered AlphaOps cohort contains duplicate tickers")
    return sorted(selections, key=lambda row: (_int(row.get("rank"), 999), row["ticker"])), blockers


def _resolve_complete_rth_bars(
    path: Path,
    market_date: date,
    close_time_et: str | None,
    *,
    expected_symbols: set[str],
) -> SourceBars:
    """Resolve either one aggregate file or canonical per-symbol artifacts."""

    normalized_symbols = {symbol.upper().strip() for symbol in expected_symbols if symbol.strip()}
    if not normalized_symbols:
        raise ValueError("delivered AlphaOps cohort has no symbols to reconcile")
    if path.is_file():
        return _load_complete_rth_bars(
            path,
            market_date,
            close_time_et,
            expected_symbols=normalized_symbols,
        )
    if not path.is_dir():
        raise ValueError(f"source bars do not exist: {path}")
    aggregate = path / f"{market_date.isoformat()}_canonical_intraday.csv"
    if aggregate.is_file():
        return _load_complete_rth_bars(
            aggregate,
            market_date,
            close_time_et,
            expected_symbols=normalized_symbols,
        )

    symbol_sources: list[SourceBars] = []
    missing_paths: list[str] = []
    for symbol in sorted(normalized_symbols):
        candidate = path / symbol / f"{market_date.isoformat()}_canonical_intraday.csv"
        if not candidate.is_file():
            missing_paths.append(str(candidate))
            continue
        symbol_sources.append(
            _load_complete_rth_bars(
                candidate,
                market_date,
                close_time_et,
                expected_symbols={symbol},
            )
        )
    if missing_paths:
        raise ValueError(
            "missing canonical per-symbol RTH artifact(s): " + ", ".join(missing_paths)
        )
    combined = {
        symbol: source.bars_by_symbol[symbol]
        for source in symbol_sources
        for symbol in source.bars_by_symbol
    }
    selected_bytes = _render_selected_bars_csv(combined)
    canonical_rows = [
        _canonical_bar(row)
        for symbol in sorted(combined)
        for row in combined[symbol]
    ]
    source_paths = tuple(source.source_path for source in symbol_sources)
    upstream_sha = _canonical_sha256(
        [
            {"path": source.source_path, "raw_sha256": source.upstream_raw_sha256}
            for source in symbol_sources
        ]
    )
    first = symbol_sources[0]
    return SourceBars(
        bars_by_symbol=combined,
        raw_sha256=_sha256_bytes(selected_bytes),
        upstream_raw_sha256=upstream_sha,
        normalized_sha256=_canonical_sha256(canonical_rows),
        source_path=str(path.resolve()),
        source_paths=source_paths,
        selected_artifact_bytes=selected_bytes,
        expected_bar_count_per_symbol=first.expected_bar_count_per_symbol,
        session_open_et=first.session_open_et,
        session_close_et=first.session_close_et,
    )


def _load_complete_rth_bars(
    path: Path,
    market_date: date,
    close_time_et: str | None,
    *,
    expected_symbols: set[str] | None = None,
) -> SourceBars:
    if not path.is_file():
        raise ValueError(f"source bars do not exist: {path}")
    if close_time_et is None:
        raise ValueError("market session has no scheduled close")
    raw = path.read_bytes()
    if not raw:
        raise ValueError("source bars are empty")
    rows_by_symbol: dict[str, list[dict[str, Any]]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"symbol", "timestamp", "open", "high", "low", "close", "volume"}
        if reader.fieldnames is None or not required.issubset(
            {str(name).strip().lower() for name in reader.fieldnames}
        ):
            raise ValueError("source bars require symbol,timestamp,open,high,low,close,volume")
        for row_number, raw_row in enumerate(reader, start=2):
            row = {str(key).strip().lower(): value for key, value in raw_row.items()}
            symbol = str(row.get("symbol") or "").upper().strip()
            if not symbol:
                if expected_symbols is not None:
                    continue
                raise ValueError(f"source bars row {row_number} has no symbol")
            if expected_symbols is not None and symbol not in expected_symbols:
                continue
            timestamp = _parse_aware(str(row.get("timestamp") or ""), f"row {row_number} timestamp")
            local = timestamp.astimezone(MARKET_TIMEZONE)
            if local.date() != market_date:
                raise ValueError(f"source bars row {row_number} is outside {market_date}")
            try:
                open_price = Decimal(str(row["open"]))
                high = Decimal(str(row["high"]))
                low = Decimal(str(row["low"]))
                close = Decimal(str(row["close"]))
                volume = int(Decimal(str(row["volume"])))
            except (KeyError, ValueError, ArithmeticError) as exc:
                raise ValueError(f"source bars row {row_number} has invalid OHLCV") from exc
            if min(open_price, high, low, close) <= 0 or volume <= 0:
                raise ValueError(f"source bars row {row_number} has non-positive price/volume")
            if high < max(open_price, low, close) or low > min(open_price, high, close):
                raise ValueError(f"source bars row {row_number} violates OHLC bounds")
            rows_by_symbol.setdefault(symbol, []).append(
                {
                    "symbol": symbol,
                    "timestamp": timestamp,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
    if expected_symbols is not None:
        missing_symbols = sorted(expected_symbols - set(rows_by_symbol))
        if missing_symbols:
            raise ValueError(
                "source bars are missing delivered symbol(s): " + ", ".join(missing_symbols)
            )
    if not rows_by_symbol:
        raise ValueError("source bars contain no delivered symbols")

    open_dt = datetime.combine(
        market_date,
        datetime.strptime("09:31", "%H:%M").time(),
        MARKET_TIMEZONE,
    )
    close_dt = datetime.combine(
        market_date,
        datetime.strptime(close_time_et, "%H:%M").time(),
        MARKET_TIMEZONE,
    )
    expected_grid: list[datetime] = []
    cursor = open_dt
    while cursor <= close_dt:
        expected_grid.append(cursor)
        cursor += timedelta(minutes=1)
    expected_utc = tuple(value.astimezone(ZoneInfo("UTC")) for value in expected_grid)
    canonical_rows: list[dict[str, Any]] = []
    normalized: dict[str, tuple[dict[str, Any], ...]] = {}
    for symbol, symbol_rows in sorted(rows_by_symbol.items()):
        ordered = sorted(symbol_rows, key=lambda row: row["timestamp"])
        timestamps = tuple(row["timestamp"].astimezone(ZoneInfo("UTC")) for row in ordered)
        if timestamps != expected_utc:
            duplicates = len(timestamps) - len(set(timestamps))
            missing = len(set(expected_utc) - set(timestamps))
            extra = len(set(timestamps) - set(expected_utc))
            raise ValueError(
                f"{symbol}: incomplete one-minute RTH bar-close grid "
                f"(expected={len(expected_utc)}, actual={len(timestamps)}, "
                f"missing={missing}, extra={extra}, duplicates={duplicates})"
            )
        frozen = tuple(ordered)
        normalized[symbol] = frozen
        canonical_rows.extend(_canonical_bar(row) for row in frozen)
    normalized_sha = _canonical_sha256(canonical_rows)
    selected_bytes = _render_selected_bars_csv(normalized)
    return SourceBars(
        bars_by_symbol=normalized,
        raw_sha256=_sha256_bytes(selected_bytes),
        upstream_raw_sha256=_sha256_bytes(raw),
        normalized_sha256=normalized_sha,
        source_path=str(path.resolve()),
        source_paths=(str(path.resolve()),),
        selected_artifact_bytes=selected_bytes,
        expected_bar_count_per_symbol=len(expected_utc),
        session_open_et="09:30",
        session_close_et=close_time_et,
    )


def _evaluate_selection(
    *,
    selection: Mapping[str, Any],
    bars: tuple[dict[str, Any], ...],
    source: SourceBars,
    market_date: str,
    reconciled_at: str,
    slippage_bps: float,
    fee_bps: float,
    notional_per_trade: float,
) -> tuple[
    dict[str, Any],
    dict[str, Any] | None,
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    snapshot = _signal_snapshot(selection)
    trigger = _positive_decimal(snapshot.get("entry_trigger") or snapshot.get("breakout_trigger"))
    stop = _positive_decimal(snapshot.get("invalidation") or snapshot.get("invalidation_level"))
    target = _positive_decimal(snapshot.get("target_1") or snapshot.get("first_target"))
    evaluation_id = _identity("alpha-evaluation", str(selection["selection_id"]), market_date)
    base = {
        "evaluation_id": evaluation_id,
        "selection_id": selection["selection_id"],
        "signal_id": selection["signal_id"],
        "market_date": market_date,
        "ticker": selection["ticker"],
        "scan_id": selection["scan_id"],
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": ALPHAOPS_COHORT,
        "execution_policy_version": ALPHAOPS_EXECUTION_POLICY,
        "source_bar_hash_sha256": source.normalized_sha256,
        "source_bar_raw_sha256": source.raw_sha256,
        "source_bar_artifact_path": source.source_path,
        "bar_timestamp_semantics": ALPHAOPS_BAR_SEMANTICS,
        "reconciled_at": reconciled_at,
        "research_only": True,
        "broker_execution_enabled": False,
        "setup_key": str(snapshot.get("setup_key") or "unknown"),
        "signal_source": str(snapshot.get("preferred_source") or snapshot.get("source") or ""),
    }
    if trigger is None or stop is None or target is None or not (stop < trigger < target):
        evaluation = {
            **base,
            "terminal_state": "incomplete",
            "reconciliation_status": "incomplete_missing_signal_levels",
            "activated": None,
            "filled": False,
            "closed": False,
            "net_return_pct": None,
            "missing_truth": ["positive ordered trigger, invalidation, and target are required"],
        }
        labels = _learning_labels(
            evaluation,
            setup_key=str(snapshot.get("setup_key") or "unknown"),
            r_multiple=None,
        )
        return evaluation, None, labels, None, None

    activation_index = next(
        (index for index, bar in enumerate(bars) if bar["high"] >= trigger),
        None,
    )
    if activation_index is None:
        evaluation = {
            **base,
            "terminal_state": "not_triggered",
            "reconciliation_status": "complete",
            "activated": False,
            "filled": False,
            "closed": False,
            "net_return_pct": None,
            "entry_trigger": float(trigger),
            "invalidation": float(stop),
            "target_1": float(target),
        }
        labels = _learning_labels(
            evaluation,
            setup_key=str(snapshot.get("setup_key") or "unknown"),
            r_multiple=None,
        )
        last = bars[-1]
        outcome = _outcome_row(
            evaluation=evaluation,
            bars=bars,
            entry_index=None,
            entry_price=None,
            reconciled_at=reconciled_at,
            source=source,
        )
        event = _outcome_event(evaluation, reconciled_at, last["close"])
        return evaluation, None, labels, outcome, event

    entry_bar = bars[activation_index]
    raw_entry = max(trigger, entry_bar["open"])
    slippage_rate = Decimal(str(slippage_bps)) / Decimal("10000")
    fee_rate = Decimal(str(fee_bps)) / Decimal("10000")
    entry_fill = raw_entry * (Decimal("1") + slippage_rate)
    exit_bar = bars[-1]
    exit_reason = "eod"
    raw_exit = exit_bar["close"]
    for bar in bars[activation_index:]:
        stop_hit = bar["low"] <= stop
        target_hit = bar["high"] >= target
        if stop_hit:
            exit_bar = bar
            exit_reason = "stop"
            raw_exit = min(stop, bar["open"])
            break
        if target_hit:
            exit_bar = bar
            exit_reason = "target"
            raw_exit = max(target, bar["open"])
            break
    exit_fill = raw_exit * (Decimal("1") - slippage_rate)
    notional = Decimal(str(notional_per_trade))
    quantity = notional / entry_fill
    exit_gross = quantity * exit_fill
    fees = (notional * fee_rate) + (exit_gross * fee_rate)
    net_pnl = exit_gross - notional - fees
    net_return_pct = (net_pnl / notional) * Decimal("100")
    slippage_cost = (quantity * (entry_fill - raw_entry)) + (
        quantity * (raw_exit - exit_fill)
    )
    initial_risk = quantity * (entry_fill - stop)
    r_multiple = net_pnl / initial_risk if initial_risk > 0 else None
    trade_id = _identity("alpha-paper-trade", str(selection["selection_id"]), market_date)
    trade = {
        **base,
        "trade_id": trade_id,
        "direction": "long",
        "decision_time": str(selection.get("selected_at") or ""),
        "entry_time": entry_bar["timestamp"].isoformat(),
        "entry_time_semantics": "completed_bar_close",
        "entry_raw_price": _floatq(raw_entry, _PRICE_QUANT),
        "entry_fill_price": _floatq(entry_fill, _PRICE_QUANT),
        "exit_time": exit_bar["timestamp"].isoformat(),
        "exit_time_semantics": "completed_bar_close",
        "exit_raw_price": _floatq(raw_exit, _PRICE_QUANT),
        "exit_fill_price": _floatq(exit_fill, _PRICE_QUANT),
        "exit_reason": exit_reason,
        "quantity": _floatq(quantity, _PRICE_QUANT),
        "notional": _floatq(notional, _MONEY_QUANT),
        "net_pnl": _floatq(net_pnl, _MONEY_QUANT),
        "net_return_pct": _floatq(net_return_pct, _PCT_QUANT),
        "r_multiple": _floatq(r_multiple, _PCT_QUANT) if r_multiple is not None else None,
        "fees": _floatq(fees, _MONEY_QUANT),
        "slippage_cost": _floatq(slippage_cost, _MONEY_QUANT),
        "slippage_bps_per_side": slippage_bps,
        "fee_bps_per_side": fee_bps,
        "created_at": reconciled_at,
        "intrabar_ambiguity_policy": "stop_first",
    }
    evaluation = {
        **base,
        "terminal_state": "closed",
        "exit_reason": exit_reason,
        "reconciliation_status": "complete",
        "activated": True,
        "filled": True,
        "closed": True,
        "net_return_pct": trade["net_return_pct"],
        "entry_trigger": float(trigger),
        "invalidation": float(stop),
        "target_1": float(target),
        "trade_id": trade_id,
    }
    labels = _learning_labels(
        evaluation,
        setup_key=str(snapshot.get("setup_key") or "unknown"),
        r_multiple=trade["r_multiple"],
    )
    outcome = _outcome_row(
        evaluation=evaluation,
        bars=bars,
        entry_index=activation_index,
        entry_price=entry_fill,
        reconciled_at=reconciled_at,
        source=source,
    )
    event = _outcome_event(evaluation, reconciled_at, exit_fill)
    return evaluation, trade, labels, outcome, event


def _learning_labels(
    evaluation: Mapping[str, Any],
    *,
    setup_key: str,
    r_multiple: float | None,
) -> list[dict[str, Any]]:
    common = {
        "evaluation_id": evaluation["evaluation_id"],
        "signal_id": evaluation["signal_id"],
        "market_date": evaluation["market_date"],
        "ticker": evaluation["ticker"],
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": ALPHAOPS_COHORT,
        "r_multiple": r_multiple,
        "source_bar_hash_sha256": evaluation["source_bar_hash_sha256"],
        "created_at": evaluation["reconciled_at"],
        "setup_key": setup_key,
        "scan_id": evaluation.get("scan_id"),
        "source": evaluation.get("signal_source"),
        "forward_observation": True,
    }
    status = str(evaluation.get("reconciliation_status") or "")
    complete = status == "complete"
    activated = evaluation.get("activated")
    closed = bool(evaluation.get("closed"))
    return [
        {
            **common,
            "label_id": _identity("alpha-label", str(evaluation["evaluation_id"]), "activation"),
            "label_family": "activation",
            "label_value": 1.0 if activated is True else (0.0 if activated is False else None),
            "eligible": complete and activated is not None,
            "exclusion_reason": "" if complete and activated is not None else status,
        },
        {
            **common,
            "label_id": _identity(
                "alpha-label", str(evaluation["evaluation_id"]), "return_after_cost"
            ),
            "label_family": "return_after_cost",
            "label_value": evaluation.get("net_return_pct") if closed else None,
            "eligible": complete and closed,
            "exclusion_reason": "" if complete and closed else (
                "not_activated" if activated is False else status
            ),
        },
    ]


def _outcome_row(
    *,
    evaluation: Mapping[str, Any],
    bars: tuple[dict[str, Any], ...],
    entry_index: int | None,
    entry_price: Decimal | None,
    reconciled_at: str,
    source: SourceBars,
) -> dict[str, Any]:
    observed = bars[entry_index:] if entry_index is not None else ()
    return {
        "signal_id": evaluation["signal_id"],
        "market_date": evaluation["market_date"],
        "ticker": evaluation["ticker"],
        "outcome_source": "complete_sourced_rth_1min",
        "entry_time": (
            bars[entry_index]["timestamp"].isoformat() if entry_index is not None else ""
        ),
        "entry_price": _floatq(entry_price, _PRICE_QUANT) if entry_price else None,
        "price_1m": _bar_close_after(bars, entry_index, 1),
        "price_5m": _bar_close_after(bars, entry_index, 5),
        "price_15m": _bar_close_after(bars, entry_index, 15),
        "lunch_price": _lunch_price(observed),
        "close_price": _floatq(bars[-1]["close"], _PRICE_QUANT),
        "high_after_entry": (
            _floatq(max(row["high"] for row in observed), _PRICE_QUANT)
            if observed
            else None
        ),
        "low_after_entry": (
            _floatq(min(row["low"] for row in observed), _PRICE_QUANT)
            if observed
            else None
        ),
        "halted": None,
        "notes": "Authoritative delivered-cohort paper reconstruction; no broker order.",
        "imported_at": reconciled_at,
        "validated_against_signal_timestamp": True,
        "outcome_status": (
            "complete" if evaluation.get("activated") else "complete_not_triggered"
        ),
        "source_bar_raw_sha256": source.raw_sha256,
        "source_bar_normalized_sha256": source.normalized_sha256,
        "bar_timestamp_semantics": ALPHAOPS_BAR_SEMANTICS,
        "research_only": True,
    }


def _outcome_event(
    evaluation: Mapping[str, Any],
    reconciled_at: str,
    event_price: Decimal,
) -> dict[str, Any]:
    return {
        "event_id": _identity("alpha-outcome-event", str(evaluation["evaluation_id"])),
        "signal_id": evaluation["signal_id"],
        "event_type": "paper_outcome_reconciled",
        "event_timestamp": reconciled_at,
        "event_price": _floatq(event_price, _PRICE_QUANT),
        "source": "alpha_paper_reconcile",
        "notes": str(evaluation.get("terminal_state") or ""),
        "evaluation_id": evaluation["evaluation_id"],
        "research_only": True,
    }


def _build_scorecard(
    *,
    market_date: str,
    selections: Sequence[Mapping[str, Any]],
    evaluations: Sequence[Mapping[str, Any]],
    trades: Sequence[Mapping[str, Any]],
    source: SourceBars | None,
    reconciled_at: str,
    no_trade: bool,
) -> dict[str, Any]:
    closed = [row for row in trades if row.get("exit_reason") in {"stop", "target", "eod"}]
    unresolved = [
        row for row in evaluations if str(row.get("reconciliation_status") or "") != "complete"
    ]
    triggered = [row for row in evaluations if row.get("activated") is True]
    not_triggered = [row for row in evaluations if row.get("activated") is False]
    returns = [float(row["net_return_pct"]) for row in closed]
    pnls = [float(row["net_pnl"]) for row in closed]
    r_values = [float(row["r_multiple"]) for row in closed if row.get("r_multiple") is not None]
    wins = sum(value > 0 for value in pnls)
    losses = sum(value < 0 for value in pnls)
    flats = sum(value == 0 for value in pnls)
    positive = sum(value for value in pnls if value > 0)
    negative = abs(sum(value for value in pnls if value < 0))
    selected_count = 0 if no_trade else len(selections)
    complete = not unresolved
    allocated = sum(float(row["notional"]) for row in closed)
    net_pnl = sum(pnls) if closed else None
    return {
        "scorecard_id": _identity(
            "alpha-scorecard",
            market_date,
            ALPHAOPS_STRATEGY_ID,
            ALPHAOPS_STRATEGY_VERSION,
            ALPHAOPS_EXECUTION_POLICY,
        ),
        "market_date": market_date,
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": ALPHAOPS_COHORT,
        "execution_policy_version": ALPHAOPS_EXECUTION_POLICY,
        "selected_count": selected_count,
        "delivered_count": selected_count,
        "resolved_count": len(triggered) + len(not_triggered),
        "triggered_count": len(triggered),
        "not_triggered_count": len(not_triggered),
        "filled_count": len(triggered),
        "closed_count": len(closed),
        "unresolved_count": len(unresolved),
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "activation_rate_pct": (
            round(len(triggered) / selected_count * 100.0, 6) if selected_count else None
        ),
        "win_rate_pct": round(wins / len(closed) * 100.0, 6) if closed else None,
        "average_net_return_pct": round(sum(returns) / len(returns), 6) if returns else None,
        "net_pnl": round(net_pnl, 6) if net_pnl is not None else None,
        "return_on_allocated_capital_pct": (
            round(net_pnl / allocated * 100.0, 6)
            if net_pnl is not None and allocated > 0
            else None
        ),
        "average_r": round(sum(r_values) / len(r_values), 6) if r_values else None,
        "expectancy_r": round(sum(r_values) / len(r_values), 6) if r_values else None,
        "profit_factor": round(positive / negative, 6) if negative > 0 else None,
        "fees": round(sum(float(row["fees"]) for row in closed), 6),
        "slippage_cost": round(sum(float(row["slippage_cost"]) for row in closed), 6),
        "reconciliation_status": "complete" if complete else "incomplete",
        "created_at": reconciled_at,
        "session_status": "no_signal" if no_trade else (
            "evaluated" if complete else "incomplete"
        ),
        "no_trade_count": 1 if no_trade else 0,
        "source_selection_count": len(selections),
        "source_bar_hash_sha256": source.normalized_sha256 if source else None,
        "source_bar_raw_sha256": source.raw_sha256 if source else None,
        "return_observed": bool(closed),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _retain_source_artifact(out_dir: Path, source: SourceBars) -> str:
    destination = (
        out_dir
        / "source_bars"
        / f"alphaops_delivered_cohort_{source.raw_sha256}.csv"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if _sha256_bytes(destination.read_bytes()) != source.raw_sha256:
            raise StorageError("retained AlphaOps source artifact hash conflict")
    else:
        destination.write_bytes(source.selected_artifact_bytes)
    return str(destination.resolve())


def _write_reconciliation_result(out_dir: str | Path, result: dict[str, Any]) -> dict[str, Any]:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    date_key = str(result.get("market_date") or "unknown")
    fingerprint = _canonical_sha256(result)
    immutable = output / f"alphaops_{date_key}_{fingerprint[:16]}.json"
    body = json.dumps(result, indent=2, sort_keys=True, default=_json_default) + "\n"
    if immutable.exists() and immutable.read_text(encoding="utf-8") != body:
        raise StorageError("AlphaOps reconciliation artifact identity conflict")
    immutable.write_text(body, encoding="utf-8")
    latest = output / "alphaops_reconciliation_latest.json"
    latest.write_text(body, encoding="utf-8")
    return {**result, "artifact_path": str(immutable.resolve())}


def _blocked_result(market_date: str, reasons: Sequence[str]) -> dict[str, Any]:
    return {
        "status": "blocked_incomplete",
        "exit_code": 2,
        "market_date": market_date,
        "strategy_id": ALPHAOPS_STRATEGY_ID,
        "strategy_version": ALPHAOPS_STRATEGY_VERSION,
        "cohort": ALPHAOPS_COHORT,
        "blocked_reasons": list(reasons),
        "return_observed": False,
        "net_return_pct": None,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _selection_contract(rows: Sequence[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            row.get("selection_id"),
            row.get("scan_id"),
            row.get("signal_id"),
            row.get("ticker"),
            _int(row.get("rank"), 0),
            row.get("strategy_id"),
            row.get("strategy_version"),
            row.get("cohort"),
            row.get("decision"),
            row.get("selected_at"),
            row.get("event_key"),
            row.get("body_sha256"),
            _selection_snapshot_sha(row),
        )
        for row in rows
    )


def _membership_sha256(rows: Sequence[Mapping[str, Any]]) -> str:
    return _canonical_sha256(
        [
            {
                "selection_id": str(row.get("selection_id") or ""),
                "scan_id": str(row.get("scan_id") or ""),
                "signal_id": str(row.get("signal_id") or ""),
                "ticker": str(row.get("ticker") or ""),
                "rank": _int(row.get("rank"), 0),
                "decision": str(row.get("decision") or ""),
                "event_key": str(row.get("event_key") or ""),
                "body_sha256": str(row.get("body_sha256") or ""),
                "signal_snapshot_sha256": _selection_snapshot_sha(row),
            }
            for row in sorted(
                rows,
                key=lambda item: (
                    _int(item.get("rank"), 999),
                    str(item.get("ticker") or ""),
                ),
            )
        ]
    )


def _validated_transport_receipt(
    receipt: Mapping[str, Any],
    *,
    expected_body: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    verified = dict(receipt)
    if str(verified.get("transport") or "") != "telegram":
        raise StorageError("AlphaOps delivery receipt is not Telegram transport evidence")
    transmitted = str(verified.get("transmitted_text") or "")
    encoded = transmitted.encode("utf-8")
    digest = _sha256_bytes(encoded)
    if not transmitted:
        raise StorageError("AlphaOps Telegram receipt has no transmitted text")
    if int(verified.get("transmitted_byte_count") or -1) != len(encoded):
        raise StorageError("AlphaOps Telegram receipt byte count fails recomputation")
    if str(verified.get("transmitted_bytes_sha256") or "") != digest:
        raise StorageError("AlphaOps Telegram receipt SHA-256 fails recomputation")
    if expected_body is not None and transmitted != expected_body:
        raise StorageError("Telegram transmitted text differs from the frozen AlphaOps body")
    if expected_sha256 is not None and digest != expected_sha256:
        raise StorageError("Telegram transmitted bytes differ from frozen AlphaOps SHA-256")
    raw_http_status = verified.get("http_status")
    if raw_http_status is None:
        raise StorageError("AlphaOps Telegram receipt has no HTTP status")
    try:
        http_status = int(raw_http_status)
    except (TypeError, ValueError) as exc:
        raise StorageError("AlphaOps Telegram receipt has no HTTP status") from exc
    if not 200 <= http_status < 300:
        raise StorageError("AlphaOps Telegram receipt HTTP status is not successful")
    telegram_response = verified.get("telegram_response")
    if not isinstance(telegram_response, Mapping) or telegram_response.get("ok") is not True:
        raise StorageError("AlphaOps Telegram provider acknowledgement is not successful")
    message_id = verified.get("message_id")
    if message_id in {None, ""}:
        raise StorageError("AlphaOps Telegram receipt has no provider message_id")
    return verified


def _signal_snapshot(selection: Mapping[str, Any]) -> dict[str, Any]:
    payload = selection.get("payload_json")
    if isinstance(payload, Mapping):
        snapshot = payload.get("signal_snapshot")
        if isinstance(snapshot, Mapping):
            return dict(snapshot)
    snapshot = selection.get("signal_snapshot")
    return dict(snapshot) if isinstance(snapshot, Mapping) else {}


def _selection_snapshot_sha(selection: Mapping[str, Any]) -> str:
    payload = selection.get("payload_json")
    if isinstance(payload, Mapping):
        return str(payload.get("signal_snapshot_sha256") or "")
    return str(selection.get("signal_snapshot_sha256") or "")


def _canonical_bar(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "symbol": row["symbol"],
        "timestamp": row["timestamp"].isoformat(),
        "open": _decimal_text(row["open"]),
        "high": _decimal_text(row["high"]),
        "low": _decimal_text(row["low"]),
        "close": _decimal_text(row["close"]),
        "volume": row["volume"],
    }


def _render_selected_bars_csv(
    bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> bytes:
    handle = io.StringIO(newline="")
    fields = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
    writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for symbol in sorted(bars_by_symbol):
        for row in bars_by_symbol[symbol]:
            canonical = _canonical_bar(row)
            writer.writerow(canonical)
    return handle.getvalue().encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default).encode(
            "utf-8"
        )
    )


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _identity(prefix: str, *parts: str) -> str:
    digest = _sha256_bytes("\x1f".join(parts).encode("utf-8"))
    return f"{prefix}:{digest[:32]}"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _parse_aware(value: str, field: str) -> datetime:
    if not value.strip():
        raise StorageError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageError(f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageError(f"{field} must include a timezone offset")
    return parsed


def _positive_decimal(value: Any) -> Decimal | None:
    if value in {None, ""}:
        return None
    try:
        parsed = Decimal(str(value))
    except ArithmeticError:
        return None
    return parsed if parsed > 0 else None


def _floatq(value: Decimal, quantum: Decimal) -> float:
    return float(value.quantize(quantum, rounding=ROUND_HALF_UP))


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _lunch_price(bars: Sequence[Mapping[str, Any]]) -> float | None:
    for row in bars:
        local = row["timestamp"].astimezone(MARKET_TIMEZONE)
        if local.hour == 12 and local.minute == 0:
            return _floatq(row["close"], _PRICE_QUANT)
    return None


def _bar_close_after(
    bars: Sequence[Mapping[str, Any]],
    entry_index: int | None,
    minutes: int,
) -> float | None:
    if entry_index is None:
        return None
    index = entry_index + minutes
    if index >= len(bars):
        return None
    return _floatq(bars[index]["close"], _PRICE_QUANT)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")
