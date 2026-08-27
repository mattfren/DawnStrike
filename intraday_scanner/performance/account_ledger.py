"""Canonical simulated-account ledger for prospective AlphaOps v5 paper truth."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_ACTIVATION_TIMESTAMP,
    ALPHAOPS_V5_COST_MODEL_VERSION,
    ALPHAOPS_V5_POLICY_VERSION,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
    DEFAULT_V5_POLICY,
)
from intraday_scanner.market_calendar import MarketSessionStatus, market_session
from intraday_scanner.performance.contracts import (
    Cohort,
    money_to_cents,
    normalize_cohort,
    safe_float,
    stable_hash,
)

ACCOUNT_COHORT = Cohort.OFFICIAL_FORWARD_PAPER.value
ACCOUNT_CURRENCY = "USD"
ACCOUNT_TYPE = "simulated_paper"
ACCOUNT_OPENING_EQUITY_CENTS = money_to_cents(DEFAULT_V5_POLICY.simulated_opening_equity)
V5_FEE_BPS = 1.0
if ACCOUNT_OPENING_EQUITY_CENTS is None:  # pragma: no cover - frozen policy invariant
    raise RuntimeError("AlphaOps v5 simulated opening equity is invalid")


def build_v5_account_ledger(
    *,
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    scorecards: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    benchmark: dict[str, float],
    input_hash_sha256: str,
    calculated_at: str,
    as_of_market_date: str | None,
) -> list[dict[str, Any]]:
    """Build every v5 market-day state without converting missing evidence to zero."""

    activation_date = _activation_date()
    as_of = _date_or_none(as_of_market_date)
    if as_of is None or as_of < activation_date:
        return []

    v5_trades = [row for row in trades if _is_exact_v5(row)]
    v5_positions = [row for row in positions if _is_exact_v5(row)]
    v5_scorecards = [
        row
        for row in scorecards
        if _is_exact_v5(row) and _is_official_cohort(row.get("cohort"))
    ]
    v5_intents = [
        row
        for row in intents
        if _is_exact_v5(row)
        and str(_payload(row).get("account_id") or row.get("account_id") or "")
        == ALPHAOPS_V5_ACCOUNT_ID
    ]
    v5_selections = [
        row
        for row in selections
        if _is_exact_v5(row) and _is_official_cohort(row.get("cohort"))
    ]

    evidence_dates = {
        observed
        for row in (
            *v5_trades,
            *v5_positions,
            *v5_scorecards,
            *v5_intents,
            *v5_selections,
        )
        if (observed := _row_date(row)) is not None
    }
    end_date = min(as_of, max(evidence_dates, default=as_of))
    if end_date < activation_date:
        return []

    trades_by_day = _group_by_day(v5_trades)
    positions_by_day = _group_by_day(v5_positions)
    scorecards_by_day = _group_by_day(v5_scorecards)
    intents_by_day = _group_by_day(v5_intents)
    selections_by_day = _group_by_day(v5_selections)

    output: list[dict[str, Any]] = []
    carried_equity: int | None = ACCOUNT_OPENING_EQUITY_CENTS
    for market_day in _open_market_days(activation_date, end_date):
        day = market_day.isoformat()
        day_trades = trades_by_day.get(day, [])
        day_positions = positions_by_day.get(day, [])
        day_scorecards = scorecards_by_day.get(day, [])
        day_intents = intents_by_day.get(day, [])
        day_selections = selections_by_day.get(day, [])
        open_positions = [
            row
            for row in day_positions
            if str(row.get("status") or "").strip().upper()
            in {"OPEN", "HELD", "UNREALIZED"}
            and not _has_canonical_trade(row, day_trades)
        ]
        source_refs = _source_refs(
            *(row.get("trade_id") for row in day_trades),
            *(row.get("position_id") for row in open_positions),
            *(row.get("scorecard_id") for row in day_scorecards),
            *(row.get("intent_id") for row in day_intents),
            *(row.get("selection_id") for row in day_selections),
            ALPHAOPS_V5_POLICY_VERSION,
            ALPHAOPS_V5_COST_MODEL_VERSION,
        )
        has_session_evidence = bool(
            day_trades or open_positions or day_scorecards or day_intents or day_selections
        )
        explicit_no_trade = _is_explicit_account_no_trade(
            scorecards=day_scorecards,
            intents=day_intents,
            selections=day_selections,
            trades=day_trades,
            open_positions=open_positions,
        )
        complete_trades = bool(day_trades) and all(
            _trade_is_complete(row, positions=v5_positions, fills=fills, intents=intents)
            for row in day_trades
        )
        realized_trades = day_trades if complete_trades else []

        gross = _sum_trade_money(realized_trades, "gross_pnl")
        fees = _sum_trade_money(realized_trades, "fees")
        slippage = _sum_trade_money(realized_trades, "slippage_cost")
        net = _sum_trade_money(realized_trades, "net_pnl")
        if (
            realized_trades
            and gross is None
            and net is not None
            and fees is not None
            and slippage is not None
        ):
            gross = net + fees + slippage

        status = "MISSING"
        evidence_state = "missing"
        observed_zero = False
        unrealized_change: int | None = None
        beginning = carried_equity
        ending: int | None = None
        if open_positions:
            status = "PENDING"
            evidence_state = "pending"
        elif complete_trades:
            if beginning is None:
                status = "PENDING"
                evidence_state = "pending"
            elif net is not None:
                status = "COMPLETE"
                evidence_state = "complete"
                unrealized_change = 0
                ending = beginning + net
        elif explicit_no_trade:
            status = "NO_TRADE"
            evidence_state = "no_trade"
            observed_zero = True
            gross = fees = slippage = net = unrealized_change = 0
            ending = beginning
        elif has_session_evidence:
            status = "PENDING"
            evidence_state = "pending"

        external_flow = 0
        accounting_delta = _accounting_delta(
            beginning=beginning,
            external_flow=external_flow,
            realized_net=net,
            unrealized_change=unrealized_change,
            ending=ending,
        )
        if accounting_delta not in {None, 0}:
            status = "DEGRADED"
            evidence_state = "degraded"
            ending = None
        net_return = (
            0.0
            if observed_zero
            else _return_pct(net, beginning) if ending is not None else None
        )
        gross_return = (
            0.0
            if observed_zero
            else _return_pct(gross, beginning) if ending is not None else None
        )
        market_benchmark = benchmark.get(day)
        excess_return = (
            round(net_return - market_benchmark, 4)
            if net_return is not None and market_benchmark is not None
            else None
        )
        cash = ending if ending is not None and not open_positions else None
        position_market_value = 0 if ending is not None and not open_positions else None
        source_hash = stable_hash(
            {
                "source_refs": source_refs,
                "trades": day_trades,
                "positions": open_positions,
                "scorecards": day_scorecards,
                "intents": day_intents,
                "selections": day_selections,
            }
        )
        row = {
            "ledger_id": stable_hash([ALPHAOPS_V5_ACCOUNT_ID, day]),
            "account_id": ALPHAOPS_V5_ACCOUNT_ID,
            "market_date": day,
            "cohort": ACCOUNT_COHORT,
            "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
            "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
            "execution_policy_version": ALPHAOPS_V5_POLICY_VERSION,
            "cost_model_version": ALPHAOPS_V5_COST_MODEL_VERSION,
            "status": status,
            "evidence_state": evidence_state,
            "beginning_equity_cents": beginning,
            "external_flow_cents": external_flow,
            "realized_gross_pnl_cents": gross,
            "fees_cents": fees,
            "slippage_cents": slippage,
            "realized_net_pnl_cents": net,
            "unrealized_pnl_change_cents": unrealized_change,
            "cash_cents": cash,
            "position_market_value_cents": position_market_value,
            "ending_equity_cents": ending,
            "market_benchmark_return_pct": market_benchmark,
            "cash_benchmark_return_pct": None,
            "gross_return_pct": gross_return,
            "net_return_pct": net_return,
            "excess_return_pct": excess_return,
            "accounting_delta_cents": accounting_delta,
            "trade_count": len(day_trades),
            "open_position_count": len(open_positions),
            "observed_zero": observed_zero,
            "source_refs": list(source_refs),
            "source_hash_sha256": source_hash,
            "input_hash_sha256": input_hash_sha256,
            "calculated_at": calculated_at,
            "external_flow_policy": "fixed_simulated_account_no_funding_interface",
            "return_identity": (
                "(ending_equity - external_flows - beginning_equity) / beginning_equity"
            ),
            "research_only": True,
            "broker_execution_enabled": False,
        }
        output.append(row)
        carried_equity = ending
    return output


def persist_v5_account_ledger(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    calculated_at: str,
) -> None:
    """Persist the immutable account contract and replace its derived daily ledger."""

    account = {
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "activation_timestamp": ALPHAOPS_V5_ACTIVATION_TIMESTAMP,
        "opening_equity_cents": ACCOUNT_OPENING_EQUITY_CENTS,
        "currency": ACCOUNT_CURRENCY,
        "account_type": ACCOUNT_TYPE,
        "execution_policy_version": ALPHAOPS_V5_POLICY_VERSION,
        "cost_model_version": ALPHAOPS_V5_COST_MODEL_VERSION,
        "research_only": 1,
        "broker_execution_enabled": 0,
        "created_at": calculated_at,
    }
    existing = connection.execute(
        """
        SELECT strategy_id, strategy_version, activation_timestamp,
               opening_equity_cents, currency, account_type,
               execution_policy_version, cost_model_version,
               research_only, broker_execution_enabled
        FROM paper_accounts WHERE account_id = ?
        """,
        (ALPHAOPS_V5_ACCOUNT_ID,),
    ).fetchone()
    immutable_values = tuple(
        account[key] for key in account if key not in {"account_id", "created_at"}
    )
    if existing is not None and tuple(existing) != immutable_values:
        raise ValueError("AlphaOps v5 paper account contract changed after activation")
    connection.execute(
        """
        INSERT OR IGNORE INTO paper_accounts
        (account_id, strategy_id, strategy_version, activation_timestamp,
         opening_equity_cents, currency, account_type, execution_policy_version,
         cost_model_version, research_only, broker_execution_enabled, created_at,
         payload_json)
        VALUES (:account_id, :strategy_id, :strategy_version, :activation_timestamp,
                :opening_equity_cents, :currency, :account_type,
                :execution_policy_version, :cost_model_version, :research_only,
                :broker_execution_enabled, :created_at, :payload_json)
        """,
        {**account, "payload_json": json.dumps(account, sort_keys=True)},
    )
    connection.execute(
        "DELETE FROM paper_account_daily_ledger WHERE account_id = ?",
        (ALPHAOPS_V5_ACCOUNT_ID,),
    )
    for row in rows:
        connection.execute(
            """
            INSERT INTO paper_account_daily_ledger
            (ledger_id, account_id, market_date, cohort, strategy_id, strategy_version,
             execution_policy_version, cost_model_version, status, evidence_state,
             beginning_equity_cents, external_flow_cents, realized_gross_pnl_cents,
             fees_cents, slippage_cents, realized_net_pnl_cents,
             unrealized_pnl_change_cents, cash_cents, position_market_value_cents,
             ending_equity_cents, market_benchmark_return_pct,
             cash_benchmark_return_pct, gross_return_pct, net_return_pct,
             excess_return_pct, accounting_delta_cents, trade_count,
             open_position_count, source_refs_json, source_hash_sha256,
             input_hash_sha256, calculated_at, payload_json)
            VALUES
            (:ledger_id, :account_id, :market_date, :cohort, :strategy_id,
             :strategy_version, :execution_policy_version, :cost_model_version,
             :status, :evidence_state, :beginning_equity_cents,
             :external_flow_cents, :realized_gross_pnl_cents, :fees_cents,
             :slippage_cents, :realized_net_pnl_cents,
             :unrealized_pnl_change_cents, :cash_cents,
             :position_market_value_cents, :ending_equity_cents,
             :market_benchmark_return_pct, :cash_benchmark_return_pct,
             :gross_return_pct, :net_return_pct, :excess_return_pct,
             :accounting_delta_cents, :trade_count, :open_position_count,
             :source_refs_json, :source_hash_sha256, :input_hash_sha256,
             :calculated_at, :payload_json)
            """,
            {
                **row,
                "source_refs_json": json.dumps(row["source_refs"], sort_keys=True),
                "payload_json": json.dumps(row, sort_keys=True),
            },
        )


def ledger_equity_observations(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adapt account-ledger rows to the canonical daily aggregation lookup."""

    return [
        {
            "observation_id": row["ledger_id"],
            "market_date": row["market_date"],
            "cohort": row["cohort"],
            "strategy_id": row["strategy_id"],
            "strategy_version": row["strategy_version"],
            "opening_equity_cents": row["beginning_equity_cents"],
            "ending_equity_cents": row["ending_equity_cents"],
            "external_flow_cents": row["external_flow_cents"],
            "cash_cents": row["cash_cents"],
            "position_market_value_cents": row["position_market_value_cents"],
            "accounting_delta_cents": row["accounting_delta_cents"],
            "cash_benchmark_return_pct": row["cash_benchmark_return_pct"],
            "ledger_status": row["status"],
            "account_id": row["account_id"],
            "net_return_pct": row["net_return_pct"],
            "gross_return_pct": row["gross_return_pct"],
            "market_benchmark_return_pct": row["market_benchmark_return_pct"],
            "source_refs_json": json.dumps(row["source_refs"], sort_keys=True),
            "source_hash_sha256": row["source_hash_sha256"],
            "observed_at": row["calculated_at"],
            "payload_json": json.dumps(row, sort_keys=True),
        }
        for row in rows
    ]


def _activation_date() -> date:
    return datetime.fromisoformat(ALPHAOPS_V5_ACTIVATION_TIMESTAMP).date()


def _date_or_none(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _row_date(row: dict[str, Any]) -> date | None:
    return _date_or_none(
        row.get("market_date")
        or row.get("selected_at")
        or row.get("decision_time")
        or row.get("created_at")
    )


def _open_market_days(start: date, end: date) -> list[date]:
    output: list[date] = []
    current = start
    while current <= end:
        status = market_session(current).status
        if status in {MarketSessionStatus.OPEN, MarketSessionStatus.EARLY_CLOSE}:
            output.append(current)
        current += timedelta(days=1)
    return output


def _payload(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("payload_json")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_exact_v5(row: dict[str, Any]) -> bool:
    payload = _payload(row)
    return (
        str(row.get("strategy_id") or payload.get("strategy_id") or "")
        == ALPHAOPS_V5_STRATEGY_ID
        and str(row.get("strategy_version") or payload.get("strategy_version") or "")
        == ALPHAOPS_V5_STRATEGY_VERSION
    )


def _is_official_cohort(value: object) -> bool:
    return (
        normalize_cohort(value, default=Cohort.ALPHAOPS_SIGNAL_RESEARCH)
        == Cohort.OFFICIAL_FORWARD_PAPER
    )


def _group_by_day(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    output: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        observed = _row_date(row)
        if observed is not None:
            output.setdefault(observed.isoformat(), []).append(row)
    return output


def _has_canonical_trade(
    position: dict[str, Any], trades: Iterable[dict[str, Any]]
) -> bool:
    payload = _payload(position)
    position_signal = str(position.get("signal_id") or payload.get("signal_id") or "")
    selection_id = str(payload.get("selection_id") or "")
    return any(
        (position_signal and position_signal == str(row.get("signal_id") or ""))
        or (selection_id and selection_id == str(row.get("selection_id") or ""))
        for row in trades
    )


def _trade_is_complete(
    row: dict[str, Any],
    *,
    positions: Iterable[dict[str, Any]] = (),
    fills: Iterable[dict[str, Any]] = (),
    intents: Iterable[dict[str, Any]] = (),
) -> bool:
    """Require an exact durable paper lifecycle before official realization.

    ``strategy_paper_trades`` is an EOD/model projection and is not FillTruth
    by itself.  A realized v5 trade must join one closed paper position and its
    durable entry/exit fills on immutable identity and prices.  This deliberately
    ignores caller-supplied ``committed`` booleans in payloads.
    """

    if not bool(
        safe_float(row.get("entry_fill_price")) is not None
        and safe_float(row.get("exit_fill_price")) is not None
        and safe_float(row.get("quantity")) is not None
        and money_to_cents(row.get("net_pnl")) is not None
        and money_to_cents(row.get("fees")) is not None
        and money_to_cents(row.get("slippage_cost")) is not None
        and _valid_sha256(row.get("source_bar_hash_sha256"))
    ):
        return False
    trade_date = str(row.get("market_date") or "")[:10]
    trade_ticker = str(row.get("ticker") or "").strip().upper()
    trade_signal = str(row.get("signal_id") or "").strip()
    trade_selection = str(row.get("selection_id") or "").strip()
    trade_quantity = safe_float(row.get("quantity"))
    trade_entry = safe_float(row.get("entry_fill_price"))
    trade_exit = safe_float(row.get("exit_fill_price"))
    trade_direction = str(row.get("direction") or "").strip().lower()
    trade_account = str(_payload(row).get("account_id") or row.get("account_id") or "").strip()
    for position in positions:
        position_payload = _payload(position)
        if str(position.get("status") or "").strip().upper() not in {
            "CLOSED",
            "REALIZED",
            "COMPLETE",
        }:
            continue
        if (
            str(position.get("market_date") or "")[:10] != trade_date
            or str(position.get("ticker") or "").strip().upper() != trade_ticker
            or str(position.get("signal_id") or "").strip() != trade_signal
            or not _same_number(position.get("quantity"), trade_quantity)
            or not _same_number(position.get("entry_price"), trade_entry)
            or not _same_number(position.get("exit_price"), trade_exit)
        ):
            continue
        position_selection = str(
            position.get("selection_id") or position_payload.get("selection_id") or ""
        ).strip()
        position_account = str(
            position.get("account_id") or position_payload.get("account_id") or ""
        ).strip()
        trade_strategy = str(
            row.get("strategy_id") or _payload(row).get("strategy_id") or ""
        ).strip()
        trade_version = str(
            row.get("strategy_version") or _payload(row).get("strategy_version") or ""
        ).strip()
        trade_cohort = str(row.get("cohort") or _payload(row).get("cohort") or "").strip()
        trade_episode = str(_payload(row).get("episode_id") or "").strip()
        position_strategy = str(
            position.get("strategy_id") or position_payload.get("strategy_id") or ""
        ).strip()
        position_version = str(
            position.get("strategy_version") or position_payload.get("strategy_version") or ""
        ).strip()
        position_cohort = str(
            position.get("cohort") or position_payload.get("cohort") or ""
        ).strip()
        position_episode = str(position_payload.get("episode_id") or "").strip()
        position_eligible = position_payload.get("official_paper_eligible")
        if (
            not trade_selection
            or not trade_account
            or not trade_strategy
            or not trade_version
            or not trade_cohort
            or not trade_episode
            or not position_selection
            or not position_account
            or not position_strategy
            or not position_version
            or not position_cohort
            or not position_episode
            or trade_selection != position_selection
            or trade_account != position_account
            or trade_strategy != position_strategy
            or trade_version != position_version
            or trade_cohort != position_cohort
            or trade_episode != position_episode
            or position_eligible is not True
            or position_payload.get("research_only") is not True
            or position_payload.get("broker_execution_enabled") is not False
            or trade_account != ALPHAOPS_V5_ACCOUNT_ID
            or trade_strategy != ALPHAOPS_V5_STRATEGY_ID
            or trade_version != ALPHAOPS_V5_STRATEGY_VERSION
            or normalize_cohort(trade_cohort, default=Cohort.ALPHAOPS_SIGNAL_RESEARCH)
            != Cohort.OFFICIAL_FORWARD_PAPER
        ):
            continue
        if _is_reconciliation_derived(position, position_payload):
            continue
        position_id = str(position.get("position_id") or "").strip()
        entry_intent = str(position.get("entry_intent_id") or "").strip()
        exit_intent = str(position.get("exit_intent_id") or "").strip()
        if not position_id or not entry_intent or not exit_intent:
            continue
        linked_fills = [
            fill
            for fill in fills
            if str(fill.get("position_id") or "").strip() == position_id
        ]
        if len(linked_fills) != 2:
            continue
        entries = [
            fill
            for fill in linked_fills
            if str(fill.get("side") or "").strip().upper()
            == ("SELL_SHORT" if trade_direction == "short" else "BUY")
        ]
        exits = [
            fill
            for fill in linked_fills
            if str(fill.get("side") or "").strip().upper()
            in {"SELL", "BUY_TO_COVER"}
        ]
        if len(entries) != 1 or len(exits) != 1:
            continue
        entry_fill, exit_fill = entries[0], exits[0]
        if _is_reconciliation_derived(
            entry_fill, _payload(entry_fill)
        ) or _is_reconciliation_derived(exit_fill, _payload(exit_fill)):
            continue
        if trade_direction not in {"long", "short"}:
            continue
        if (
            trade_direction == "short"
            and str(exit_fill.get("side") or "").strip().upper() != "BUY_TO_COVER"
        ):
            continue
        if trade_direction == "long" and str(exit_fill.get("side") or "").strip().upper() != "SELL":
            continue
        if not _fill_matches(
            entry_fill,
            position_id=position_id,
            intent_id=entry_intent,
            trade=row,
            price=trade_entry,
            expected_time=str(row.get("entry_time") or ""),
            expected_action="ENTER_SHORT" if trade_direction == "short" else "ENTER_LONG",
            expected_episode=str(position_payload.get("episode_id") or "").strip(),
            intents=intents,
        ) or not _fill_matches(
            exit_fill,
            position_id=position_id,
            intent_id=exit_intent,
            trade=row,
            price=trade_exit,
            expected_time=str(row.get("exit_time") or ""),
            expected_action="EXIT_SHORT" if trade_direction == "short" else "EXIT_LONG",
            expected_episode=str(position_payload.get("episode_id") or "").strip(),
            intents=intents,
        ):
            continue
        if not _trade_math_matches(
            row, position, entry_fill, exit_fill, trade_direction, intents=intents
        ):
            continue
        return True
    return False


def _fill_matches(
    fill: dict[str, Any],
    *,
    position_id: str,
    intent_id: str,
    trade: dict[str, Any],
    price: float | None,
    expected_time: str,
    expected_action: str,
    expected_episode: str,
    intents: Iterable[dict[str, Any]],
) -> bool:
    payload = _payload(fill)
    trade_hash = str(trade.get("source_bar_hash_sha256") or "").strip().lower()
    fill_hash = str(
        fill.get("source_bar_hash_sha256") or payload.get("source_bar_hash_sha256") or ""
    ).strip().lower()
    intent = next(
        (
            candidate
            for candidate in intents
            if str(candidate.get("intent_id") or "").strip() == intent_id
        ),
        None,
    )
    intent_payload = _payload(intent or {})
    fingerprint = str(intent_payload.get("decision_fingerprint") or "").strip()
    intent_source_hash = str(
        intent_payload.get("source_bar_hash_sha256") or ""
    ).strip().lower()
    fill_fingerprint = str(
        fill.get("decision_fingerprint") or payload.get("decision_fingerprint") or ""
    ).strip()
    expected_slippage_bps = (
        DEFAULT_V5_POLICY.entry_slippage_bps
        if expected_action in {"ENTER_LONG", "ENTER_SHORT"}
        else DEFAULT_V5_POLICY.exit_slippage_bps
    )
    fill_slippage_bps = safe_float(
        fill.get("slippage_bps") or payload.get("slippage_bps")
    )
    trade_payload = _payload(trade)
    trade_cohort = str(trade.get("cohort") or trade_payload.get("cohort") or "").strip()
    return bool(
        str(fill.get("fill_id") or "").strip()
        and str(fill.get("position_id") or "").strip() == position_id
        and str(fill.get("intent_id") or "").strip() == intent_id
        and str(fill.get("signal_id") or "").strip()
        == str(trade.get("signal_id") or "").strip()
        and str(fill.get("market_date") or "")[:10]
        == str(trade.get("market_date") or "")[:10]
        and str(fill.get("ticker") or "").strip().upper()
        == str(trade.get("ticker") or "").strip().upper()
        and _same_number(fill.get("quantity"), safe_float(trade.get("quantity")))
        and _same_number(fill.get("fill_price"), price)
        and fill_slippage_bps is not None
        and abs(fill_slippage_bps - expected_slippage_bps) <= 1e-9
        and str(fill.get("fill_time") or "") == expected_time
        and intent is not None
        and str(intent.get("decision_time") or "") == expected_time
        and str(intent.get("action") or "").strip().upper() == expected_action
        and str(intent.get("signal_id") or "").strip()
        == str(trade.get("signal_id") or "").strip()
        and str(intent.get("market_date") or "")[:10]
        == str(trade.get("market_date") or "")[:10]
        and str(intent.get("ticker") or "").strip().upper()
        == str(trade.get("ticker") or "").strip().upper()
        and str(intent.get("strategy_id") or intent_payload.get("strategy_id") or "").strip()
        == str(trade.get("strategy_id") or _payload(trade).get("strategy_id") or "").strip()
        and str(
            intent.get("strategy_version") or intent_payload.get("strategy_version") or ""
        ).strip()
        == str(
            trade.get("strategy_version") or _payload(trade).get("strategy_version") or ""
        ).strip()
        and str(intent.get("account_id") or intent_payload.get("account_id") or "").strip()
        == str(trade.get("account_id") or _payload(trade).get("account_id") or "").strip()
        and str(intent_payload.get("selection_id") or "").strip()
        == str(trade.get("selection_id") or _payload(trade).get("selection_id") or "").strip()
        and str(intent_payload.get("episode_id") or "").strip() == expected_episode
        and str(payload.get("account_id") or "").strip()
        == str(trade.get("account_id") or _payload(trade).get("account_id") or "").strip()
        and str(payload.get("strategy_id") or "").strip()
        == str(trade.get("strategy_id") or _payload(trade).get("strategy_id") or "").strip()
        and str(payload.get("strategy_version") or "").strip()
        == str(
            trade.get("strategy_version") or _payload(trade).get("strategy_version") or ""
        ).strip()
        and str(payload.get("selection_id") or "").strip()
        == str(trade.get("selection_id") or _payload(trade).get("selection_id") or "").strip()
        and str(payload.get("cohort") or "").strip() == trade_cohort
        and str(payload.get("episode_id") or "").strip() == expected_episode
        and str(
            intent.get("source_observation_id")
            or intent_payload.get("source_observation_id")
            or ""
        ).strip()
        and _valid_sha256(fingerprint)
        and _valid_sha256(intent_source_hash)
        and intent_source_hash == trade_hash
        and str(intent.get("mode") or "").strip() == "paper_execute"
        and str(intent.get("lifecycle_state") or "").strip().upper()
        in {"ENTRY_TRIGGERED", "EXIT_TRIGGERED"}
        and intent_payload.get("official_paper_eligible") is True
        and intent_payload.get("research_only") is True
        and intent_payload.get("broker_execution_enabled") is False
        and fingerprint
        and fill_fingerprint == fingerprint
        and (not fill_hash or fill_hash == trade_hash)
    )


def _trade_math_matches(
    trade: dict[str, Any],
    position: dict[str, Any],
    entry_fill: dict[str, Any],
    exit_fill: dict[str, Any],
    direction: str,
    *,
    intents: Iterable[dict[str, Any]],
) -> bool:
    quantity = safe_float(trade.get("quantity"))
    entry = safe_float(entry_fill.get("fill_price"))
    exit_price = safe_float(exit_fill.get("fill_price"))
    if quantity is None or entry is None or exit_price is None or quantity <= 0:
        return False
    gross = (
        (entry - exit_price) * quantity
        if direction == "short"
        else (exit_price - entry) * quantity
    )
    payload = _payload(trade)
    entry_intent = next(
        (
            item
            for item in intents
            if str(item.get("intent_id") or "").strip()
            == str(entry_fill.get("intent_id") or "").strip()
        ),
        None,
    )
    exit_intent = next(
        (
            item
            for item in intents
            if str(item.get("intent_id") or "").strip()
            == str(exit_fill.get("intent_id") or "").strip()
        ),
        None,
    )
    raw_entry = safe_float((entry_intent or {}).get("decision_price"))
    raw_exit = safe_float((exit_intent or {}).get("decision_price"))
    if raw_entry is None or raw_exit is None:
        return False
    if not _cost_model_identity_matches(trade, payload):
        return False
    if direction == "long" and not (entry >= raw_entry and raw_exit >= exit_price):
        return False
    if direction == "short" and not (entry <= raw_entry and raw_exit <= exit_price):
        return False
    expected_fees_float = (
        (entry * quantity + exit_price * quantity) * V5_FEE_BPS / 10_000.0
        + quantity * DEFAULT_V5_POLICY.commission_per_share_per_side * 2.0
    )
    expected_slippage_float = (
        ((entry - raw_entry) + (raw_exit - exit_price)) * quantity
        if direction == "long"
        else ((raw_entry - entry) + (exit_price - raw_exit)) * quantity
    )
    fees = money_to_cents(trade.get("fees"))
    slippage = money_to_cents(trade.get("slippage_cost"))
    net = money_to_cents(trade.get("net_pnl"))
    notional = money_to_cents(trade.get("notional"))
    if fees is None or slippage is None or net is None or notional is None:
        return False
    expected_gross = money_to_cents(gross)
    expected_notional = money_to_cents(entry * quantity)
    expected_return = round(net / notional * 100.0, 4) if notional else None
    position_gross = money_to_cents(position.get("realized_pnl"))
    reported_return = safe_float(trade.get("net_return_pct"))
    return bool(
        expected_gross is not None
        and abs((fees or 0) - (money_to_cents(expected_fees_float) or 0)) <= 1
        and abs((slippage or 0) - (money_to_cents(expected_slippage_float) or 0)) <= 1
        and abs(net + fees + slippage - expected_gross) <= 1
        and expected_notional is not None
        and abs(notional - expected_notional) <= 1
        and position_gross is not None
        and abs(position_gross - expected_gross) <= 1
        and expected_return is not None
        and reported_return is not None
        and abs(reported_return - expected_return) <= 0.01
        and _same_number(entry_fill.get("quantity"), quantity)
        and _same_number(exit_fill.get("quantity"), quantity)
    )


def _cost_model_identity_matches(trade: dict[str, Any], payload: dict[str, Any]) -> bool:
    version = str(
        trade.get("cost_model_version") or payload.get("cost_model_version") or ""
    ).strip()
    fee_bps = safe_float(trade.get("fee_bps") or payload.get("fee_bps"))
    commission = safe_float(
        trade.get("commission_per_share_per_side")
        or payload.get("commission_per_share_per_side")
    )
    return bool(
        version == ALPHAOPS_V5_COST_MODEL_VERSION
        and fee_bps is not None
        and abs(fee_bps - V5_FEE_BPS) <= 1e-9
        and commission is not None
        and abs(commission - DEFAULT_V5_POLICY.commission_per_share_per_side) <= 1e-9
    )


def _is_reconciliation_derived(row: dict[str, Any], payload: dict[str, Any]) -> bool:
    return bool(
        payload.get("canonical_eod_repair") is True
        or payload.get("source_reconciliation_trade_id")
        or row.get("canonical_eod_repair") is True
        or row.get("source_reconciliation_trade_id")
    )


def _same_number(left: Any, right: float | None) -> bool:
    value = safe_float(left)
    return value is not None and right is not None and abs(value - right) <= 1e-6


def _valid_sha256(value: Any) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{64}", str(value or "").strip()))


def _is_explicit_account_no_trade(
    *,
    scorecards: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
) -> bool:
    if trades or open_positions:
        return False
    complete_scorecard = any(
        str(row.get("reconciliation_status") or "").lower() == "complete"
        and int(row.get("filled_count") or 0) == 0
        and int(row.get("closed_count") or 0) == 0
        and int(row.get("unresolved_count") or 0) == 0
        and str(row.get("session_status") or "").lower()
        in {"selected", "explicit_no_trade"}
        for row in scorecards
    )
    blocked_intents = bool(intents) and all(
        str(row.get("action") or "").upper() == "STAND_DOWN"
        and _payload(row).get("official_paper_eligible") is False
        for row in intents
    )
    canonical_no_trade_selection = (
        len(selections) == 1
        and _selection_is_canonical_no_trade(selections[0])
    )
    return complete_scorecard or blocked_intents or canonical_no_trade_selection


def _selection_is_canonical_no_trade(row: dict[str, Any]) -> bool:
    payload = _payload(row)
    decision_payload = payload.get("decision_payload")
    rank = row.get("rank") if row.get("rank") is not None else payload.get("rank")
    if rank is None:
        return False
    try:
        normalized_rank = int(rank)
    except (TypeError, ValueError):
        return False
    return bool(
        str(row.get("decision") or payload.get("decision") or "").lower()
        == "no_trade"
        and str(row.get("ticker") or payload.get("ticker") or "").upper()
        == "NO_TRADE"
        and normalized_rank == 0
        and str(row.get("signal_id") or payload.get("signal_id") or "").startswith(
            "no_trade:"
        )
        and isinstance(decision_payload, dict)
        and decision_payload.get("no_trade") is True
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _sum_trade_money(rows: list[dict[str, Any]], key: str) -> int | None:
    if not rows:
        return None
    values: list[int] = []
    for row in rows:
        payload = _payload(row)
        value = money_to_cents(row.get(key) if row.get(key) is not None else payload.get(key))
        if value is None:
            return None
        values.append(value)
    return sum(values)


def _return_pct(pnl_cents: int | None, beginning_equity_cents: int | None) -> float | None:
    if pnl_cents is None or beginning_equity_cents is None or beginning_equity_cents <= 0:
        return None
    return round(pnl_cents / beginning_equity_cents * 100.0, 4)


def _accounting_delta(
    *,
    beginning: int | None,
    external_flow: int | None,
    realized_net: int | None,
    unrealized_change: int | None,
    ending: int | None,
) -> int | None:
    if (
        beginning is None
        or external_flow is None
        or realized_net is None
        or unrealized_change is None
        or ending is None
    ):
        return None
    return ending - (beginning + external_flow + realized_net + unrealized_change)


def _source_refs(*values: object) -> tuple[str, ...]:
    return tuple(sorted({str(value) for value in values if str(value or "").strip()}))
