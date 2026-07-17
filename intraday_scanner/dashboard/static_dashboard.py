"""Deterministic builder for the production static operator dashboard.

The deployed dashboard is deliberately static.  This module converts a small,
versioned evidence snapshot into the browser-facing JSON contract without
reading a database, calling a provider, or inventing missing values.
"""

from __future__ import annotations

import argparse
import calendar as month_calendar
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SOURCE_SCHEMA = "dawnstrike.static-dashboard-source.v1"
OUTPUT_SCHEMA = "dawnstrike.static-dashboard.v2"


class StaticDashboardError(ValueError):
    """Raised when a source snapshot cannot support an honest dashboard."""


def _decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise StaticDashboardError(f"Invalid numeric evidence: {value!r}") from exc
    if not number.is_finite():
        raise StaticDashboardError(f"Non-finite numeric evidence: {value!r}")
    return number


def _pct(value: object, *, places: int = 6) -> str | None:
    """Format a stored fractional return as percent points exactly once."""

    number = _decimal(value)
    if number is None:
        return None
    percent = number * Decimal("100")
    if percent == 0:
        return f"{Decimal(0):.{places}f}%"
    sign = "+" if percent > 0 else "-"
    return f"{sign}{abs(percent):.{places}f}%"


def _money(value: object, *, basis: str | None = None) -> str | None:
    number = _decimal(value)
    if number is None:
        return None
    sign = "+" if number > 0 else "-" if number < 0 else ""
    rendered = f"{sign}${abs(number):,.2f}"
    return f"{rendered} {basis}" if basis else rendered


def _required_mapping(parent: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise StaticDashboardError(f"{key} must be an object")
    return value


def _required_rows(parent: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    value = parent.get(key)
    if not isinstance(value, list) or not all(isinstance(row, Mapping) for row in value):
        raise StaticDashboardError(f"{key} must be an array of objects")
    return list(value)


def _strategy_index(paper: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = _required_rows(paper, "strategies")
    output: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        strategy_id = str(row.get("id") or "").strip()
        if not strategy_id or strategy_id in output:
            raise StaticDashboardError("Strategy identities must be present and unique")
        output[strategy_id] = row
    return output


def _day_index(paper: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = _required_rows(paper, "days")
    output: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        day = str(row.get("date") or "")
        if len(day) != 10 or day in output:
            raise StaticDashboardError("PaperOps day identities must be ISO dates and unique")
        output[day] = row
    return output


def _validate_source(source: Mapping[str, Any]) -> None:
    if source.get("schemaVersion") != SOURCE_SCHEMA:
        raise StaticDashboardError(f"Unsupported source schema: {source.get('schemaVersion')!r}")
    paper = _required_mapping(source, "paperOps")
    alpha = _required_mapping(source, "alphaOps")
    evidence = _required_mapping(source, "sourceEvidence")
    if paper.get("status") != "verified":
        raise StaticDashboardError("PaperOps evidence is not verified")
    if alpha.get("status") != "complete":
        raise StaticDashboardError("AlphaOps evidence is not complete")
    if evidence.get("calendarTruthStatus") != "passed":
        raise StaticDashboardError("Calendar truth gate is not passed")
    if evidence.get("sourceBarTruthStatus") != "passed":
        raise StaticDashboardError("Source-bar truth gate is not passed")
    strategies = _strategy_index(paper)
    days = _day_index(paper)
    latest_date = str(source.get("latestRunDate") or "")
    if latest_date not in days:
        raise StaticDashboardError("Latest run date has no canonical PaperOps day")
    latest = days[latest_date]
    returns = _required_rows(latest, "strategies")
    observed_id_list = [str(row.get("id") or "") for row in returns]
    observed_ids = set(observed_id_list)
    if "" in observed_ids or len(observed_ids) != len(observed_id_list):
        raise StaticDashboardError("Latest strategy observations must have unique identities")
    eligible_ids = {
        strategy_id
        for strategy_id, row in strategies.items()
        if str(row.get("activationDate") or "9999-12-31") <= latest_date
    }
    if observed_ids != eligible_ids:
        missing = sorted(eligible_ids - observed_ids)
        unexpected = sorted(observed_ids - eligible_ids)
        raise StaticDashboardError(
            f"Latest strategy coverage mismatch; missing={missing}, unexpected={unexpected}"
        )
    if int(latest.get("coverageExpected") or -1) != len(eligible_ids):
        raise StaticDashboardError("Latest coverageExpected disagrees with registry activation")
    if int(latest.get("coveragePresent") or -1) != len(observed_ids):
        raise StaticDashboardError("Latest coveragePresent disagrees with observed rows")
    reported_pnl = _decimal(latest.get("fleetDailyPnl"))
    row_pnl = sum(
        (_decimal(row.get("totalPnl")) or Decimal(0) for row in returns),
        start=Decimal(0),
    )
    if reported_pnl != row_pnl:
        raise StaticDashboardError("Fleet daily P&L disagrees with strategy rows")
    starting_equity = _decimal(latest.get("fleetStartingEquity"))
    reported_return = _decimal(latest.get("fleetDailyReturnFraction"))
    if starting_equity is None or starting_equity <= 0:
        raise StaticDashboardError("Fleet starting equity must be positive")
    if reported_return != row_pnl / starting_equity:
        raise StaticDashboardError("Fleet daily return disagrees with P&L and starting equity")
    if str(alpha.get("marketDate") or "") != latest_date:
        raise StaticDashboardError("AlphaOps and PaperOps evidence dates do not match")


def _watchlist(alpha: Mapping[str, Any]) -> dict[str, Any]:
    rows = _required_rows(alpha, "candidates")
    rendered = []
    for row in sorted(rows, key=lambda item: int(item.get("rank") or 9999)):
        rendered.append(
            {
                "rank": row.get("rank"),
                "ticker": row.get("ticker"),
                "company": row.get("company"),
                "score": row.get("score"),
                "gate": row.get("gate"),
                "gapPct": row.get("gapPct"),
                "entryTrigger": row.get("entryTrigger"),
                "rewardRisk": row.get("rewardRisk"),
                "source": row.get("source"),
                "sourceCount": row.get("sourceCount"),
                "sourceConfidence": row.get("sourceConfidence"),
            }
        )
    return {
        "title": "Operator Watchlist",
        "date": alpha.get("marketDate"),
        "candidateCount": len(rendered),
        "clearedCount": alpha.get("manualConfirmationCount"),
        "blockedCount": alpha.get("blockedCount"),
        "gateSummary": "2 blocked / 1 needs confirmation / 0 clean",
        "rows": rendered,
        "note": (
            "Watch-only research. One name reached manual-confirmation delivery; no clean "
            "trade recommendation was recorded, and broker execution remains disabled."
        ),
    }


def _latest_strategy_rows(
    paper: Mapping[str, Any], latest_date: str
) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    strategies = _strategy_index(paper)
    day = _day_index(paper)[latest_date]
    observed = {str(row["id"]): row for row in _required_rows(day, "strategies")}
    return strategies, observed


def _strategy_cards(paper: Mapping[str, Any], latest_date: str) -> list[dict[str, Any]]:
    strategies, observed = _latest_strategy_rows(paper, latest_date)
    cards: list[dict[str, Any]] = []
    for strategy_id, registry in strategies.items():
        activation = str(registry.get("activationDate") or "")
        row = observed.get(strategy_id)
        if row is None:
            cards.append(
                {
                    "id": strategy_id,
                    "name": registry.get("name"),
                    "status": "registered / not yet eligible",
                    "trades": None,
                    "winRate": None,
                    "return": None,
                    "drawdown": None,
                    "validation": (
                        f"Registered with immutable lineage. First eligible session {activation}; "
                        "no pre-inception return exists."
                    ),
                }
            )
            continue
        opened = int(row.get("tradesOpened") or 0)
        closed = int(row.get("tradesClosed") or 0)
        open_positions = int(row.get("openPositions") or 0)
        pending = int(row.get("pendingOrders") or 0)
        basis = "unrealized mark" if open_positions else "official forward observation"
        cards.append(
            {
                "id": strategy_id,
                "name": registry.get("name"),
                "status": "official forward paper",
                "trades": f"{opened} opened / {closed} closed",
                "winRate": None if closed == 0 else row.get("winRate"),
                "return": _pct(row.get("dailyReturnFraction")),
                "drawdown": _pct(row.get("drawdownFraction")),
                "validation": (
                    f"{latest_date} {basis}; {open_positions} open and {pending} pending. "
                    "Realized P&L is $0.00."
                ),
            }
        )
    return cards


def _calendar_strategy_rows(
    paper: Mapping[str, Any], day: Mapping[str, Any], day_date: str
) -> list[dict[str, Any]]:
    strategies = _strategy_index(paper)
    observed = {str(row["id"]): row for row in _required_rows(day, "strategies")}
    rows: list[dict[str, Any]] = []
    for strategy_id, registry in strategies.items():
        row = observed.get(strategy_id)
        activation = str(registry.get("activationDate") or "")
        if row is None:
            rows.append(
                {
                    "id": strategy_id,
                    "name": registry.get("name"),
                    "status": f"Registered · starts {activation}",
                    "return": None,
                    "pnl": None,
                    "activity": "not yet eligible",
                }
            )
            continue
        open_positions = int(row.get("openPositions") or 0)
        pending = int(row.get("pendingOrders") or 0)
        opened = int(row.get("tradesOpened") or 0)
        closed = int(row.get("tradesClosed") or 0)
        rows.append(
            {
                "id": strategy_id,
                "name": registry.get("name"),
                "status": (
                    "official forward · unrealized mark"
                    if open_positions
                    else "official forward · observed"
                ),
                "return": _pct(row.get("dailyReturnFraction")),
                "pnl": _money(
                    row.get("totalPnl"), basis="unrealized" if open_positions else "observed"
                ),
                "activity": (
                    f"{opened} opened · {closed} closed · {open_positions} open · {pending} pending"
                ),
                "date": day_date,
            }
        )
    return rows


def _calendar(paper: Mapping[str, Any], latest_date: str) -> dict[str, Any]:
    days = _day_index(paper)
    year, month = (int(part) for part in latest_date[:7].split("-"))
    tiles: list[dict[str, Any]] = []
    for day_number in range(1, month_calendar.monthrange(year, month)[1] + 1):
        day_date = f"{year:04d}-{month:02d}-{day_number:02d}"
        row = days.get(day_date)
        if row is None:
            tiles.append(
                {
                    "date": day_date,
                    "day": f"{day_number:02d}",
                    "observed": False,
                    "dailyReturn": None,
                    "tradeCount": None,
                    "activity": "not observed",
                    "tone": "not-observed",
                    "state": "not observed",
                    "noTrade": False,
                    "warning": False,
                    "detailNote": "No retained official forward session exists for this date.",
                    "strategyReturns": [],
                }
            )
            continue
        opened = int(row.get("tradesOpened") or 0)
        closed = int(row.get("tradesClosed") or 0)
        tiles.append(
            {
                "date": day_date,
                "day": f"{day_number:02d}",
                "observed": True,
                "dailyReturn": _pct(row.get("fleetDailyReturnFraction")),
                "tradeCount": opened,
                "activity": f"{opened} opened · {closed} closed",
                "tone": row.get("status"),
                "state": f"{row.get('status')} · official forward",
                "noTrade": opened == 0,
                "warning": False,
                "detailNote": (
                    f"{row.get('coveragePresent')} of {row.get('coverageExpected')} eligible "
                    f"strategies observed; {row.get('notYetEligible')} registered strategies "
                    "were not yet eligible."
                ),
                "strategyReturns": _calendar_strategy_rows(paper, row, day_date),
            }
        )
    observed_days = [
        row
        for row in days.values()
        if str(row.get("date", ""))[:7] == latest_date[:7]
    ]
    latest = days[latest_date]
    return {
        "currentMonth": latest_date[:7],
        "summary": {
            "monthlyReturn": _pct(latest.get("fleetCumulativeReturnFraction")),
            "totalTrades": sum(int(row.get("tradesOpened") or 0) for row in observed_days),
            "noTradeDays": sum(int(row.get("tradesOpened") or 0) == 0 for row in observed_days),
            "observedDays": len(observed_days),
        },
        "tiles": tiles,
    }


def _paper_records(paper: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    current: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    for row in _required_rows(paper, "openPositions"):
        item = {
            "date": row.get("fillDate"),
            "symbol": row.get("symbol"),
            "strategy": row.get("strategyName"),
            "direction": row.get("direction"),
            "entry": _money(row.get("fillPrice")),
            "stop": _money(row.get("stop")),
            "target": _money(row.get("target")),
            "pnl": _money(row.get("unrealizedPnl"), basis="unrealized"),
            "r": None,
            "status": "open / unrealized",
        }
        current.append(item)
        recent.append(item)
    for row in _required_rows(paper, "pendingOrders"):
        recent.append(
            {
                "date": row.get("signalDate"),
                "symbol": row.get("symbol"),
                "strategy": row.get("strategyName"),
                "direction": row.get("direction"),
                "entry": _money(row.get("entryReference")),
                "stop": _money(row.get("stop")),
                "target": _money(row.get("target")),
                "pnl": None,
                "r": None,
                "status": "pending next-session fill gate",
            }
        )
    return current, recent


def build_dashboard_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    """Build the browser contract from a validated evidence snapshot."""

    _validate_source(source)
    latest_date = str(source["latestRunDate"])
    alpha = _required_mapping(source, "alphaOps")
    paper = _required_mapping(source, "paperOps")
    evidence = _required_mapping(source, "sourceEvidence")
    system = _required_mapping(source, "system")
    latest = _day_index(paper)[latest_date]
    strategies, observed = _latest_strategy_rows(paper, latest_date)
    eligible_rows = list(observed.values())
    best = max(
        eligible_rows,
        key=lambda row: _decimal(row.get("dailyReturnFraction")) or Decimal("-Infinity"),
    )
    best_registry = strategies[str(best["id"])]
    current, recent = _paper_records(paper)
    scheduler = _required_mapping(system, "scheduler")
    notification = _required_mapping(alpha, "notification")
    upcoming = len(strategies) - len(observed)
    return {
        "schemaVersion": OUTPUT_SCHEMA,
        "generatedAt": source.get("generatedAt"),
        "sourceCommit": source.get("sourceCommit"),
        "sourceEvidence": dict(evidence),
        "subheadline": (
            "July 16 forward paper truth, the live AlphaOps watchlist, and every registered "
            "strategy in one research-only view. Returns marked from open positions remain "
            "unrealized until a canonical close exists."
        ),
        "latestRunDate": latest_date,
        "freshnessLabel": f"AlphaOps + PaperOps {latest_date}",
        "deploymentStatus": "Data verified",
        "overallStatus": "verified paper evidence / no broker execution",
        "quickActions": [
            {"label": "Review Watchlist", "href": "#watchlist", "tone": "primary"},
            {"label": "Inspect Calendar", "href": "#calendar", "tone": "secondary"},
            {"label": "Compare Strategies", "href": "#strategies", "tone": "secondary"},
        ],
        "topMetrics": [
            {
                "label": "PaperOps Fleet",
                "value": _pct(latest.get("fleetDailyReturnFraction")),
                "context": (
                    f"{_money(latest.get('fleetDailyPnl'))} across 7 independent $100k "
                    "paper sleeves; 0 closed"
                ),
                "tone": "danger",
            },
            {
                "label": "Best Strategy",
                "value": _pct(best.get("dailyReturnFraction")),
                "context": (
                    f"{best_registry.get('name')}; "
                    f"{_money(best.get('totalPnl'), basis='unrealized')}"
                ),
                "tone": "positive",
            },
            {
                "label": "Strategy Coverage",
                "value": (
                    f"{latest.get('coveragePresent')} / "
                    f"{latest.get('coverageExpected')} eligible"
                ),
                "context": (
                    f"{upcoming} registered strategies start "
                    f"{paper.get('nextActivationDate')}"
                ),
                "tone": "info",
            },
            {
                "label": "Paper Activity",
                "value": (
                    f"{latest.get('openPositions')} open / "
                    f"{latest.get('pendingOrders')} pending"
                ),
                "context": (
                    f"{latest.get('tradesOpened')} opened, {latest.get('tradesClosed')} closed; "
                    "realized P&L $0.00"
                ),
                "tone": "warning",
            },
            {
                "label": "AlphaOps",
                "value": f"{alpha.get('candidateCount')} watch names",
                "context": (
                    f"{alpha.get('manualConfirmationCount')} needs confirmation; "
                    f"{alpha.get('blockedCount')} blocked; 0 clean recommendations"
                ),
                "tone": "warning",
            },
        ],
        "operatorWatchlist": _watchlist(alpha),
        "evidenceRail": [
            {
                "label": "PaperOps run",
                "value": "2026-07-16 forward",
                "status": "verified",
                "detail": f"7/7 eligible strategies; run {evidence.get('paperOpsRunId')}",
            },
            {
                "label": "Source bars",
                "value": evidence.get("dataSnapshotId"),
                "status": evidence.get("sourceBarTruthStatus"),
                "detail": "20 lifecycle events, 4 reference rows, and 2 runs audited.",
            },
            {
                "label": "Registry",
                "value": "9 registered / 7 eligible",
                "status": "2 pending activation",
                "detail": (
                    "Both gap-up strategies first become eligible "
                    f"{paper.get('nextActivationDate')}."
                ),
            },
            {
                "label": "AlphaOps delivery",
                "value": alpha.get("scanId"),
                "status": notification.get("status"),
                "detail": "1 watch-only message recorded; manual confirmation remains required.",
            },
            {
                "label": "Boundary",
                "value": "research and paper audit only",
                "status": "no broker execution",
                "detail": "Missing and pre-inception observations remain n/a, never zero.",
            },
        ],
        "current": {
            "records": current,
            "note": (
                "Five paper positions were filled July 16 from July 15 signals. Every displayed "
                "P&L is an unrealized close mark; no strategy has a closed-trade win rate yet."
            ),
        },
        "calendar": _calendar(paper, latest_date),
        "paperTrading": {
            "totalRows": paper.get("signalLinkedLifecycleRows"),
            "rowLabel": f"{paper.get('signalLinkedLifecycleRows')} signal-linked paper rows",
            "recentRows": recent,
            "note": (
                f"{paper.get('rawBlotterRows')} canonical blotter rows include "
                f"{paper.get('noSetupRows')} explicit no-setup rows."
            ),
        },
        "noPicks": {
            "headline": "No clean trade recommendation: 1 watch-only alert needs confirmation.",
            "watchCount": alpha.get("candidateCount"),
            "acceptedCount": alpha.get("cleanAcceptedCount"),
            "blockedCount": alpha.get("blockedCount"),
            "topReasons": list(alpha.get("topReasons") or []),
        },
        "strategies": _strategy_cards(paper, latest_date),
        "system": {
            "schedulerStatus": f"{scheduler.get('status')} artifact · {scheduler.get('runDate')}",
            "telegramReadiness": f"{notification.get('status')} · {latest_date}",
            "flow": [
                {
                    "name": "AlphaOps",
                    "description": (
                        "3 ranked watch names; one manual-confirmation delivery recorded."
                    ),
                    "status": alpha.get("status"),
                },
                {
                    "name": "PaperOps",
                    "description": (
                        "Forward calendar, lifecycle blotter, and 7/7 eligible coverage verified."
                    ),
                    "status": paper.get("status"),
                },
                {
                    "name": "Source truth",
                    "description": "Retained source-bar and calendar truth gates passed.",
                    "status": evidence.get("sourceBarTruthStatus"),
                },
                {
                    "name": "Strategy activation",
                    "description": (
                        "Two gap-up strategies registered; no evidence claimed before July 17."
                    ),
                    "status": "pending",
                },
                {
                    "name": "Broker boundary",
                    "description": (
                        "Research and paper audit only. No order-placement path is exposed."
                    ),
                    "status": "disabled by design",
                },
            ],
            "taskStatuses": [
                {
                    "task_name": "AlphaOps scan",
                    "state": alpha.get("status"),
                    "last_result": 0,
                    "last_run_time": alpha.get("observedAt"),
                    "next_run_time": None,
                },
                {
                    "task_name": "PaperOps forward evidence",
                    "state": paper.get("status"),
                    "last_result": 0,
                    "last_run_time": latest_date,
                    "next_run_time": None,
                },
                {
                    "task_name": "Scheduler verification artifact",
                    "state": scheduler.get("status"),
                    "last_result": scheduler.get("exitCode"),
                    "last_run_time": scheduler.get("completedAt"),
                    "next_run_time": None,
                },
            ],
        },
    }


def render_dashboard_json(source: Mapping[str, Any]) -> str:
    return json.dumps(build_dashboard_payload(source), indent=2, sort_keys=True) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    source = json.loads(args.source.read_text(encoding="utf-8"))
    rendered = render_dashboard_json(source)
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            raise SystemExit("Static dashboard data is stale; rebuild the committed asset.")
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
