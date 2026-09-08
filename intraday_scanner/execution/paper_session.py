"""Drive one paper-trading session from gate-approved AlphaOps signals.

This is the wiring between the strategy and the broker. It does not decide
which setups are good - the alert gate already did that, and this module is
deliberately unable to promote a signal the gate refused.

Everything it does is recorded in a receipt whose funnel explains a zero-trade
session as precisely as a trading one.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.execution.paper_broker import (
    LiveTradingRefused,
    PaperBrokerClient,
    PaperBrokerError,
)
from intraday_scanner.execution.paper_engine import (
    EntryPlan,
    PaperExecutionEngine,
    PaperExecutionStore,
    utc_now,
)
from intraday_scanner.execution.risk_gate import RiskSettings, entries_enabled

RECEIPT_SCHEMA = "dawnstrike.paper_execution_session.v1"

# Only these gate verdicts may reach the broker. This set is intentionally
# narrow and this module never widens it.
ENTRY_ELIGIBLE_STATUSES = frozenset({"PASS", "ALERT_OK"})

# An observation with no usable timestamp is treated as infinitely stale, so a
# missing field fails closed instead of silently disabling the staleness gate.
UNKNOWN_AGE_SECONDS = float("inf")


def _num(value: Any) -> float | None:
    """Parse a level that may arrive as 6.72, '6.72' or '$6.72'."""

    if value is None:
        return None
    text = str(value).strip().replace("$", "").replace(",", "")
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _parse_stamp(value: Any) -> datetime | None:
    try:
        seen = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return seen.replace(tzinfo=timezone.utc) if seen.tzinfo is None else seen


def _age_seconds(payload: dict[str, Any], row_ts: Any, now: datetime) -> float:
    for key in ("observed_at", "premarket_range_observed_at", "as_of", "timestamp"):
        seen = _parse_stamp(payload.get(key)) if payload.get(key) else None
        if seen is not None:
            return max(0.0, (now - seen).total_seconds())
    seen = _parse_stamp(row_ts) if row_ts else None
    if seen is not None:
        return max(0.0, (now - seen).total_seconds())
    return UNKNOWN_AGE_SECONDS


def load_candidates(db_path: str | Path, market_date: str) -> list[dict[str, Any]]:
    """Read the day's signals. Read-only: the strategy database is never written."""

    uri = f"file:{Path(db_path).as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT ticker, timestamp, can_alert, no_trade_reason, payload_json "
            "FROM alpha_signals WHERE substr(timestamp, 1, 10) = ? "
            "ORDER BY alpha_score DESC",
            (market_date,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        out.append(
            {
                "ticker": str(row["ticker"] or "").upper(),
                "timestamp": row["timestamp"],
                "can_alert": bool(row["can_alert"]),
                "no_trade_reason": row["no_trade_reason"],
                "payload": payload,
            }
        )
    return out


def _plan_from(candidate: dict[str, Any], market_date: str, now: datetime) -> EntryPlan | None:
    payload = candidate["payload"]
    entry = _num(payload.get("entry_trigger")) or _num(payload.get("breakout_trigger"))
    stop = _num(payload.get("invalidation_level")) or _num(payload.get("invalidation"))
    target = _num(payload.get("target_1")) or _num(payload.get("first_target"))
    if not (entry and stop and target):
        return None
    return EntryPlan(
        symbol=candidate["ticker"],
        market_date=market_date,
        entry=entry,
        stop=stop,
        target=target,
        strategy_version=str(payload.get("strategy_version") or "alphaops-v5"),
        signal_id=str(payload.get("signal_key") or candidate["ticker"]),
        data_age_seconds=_age_seconds(payload, candidate.get("timestamp"), now),
    )


def _screen(candidate: dict[str, Any]) -> str | None:
    """Return the reason this candidate may not reach the broker, or None."""

    payload = candidate["payload"]
    if not candidate["can_alert"]:
        return "gate_can_alert_false"
    status = str(payload.get("alert_gate_status") or "").upper()
    if status not in ENTRY_ELIGIBLE_STATUSES:
        return f"gate_status_{status.lower() or 'missing'}"
    if payload.get("strategy_receipt_paper_entry_eligible") is not True:
        return "receipt_not_paper_entry_eligible"
    return None


def run_paper_session(
    *,
    db_path: str | Path,
    market_date: str,
    store_path: str | Path,
    receipt_path: str | Path | None = None,
    force_exit: bool = False,
    settings: RiskSettings | None = None,
    client: PaperBrokerClient | None = None,
) -> dict[str, Any]:
    """Run reconcile, then entries, then management for one session.

    Never raises on an ordinary broker refusal; the receipt records what
    happened either way.
    """

    now = datetime.now(timezone.utc)
    funnel: Counter[str] = Counter()
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "market_date": market_date,
        "generated_at": utc_now(),
        "broker_endpoint": "https://paper-api.alpaca.markets",
        "paper_execution_enabled": True,
        "live_trading_enabled": False,
        "broker_execution_enabled": False,
        "entries_enabled": entries_enabled(),
        "actions": [],
    }

    try:
        broker = client or PaperBrokerClient()
        engine = PaperExecutionEngine(
            client=broker,
            store=PaperExecutionStore(store_path),
            settings=settings or RiskSettings.from_env(),
        )
        receipt["preflight"] = engine.preflight()
    except (PaperBrokerError, LiveTradingRefused) as exc:
        receipt["status"] = "preflight_failed"
        receipt["error"] = str(exc)
        _write(receipt_path, receipt)
        return receipt

    # Recover from the broker BEFORE deciding anything, so a restart mid-session
    # sees the orders it already placed rather than assuming a clean slate.
    receipt["reconcile_before"] = engine.reconcile(market_date)

    candidates = load_candidates(db_path, market_date)
    funnel["candidates_in_database"] = len(candidates)

    market_open = bool(receipt["preflight"].get("market_open"))
    for candidate in candidates:
        refusal = _screen(candidate)
        if refusal:
            funnel[refusal] += 1
            continue
        funnel["gate_approved"] += 1
        plan = _plan_from(candidate, market_date, now)
        if plan is None:
            funnel["incomplete_plan_levels"] += 1
            continue
        if not market_open:
            funnel["market_closed"] += 1
            continue
        result = engine.submit_entry(plan)
        funnel[f"entry_{result['reason']}"] += 1
        receipt["actions"].append(
            {
                "symbol": plan.symbol,
                "submitted": result["submitted"],
                "reason": result["reason"],
                "entry": plan.entry,
                "stop": plan.stop,
                "target": plan.target,
                "data_age_seconds": (
                    None
                    if plan.data_age_seconds == UNKNOWN_AGE_SECONDS
                    else round(plan.data_age_seconds, 1)
                ),
            }
        )

    # Management runs regardless of the entry outcome, and regardless of whether
    # entries were enabled at all.
    receipt["management"] = engine.manage_positions(market_date=market_date, force_exit=force_exit)
    receipt["reconcile_after"] = engine.reconcile(market_date)
    receipt["funnel"] = dict(funnel)
    receipt["status"] = "completed"
    _write(receipt_path, receipt)
    return receipt


def _write(path: str | Path | None, receipt: dict[str, Any]) -> None:
    if path is None:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(target)
