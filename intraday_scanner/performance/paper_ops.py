"""Read and reconcile the existing PaperOps daily evidence export.

PaperOps is an input source, not a second performance ledger.  Its daily
strategy summaries are normalized into the canonical performance row shape,
with replay evidence kept historical and forward shadow evidence kept in the
shadow-challenger cohort.  Rows whose equity, P&L, or cost identity cannot be
proven are retained as quarantined observations and never contribute to valid
returns.
"""

from __future__ import annotations

import csv
import hashlib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from intraday_scanner.performance.contracts import (
    Cohort,
    money_to_cents,
    stable_hash,
)

CALENDAR_RELATIVE_PATH = Path("calendar") / "strategy_daily_returns.csv"
LOGICAL_SOURCE_REF = "data/v2_paper_ops_live/calendar/strategy_daily_returns.csv"


def load_paper_ops(
    root: str | Path | None,
    *,
    market_date: str | None = None,
) -> dict[str, Any]:
    """Load the bounded PaperOps calendar export without mutating its source."""

    if root is None:
        return _not_configured()
    root_path = Path(root)
    source_path = root_path / CALENDAR_RELATIVE_PATH
    if not source_path.is_file():
        return {
            "state": "missing",
            "root": str(root_path),
            "source_files": [],
            "source_row_count": 0,
            "accepted_count": 0,
            "quarantined_count": 0,
            "issue_count": 0,
            "source_return_field_mismatch_count": 0,
            "rows": [],
            "equity": [],
            "issues": [],
            "hash_inputs": {"state": "missing", "root": str(root_path)},
        }

    file_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    rows: list[dict[str, Any]] = []
    equity: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    source_return_field_mismatch_count = 0
    seen_ids: set[str] = set()
    source_row_count = 0

    with source_path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        for row_number, raw in enumerate(reader, start=2):
            source_row_count += 1
            raw_row = {str(key): str(value or "") for key, value in raw.items()}
            date_value = raw_row.get("date", "").strip()
            if market_date and date_value and date_value > market_date:
                continue
            observation = _normalize_row(
                raw_row,
                row_number=row_number,
                file_hash=file_hash,
            )
            record_id = str(observation["record_id"])
            if record_id in seen_ids:
                observation["record_status"] = "quarantined"
                observation["quarantine_reason"] = "duplicate_paper_ops_identity"
                observation["net_pnl_cents"] = None
                observation["return_pct"] = None
                observation["gross_pnl_cents"] = None
                observation["unrealized_pnl_cents"] = None
                observation["equity"] = None
                observation["issues"].append(
                    _issue(
                        record_id,
                        date_value,
                        "duplicate_paper_ops_identity",
                        "PaperOps calendar identity occurred more than once.",
                    )
                )
            seen_ids.add(record_id)
            source_return_field_mismatch_count += int(
                observation.pop("source_return_field_mismatch", False)
            )
            row_issues = list(observation.pop("issues", []))
            issues.extend(row_issues)
            row_equity = observation.pop("equity", None)
            if row_equity is not None:
                equity.append(row_equity)
            rows.append(observation)

    quarantined_count = sum(row.get("record_status") == "quarantined" for row in rows)
    accepted_count = len(rows) - quarantined_count
    state = "complete" if not issues else "partial"
    report = {
        "state": state,
        "root": str(root_path),
        "source_files": [LOGICAL_SOURCE_REF],
        "source_row_count": source_row_count,
        "accepted_count": accepted_count,
        "quarantined_count": quarantined_count,
        "issue_count": len(issues),
        "source_return_field_mismatch_count": source_return_field_mismatch_count,
        "source_file_sha256": file_hash,
        "rows": rows,
        "equity": equity,
        "issues": issues,
        "hash_inputs": {
            "source_file_sha256": file_hash,
            "source_row_count": source_row_count,
            "rows": [
                {
                    "record_id": row["record_id"],
                    "record_status": row["record_status"],
                    "source_hash_sha256": row["source_hash_sha256"],
                    "quarantine_reason": row.get("quarantine_reason"),
                }
                for row in rows
            ],
        },
    }
    return report


def _normalize_row(raw: dict[str, str], *, row_number: int, file_hash: str) -> dict[str, Any]:
    date_value = raw.get("date", "").strip()
    mode = raw.get("mode", "").strip().lower()
    strategy_id = raw.get("strategy_id", "").strip()
    strategy_version = raw.get("strategy_version", "").strip()
    run_id = raw.get("run_id", "").strip()
    data_snapshot_id = raw.get("data_snapshot_id", "").strip()
    record_id = ":".join(
        (
            "paper_ops_daily",
            date_value or "missing-date",
            mode or "missing-mode",
            strategy_id or "missing-strategy",
            strategy_version or "missing-version",
            run_id or f"row-{row_number}",
        )
    )
    cohort = (
        Cohort.HISTORICAL_BACKTEST
        if mode == "replay"
        else Cohort.SHADOW_CHALLENGER
    )
    starting = money_to_cents(raw.get("starting_equity"))
    ending = money_to_cents(raw.get("ending_equity"))
    realized = money_to_cents(raw.get("realized_pnl"))
    unrealized = money_to_cents(raw.get("unrealized_pnl"))
    fees = money_to_cents(raw.get("fees_paid"))
    slippage = money_to_cents(raw.get("slippage_estimate"))
    total = money_to_cents(raw.get("total_pnl"))
    closed = _int(raw.get("trades_closed"))
    open_positions = _int(raw.get("open_positions"))
    exposure_pct = _decimal(raw.get("exposure_pct"))
    reasons: list[str] = []
    if not date_value or not strategy_id or not strategy_version or not run_id:
        reasons.append("missing_paper_ops_identity")
    if starting is None or ending is None or starting <= 0:
        reasons.append("missing_or_invalid_paper_ops_equity")
    if realized is None or unrealized is None or total is None:
        reasons.append("missing_paper_ops_pnl_component")
    if fees is None or slippage is None:
        reasons.append("missing_paper_ops_cost_component")
    if total is not None and realized is not None and unrealized is not None:
        if abs(total - (realized + unrealized)) > 1:
            reasons.append("paper_ops_total_pnl_mismatch")
    if (
        starting is not None
        and ending is not None
        and realized is not None
        and unrealized is not None
        and fees is not None
        and slippage is not None
        and abs((ending - starting) - (realized + unrealized - fees - slippage)) > 1
    ):
        reasons.append("paper_ops_equity_pnl_cost_mismatch")

    return_value = _decimal(raw.get("daily_return_pct"))
    source_return_field_mismatch = False
    derived_return_pct: float | None = None
    if starting is not None and ending is not None and starting > 0:
        derived_return_pct = round(
            float(
                (Decimal(ending) - Decimal(starting))
                / Decimal(starting)
                * Decimal("100")
            ),
            4,
        )
        if return_value is not None:
            # The export labels this field as a percent but stores a fraction
            # in some runs. It is reported as a source warning and never used
            # for canonical return calculation.
            source_return_field_mismatch = abs(
                derived_return_pct - float(return_value)
            ) > 0.01 and abs(derived_return_pct - float(return_value * 100)) > 0.01

    valid = not reasons
    status = "realized" if valid else "quarantined"
    if valid and (closed or 0) == 0 and (open_positions or 0) == 0 and (ending == starting):
        status = "no_trade"
    gross = (
        realized + unrealized
        if valid and realized is not None and unrealized is not None
        else None
    )
    net = ending - starting if valid and starting is not None and ending is not None else None
    notional = (
        int(round(starting * exposure_pct / Decimal("100")))
        if starting is not None and exposure_pct is not None
        else None
    )
    source_refs = [LOGICAL_SOURCE_REF]
    for value in (run_id, data_snapshot_id, raw.get("strategy_semantics_fingerprint")):
        if value:
            source_refs.append(str(value))
    issue_rows = [
        _issue(record_id, date_value, reason, _message(reason)) for reason in reasons
    ]
    equity = None
    if starting is not None and ending is not None:
        equity = {
            "observation_id": f"paper_ops_equity:{record_id}",
            "market_date": date_value,
            "cohort": cohort.value,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "opening_equity_cents": starting,
            "ending_equity_cents": ending,
            "source_refs": source_refs,
            "source_hash_sha256": stable_hash({"file": file_hash, "row": raw}),
            "observed_at": None,
        }
    return {
        "record_id": record_id,
        "market_date": date_value,
        "ticker": "PORTFOLIO",
        "cohort": cohort,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "signal_id": None,
        "rank": None,
        "record_status": status,
        "entry_price": None,
        "exit_price": None,
        "quantity": None,
        "notional_cents": notional,
        "gross_pnl_cents": gross,
        "gross_return_pct": (
            round(float(Decimal(gross) / Decimal(starting) * Decimal("100")), 4)
            if gross is not None and starting is not None and starting > 0
            else None
        ),
        "fees_cents": fees if valid else None,
        "slippage_cents": slippage if valid else None,
        "net_pnl_cents": net,
        "return_pct": derived_return_pct,
        "benchmark_return_pct": None,
        "excess_return_pct": None,
        "source_refs": tuple(sorted(set(source_refs))),
        "source_hash_sha256": stable_hash({"file": file_hash, "row": raw}),
        "input_hash_sha256": "",
        "observed_at": None,
        "reconciled_at": "",
        "quarantine_reason": "; ".join(reasons) if reasons else None,
        "execution_policy_version": raw.get("execution_policy_version")
        or "unknown-paper-ops-policy",
        "trade_count": max(closed or 0, 0),
        "open_position_count": max(open_positions or 0, 0),
        "unrealized_pnl_cents": unrealized if valid else None,
        "record_type": "portfolio_observation",
        "source_return_field_mismatch": source_return_field_mismatch,
        "issues": issue_rows,
        "equity": equity,
    }


def _not_configured() -> dict[str, Any]:
    return {
        "state": "not_configured",
        "root": None,
        "source_files": [],
        "source_row_count": 0,
        "accepted_count": 0,
        "quarantined_count": 0,
        "issue_count": 0,
        "source_return_field_mismatch_count": 0,
        "rows": [],
        "equity": [],
        "issues": [],
        "hash_inputs": {"state": "not_configured"},
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _int(value: Any) -> int | None:
    decimal = _decimal(value)
    return int(decimal) if decimal is not None else None


def _issue(record_id: str, market_date: str, code: str, message: str) -> dict[str, Any]:
    return {
        "issue_id": stable_hash([record_id, code]),
        "record_id": record_id,
        "market_date": market_date,
        "severity": "error",
        "issue_code": code,
        "message": message,
    }


def _message(code: str) -> str:
    return {
        "missing_paper_ops_identity": (
            "PaperOps daily row is missing date, strategy, version, or run identity."
        ),
        "missing_or_invalid_paper_ops_equity": (
            "PaperOps daily row lacks a positive opening and ending equity observation."
        ),
        "missing_paper_ops_pnl_component": (
            "PaperOps daily row lacks realized, unrealized, or total P&L."
        ),
        "missing_paper_ops_cost_component": "PaperOps daily row lacks explicit fees or slippage.",
        "paper_ops_total_pnl_mismatch": (
            "PaperOps total P&L does not equal realized plus unrealized P&L within one cent."
        ),
        "paper_ops_equity_pnl_cost_mismatch": (
            "PaperOps ending equity does not equal opening equity plus after-cost P&L "
            "within one cent."
        ),
        "duplicate_paper_ops_identity": "PaperOps calendar identity occurred more than once.",
    }.get(code, "PaperOps row failed canonical reconciliation.")
